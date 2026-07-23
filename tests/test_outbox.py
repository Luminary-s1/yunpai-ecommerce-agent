from __future__ import annotations

import base64
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from conftest import make_settings
from ecommerce_agent.database import Database, utc_now
from ecommerce_agent.outbox import DurableOutbox, OutboxError, OutboxReconcileRequest
from ecommerce_agent.taobao import (
    ChannelReplyRequest,
    CredentialCipher,
    ReplyDraftCreateRequest,
    ReplyDraftSendRequest,
    TaobaoError,
    TaobaoIntegrationService,
)


def _insert_conversation(db: Database, tenant_id: str, conversation_id: str) -> str:
    event_id = f"event-{conversation_id}"
    now = utc_now()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO channel_conversations(
                id, tenant_id, platform, shop_id, external_conversation_id,
                buyer_hash, buyer_nick_masked, owner_mode, assigned_to, version,
                last_event_id, last_message_at, created_at, updated_at
            ) VALUES (?, ?, 'taobao', 'shop-1', ?, 'buyer-hash', '买***家',
                      'human', 'admin-test', 1, 'external-event', ?, ?, ?)
            """,
            (conversation_id, tenant_id, f"external-{conversation_id}", now, now, now),
        )
        conn.execute(
            """
            INSERT INTO channel_events(
                id, tenant_id, platform, shop_id, conversation_id, external_event_id,
                direction, message_type, content_redacted, payload_hash,
                routing_ciphertext, request_id, action_mode, status, created_at, updated_at
            ) VALUES (?, ?, 'taobao', 'shop-1', ?, ?, 'inbound', '1', '测试消息',
                      'payload-hash', 'encrypted-routing', 'request-1', '1',
                      'received', ?, ?)
            """,
            (event_id, tenant_id, conversation_id, f"external-{event_id}", now, now),
        )
    return event_id


def _outbox(db: Database, *, max_attempts: int = 2) -> DurableOutbox:
    return DurableOutbox(
        db,
        lease_seconds=5,
        max_attempts=max_attempts,
        retry_base_seconds=0,
        retry_max_seconds=0,
    )


def _enqueue(
    outbox: DurableOutbox,
    *,
    tenant_id: str = "tenant-a",
    conversation_id: str = "conversation-a",
    event_id: str = "event-conversation-a",
    key: str = "reply:test:001",
) -> dict:
    return outbox.enqueue(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        event_id=event_id,
        idempotency_key=key,
        content_redacted="脱敏回复",
        payload_ciphertext="encrypted-payload",
        actor="admin-test",
        allow_bot=False,
    )


def test_outbox_claim_is_atomic_across_database_instances(tmp_path) -> None:
    path = tmp_path / "agent.sqlite3"
    db = Database(path)
    db.initialize()
    _insert_conversation(db, "tenant-a", "conversation-a")
    item = _enqueue(_outbox(db))

    def claim(worker: int) -> int:
        independent = _outbox(Database(path))
        return len(
            independent.claim_due(
                f"worker-{worker}", limit=1, outbox_id=str(item["id"])
            )
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(8)))

    assert sum(results) == 1
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, attempt_count FROM channel_outbox WHERE id=?", (item["id"],)
        ).fetchone()
    assert dict(row) == {"status": "sending", "attempt_count": 1}


def test_outbox_crash_boundary_retry_dead_letter_and_reconciliation(tmp_path) -> None:
    db = Database(tmp_path / "agent.sqlite3")
    db.initialize()
    _insert_conversation(db, "tenant-a", "conversation-a")
    outbox = _outbox(db, max_attempts=2)

    before = _enqueue(outbox, key="reply:before-dispatch")
    claimed = outbox.claim_due("worker-a", limit=1, outbox_id=before["id"])[0]
    with db.connect() as conn:
        conn.execute(
            "UPDATE channel_outbox SET lease_until=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), claimed["id"]),
        )
    recovered = outbox.recover_expired_leases()
    assert recovered == {"requeued_before_dispatch": 1, "uncertain_after_dispatch": 0}
    assert outbox.get(before["id"])["attempt_count"] == 0

    after = _enqueue(outbox, key="reply:after-dispatch")
    claimed = outbox.claim_due("worker-b", limit=1, outbox_id=after["id"])[0]
    outbox.mark_dispatch_started(after["id"], "worker-b")
    with db.connect() as conn:
        conn.execute(
            "UPDATE channel_outbox SET lease_until=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), claimed["id"]),
        )
    recovered = outbox.recover_expired_leases()
    assert recovered == {"requeued_before_dispatch": 0, "uncertain_after_dispatch": 1}
    uncertain = outbox.get(after["id"])
    assert uncertain["status"] == "failed"
    assert uncertain["delivery_state"] == "uncertain"

    with pytest.raises(OutboxError, match="version conflict"):
        outbox.reconcile(
            after["id"],
            "tenant-a",
            OutboxReconcileRequest(
                resolution="not_delivered",
                expected_record_version=uncertain["record_version"] - 1,
                note="平台确认没有收到这次发送",
            ),
            "admin-test",
        )
    requeued = outbox.reconcile(
        after["id"],
        "tenant-a",
        OutboxReconcileRequest(
            resolution="not_delivered",
            expected_record_version=uncertain["record_version"],
            note="平台确认没有收到这次发送",
        ),
        "admin-test",
    )
    assert requeued["status"] == "queued"
    assert requeued["attempt_count"] == 0

    retry = _enqueue(outbox, key="reply:dead-letter")
    first = outbox.claim_due("worker-c", limit=1, outbox_id=retry["id"])[0]
    first_failed = outbox.mark_failed(
        first["id"], "worker-c", kind="retryable", error="connect failed"
    )
    assert first_failed["status"] == "queued"
    assert first_failed["delivery_state"] == "retry_scheduled"
    second = outbox.claim_due("worker-d", limit=1, outbox_id=retry["id"])[0]
    dead = outbox.mark_failed(
        second["id"], "worker-d", kind="retryable", error="connect failed again"
    )
    assert dead["status"] == "failed"
    assert dead["delivery_state"] == "dead_letter"
    assert dead["dead_letter_at"]


def _connected_service(tmp_path, *, worker_enabled: bool = False):
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    settings = replace(
        make_settings(tmp_path),
        taobao_enabled=True,
        taobao_app_key="app-key-1",
        taobao_app_secret="app-secret-1",
        taobao_redirect_uri="https://example.test/callback",
        taobao_credential_key=key,
        taobao_qimen_customer_id="customer-1",
        taobao_qimen_route_verified=True,
        taobao_chatrobot_request_token="request-token-1",
        taobao_chatrobot_tenant_id="robot-tenant-1",
        outbox_worker_enabled=worker_enabled,
        outbox_sync_dispatch=False,
        outbox_poll_seconds=0.05,
        outbox_lease_seconds=5,
        outbox_batch_size=10,
        outbox_max_attempts=2,
        outbox_retry_base_seconds=0,
        outbox_retry_max_seconds=0,
    )
    db = Database(settings.app_db_path)
    db.initialize()
    cipher = CredentialCipher(key)
    now = utc_now()
    routing = cipher.encrypt(
        {
            "header": {"actionMode": 1, "requestId": "request-1", "tenantId": "robot-1"},
            "sender": {"domain": "cntaobao", "nick": "买家甲"},
            "receivers": [{"domain": "cntaobao", "nick": "客服甲"}],
            "channel_type": "bc",
        }
    )
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO platform_connections(
                id, tenant_id, platform, shop_id, status, account_id, account_nick,
                credential_ciphertext, token_expires_at, metadata_json, created_at, updated_at
            ) VALUES ('connection-1', 'tenant-test', 'taobao', 'shop-1', 'authorized',
                      'shop-1', '测试店铺', ?, NULL, '{}', ?, ?)
            """,
            (cipher.encrypt({"access_token": "access-secret"}), now, now),
        )
        conn.execute(
            """
            INSERT INTO channel_conversations(
                id, tenant_id, platform, shop_id, external_conversation_id,
                buyer_hash, buyer_nick_masked, owner_mode, assigned_to, version,
                last_event_id, last_message_at, created_at, updated_at
            ) VALUES ('conversation-1', 'tenant-test', 'taobao', 'shop-1', 'external-1',
                      'buyer-hash', '买***甲', 'human', 'admin-test', 1,
                      'external-event-1', ?, ?, ?)
            """,
            (now, now, now),
        )
        conn.execute(
            """
            INSERT INTO channel_events(
                id, tenant_id, platform, shop_id, conversation_id, external_event_id,
                direction, message_type, content_redacted, payload_hash,
                routing_ciphertext, request_id, action_mode, status, created_at, updated_at
            ) VALUES ('event-1', 'tenant-test', 'taobao', 'shop-1', 'conversation-1',
                      'external-event-1', 'inbound', '1', '什么时候发货', 'payload-hash',
                      ?, 'request-1', '1', 'received', ?, ?)
            """,
            (routing, now, now),
        )
    return settings, db, TaobaoIntegrationService(db, settings)


def test_taobao_async_dispatch_rechecks_owner_and_encrypts_payload(tmp_path) -> None:
    _settings, db, service = _connected_service(tmp_path)
    calls: list[dict] = []

    def success(*_args, **kwargs):
        calls.append(kwargs)
        return {"message_chatrobot_async_response": {"is_success": True}}

    service.top.call = success  # type: ignore[method-assign]
    queued = service.send_reply(
        "conversation-1",
        "tenant-test",
        ChannelReplyRequest(text="您好，预计今天发货", idempotency_key="reply:async:001"),
        "admin-test",
    )
    assert queued["status"] == "queued"
    assert calls == []
    with db.connect() as conn:
        stored_row = conn.execute(
            "SELECT payload_ciphertext, event_id, source_event_id FROM channel_outbox WHERE id=?",
            (queued["id"],),
        ).fetchone()
        queued_event = conn.execute(
            "SELECT direction, status FROM channel_events WHERE id=?",
            (stored_row["event_id"],),
        ).fetchone()
    stored = stored_row["payload_ciphertext"]
    assert "预计今天发货" not in stored
    assert stored_row["event_id"] != stored_row["source_event_id"]
    assert dict(queued_event) == {"direction": "outbound", "status": "queued"}

    report = service.run_outbox_once(worker_id="test-worker")
    assert report["claimed"] == 1
    assert report["items"][0]["delivery_state"] == "confirmed"
    assert len(calls) == 1
    with db.connect() as conn:
        sent_event = conn.execute(
            "SELECT status FROM channel_events WHERE id=?", (stored_row["event_id"],)
        ).fetchone()[0]
    assert sent_event == "sent"

    draft = service.create_reply_draft(
        "conversation-1",
        "tenant-test",
        ReplyDraftCreateRequest(
            expected_conversation_version=1,
            ai_suggestion="建议今天发货",
            final_text="您好，仓库确认今天发货",
            evidence_ids=["event-1"],
            risk_level="medium",
            idempotency_key="reply:async:draft",
        ),
        "admin-test",
    )
    sending_draft = service.send_reply_draft(
        "conversation-1",
        draft["id"],
        "tenant-test",
        ReplyDraftSendRequest(expected_record_version=draft["record_version"]),
        "admin-test",
    )
    assert sending_draft["status"] == "sending"
    service.run_outbox_once(worker_id="test-worker")
    detail = service.conversation_detail("conversation-1", "tenant-test")
    completed_draft = next(item for item in detail["drafts"] if item["id"] == draft["id"])
    assert completed_draft["status"] == "sent"
    assert completed_draft["sent_at"]

    paused = service.send_reply(
        "conversation-1",
        "tenant-test",
        ChannelReplyRequest(text="这条不应发出", idempotency_key="reply:async:paused"),
        "admin-test",
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE channel_conversations SET owner_mode='paused', version=version+1 "
            "WHERE id='conversation-1'"
        )
    report = service.run_outbox_once(worker_id="test-worker")
    assert report["items"][0]["id"] == paused["id"]
    assert report["items"][0]["delivery_state"] == "cancelled"
    assert len(calls) == 2
    with db.connect() as conn:
        cancelled_event = conn.execute(
            "SELECT status FROM channel_events WHERE id=?", (paused["event_id"],)
        ).fetchone()[0]
    assert cancelled_event == "failed"
    service.close()


def test_taobao_connect_failure_retries_then_dead_letters(tmp_path) -> None:
    _settings, _db, service = _connected_service(tmp_path)

    def connect_failure(*_args, **_kwargs):
        raise httpx.ConnectError(
            "connection refused",
            request=httpx.Request("POST", "https://mock.test/top"),
        )

    service.top.call = connect_failure  # type: ignore[method-assign]
    queued = service.send_reply(
        "conversation-1",
        "tenant-test",
        ChannelReplyRequest(text="连接失败重试", idempotency_key="reply:connect:001"),
        "admin-test",
    )
    first = service.run_outbox_once(worker_id="worker-connect", limit=1)["items"][0]
    assert first["status"] == "queued"
    assert first["delivery_state"] == "retry_scheduled"
    assert first["attempt_count"] == 1
    second = service.run_outbox_once(worker_id="worker-connect", limit=1)["items"][0]
    assert second["id"] == queued["id"]
    assert second["status"] == "failed"
    assert second["delivery_state"] == "dead_letter"
    assert second["attempt_count"] == 2
    with pytest.raises(TaobaoError, match="reconcile delivery state"):
        service.send_reply(
            "conversation-1",
            "tenant-test",
            ChannelReplyRequest(text="连接失败重试", idempotency_key="reply:connect:001"),
            "admin-test",
        )
    service.close()


def test_taobao_worker_recovers_uncertain_after_manual_reconciliation(tmp_path) -> None:
    _settings, db, service = _connected_service(tmp_path, worker_enabled=True)
    attempts = 0

    def timeout_then_success(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("delivery outcome unknown")
        return {"message_chatrobot_async_response": {"is_success": True}}

    service.top.call = timeout_then_success  # type: ignore[method-assign]
    queued = service.send_reply(
        "conversation-1",
        "tenant-test",
        ChannelReplyRequest(text="需要核对的回复", idempotency_key="reply:worker:001"),
        "admin-test",
    )
    service.start_outbox_worker()
    deadline = time.monotonic() + 3
    uncertain = None
    while time.monotonic() < deadline:
        uncertain = service.outbox.get(queued["id"])
        if uncertain and uncertain["delivery_state"] == "uncertain":
            break
        time.sleep(0.02)
    assert uncertain is not None
    assert uncertain["delivery_state"] == "uncertain"
    assert attempts == 1

    requeued = service.reconcile_outbox(
        queued["id"],
        "tenant-test",
        OutboxReconcileRequest(
            resolution="not_delivered",
            expected_record_version=uncertain["record_version"],
            note="平台人工查询确认消息没有投递",
        ),
        "admin-test",
    )
    assert requeued["status"] == "queued"
    deadline = time.monotonic() + 3
    confirmed = None
    while time.monotonic() < deadline:
        confirmed = service.outbox.get(queued["id"])
        if confirmed and confirmed["delivery_state"] == "confirmed":
            break
        time.sleep(0.02)
    assert confirmed is not None
    assert confirmed["delivery_state"] == "confirmed"
    assert attempts == 2
    assert service.outbox_worker_status()["running"] is True
    service.close()
    assert service.outbox_worker_status()["running"] is False
