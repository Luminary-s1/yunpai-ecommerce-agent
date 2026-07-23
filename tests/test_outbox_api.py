from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from conftest import make_settings
from ecommerce_agent.api import create_app
from ecommerce_agent.database import utc_now


def test_outbox_admin_api_is_tenant_scoped_and_versioned(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    now = utc_now()
    with service.db.connect() as conn:
        conn.execute(
            """
            INSERT INTO channel_conversations(
                id, tenant_id, platform, shop_id, external_conversation_id,
                buyer_hash, buyer_nick_masked, owner_mode, assigned_to, version,
                last_event_id, last_message_at, created_at, updated_at
            ) VALUES ('conversation-api', 'tenant-test', 'taobao', 'shop-1', 'external-api',
                      'buyer-hash', '买***家', 'human', 'admin-test', 1,
                      'event-api', ?, ?, ?)
            """,
            (now, now, now),
        )
        conn.execute(
            """
            INSERT INTO channel_events(
                id, tenant_id, platform, shop_id, conversation_id, external_event_id,
                direction, message_type, content_redacted, payload_hash,
                routing_ciphertext, request_id, action_mode, status, created_at, updated_at
            ) VALUES ('event-api', 'tenant-test', 'taobao', 'shop-1', 'conversation-api',
                      'external-event-api', 'inbound', '1', '测试', 'hash', 'ciphertext',
                      'request-api', '1', 'received', ?, ?)
            """,
            (now, now),
        )
    item = service.taobao.outbox.enqueue(
        tenant_id="tenant-test",
        conversation_id="conversation-api",
        event_id="event-api",
        idempotency_key="reply:api:outbox",
        content_redacted="脱敏回复",
        payload_ciphertext="secret-ciphertext",
        actor="admin-test",
        allow_bot=False,
    )
    claimed = service.taobao.outbox.claim_due(
        "worker-api", limit=1, outbox_id=item["id"]
    )[0]
    uncertain = service.taobao.outbox.mark_failed(
        claimed["id"],
        "worker-api",
        kind="uncertain",
        error="response lost",
    )

    headers = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}
    with TestClient(app) as client:
        assert client.get("/v1/integrations/taobao/outbox").status_code == 401
        listed = client.get(
            "/v1/integrations/taobao/outbox?delivery_state=uncertain", headers=headers
        )
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()] == [item["id"]]
        assert "payload_ciphertext" not in listed.json()[0]

        summary = client.get("/v1/integrations/taobao/outbox/summary", headers=headers)
        assert summary.status_code == 200
        assert summary.json()["requires_reconciliation"] == 1
        assert summary.json()["counts"]["failed/uncertain"] == 1

        conflict = client.post(
            f"/v1/integrations/taobao/outbox/{item['id']}/reconcile",
            headers=headers,
            json={
                "resolution": "confirmed",
                "expected_record_version": uncertain["record_version"] - 1,
                "note": "平台人工核对确认已经送达",
            },
        )
        assert conflict.status_code == 409

        reconciled = client.post(
            f"/v1/integrations/taobao/outbox/{item['id']}/reconcile",
            headers=headers,
            json={
                "resolution": "confirmed",
                "expected_record_version": uncertain["record_version"],
                "note": "平台人工核对确认已经送达",
            },
        )
        assert reconciled.status_code == 200
        assert reconciled.json()["status"] == "sent"
        assert reconciled.json()["delivery_state"] == "confirmed"

        run = client.post("/v1/integrations/taobao/outbox/run?limit=5", headers=headers)
        assert run.status_code == 200
        assert run.json()["claimed"] == 0


def test_lifespan_starts_worker_and_readiness_reports_it(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        outbox_worker_enabled=True,
        outbox_sync_dispatch=False,
        outbox_poll_seconds=0.05,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["checks"]["outbox_worker"] is True
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["outbox"]["worker"]["running"] is True
    assert app.state.agent.taobao.outbox_worker_status()["running"] is False
