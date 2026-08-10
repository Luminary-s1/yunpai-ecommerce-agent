from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.business.registry import business_module_catalog
from ecommerce_agent.connectors import ExternalAction, VirtualTaobaoConnector
from ecommerce_agent.service import AgentService
from ecommerce_agent.tools import ToolExecutionContext

from conftest import make_settings


def test_module_catalog_matches_registered_agent_tools(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        modules = business_module_catalog()
        declared = {name for module in modules for name in module.agent_tools}
        registered = {item["name"] for item in service.tools.catalog_for_model()}
        assert declared == registered
        traffic_lab = next(item for item in modules if item.module_id == "traffic_lab")
        assert traffic_lab.status == "available"
        assert traffic_lab.agent_tools == ["get_listing_traffic_insights"]
        assert service.tools.get("get_listing_traffic_insights").kind == "read"
    finally:
        service.close()


def test_virtual_connector_syncs_inventory_and_competitor_data(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        catalog = service.operations.connector_catalog()
        assert catalog[0]["connector_id"] == "virtual_taobao"
        assert catalog[0]["virtual"] is True

        inventory_run = service.operations.sync(
            tenant_id="tenant-test",
            connector_id="virtual_taobao",
            resource="inventory",
            actor="test",
        )
        competitor_run = service.operations.sync(
            tenant_id="tenant-test",
            connector_id="virtual_taobao",
            resource="competitor_price",
            actor="test",
        )

        assert inventory_run["items_applied"] == 2
        assert competitor_run["items_applied"] == 2
        risks = service.operations.inventory.risks(
            "tenant-test", sku_id="YP-SKU-001"
        )
        assert risks[0]["risk_code"] == "stockout_risk"
        assert risks[0]["recommended_replenishment"] == "96.00"

        analysis = service.operations.competitive.analyze_prices(
            "tenant-test", "YP-SKU-001"
        )
        assert len(analysis["observations"]) == 2
        assert analysis["summary"]["competitors"] == 2
        assert analysis["summary"]["estimated_observations"] == 2
        assert len(analysis["trends"]) == 2
        assert {item["type"] for item in analysis["recommendations"]} == {
            "entity_quality",
        }
        assert analysis["summary"]["actionable_competitors"] == 0
        assert all(item["evidence"]["is_estimate"] for item in analysis["observations"])
        assert {item["position"] for item in analysis["observations"]} == {
            "our_price_lower",
            "our_price_higher",
        }
        overview = service.operations.competitive.overview("tenant-test")
        assert overview["monitored_skus"] == 1
        assert overview["observation_count"] == 2
        assert overview["estimated_count"] == 2
        assert service.operations.competitive.list_observations(
            "tenant-test", subject_sku="YP-SKU-001"
        )

        tool_names = {item["name"] for item in service.tools.catalog_for_model()}
        assert {"get_inventory_risk", "get_competitor_price_analysis"} <= tool_names

        spec, arguments = service.tools.validate_selection(
            name="get_inventory_risk",
            arguments={"sku_id": "YP-SKU-001"},
            requested_mode="observe",
            context=ToolExecutionContext(
                tenant_id="tenant-test",
                client_id="client-test",
                session_id="session-test",
                trace_id="trace-test",
                trusted_context={},
            ),
        )
        tool_result = service.tools.execute(
            spec=spec,
            arguments=arguments,
            context=ToolExecutionContext(
                tenant_id="tenant-test",
                client_id="client-test",
                session_id="session-test",
                trace_id="trace-test",
                trusted_context={},
            ),
        )
        assert tool_result.postcondition_met is True
        assert tool_result.output["risks"][0]["sku_id"] == "YP-SKU-001"
    finally:
        service.close()


def test_virtual_connector_webhook_action_and_idempotency_contract() -> None:
    connector = VirtualTaobaoConnector()
    body = json.dumps(
        {
            "event_id": "virtual-event-1",
            "event_type": "inventory.updated",
            "resource": "inventory",
        }
    ).encode("utf-8")
    signature = hmac.new(
        b"yunpai-virtual-taobao", body, hashlib.sha256
    ).hexdigest()
    event = connector.verify_webhook({"x-virtual-signature": signature}, body)
    assert event.verified is True
    assert event.event_id == "virtual-event-1"

    action = ExternalAction(
        action="update_safety_stock_buffer",
        idempotency_key="virtual-action-001",
        payload={"sku_id": "YP-SKU-001", "buffer": 12},
        dry_run=False,
    )
    first = connector.execute(action)
    second = connector.execute(action)
    assert first.external_request_id == second.external_request_id
    assert connector.verify(action, first).verified is True


def test_operations_api_exposes_modules_and_virtual_connector(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    admin_headers = {
        "X-Admin-Id": "admin-test",
        "X-Admin-Key": "test-admin-key-123456",
    }
    with TestClient(app) as client:
        modules = client.get("/v1/modules", headers=admin_headers)
        assert modules.status_code == 200
        statuses = {item["module_id"]: item["status"] for item in modules.json()}
        assert statuses["inventory"] == "available"
        assert statuses["competitive_intelligence"] == "available"

        connection = client.post(
            "/v1/connectors/virtual_taobao/test", headers=admin_headers
        )
        assert connection.status_code == 200
        assert connection.json()["mode"] == "virtual"

        sync = client.post(
            "/v1/connectors/virtual_taobao/sync",
            headers=admin_headers,
            json={"resource": "inventory"},
        )
        assert sync.status_code == 200
        assert sync.json()["items_applied"] == 2

        risks = client.get(
            "/v1/inventory/risks?sku_id=YP-SKU-001", headers=admin_headers
        )
        assert risks.status_code == 200
        assert risks.json()[0]["evidence"]["connector_id"] == "virtual_taobao"

        competitive_sync = client.post(
            "/v1/connectors/virtual_taobao/sync",
            headers=admin_headers,
            json={"resource": "competitor_price"},
        )
        assert competitive_sync.status_code == 200
        overview = client.get("/v1/competitive/overview", headers=admin_headers)
        assert overview.status_code == 200
        assert overview.json()["monitored_skus"] == 1
        observations = client.get(
            "/v1/competitive/observations?subject_sku=YP-SKU-001",
            headers=admin_headers,
        )
        assert observations.status_code == 200
        assert len(observations.json()) == 2
