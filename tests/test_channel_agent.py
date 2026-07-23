from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode

import pytest
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.channel_agent import ChannelAgentError
from ecommerce_agent.database import SessionScopeError
from ecommerce_agent.releases import (
    ReleasePolicyCreateRequest,
    ReleaseReplayCase,
    ReleaseReplayRequest,
    ReleaseTransitionRequest,
    ReplayExpectation,
)
from ecommerce_agent.service import AgentService
from ecommerce_agent.taobao import OwnershipRequest, TaobaoRemoteError, sign_parameters

from conftest import make_settings, principal_for


ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}


def _settings(tmp_path, *, automatic: bool = True):
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    return replace(
        make_settings(tmp_path),
        taobao_enabled=True,
        taobao_auto_reply_enabled=automatic,
        taobao_app_key="app-key-1",
        taobao_app_secret="app-secret-1",
        taobao_credential_key=key,
        taobao_qimen_customer_id="customer-1",
        taobao_qimen_route_verified=True,
        taobao_chatrobot_request_token="request-token-1",
        taobao_chatrobot_tenant_id="robot-tenant-1",
        release_gate_required=False,
        channel_agent_worker_enabled=False,
        outbox_worker_enabled=False,
        outbox_sync_dispatch=False,
        channel_agent_retry_base_seconds=1,
        channel_agent_retry_max_seconds=1,
    )


def _qimen_form(settings, *, message_id: str, text: str) -> str:
    event = {
        "header": {
            "actionMode": 1,
            "requestId": f"request-{message_id}",
            "tenantId": "robot-tenant-1",
            "serializeType": "Json",
            "type": 1,
        },
        "body": {
            "bizUniqueId": "conversation-channel-agent-1",
            "channelType": "bc",
            "content": json.dumps({"text": text}, ensure_ascii=False),
            "contentType": 1,
            "messageType": 1,
            "msgId": message_id,
            "sender": {"domain": "cntaobao", "nick": "买家甲", "role": "buyer"},
            "receivers": [
                {"domain": "cntaobao", "nick": "客服甲", "role": "customService"}
            ],
        },
    }
    params = {
        "method": "qimen.taobao.message.chatrobot.sync",
        "app_key": settings.taobao_app_key,
        "timestamp": datetime.now(timezone(timedelta(hours=8))).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "v": "2.0",
        "sign_method": "md5",
        "customerId": settings.taobao_qimen_customer_id,
        "event": json.dumps(event, ensure_ascii=False, separators=(",", ":")),
        "buyerId": "buyer-channel-agent-1",
        "buyerNick": "买家甲",
        "sellerId": "seller-channel-agent-1",
        "sellerNick": "测试店铺",
    }
    params["sign"] = sign_parameters(params, settings.taobao_app_secret, "md5")
    return urlencode(params)


def _receive(service: AgentService, settings, message_id: str, text: str = "尺码怎么选"):
    return service.taobao.receive_qimen(
        dict(parse_qsl(_qimen_form(settings, message_id=message_id, text=text)))
    )


def _activate_release(
    service: AgentService,
    *,
    mode: str = "automatic",
    intent_allowlist: list[str] | None = None,
    replay_intent: str = "product",
    max_runtime_severe_errors: int = 0,
) -> dict:
    allowed = intent_allowlist or ["product"]
    release = service.releases.create(
        "tenant-test",
        ReleasePolicyCreateRequest(
            release_key="channel-agent.automatic",
            name="渠道 Agent 自动回复",
            platform="taobao",
            store_id="seller-channel-agent-1",
            mode=mode,
            traffic_percentage=100,
            intent_allowlist=allowed,
            max_risk_level="low",
            require_sources=True,
            allow_model_fallback=False,
            min_replay_cases=1,
            max_replay_failure_rate=0,
            max_replay_severe_errors=0,
            runtime_min_samples=1,
            max_runtime_failure_rate=0,
            max_runtime_severe_errors=max_runtime_severe_errors,
        ),
        "creator-a",
    )
    replay = service.releases.run_replay(
        "tenant-test",
        release["id"],
        ReleaseReplayRequest(
            cases=[
                ReleaseReplayCase(
                    case_id="product-1",
                    message="尺码怎么选",
                    expectation=ReplayExpectation(
                        expected_intent=replay_intent,
                        expected_requires_human=False,
                        require_sources=True,
                    ),
                )
            ]
        ),
        "creator-a",
        lambda case: SimpleNamespace(
            answer="请参考尺码表。",
            intent=replay_intent,
            risk_level="low",
            requires_human=False,
            sources=[{"id": "knowledge-1"}],
            model_fallback=False,
        ),
    )
    assert replay["passed"] is True
    evaluated = service.releases.get_policy("tenant-test", release["id"])
    approved = service.releases.approve(
        "tenant-test",
        release["id"],
        ReleaseTransitionRequest(expected_record_version=evaluated["record_version"]),
        "reviewer-b",
    )
    return service.releases.activate(
        "tenant-test",
        release["id"],
        ReleaseTransitionRequest(expected_record_version=approved["record_version"]),
        "release-admin",
    )


def test_idempotent_agent_invocation_reuses_messages_trace_and_context(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        principal = principal_for(service, "buyer-channel-idempotent")
        first = service.chat(
            principal,
            "channel-invocation-session",
            "尺码怎么选",
            {"platform": "taobao", "shop_id": "shop-1"},
            idempotency_key="channel-event:event-001",
        )
        second = service.chat(
            principal,
            "channel-invocation-session",
            "尺码怎么选",
            {"platform": "taobao", "shop_id": "shop-1"},
            idempotency_key="channel-event:event-001",
        )

        assert second == first
        with service.db.connect() as conn:
            invocation = conn.execute(
                "SELECT status, attempt_count FROM agent_invocations"
            ).fetchone()
            messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            snapshots = conn.execute(
                "SELECT COUNT(*) FROM context_snapshots WHERE trace_id=?",
                (first.trace_id,),
            ).fetchone()[0]
        assert dict(invocation) == {"status": "completed", "attempt_count": 1}
        assert messages == 2
        assert snapshots >= 1

        with pytest.raises(SessionScopeError, match="another request"):
            service.chat(
                principal,
                "channel-invocation-session",
                "换成另一条问题",
                {"platform": "taobao", "shop_id": "shop-1"},
                idempotency_key="channel-event:event-001",
            )
    finally:
        service.close()


def test_inbound_event_and_job_are_atomic_duplicate_safe_and_single_claim(tmp_path) -> None:
    settings = _settings(tmp_path, automatic=False)
    service = AgentService(settings)
    try:
        inbound = _receive(service, settings, "event-atomic-1")
        duplicate = _receive(service, settings, "event-atomic-1")
        assert inbound.is_new is True
        assert inbound.job_id
        assert duplicate.is_new is False
        assert duplicate.event_id == inbound.event_id

        def claim(index: int):
            return service.channel_agents.claim_due(
                f"worker-{index}", limit=1, job_id=inbound.job_id
            )

        with ThreadPoolExecutor(max_workers=12) as pool:
            claims = list(pool.map(claim, range(12)))
        claimed = [item for batch in claims for item in batch]
        assert len(claimed) == 1
        with service.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM channel_events").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM channel_agent_jobs").fetchone()[0] == 1
            conn.execute(
                "UPDATE channel_agent_jobs SET lease_until='2000-01-01T00:00:00+00:00'"
            )
        assert service.channel_agents.recover_expired_leases() == 1
        reclaimed = service.channel_agents.claim_due(
            "recovery-worker", limit=1, job_id=inbound.job_id
        )
        assert len(reclaimed) == 1
        assert reclaimed[0]["attempt_count"] == 2
    finally:
        service.close()


def test_worker_retry_reuses_completed_agent_result_after_delivery_failure(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    calls: list[str] = []

    with TestClient(app) as client:
        service = app.state.agent

        def flaky_send(conversation_id, tenant_id, request, actor, *, allow_bot=False):
            calls.append(request.text)
            if len(calls) == 1:
                raise RuntimeError("simulated interruption before durable delivery")
            return {
                "id": "outbox-recovered-1",
                "status": "queued",
                "delivery_state": "queued",
            }

        monkeypatch.setattr(service.taobao, "send_reply", flaky_send)
        response = client.post(
            "/v1/integrations/taobao/qimen",
            content=_qimen_form(
                settings,
                message_id="event-retry-1",
                text="尺码怎么选",
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 200
        with service.db.connect() as conn:
            first_job = conn.execute("SELECT * FROM channel_agent_jobs").fetchone()
            invocation = conn.execute("SELECT * FROM agent_invocations").fetchone()
            message_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            conn.execute(
                "UPDATE channel_agent_jobs SET next_attempt_at='2000-01-01T00:00:00+00:00'"
            )
        assert first_job["status"] == "retry"
        assert invocation["status"] == "completed"
        assert message_count == 2

        report = service.channel_agents.run_once(worker_id="recovery-worker", limit=1)
        assert report["items"][0]["status"] == "completed"
        assert report["items"][0]["outbox_id"] == "outbox-recovered-1"
        assert calls == [calls[0], calls[0]]
        with service.db.connect() as conn:
            final_job = conn.execute("SELECT * FROM channel_agent_jobs").fetchone()
            assert conn.execute("SELECT COUNT(*) FROM agent_invocations").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        assert final_job["attempt_count"] == 2


def test_automatic_reply_binds_outbox_to_its_exact_inbound_event(tmp_path) -> None:
    settings = _settings(tmp_path)
    service = AgentService(settings)
    try:
        now = datetime.now(timezone.utc).isoformat()
        with service.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_connections(
                    id, tenant_id, platform, shop_id, status, account_id, account_nick,
                    credential_ciphertext, token_expires_at, metadata_json,
                    created_at, updated_at
                ) VALUES ('connection-agent-test', 'tenant-test', 'taobao',
                          'seller-channel-agent-1', 'authorized', 'seller-1', '测试店铺',
                          'not-used-before-dispatch', NULL, '{}', ?, ?)
                """,
                (now, now),
            )
        first = _receive(service, settings, "event-source-1")
        second = _receive(service, settings, "event-source-2")
        assert first.job_id and second.job_id

        result = service.channel_agents.run_job_once(first.job_id)
        assert result["status"] == "completed"
        assert result["action"] == "send"
        with service.db.connect() as conn:
            outbox = conn.execute(
                "SELECT source_event_id FROM channel_outbox WHERE id=?",
                (result["outbox_id"],),
            ).fetchone()
            second_job = conn.execute(
                "SELECT status FROM channel_agent_jobs WHERE id=?", (second.job_id,)
            ).fetchone()
        assert outbox["source_event_id"] == first.event_id
        assert second_job["status"] == "queued"
    finally:
        service.close()


def test_async_outbox_failure_updates_release_observation_and_auto_pauses(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    service = AgentService(settings)
    try:
        active = _activate_release(service)
        now = datetime.now(timezone.utc).isoformat()
        credential = service.taobao.cipher.encrypt({"access_token": "token-test"})
        with service.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_connections(
                    id, tenant_id, platform, shop_id, status, account_id, account_nick,
                    credential_ciphertext, token_expires_at, metadata_json,
                    created_at, updated_at
                ) VALUES ('connection-async-failure', 'tenant-test', 'taobao',
                          'seller-channel-agent-1', 'authorized', 'seller-1', '测试店铺',
                          ?, NULL, '{}', ?, ?)
                """,
                (credential, now, now),
            )
        inbound = _receive(service, settings, "event-async-failure-1")
        completed = service.channel_agents.run_job_once(str(inbound.job_id))
        assert completed["status"] == "completed"
        assert completed["action"] == "send"

        def reject(*args, **kwargs):
            raise TaobaoRemoteError(
                "platform rejected the reply", outcome="rejected", code="INVALID"
            )

        monkeypatch.setattr(service.taobao.top, "call", reject)
        dispatch = service.taobao.run_outbox_once(worker_id="outbox-failure-worker")
        assert dispatch["items"][0]["delivery_state"] == "rejected"
        observation = service.releases.list_observations(
            "tenant-test", active["id"]
        )[0]
        assert observation["action"] == "blocked"
        assert "delivery_rejected" in observation["violations"]
        assert service.releases.get_policy("tenant-test", active["id"])["status"] == "paused"
        job = service.channel_agents.get_job(str(inbound.job_id), "tenant-test")
        assert job["error_kind"] == "delivery_rejected"
    finally:
        service.close()


def test_release_violation_materializes_a_human_handoff_without_sending(
    tmp_path,
) -> None:
    settings = _settings(tmp_path)
    service = AgentService(settings)
    try:
        _activate_release(
            service,
            mode="collaborative",
            intent_allowlist=["order"],
            replay_intent="order",
            max_runtime_severe_errors=10,
        )
        inbound = _receive(service, settings, "event-policy-handoff-1")
        result = service.channel_agents.run_job_once(str(inbound.job_id))
        assert result["status"] == "completed"
        assert result["action"] == "handoff"
        with service.db.connect() as conn:
            conversation = conn.execute(
                "SELECT owner_mode, assigned_to FROM channel_conversations"
            ).fetchone()
            handoff = conn.execute(
                "SELECT reason FROM handoff_tasks WHERE message_id=?",
                (result["assistant_message_id"],),
            ).fetchone()
            assert conn.execute("SELECT COUNT(*) FROM channel_outbox").fetchone()[0] == 0
        assert dict(conversation) == {
            "owner_mode": "human",
            "assigned_to": "agent-handoff",
        }
        assert handoff["reason"] == "release_policy_handoff"
    finally:
        service.close()


def test_shadow_handoff_is_observed_without_live_side_effects(tmp_path) -> None:
    settings = _settings(tmp_path)
    service = AgentService(settings)
    try:
        active = _activate_release(
            service,
            mode="shadow",
            max_runtime_severe_errors=10,
        )
        inbound = _receive(
            service,
            settings,
            "event-shadow-handoff-1",
            text="我要转人工客服",
        )
        result = service.channel_agents.run_job_once(str(inbound.job_id))
        assert result["status"] == "completed"
        assert result["action"] == "shadow"
        observation = service.releases.list_observations(
            "tenant-test", active["id"]
        )[0]
        assert observation["requires_human"] is True
        assert observation["action"] == "shadow"
        with service.db.connect() as conn:
            owner = conn.execute(
                "SELECT owner_mode FROM channel_conversations"
            ).fetchone()[0]
            handoffs = conn.execute("SELECT COUNT(*) FROM handoff_tasks").fetchone()[0]
            outbox = conn.execute("SELECT COUNT(*) FROM channel_outbox").fetchone()[0]
            sop_runs = conn.execute("SELECT COUNT(*) FROM sop_runs").fetchone()[0]
        assert owner == "bot"
        assert handoffs == 0
        assert outbox == 0
        assert sop_runs == 0
    finally:
        service.close()


def test_owner_change_blocks_a_queued_job_before_agent_execution(tmp_path) -> None:
    settings = _settings(tmp_path)
    service = AgentService(settings)
    try:
        inbound = _receive(service, settings, "event-owner-race-1")
        with service.db.connect() as conn:
            conversation = conn.execute(
                "SELECT id, version FROM channel_conversations"
            ).fetchone()
        service.taobao.change_ownership(
            str(conversation["id"]),
            "tenant-test",
            OwnershipRequest(
                owner_mode="human",
                expected_version=int(conversation["version"]),
            ),
            "admin-test",
        )
        result = service.channel_agents.run_job_once(str(inbound.job_id))
        assert result["status"] == "blocked"
        assert result["error_kind"] == "safety_gate"
        with service.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM agent_invocations").fetchone()[0] == 0
    finally:
        service.close()


def test_first_attempt_failure_dead_letters_when_retry_budget_is_one(
    tmp_path, monkeypatch
) -> None:
    settings = replace(_settings(tmp_path), channel_agent_max_attempts=1)
    service = AgentService(settings)
    try:
        inbound = _receive(service, settings, "event-dead-letter-1")

        def fail_chat(*args, **kwargs):
            raise RuntimeError("simulated Agent runtime failure")

        monkeypatch.setattr(service.channel_agents, "chat", fail_chat)
        result = service.channel_agents.run_job_once(str(inbound.job_id))
        assert result["status"] == "dead_letter"
        assert result["stage"] == "done"
        assert result["error_kind"] == "runtimeerror"
        with pytest.raises(ChannelAgentError, match="not found"):
            service.channel_agents.get_job(str(inbound.job_id), "other-tenant")
    finally:
        service.close()


def test_agent_job_admin_api_is_tenant_scoped(tmp_path) -> None:
    settings = _settings(tmp_path, automatic=False)
    app = create_app(settings)
    with TestClient(app) as client:
        service = app.state.agent
        inbound = _receive(service, settings, "event-admin-1")
        listing = client.get(
            "/v1/integrations/taobao/agent-jobs", headers=ADMIN_HEADERS
        )
        assert listing.status_code == 200
        assert [item["id"] for item in listing.json()] == [inbound.job_id]
        detail = client.get(
            f"/v1/integrations/taobao/agent-jobs/{inbound.job_id}",
            headers=ADMIN_HEADERS,
        )
        assert detail.status_code == 200
        assert detail.json()["event_id"] == inbound.event_id
        run = client.post(
            "/v1/integrations/taobao/agent-jobs/run?limit=5", headers=ADMIN_HEADERS
        )
        assert run.status_code == 200
        assert run.json()["items"][0]["action"] == "disabled"
        summary = client.get(
            "/v1/integrations/taobao/agent-jobs/summary", headers=ADMIN_HEADERS
        )
        assert summary.json()["counts"]["blocked"] == 1
        assert client.get("/v1/integrations/taobao/agent-jobs").status_code == 401


def test_channel_agent_worker_lifecycle_is_part_of_readiness(tmp_path) -> None:
    settings = replace(
        _settings(tmp_path),
        channel_agent_worker_enabled=True,
        channel_agent_poll_seconds=0.05,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["checks"]["channel_agent_worker"] is True
        health = client.get("/health").json()
        assert health["channel_agent"]["worker"]["running"] is True
    assert app.state.agent.channel_agents.worker_status()["running"] is False
