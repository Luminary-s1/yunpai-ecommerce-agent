from dataclasses import replace

from fastapi.testclient import TestClient

from conftest import make_settings
from ecommerce_agent.api import create_app


def test_taobao_capability_endpoint_is_admin_only_and_reports_blockers(tmp_path) -> None:
    settings = replace(make_settings(tmp_path), taobao_enabled=True)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/v1/integrations/taobao/capabilities").status_code == 401
        response = client.get(
            "/v1/integrations/taobao/capabilities",
            headers={"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["platform"] == "taobao"
        assert body["mode"] == "manual_takeover"
        assert body["official_contract"] == {
            "access_model": "service_market_customer_service_robot",
            "merchant_ui": "independent_ecommerce_backend",
            "inbound_method": "qimen.taobao.message.chatrobot.sync",
            "outbound_method": "taobao.message.chatrobot.async",
            "subscription_methods": [
                "taobao.message.chatrobot.assist.subscribe",
                "taobao.message.chatrobot.assist.query",
            ],
            "top_gateway": "https://eco.taobao.com/router/rest",
            "requires_platform_allocated": ["customerId", "request_token", "tenant_id"],
        }
        assert body["capabilities"]["chatrobot_outbound"]["available"] is False
        assert "request_token" in body["capabilities"]["chatrobot_outbound"][
            "missing_when_unavailable"
        ]
