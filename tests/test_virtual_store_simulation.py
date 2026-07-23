from __future__ import annotations

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.service import AgentService
from ecommerce_agent.simulation import VirtualStoreSimulation

from conftest import make_settings


ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}


def test_virtual_store_fixture_runs_all_modules_and_replays_idempotently(
    tmp_path,
) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        simulation = VirtualStoreSimulation(service)
        report = simulation.run(
            tenant_id="tenant-test",
            actor="admin-test",
            include_customer_service=True,
        )
        assert report["virtual"] is True
        assert report["production_claim"] is False
        assert report["report_contract_version"] == "simulation-evidence-v1"
        assert report["passed"] is True
        assert report["summary"] == {
            "total": 13,
            "passed": 13,
            "failed": 0,
            "skipped": 0,
        }
        available = [
            item
            for item in report["module_coverage"]
            if item["status"] == "available"
        ]
        assert len(available) == 7
        assert all(item["verification"] == "passed" for item in available)
        planned = {
            item["module_id"]: item["verification"]
            for item in report["module_coverage"]
            if item["status"] == "planned"
        }
        assert planned == {
            "marketing": "planned_not_executed",
            "finance": "planned_not_executed",
        }
        assert report["loaded"]["catalog"] == 6
        assert report["loaded"]["inventory"] == 10
        assert report["loaded"]["orders"] == 8
        assert {item["module"] for item in report["scenarios"]} >= {
            "catalog",
            "orders",
            "inventory",
            "metrics",
            "competitive_intelligence",
            "competitive_monitoring",
            "customer_service",
            "order_agent",
            "handoff_dispatch",
            "admin_console",
            "tenant_isolation",
            "connector_contract",
            "customer_service_evaluation",
        }
        assert all(item["input"] for item in report["scenarios"])
        assert all(item["expected"] for item in report["scenarios"])
        assert all(item["output"] for item in report["scenarios"])
        assert all(
            assertion["passed"] is True
            for item in report["scenarios"]
            for assertion in item["assertions"]
        )
        evidence = {item["id"]: item["output"] for item in report["scenarios"]}
        assert len(evidence["D01"]["items"]) == 6
        assert len(evidence["D02"]["records"]) == 8
        assert evidence["D05"]["tool_output"]["quality_gate"][
            "eligible_competitors"
        ] == 1
        assert evidence["D07"]["agent_response"]["sources"]
        assert evidence["D08"]["blocked_probe_result"]["error_type"] == "ValueError"
        assert evidence["D09"]["dispatch_job"]["status"] == "assigned"
        assert evidence["D12"]["first_result"]["external_request_id"] == evidence[
            "D12"
        ]["replay_result"]["external_request_id"]
        assert evidence["D13"]["evaluation_report"]["gate"]["passed"] is True
        assert evidence["D13"]["primary_runtime_counts"]["before"] == evidence[
            "D13"
        ]["primary_runtime_counts"]["after"]

        replay = simulation.run(
            tenant_id="tenant-test",
            actor="admin-test",
            include_customer_service=True,
        )
        assert replay["passed"] is True
        assert replay["loaded"]["write_statuses"] == {
            "catalog_idempotent": 6,
            "inventory_idempotent": 10,
            "orders_idempotent": 8,
        }
        assert replay["loaded"]["competitive"]["match_idempotent"] == 3
        assert replay["loaded"]["competitive"]["observation_idempotent"] == 2
        assert replay["loaded"]["competitive"]["signal_idempotent"] == 3
        assert replay["loaded"]["competitive"]["monitor_reused"] == 1
        assert replay["loaded"]["knowledge"]["reused"] == 4
    finally:
        service.close()


def test_virtual_store_api_requires_explicit_virtual_confirmation(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        summary = client.get(
            "/v1/simulations/virtual-store", headers=ADMIN_HEADERS
        )
        assert summary.status_code == 200
        assert summary.json()["report_contract_version"] == "simulation-evidence-v1"
        assert len(summary.json()["demands"]) == 13
        demand_d07 = next(
            item for item in summary.json()["demands"] if item["id"] == "D07"
        )
        assert demand_d07["input"]["message"] == "晴川 AF5 空气炸锅保修多久？"
        assert summary.json()["records"] == {
            "catalog": 6,
            "inventory": 10,
            "orders": 8,
            "competitive_candidates": 3,
            "knowledge": 4,
            "demands": 13,
        }

        missing_confirmation = client.post(
            "/v1/simulations/virtual-store/run",
            headers=ADMIN_HEADERS,
            json={"fixture_id": "qingchuan-home-appliance-v1"},
        )
        assert missing_confirmation.status_code == 422

        run = client.post(
            "/v1/simulations/virtual-store/run",
            headers=ADMIN_HEADERS,
            json={
                "fixture_id": "qingchuan-home-appliance-v1",
                "confirm_virtual": True,
                "include_customer_service": True,
            },
        )
        assert run.status_code == 200
        assert run.json()["passed"] is True
        assert run.json()["summary"]["passed"] == 13
        assert run.json()["scenarios"][0]["input"]["operation"] == (
            "CatalogService.list_items"
        )
        assert len(run.json()["scenarios"][0]["output"]["items"]) == 6
        audit = client.get(
            "/v1/admin/audit?event_type=simulation.virtual_store.completed",
            headers=ADMIN_HEADERS,
        )
        assert audit.status_code == 200
        assert audit.json()[0]["detail"]["passed"] is True
