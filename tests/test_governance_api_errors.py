from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import make_settings
from ecommerce_agent.api import create_app


HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}


def test_governance_api_rejects_invalid_scopes_and_lifecycle_conflicts(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/v1/admin/knowledge").status_code == 401
        invalid_scope = client.post(
            "/v1/admin/knowledge",
            headers=HEADERS,
            json={
                "category": "商品参数",
                "intent": "product",
                "question": "材质是什么？",
                "answer": "304 不锈钢",
                "source": "manual://admin",
                "layer": "product",
            },
        )
        assert invalid_scope.status_code == 409
        assert "store_id and sku_id" in invalid_scope.json()["detail"]

        created = client.post(
            "/v1/admin/knowledge",
            headers=HEADERS,
            json={
                "category": "店铺规则",
                "intent": "shipping",
                "question": "多久发货？",
                "answer": "48 小时内发货",
                "source": "manual://admin",
                "layer": "store",
                "store_id": "store-a",
            },
        ).json()
        premature = client.post(
            f"/v1/admin/knowledge/{created['id']}/approve",
            headers=HEADERS,
            json={"expected_record_version": 1},
        )
        assert premature.status_code == 409
        evaluated = client.post(
            f"/v1/admin/knowledge/{created['id']}/evaluate",
            headers=HEADERS,
            json={"expected_record_version": 1},
        )
        assert evaluated.status_code == 200
        stale = client.post(
            f"/v1/admin/knowledge/{created['id']}/approve",
            headers=HEADERS,
            json={"expected_record_version": 1},
        )
        assert stale.status_code == 409
        assert "version conflict" in stale.json()["detail"]


def test_sop_quality_and_channel_error_contracts(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        invalid_dsl = client.post(
            "/v1/admin/sops",
            headers=HEADERS,
            json={
                "sop_key": "invalid.sop",
                "name": "无效 SOP",
                "intent": "order",
                "risk_level": "high",
                "dsl": {
                    "trigger": {"intents": ["order"]},
                    "steps": [{"unknown": "do_anything"}],
                    "success": {},
                },
            },
        )
        assert invalid_dsl.status_code == 422
        missing_quality_target = client.post(
            "/v1/admin/qa/runs",
            headers=HEADERS,
            json={"conversation_type": "agent", "conversation_id": "missing"},
        )
        assert missing_quality_target.status_code == 404
        missing_channel = client.get(
            "/v1/integrations/taobao/conversations/missing",
            headers=HEADERS,
        )
        assert missing_channel.status_code == 404
        missing_draft = client.post(
            "/v1/integrations/taobao/conversations/missing/reply-drafts",
            headers=HEADERS,
            json={
                "expected_conversation_version": 1,
                "ai_suggestion": "建议回复",
                "idempotency_key": "reply:missing:1",
            },
        )
        assert missing_draft.status_code == 409
