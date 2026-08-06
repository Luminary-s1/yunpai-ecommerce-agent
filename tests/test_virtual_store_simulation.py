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
            "total": 17,
            "passed": 17,
            "failed": 0,
            "skipped": 0,
        }
        available = [
            item
            for item in report["module_coverage"]
            if item["status"] == "available"
        ]
        assert len(available) == 10
        assert all(item["verification"] == "passed" for item in available)
        assert report["loaded"]["catalog"] == 6
        assert report["loaded"]["inventory"] == 10
        assert report["loaded"]["orders"] == 8
        assert report["loaded"]["marketing"] == 2
        assert report["loaded"]["expenses"] == 4
        assert report["loaded"]["settlement_statements"] == 1
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
            "marketing",
            "finance",
            "ops_assistant",
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
        assert evidence["D17"]["reference_resolved"] is True
        assert evidence["D08"]["blocked_probe_result"]["error_type"] == "ValueError"
        assert evidence["D09"]["dispatch_job"]["status"] == "assigned"
        assert evidence["D12"]["first_result"]["external_request_id"] == evidence[
            "D12"
        ]["replay_result"]["external_request_id"]
        assert evidence["D13"]["evaluation_report"]["gate"]["passed"] is True
        assert evidence["D13"]["primary_runtime_counts"]["before"] == evidence[
            "D13"
        ]["primary_runtime_counts"]["after"]
        assert evidence["D14"]["content_draft"]["publication_allowed"] is False
        assert evidence["D14"]["agent_tool_output"]["data_quality"]["virtual_only"] is True
        assert evidence["D15"]["profit_report"]["management_profit"] == "1491.00"
        assert evidence["D15"]["tasks"][0]["difference_amount"] == "-16.00"
        assert evidence["D16"]["csv_import"]["rejected_rows"] == 2
        assert evidence["D16"]["csv_replay"] == {"applied": 0, "idempotent": 6}
        assert evidence["D16"]["copywriting"]["publication_allowed"] is False
        assert evidence["D16"]["report"]["totals"]["sales_amount"] == "44800.00"
        assert evidence["D16"]["report"]["data_quality"][
            "numbers_computed_by_code"
        ] is True
        assert {
            item["code"] for item in evidence["D16"]["report"]["findings"]
        } == {"sales_declining", "spend_up_sales_flat"}

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
            "marketing_idempotent": 2,
            "expenses_idempotent": 4,
            "statements_idempotent": 1,
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
        assert len(summary.json()["demands"]) == 17
        demand_d07 = next(
            item for item in summary.json()["demands"] if item["id"] == "D07"
        )
        assert demand_d07["input"]["message"] == "晴川 AF5 空气炸锅保修多久？"
        demand_d17 = next(
            item for item in summary.json()["demands"] if item["id"] == "D17"
        )
        assert demand_d17["input"]["second_message"] == "它保修多久？"
        # 场景契约声称「指代不依赖客户端额外传参」，所以公开的证据里
        # 第二轮 context 必须只有 shop_id——第一轮才带 sku_id。
        assert demand_d17["input"]["first_context"]["sku_id"] == "QC-AF5-WHITE"
        assert demand_d17["input"]["second_context"] == {
            "shop_id": "qingchuan-flagship-001"
        }
        assert summary.json()["records"] == {
            "catalog": 6,
            "inventory": 10,
            "orders": 8,
            "marketing": 2,
            "expenses": 4,
            "settlement_statements": 1,
            "competitive_candidates": 3,
            "knowledge": 4,
            "demands": 17,
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
        assert run.json()["summary"]["passed"] == 17
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


def test_d17_counterexample_fails_without_reference_resolution(
    tmp_path, monkeypatch
) -> None:
    """反证：临时移除多轮指代消解逻辑后，D17 场景断言必须失败。

    指代消解的载体是 product_advisor._REFERENCE_HINTS——当问题含指代词
    （"它"）且当前问题解析不到候选时，回看历史恢复商品候选。用 monkeypatch
    把该正则换成永不匹配的占位符，等价于移除指代消解能力；此时第二轮"它"
    无法恢复 AF5 候选，D17 场景应失败（失败在 _verify_multi_turn_reference
    的 second_candidates 含 QC-AF5 断言上）。
    """
    import ecommerce_agent.product_advisor as advisor

    monkeypatch.setattr(advisor, "_REFERENCE_HINTS", advisor.re.compile(r"(?!)"))
    service = AgentService(make_settings(tmp_path))
    try:
        simulation = VirtualStoreSimulation(service)
        report = simulation.run(
            tenant_id="tenant-test",
            actor="admin-test",
            include_customer_service=True,
        )
    finally:
        service.close()
    d17 = next(
        item for item in report["scenarios"] if item["id"] == "D17"
    )
    assert d17["status"] == "failed"


def test_d17_reference_resolution_passes_normally(tmp_path) -> None:
    """对照组：不移除指代消解时，D17 场景正常通过。

    与反证测试互补——反证证明"移除指代 → D17 失败"，本测试证明
    "正常路径 → D17 通过"，防止误伤。
    """
    service = AgentService(make_settings(tmp_path))
    try:
        simulation = VirtualStoreSimulation(service)
        report = simulation.run(
            tenant_id="tenant-test",
            actor="admin-test",
            include_customer_service=True,
        )
    finally:
        service.close()
    d17 = next(item for item in report["scenarios"] if item["id"] == "D17")
    assert d17["status"] == "passed"
    assert d17["output"]["reference_resolved"] is True
