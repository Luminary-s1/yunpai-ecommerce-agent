from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.business import OpsOperationRecordUpsert
from ecommerce_agent.service import AgentService
from ecommerce_agent.simulation import VirtualStoreSimulation

from conftest import make_settings


ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}


def test_d16_ops_report_uses_declared_period_when_dataset_has_outside_record(
    tmp_path,
) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        simulation = VirtualStoreSimulation(service)
        fixture = simulation._load_fixture()
        dataset_key = fixture["operations_dataset"]["dataset_key"]
        service.operations.ops_assistant.upsert_record(
            "tenant-test",
            OpsOperationRecordUpsert(
                dataset_key=dataset_key,
                store_id=fixture["store"]["store_id"],
                record_date="2026-07-05",
                channel="推荐",
                visitors=600,
                orders=24,
                sales_amount="4800.00",
                ad_spend="300.00",
                source_format="form",
            ),
        )

        output = simulation._verify_ops_assistant(fixture, "tenant-test")

        assert output["report"]["data_quality"]["record_count"] == 6
        assert output["report"]["period"]["start_date"] == "2026-07-10"
        assert output["report"]["period"]["end_date"] == "2026-07-15"
        assert output["report"]["totals"]["sales_amount"] == "44800.00"
    finally:
        service.close()


def test_d19_requires_a_quality_gated_statistical_conclusion(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        simulation = VirtualStoreSimulation(service)
        fixture = simulation._load_fixture()
        simulation._load_store_data(
            fixture,
            tenant_id="tenant-test",
            actor="admin-test",
        )
        output = simulation._verify_traffic_lab(
            "tenant-test",
            "admin-test",
            store_id=fixture["store"]["store_id"],
        )
        analysis = output["tool_output"]["insights"][0]["analysis"]

        assert analysis["evidence"]["quality_gate"]["status"] == "passed"
        assert analysis["evidence"]["quality_gate"]["issues"] == []
        assert analysis["evidence"]["statistical_conclusion"] == "positive_effect"
        assert analysis["evidence"]["aa_gate"]["status"] == "passed"
        tool_output = output["tool_output"]
        provenance = tool_output["references"]["source_provenance"]
        capability = service.operations.connectors.get(
            "virtual_taobao"
        ).capabilities()
        assert tool_output["source_type"] == "virtual"
        assert tool_output["virtual"] is True
        assert {
            (item["connector_id"], item["capability_version"], item["virtual"])
            for item in provenance["connectors"]
        } >= {
            (
                capability.connector_id,
                capability.capability_version,
                capability.virtual,
            )
        }
    finally:
        service.close()


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
        assert report["summary"]["total"] == len(report["scenarios"])
        assert report["summary"]["passed"] == report["summary"]["total"]
        assert report["summary"]["failed"] == 0
        assert report["summary"]["skipped"] == 0
        assert report["module_coverage"]
        assert any(
            item["module_id"] == "traffic_lab" and item["verification"] == "passed"
            for item in report["module_coverage"]
        )
        available = [
            item
            for item in report["module_coverage"]
            if item["status"] == "available"
        ]
        assert available
        assert all(item["verification"] == "passed" for item in available)
        assert report["loaded"]["catalog"] >= 6
        assert report["loaded"]["inventory"] >= 10
        assert report["loaded"]["orders"] >= 8
        assert report["loaded"]["marketing"] >= 2
        assert report["loaded"]["expenses"] >= 4
        assert report["loaded"]["settlement_statements"] >= 1
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
            "traffic_lab",
            "forecasting",
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
        assert evidence["D18"]["route"] == "handoff"
        assert evidence["D18"]["route_reason"] == "low_confidence_handoff"
        assert evidence["D18"]["requires_human"] is True
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
        assert evidence["D16"]["copywriting"]["length"] == "medium"
        assert all(
            61 <= item["char_count"] <= 120
            for item in evidence["D16"]["copywriting"]["variants"]
        )
        assert evidence["D16"]["report"]["totals"]["sales_amount"] == "44800.00"
        assert evidence["D16"]["report"]["data_quality"][
            "numbers_computed_by_code"
        ] is True
        assert evidence["D19"]["virtual"] is True
        assert evidence["D19"]["analysis_unchanged"] is True
        assert evidence["D19"]["quality_gate"]["status"] == "passed"
        assert evidence["D19"]["statistical_conclusion"] == "positive_effect"
        assert evidence["D19"]["tool_output"]["statistics_recomputed"] is False
        assert evidence["D19"]["tool_output"]["platform_weight_claim"] is False
        assert evidence["D19"]["tool_output"]["source_type"] == "virtual"
        assert evidence["D19"]["tool_output"]["virtual"] is True
        assert evidence["D20"]["virtual"] is True
        assert evidence["D20"]["evidence_unchanged"] is True
        assert set(evidence["D20"]["tool_kinds"].values()) == {"read"}
        for tool_name in ("get_demand_forecast", "get_inventory_plan"):
            tool_output = evidence["D20"]["tool_outputs"][tool_name]
            assert tool_output["source_type"] == "virtual"
            assert tool_output["virtual"] is True
            assert any(
                item["connector_id"] == "virtual_taobao"
                and item["virtual"] is True
                for item in tool_output["references"]["source_provenance"][
                    "connectors"
                ]
            )
        assert evidence["D20"]["inventory_plan"]["action_mode"] == "advisory_only"
        assert evidence["D20"]["automatic_actions"] == []
        assert {
            item["code"] for item in evidence["D16"]["report"]["findings"]
        } == {"sales_declining", "spend_up_sales_flat"}

        showcase = report["loaded"]["showcase"]
        assert showcase["channels"]["conversations"] >= 3
        assert showcase["channels"]["drafts"] >= 2
        assert showcase["quality"]["results"] >= 2
        assert "sensitive_data_redacted" in showcase["quality"]["issue_codes"]
        assert showcase["releases"]["policies"] >= 2
        assert showcase["releases"]["status_counts"]["evaluated"] >= 1
        assert showcase["releases"]["status_counts"]["draft"] >= 1

        virtual_channels = [
            item
            for item in service.taobao.list_conversations("tenant-test")
            if item["shop_id"] == "virtual-showcase-qingchuan"
        ]
        assert {item["buyer_nick_masked"] for item in virtual_channels} >= {
            "虚拟顾客甲***",
            "虚拟顾客乙***",
            "虚拟顾客丙***",
        }
        assert all(
            service.taobao.conversation_detail(item["id"], "tenant-test")["drafts"]
            for item in virtual_channels
        )
        assert {
            item["release_key"]
            for item in service.releases.list_policies("tenant-test")
        } >= {
            "virtual-showcase.customer-service-shadow",
            "virtual-showcase.after-sale-assist",
        }
        qa_summary = service.quality.summary("tenant-test")
        assert {item["code"] for item in qa_summary["issues"]} >= {
            "sensitive_data_redacted"
        }
        showcase_counts = {
            "channels": len(service.taobao.list_conversations("tenant-test")),
            "quality": qa_summary["total_runs"],
            "releases": len(service.releases.list_policies("tenant-test")),
        }
        with service.db.connect() as conn:
            forecasting_counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("forecast_runs", "inventory_plans")
            }

        replay = simulation.run(
            tenant_id="tenant-test",
            actor="admin-test",
            include_customer_service=True,
        )
        assert replay["passed"] is True
        assert {
            "catalog_idempotent": 6,
            "inventory_idempotent": 10,
            "orders_idempotent": 8,
            "marketing_idempotent": 2,
            "expenses_idempotent": 4,
            "statements_idempotent": 1,
        }.items() <= replay["loaded"]["write_statuses"].items()
        assert replay["loaded"]["competitive"]["match_idempotent"] >= 3
        assert replay["loaded"]["competitive"]["observation_idempotent"] >= 2
        assert replay["loaded"]["competitive"]["signal_idempotent"] >= 3
        assert replay["loaded"]["competitive"]["monitor_reused"] == 1
        assert replay["loaded"]["knowledge"]["reused"] >= 4
        assert replay["loaded"]["showcase"]["channels"]["conversations"] >= 3
        assert replay["loaded"]["showcase"]["quality"]["results"] >= 2
        assert replay["loaded"]["showcase"]["releases"]["policies"] >= 2
        assert {
            "channels": len(service.taobao.list_conversations("tenant-test")),
            "quality": service.quality.summary("tenant-test")["total_runs"],
            "releases": len(service.releases.list_policies("tenant-test")),
        } == showcase_counts
        with service.db.connect() as conn:
            replay_forecasting_counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in forecasting_counts
            }
        assert replay_forecasting_counts == forecasting_counts
        replay_evidence = {item["id"]: item["output"] for item in replay["scenarios"]}
        assert replay_evidence["D20"]["forecast_write_status"] == "reused"
        assert replay_evidence["D20"]["inventory_plan_write_status"] == "reused"
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
        assert summary.json()["scenario_id_registry"]["D19"] == "M5-R-WP5"
        assert summary.json()["scenario_id_registry"]["D20"] == "M6-R-WP4"
        assert {item["id"] for item in summary.json()["demands"]} >= {
            f"D{index:02d}" for index in range(1, 20)
        }
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
        demand_d18 = next(
            item for item in summary.json()["demands"] if item["id"] == "D18"
        )
        assert demand_d18["input"]["decision_confidence"] == 0.59
        assert demand_d18["input"]["configured_threshold"] == 0.6
        records = summary.json()["records"]
        assert records["catalog"] >= 6
        assert records["inventory"] >= 10
        assert records["orders"] >= 8
        assert records["competitive_candidates"] >= 3
        assert records["knowledge"] >= 4
        assert records["demands"] >= 20
        assert records["showcase_channel_conversations"] >= 3
        assert records["showcase_quality_samples"] >= 2
        assert records["showcase_release_policies"] >= 2

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
        assert run.json()["summary"]["passed"] == len(run.json()["scenarios"])
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


def test_d030_fails_when_available_module_loses_its_scenario() -> None:
    demands = VirtualStoreSimulation._load_fixture()["demands"]
    scenarios = [
        {"id": item["id"], "module": item["module"], "status": "passed"}
        for item in demands
        if item["id"] != "D19"
    ]
    coverage = VirtualStoreSimulation._module_coverage(scenarios)
    traffic_lab = next(item for item in coverage if item["module_id"] == "traffic_lab")
    assert traffic_lab["verification"] == "failed"


def test_d030_fails_when_forecasting_loses_its_scenario() -> None:
    demands = VirtualStoreSimulation._load_fixture()["demands"]
    scenarios = [
        {"id": item["id"], "module": item["module"], "status": "passed"}
        for item in demands
        if item["id"] != "D20"
    ]
    coverage = VirtualStoreSimulation._module_coverage(scenarios)
    forecasting = next(item for item in coverage if item["module_id"] == "forecasting")
    assert forecasting["verification"] == "failed"


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


def test_d18_counterexample_fails_when_handoff_threshold_is_zero(tmp_path) -> None:
    """反证：取消低置信度阈值后，D18 必须明确失败。"""

    settings = replace(make_settings(tmp_path), handoff_confidence_threshold=0.0)
    service = AgentService(settings)
    try:
        report = VirtualStoreSimulation(service).run(
            tenant_id="tenant-test",
            actor="admin-test",
            include_customer_service=True,
        )
    finally:
        service.close()

    d18 = next(item for item in report["scenarios"] if item["id"] == "D18")
    assert d18["status"] == "failed"
    assert d18["output"]["error_type"] == "AssertionError"


def test_d18_low_confidence_handoff_passes_normally(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        report = VirtualStoreSimulation(service).run(
            tenant_id="tenant-test",
            actor="admin-test",
            include_customer_service=True,
        )
    finally:
        service.close()

    d18 = next(item for item in report["scenarios"] if item["id"] == "D18")
    assert d18["status"] == "passed"
    assert d18["output"]["route_reason"] == "low_confidence_handoff"
    assert d18["output"]["requires_human"] is True
