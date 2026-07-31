from __future__ import annotations

import json
import uuid
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from pydantic import BaseModel, ConfigDict

from .business import (
    CatalogItemUpsert,
    ContentDraftUpsert,
    CompetitiveEntityMatchCreate,
    CompetitiveMatchTransition,
    CompetitiveMonitorUpsert,
    CompetitiveSignalCreate,
    CompetitorObservationCreate,
    CopywritingRequest,
    FinanceReportQuery,
    InventoryBalanceUpsert,
    MarketingDiagnosisQuery,
    MarketingPerformanceUpsert,
    MetricQuery,
    OperatingExpenseUpsert,
    OpsReportQuery,
    OrderUpsert,
    SettlementStatementUpsert,
)
from .business.registry import business_module_catalog
from .connectors import ExternalAction
from .evaluation import (
    EvaluationCaseCreate,
    EvaluationCaseReplaceRequest,
    EvaluationExpectation,
    EvaluationRunRequest,
    EvaluationSuiteCreateRequest,
    EvaluationSuiteTransition,
    EvaluationThresholds,
    EvaluationTurn,
)
from .knowledge_management import KnowledgeCreateRequest, KnowledgeTransitionRequest
from .schemas import HandoffOperatorQueueAssignment, HandoffOperatorUpsert
from .tools import ToolExecutionContext

if TYPE_CHECKING:
    from .service import AgentService


DEFAULT_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "virtual_store_v1.json"
)


class VirtualStoreSimulationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_id: Literal["qingchuan-home-appliance-v1"] = (
        "qingchuan-home-appliance-v1"
    )
    confirm_virtual: Literal[True]
    include_customer_service: bool = True


class VirtualStoreSimulation:
    def __init__(self, service: AgentService):
        self.service = service

    @staticmethod
    def fixture_summary() -> dict[str, Any]:
        fixture = VirtualStoreSimulation._load_fixture()
        return {
            "report_contract_version": "simulation-evidence-v1",
            "fixture_id": fixture["fixture_id"],
            "fixture_version": fixture["fixture_version"],
            "virtual": True,
            "store": fixture["store"],
            "demands": fixture["demands"],
            "records": {
                "catalog": len(fixture["catalog"]),
                "inventory": len(fixture["inventory"]),
                "orders": len(fixture["orders"]),
                "marketing": len(fixture["marketing"]),
                "expenses": len(fixture["expenses"]),
                "settlement_statements": len(fixture["settlement_statements"]),
                "competitive_candidates": len(
                    fixture["competitive_candidates"]
                ),
                "knowledge": len(fixture["knowledge"]),
                "demands": len(fixture["demands"]),
            },
        }

    def run(
        self,
        *,
        tenant_id: str,
        actor: str,
        include_customer_service: bool = True,
    ) -> dict[str, Any]:
        fixture = self._load_fixture()
        run_id = f"simulation-{uuid.uuid4().hex}"
        loaded = self._load_store_data(fixture, tenant_id=tenant_id, actor=actor)
        scenarios: list[dict[str, Any]] = []
        demands = {item["id"]: item for item in fixture["demands"]}

        self._scenario(
            scenarios,
            demands["D01"],
            lambda: self._verify_catalog(fixture, tenant_id),
        )
        self._scenario(
            scenarios,
            demands["D02"],
            lambda: self._verify_orders(fixture, tenant_id),
        )
        self._scenario(
            scenarios,
            demands["D03"],
            lambda: self._verify_inventory(fixture, tenant_id),
        )
        self._scenario(
            scenarios,
            demands["D04"],
            lambda: self._verify_metrics(fixture, tenant_id),
        )
        self._scenario(
            scenarios,
            demands["D05"],
            lambda: self._verify_competitive_intelligence(fixture, tenant_id),
        )
        self._scenario(
            scenarios,
            demands["D06"],
            lambda: self._verify_competitive_alerts(fixture, tenant_id),
        )
        self._scenario(
            scenarios,
            demands["D14"],
            lambda: self._verify_marketing(fixture, tenant_id),
        )
        self._scenario(
            scenarios,
            demands["D15"],
            lambda: self._verify_finance(fixture, tenant_id),
        )
        self._scenario(
            scenarios,
            demands["D16"],
            lambda: self._verify_ops_assistant(fixture, tenant_id),
        )
        if include_customer_service:
            self._scenario(
                scenarios,
                demands["D07"],
                lambda: self._verify_customer_service(fixture, tenant_id, run_id),
            )
        else:
            self._skipped(
                scenarios, demands["D07"], "include_customer_service=false"
            )
        self._scenario(
            scenarios,
            demands["D08"],
            lambda: self._verify_order_tool_scope(fixture, tenant_id),
        )
        if include_customer_service:
            self._scenario(
                scenarios,
                demands["D09"],
                lambda: self._verify_handoff_dispatch(
                    fixture, tenant_id, actor, run_id
                ),
            )
        else:
            self._skipped(
                scenarios, demands["D09"], "include_customer_service=false"
            )
        self._scenario(
            scenarios,
            demands["D10"],
            lambda: self._verify_admin_console(tenant_id),
        )
        self._scenario(
            scenarios,
            demands["D11"],
            lambda: self._verify_tenant_isolation(fixture, tenant_id),
        )
        self._scenario(
            scenarios,
            demands["D12"],
            lambda: self._verify_connector_contract(fixture),
        )
        if include_customer_service:
            self._scenario(
                scenarios,
                demands["D13"],
                lambda: self._verify_customer_service_evaluation(
                    fixture, tenant_id, actor
                ),
            )
        else:
            self._skipped(
                scenarios, demands["D13"], "include_customer_service=false"
            )

        counts = Counter(item["status"] for item in scenarios)
        module_coverage = self._module_coverage(scenarios)
        available_coverage_passed = all(
            item["verification"] == "passed"
            for item in module_coverage
            if item["status"] == "available"
        )
        passed = (
            counts["failed"] == 0
            and counts["skipped"] == 0
            and available_coverage_passed
        )
        report = {
            "report_contract_version": "simulation-evidence-v1",
            "run_id": run_id,
            "fixture_id": fixture["fixture_id"],
            "fixture_version": fixture["fixture_version"],
            "virtual": True,
            "tenant_id": tenant_id,
            "store": fixture["store"],
            "loaded": loaded,
            "scenarios": scenarios,
            "module_coverage": module_coverage,
            "summary": {
                "total": len(scenarios),
                "passed": counts["passed"],
                "failed": counts["failed"],
                "skipped": counts["skipped"],
            },
            "passed": passed,
            "production_claim": False,
        }
        self.service.db.audit(
            "simulation.virtual_store.completed",
            actor,
            run_id,
            {
                "fixture_id": fixture["fixture_id"],
                "store_id": fixture["store"]["store_id"],
                "passed": passed,
                "summary": report["summary"],
            },
            tenant_id,
        )
        return report

    @staticmethod
    def _module_coverage(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
        scenario_ids = {
            "catalog": ["D01"],
            "orders": ["D02", "D08"],
            "inventory": ["D03"],
            "competitive_intelligence": ["D05", "D06"],
            "marketing": ["D14"],
            "finance": ["D15"],
            "ops_assistant": ["D16"],
            "metrics": ["D04"],
            "customer_service": ["D07", "D09", "D10"],
            "customer_service_evaluation": ["D13"],
        }
        by_id = {item["id"]: item for item in scenarios}
        coverage: list[dict[str, Any]] = []
        for module in business_module_catalog():
            expected = scenario_ids.get(module.module_id, [])
            statuses = [by_id[item_id]["status"] for item_id in expected]
            if module.status == "available":
                verification = (
                    "passed"
                    if expected and all(status == "passed" for status in statuses)
                    else "failed"
                )
            else:
                verification = "planned_not_executed"
            coverage.append(
                {
                    "module_id": module.module_id,
                    "display_name": module.display_name,
                    "status": module.status,
                    "scenario_ids": expected,
                    "scenario_statuses": statuses,
                    "verification": verification,
                }
            )
        return coverage

    @staticmethod
    def _load_fixture() -> dict[str, Any]:
        fixture = json.loads(DEFAULT_FIXTURE_PATH.read_text(encoding="utf-8"))
        if fixture.get("virtual") is not True:
            raise ValueError("simulation fixture must be explicitly virtual")
        if fixture.get("fixture_id") != "qingchuan-home-appliance-v1":
            raise ValueError("unsupported simulation fixture")
        return fixture

    def _load_store_data(
        self, fixture: dict[str, Any], *, tenant_id: str, actor: str
    ) -> dict[str, Any]:
        store_id = str(fixture["store"]["store_id"])
        connector_id = str(fixture["connector_id"])
        source_time = fixture["source_updated_at"]
        status_counts: Counter[str] = Counter()

        for index, item in enumerate(fixture["catalog"], start=1):
            result = self.service.operations.catalog.upsert(
                tenant_id,
                CatalogItemUpsert.model_validate(
                    {
                        **item,
                        "connector_id": connector_id,
                        "store_id": store_id,
                        "currency": fixture["store"]["currency"],
                        "source_updated_at": source_time,
                        "source_id": f"{fixture['fixture_id']}:catalog:{index}",
                    }
                ),
            )
            status_counts[f"catalog_{result['write_status']}"] += 1

        for index, item in enumerate(fixture["inventory"], start=1):
            result = self.service.operations.inventory.upsert(
                tenant_id,
                InventoryBalanceUpsert.model_validate(
                    {
                        **item,
                        "connector_id": connector_id,
                        "store_id": store_id,
                        "source_updated_at": source_time,
                        "source_id": f"{fixture['fixture_id']}:inventory:{index}",
                    }
                ),
            )
            status_counts[f"inventory_{result['write_status']}"] += 1

        for index, item in enumerate(fixture["orders"], start=1):
            result = self.service.operations.orders.upsert(
                tenant_id,
                OrderUpsert.model_validate(
                    {
                        **item,
                        "connector_id": connector_id,
                        "store_id": store_id,
                        "currency": fixture["store"]["currency"],
                        "source_updated_at": source_time,
                        "source_id": f"{fixture['fixture_id']}:order:{index}",
                    }
                ),
            )
            status_counts[f"orders_{result['write_status']}"] += 1

        for index, item in enumerate(fixture["marketing"], start=1):
            result = self.service.operations.marketing.upsert_performance(
                tenant_id,
                MarketingPerformanceUpsert.model_validate(
                    {
                        **item,
                        "connector_id": connector_id,
                        "store_id": store_id,
                        "source_type": "virtual",
                        "source_updated_at": source_time,
                        "source_id": f"{fixture['fixture_id']}:marketing:{index}",
                    }
                ),
            )
            status_counts[f"marketing_{result['write_status']}"] += 1

        for index, item in enumerate(fixture["expenses"], start=1):
            result = self.service.operations.finance.upsert_expense(
                tenant_id,
                OperatingExpenseUpsert.model_validate(
                    {
                        **item,
                        "connector_id": connector_id,
                        "store_id": store_id,
                        "currency": fixture["store"]["currency"],
                        "source_type": "virtual",
                        "source_updated_at": source_time,
                        "source_id": f"{fixture['fixture_id']}:expense:{index}",
                    }
                ),
            )
            status_counts[f"expenses_{result['write_status']}"] += 1

        for index, item in enumerate(fixture["settlement_statements"], start=1):
            result = self.service.operations.finance.upsert_statement(
                tenant_id,
                SettlementStatementUpsert.model_validate(
                    {
                        **item,
                        "connector_id": connector_id,
                        "store_id": store_id,
                        "currency": fixture["store"]["currency"],
                        "source_type": "virtual",
                        "source_updated_at": source_time,
                        "source_id": f"{fixture['fixture_id']}:statement:{index}",
                    }
                ),
            )
            status_counts[f"statements_{result['write_status']}"] += 1

        competitive = self._load_competitive(
            fixture, tenant_id=tenant_id, actor=actor
        )
        knowledge = self._load_knowledge(fixture, tenant_id=tenant_id, actor=actor)
        return {
            "catalog": len(fixture["catalog"]),
            "inventory": len(fixture["inventory"]),
            "orders": len(fixture["orders"]),
            "marketing": len(fixture["marketing"]),
            "expenses": len(fixture["expenses"]),
            "settlement_statements": len(fixture["settlement_statements"]),
            "competitive": competitive,
            "knowledge": knowledge,
            "write_statuses": dict(sorted(status_counts.items())),
        }

    def _load_competitive(
        self, fixture: dict[str, Any], *, tenant_id: str, actor: str
    ) -> dict[str, int]:
        store_id = str(fixture["store"]["store_id"])
        connector_id = str(fixture["connector_id"])
        observed_at = fixture["source_updated_at"]
        counts: Counter[str] = Counter()
        for candidate in fixture["competitive_candidates"]:
            candidate_key = str(candidate["candidate_key"])
            match = self.service.operations.competitive.record_entity_match(
                tenant_id,
                CompetitiveEntityMatchCreate.model_validate(
                    {
                        "connector_id": connector_id,
                        "store_id": store_id,
                        "subject_sku": candidate["subject_sku"],
                        "competitor_name": candidate["competitor_name"],
                        "competitor_sku": candidate["competitor_sku"],
                        "subject_identity": candidate["subject_identity"],
                        "competitor_identity": candidate["competitor_identity"],
                        "comparison_keys": candidate["comparison_keys"],
                        "source_type": "virtual",
                        "source_ref": f"virtual://{fixture['fixture_id']}/match/{candidate_key}",
                        "source_id": f"{fixture['fixture_id']}:match:{candidate_key}",
                        "is_estimate": True,
                        "observed_at": observed_at,
                    }
                ),
            )
            counts[f"match_{match['write_status']}"] += 1
            decision = str(candidate["decision"])
            if match["status"] == "pending":
                match = self.service.operations.competitive.transition_entity_match(
                    tenant_id,
                    match["id"],
                    CompetitiveMatchTransition(
                        target_status=decision,
                        expected_record_version=match["record_version"],
                        note=(
                            "虚拟验收数据：关键规格一致，批准进入分析。"
                            if decision == "approved"
                            else "虚拟验收数据：型号或容量不一致，拒绝进入分析。"
                        ),
                    ),
                    actor=actor,
                )
                counts[f"match_{decision}"] += 1

            if decision != "approved":
                continue
            observation = self.service.operations.competitive.record(
                tenant_id,
                CompetitorObservationCreate.model_validate(
                    {
                        "connector_id": connector_id,
                        "store_id": store_id,
                        "subject_sku": candidate["subject_sku"],
                        "competitor_name": candidate["competitor_name"],
                        "competitor_sku": candidate["competitor_sku"],
                        "subject_price": candidate["subject_price"],
                        "competitor_price": candidate["competitor_price"],
                        "currency": fixture["store"]["currency"],
                        "source_type": "virtual",
                        "source_ref": f"virtual://{fixture['fixture_id']}/price/{candidate_key}",
                        "is_estimate": True,
                        "observed_at": observed_at,
                        "source_id": f"{fixture['fixture_id']}:price:{candidate_key}",
                        "entity_match_id": match["id"],
                    }
                ),
            )
            counts[f"observation_{observation['write_status']}"] += 1
            for signal_index, signal in enumerate(candidate["signals"], start=1):
                result = self.service.operations.competitive.record_signal(
                    tenant_id,
                    CompetitiveSignalCreate.model_validate(
                        {
                            **signal,
                            "match_id": match["id"],
                            "connector_id": connector_id,
                            "source_type": "virtual",
                            "source_ref": f"virtual://{fixture['fixture_id']}/signal/{candidate_key}/{signal_index}",
                            "source_id": f"{fixture['fixture_id']}:signal:{candidate_key}:{signal_index}",
                            "is_estimate": True,
                            "observed_at": observed_at,
                        }
                    ),
                )
                counts[f"signal_{result['write_status']}"] += 1

        existing = self.service.operations.competitive.list_monitors(
            tenant_id, store_id=store_id, subject_sku="QC-AF5-WHITE"
        )
        if existing:
            counts["monitor_reused"] += 1
        else:
            self.service.operations.competitive.upsert_monitor(
                tenant_id,
                CompetitiveMonitorUpsert(
                    store_id=store_id,
                    subject_sku="QC-AF5-WHITE",
                    undercut_threshold_percent="5.00",
                    price_drop_threshold_percent="5.00",
                    stale_after_hours=72,
                    include_estimates=True,
                    require_approved_match=True,
                    expected_record_version=0,
                ),
                actor=actor,
            )
            counts["monitor_created"] += 1
        self.service.operations.competitive.evaluate_all(tenant_id)
        return dict(sorted(counts.items()))

    def _load_knowledge(
        self, fixture: dict[str, Any], *, tenant_id: str, actor: str
    ) -> dict[str, int]:
        store_id = str(fixture["store"]["store_id"])
        existing = self.service.knowledge_management.list_items(
            tenant_id, limit=500
        )
        by_source = {str(item["source"]): item for item in existing}
        counts: Counter[str] = Counter()
        for index, document in enumerate(fixture["knowledge"], start=1):
            source = f"virtual://{fixture['fixture_id']}/knowledge/{index}"
            item = by_source.get(source)
            if item is None:
                item = self.service.knowledge_management.create(
                    tenant_id,
                    KnowledgeCreateRequest.model_validate(
                        {
                            **document,
                            "source": source,
                            "store_id": store_id,
                        }
                    ),
                    actor,
                )
                counts["created"] += 1
            else:
                counts["reused"] += 1
            if item["status"] == "candidate" and item["review_status"] == "draft":
                item = self.service.knowledge_management.evaluate(
                    tenant_id,
                    item["id"],
                    KnowledgeTransitionRequest(
                        expected_record_version=item["record_version"],
                        note="虚拟店铺场景验收知识评测通过。",
                    ),
                    actor,
                )
                counts["evaluated"] += 1
            if item["status"] == "candidate" and item["review_status"] == "evaluated":
                self.service.knowledge_management.approve(
                    tenant_id,
                    item["id"],
                    KnowledgeTransitionRequest(
                        expected_record_version=item["record_version"],
                        note="仅用于明确标记的虚拟店铺模拟验收。",
                    ),
                    actor,
                )
                counts["approved"] += 1
        return dict(sorted(counts.items()))

    def _verify_catalog(
        self, fixture: dict[str, Any], tenant_id: str
    ) -> dict[str, Any]:
        items = self.service.operations.catalog.list_items(
            tenant_id, store_id=fixture["store"]["store_id"]
        )
        assert len(items) == len(fixture["catalog"])
        assert all(item["version"] >= 1 for item in items)
        assert {item["sku_id"] for item in items} == {
            item["sku_id"] for item in fixture["catalog"]
        }
        return {
            "active_skus": len(items),
            "source_versioned": True,
            "sku_ids": sorted(item["sku_id"] for item in items),
            "items": items,
        }

    def _verify_orders(
        self, fixture: dict[str, Any], tenant_id: str
    ) -> dict[str, Any]:
        orders = self.service.operations.orders.list_orders(
            tenant_id, store_id=fixture["store"]["store_id"]
        )
        assert len(orders) == len(fixture["orders"])
        exception = next(item for item in orders if item["order_id"] == "QC-ORDER-1005")
        assert exception["logistics"]["status"] == "exception"
        assert sum(bool(item["after_sales"]) for item in orders) == 3
        return {
            "orders": len(orders),
            "after_sale_orders": 3,
            "logistics_exception_detected": True,
            "records": orders,
        }

    def _verify_inventory(
        self, fixture: dict[str, Any], tenant_id: str
    ) -> dict[str, Any]:
        risks = self.service.operations.inventory.risks(
            tenant_id, store_id=fixture["store"]["store_id"]
        )
        risk_codes = {item["risk_code"] for item in risks}
        assert "stockout" in risk_codes
        assert "stockout_risk" in risk_codes
        assert "slow_moving" in risk_codes
        assert all("evidence" in item for item in risks)
        return {
            "balances": len(risks),
            "risk_codes": sorted(risk_codes),
            "risks": risks,
        }

    def _verify_metrics(
        self, fixture: dict[str, Any], tenant_id: str
    ) -> dict[str, Any]:
        store_id = fixture["store"]["store_id"]
        values = {
            metric: self.service.operations.metrics.query(
                tenant_id, MetricQuery(metric=metric, store_id=store_id)
            )
            for metric in (
                "active_sku_count",
                "order_count",
                "gross_revenue",
                "after_sale_order_rate",
                "inventory_risk_count",
                "competitor_lower_price_count",
            )
        }
        assert values["active_sku_count"]["value"] == 6
        assert values["order_count"]["value"] == 7
        assert values["gross_revenue"]["value"] == "4181.00"
        assert values["after_sale_order_rate"]["value"] == "0.4286"
        assert values["inventory_risk_count"]["value"] >= 2
        assert values["competitor_lower_price_count"]["value"] == 1
        assert all(item["quality"] == "available" for item in values.values())
        return {
            "metric_values": {
                key: value["value"] for key, value in values.items()
            },
            "metric_results": values,
        }

    def _verify_competitive_intelligence(
        self, fixture: dict[str, Any], tenant_id: str
    ) -> dict[str, Any]:
        matches = self.service.operations.competitive.list_entity_matches(
            tenant_id, store_id=fixture["store"]["store_id"]
        )
        assert sum(item["status"] == "approved" for item in matches) == 2
        assert sum(item["status"] == "rejected" for item in matches) == 1
        context = ToolExecutionContext(
            tenant_id=tenant_id,
            client_id="simulation",
            session_id="simulation-competitive",
            trace_id="simulation-competitive",
            trusted_context={},
        )
        spec, arguments = self.service.tools.validate_selection(
            name="get_competitive_intelligence",
            arguments={
                "subject_sku": "QC-AF5-WHITE",
                "store_id": fixture["store"]["store_id"],
            },
            requested_mode="observe",
            context=context,
        )
        output = self.service.tools.execute(
            spec=spec, arguments=arguments, context=context
        ).output
        assert output["quality_gate"]["approved_match_required"] is True
        assert output["quality_gate"]["eligible_competitors"] == 1
        assert all(item["actionable"] for item in output["observations"])
        return {
            "approved_matches": 2,
            "rejected_matches": 1,
            "eligible_competitors": 1,
            "matches": matches,
            "tool_output": output,
        }

    def _verify_competitive_alerts(
        self, fixture: dict[str, Any], tenant_id: str
    ) -> dict[str, Any]:
        self.service.operations.competitive.evaluate_all(tenant_id)
        alerts = self.service.operations.competitive.list_alerts(
            tenant_id,
            store_id=fixture["store"]["store_id"],
            status="open",
        )
        undercuts = [
            item for item in alerts if item["alert_code"] == "competitor_undercut"
        ]
        assert undercuts
        approved_ids = {
            item["id"]
            for item in self.service.operations.competitive.list_entity_matches(
                tenant_id,
                store_id=fixture["store"]["store_id"],
                status="approved",
            )
        }
        assert all(item["details"]["entity_match_id"] in approved_ids for item in undercuts)
        return {
            "open_alerts": len(alerts),
            "undercut_alerts": len(undercuts),
            "persistent": True,
            "alerts": alerts,
        }

    def _verify_marketing(
        self, fixture: dict[str, Any], tenant_id: str
    ) -> dict[str, Any]:
        store_id = fixture["store"]["store_id"]
        query = MarketingDiagnosisQuery(store_id=store_id, min_roas="2.00")
        diagnosis = self.service.operations.marketing.diagnose(tenant_id, query)
        assert diagnosis["data_quality"]["record_count"] == len(fixture["marketing"])
        assert diagnosis["data_quality"]["virtual_only"] is True
        assert any(
            item["code"] == "high_spend_no_orders"
            for item in diagnosis["findings"]
        )
        existing = next(
            (
                item
                for item in self.service.operations.marketing.list_content_drafts(
                    tenant_id, store_id=store_id
                )
                if item["draft_key"] == "virtual-d14-content"
            ),
            None,
        )
        draft = self.service.operations.marketing.save_content_draft(
            tenant_id,
            ContentDraftUpsert(
                draft_key="virtual-d14-content",
                store_id=store_id,
                content_type="campaign_copy",
                title="Virtual AF5 campaign copy",
                body="Virtual draft only; final product claims require human review.",
                sku_ids=["QC-AF5-WHITE"],
                declared_prices={"QC-AF5-WHITE": "499.00"},
                source_type="virtual",
                source_id=f"{fixture['fixture_id']}:draft:D14",
                expected_record_version=existing["record_version"] if existing else 0,
            ),
        )
        assert draft["fact_check"]["passed"] is True
        assert draft["publication_allowed"] is False
        context = ToolExecutionContext(
            tenant_id=tenant_id,
            client_id="simulation",
            session_id="simulation-marketing",
            trace_id="simulation-marketing",
            trusted_context={},
        )
        spec, arguments = self.service.tools.validate_selection(
            name="get_marketing_diagnosis",
            arguments={"store_id": store_id, "min_roas": "2.00"},
            requested_mode="observe",
            context=context,
        )
        tool_output = self.service.tools.execute(
            spec=spec, arguments=arguments, context=context
        ).output
        assert tool_output["data_quality"]["virtual_only"] is True
        return {
            "diagnosis": diagnosis,
            "content_draft": draft,
            "agent_tool_output": tool_output,
            "action_boundary": diagnosis["action_boundary"],
        }

    def _verify_finance(
        self, fixture: dict[str, Any], tenant_id: str
    ) -> dict[str, Any]:
        store_id = fixture["store"]["store_id"]
        query = FinanceReportQuery(
            store_id=store_id,
            start_date="2026-07-10",
            end_date="2026-07-22",
        )
        profit = self.service.operations.finance.profit_report(tenant_id, query)
        assert profit["gross_sales"] == "4181.00"
        assert profit["management_profit"] == "1491.00"
        assert profit["data_quality"]["management_estimate"] is True
        assert profit["data_quality"]["financial_statement"] is False
        reconciliation = self.service.operations.finance.run_reconciliation(
            tenant_id, query
        )
        tasks = self.service.operations.finance.list_reconciliation_tasks(
            tenant_id, store_id=store_id
        )
        assert tasks
        assert any(item["difference_amount"] == "-16.00" for item in tasks)
        task = next(item for item in tasks if item["difference_amount"] == "-16.00")
        assert task["status"] == "open"
        context = ToolExecutionContext(
            tenant_id=tenant_id,
            client_id="simulation",
            session_id="simulation-finance",
            trace_id="simulation-finance",
            trusted_context={},
        )
        spec, arguments = self.service.tools.validate_selection(
            name="get_profit_reconciliation",
            arguments={
                "store_id": store_id,
                "start_date": "2026-07-10",
                "end_date": "2026-07-22",
            },
            requested_mode="observe",
            context=context,
        )
        tool_output = self.service.tools.execute(
            spec=spec, arguments=arguments, context=context
        ).output
        assert tool_output["profit"]["management_profit"] == "1491.00"
        return {
            "profit_report": profit,
            "reconciliation": reconciliation,
            "tasks": tasks,
            "agent_tool_output": tool_output,
        }

    def _verify_ops_assistant(
        self, fixture: dict[str, Any], tenant_id: str
    ) -> dict[str, Any]:
        store_id = fixture["store"]["store_id"]
        dataset = fixture["operations_dataset"]
        ops = self.service.operations.ops_assistant
        dataset_key = str(dataset["dataset_key"])

        imported = ops.parse_dataset(
            tenant_id,
            dataset_key=dataset_key,
            store_id=store_id,
            source_format="csv",
            content=str(dataset["csv"]),
        )
        assert imported["total_rows"] == 8
        assert imported["accepted_rows"] == 6
        assert imported["rejected_rows"] == 2
        reasons = {item["reason"] for item in imported["rejected"]}
        assert any(reason.startswith("record_date") for reason in reasons)
        assert any("ops_orders_exceed_visitors" in reason for reason in reasons)
        assert all(item["source_format"] == "csv" for item in imported["records"])
        assert all(item["version"] == 1 for item in imported["records"])

        # 同一份运营数据重复导入不得产生新版本。
        replayed = ops.parse_dataset(
            tenant_id,
            dataset_key=dataset_key,
            store_id=store_id,
            source_format="csv",
            content=str(dataset["csv"]),
        )
        assert replayed["applied"] == 0
        assert replayed["idempotent"] == 6

        json_imported = ops.parse_dataset(
            tenant_id,
            dataset_key=str(dataset["json_dataset_key"]),
            store_id=store_id,
            source_format="json",
            content=str(dataset["json"]),
        )
        assert json_imported["accepted_rows"] == 2
        assert json_imported["rejected_rows"] == 0
        assert all(item["source_format"] == "json" for item in json_imported["records"])

        copy_spec = dataset["copywriting"]
        copy_result = ops.generate_copy(
            tenant_id,
            CopywritingRequest(
                store_id=store_id,
                product_name=copy_spec["product_name"],
                selling_points=list(copy_spec["selling_points"]),
                price=copy_spec["price"],
                target_audience=copy_spec["target_audience"],
                styles=list(copy_spec["styles"]),
                variants_per_style=int(copy_spec["variants_per_style"]),
            ),
        )
        assert copy_result["batch_size"] == 3
        assert copy_result["publication_allowed"] is False
        assert {item["style"] for item in copy_result["variants"]} == {
            "formal",
            "playful",
            "urgent",
        }
        # 模型可用与不可用都必须显式标记生成方式，且不得产生空文案。
        assert all(
            item["generator"] in {"model", "template", "template_fallback"}
            for item in copy_result["variants"]
        )
        assert all(item["title"] and item["body"] for item in copy_result["variants"])

        report = ops.analysis_report(
            tenant_id,
            OpsReportQuery(dataset_key=dataset_key, store_id=store_id),
        )
        assert report["data_quality"]["record_count"] == 6
        assert report["data_quality"]["numbers_computed_by_code"] is True
        assert report["totals"]["visitors"] == 6000
        assert report["totals"]["orders"] == 224
        assert report["totals"]["sales_amount"] == "44800.00"
        assert report["totals"]["ad_spend"] == "4280.00"
        assert report["totals"]["average_order_value"] == "200.00"
        assert report["totals"]["roi"] == "10.4673"
        directions = {item["metric"]: item["direction"] for item in report["trends"]}
        assert directions["sales_amount"] == "down"
        assert directions["ad_spend"] == "up"
        assert {item["code"] for item in report["findings"]} == {
            "sales_declining",
            "spend_up_sales_flat",
        }
        assert report["narrative_generator"] in {
            "model",
            "disabled",
            "fallback_summary_only",
        }
        assert report["action_boundary"].startswith("仅输出数据解读")
        return {
            "csv_import": imported,
            "csv_replay": {
                "applied": replayed["applied"],
                "idempotent": replayed["idempotent"],
            },
            "json_import": json_imported,
            "copywriting": copy_result,
            "report": report,
        }

    def _verify_customer_service(
        self, fixture: dict[str, Any], tenant_id: str, run_id: str
    ) -> dict[str, Any]:
        principal = self.service.auth.authenticate(
            self.service.settings.bootstrap_client_id,
            self.service.settings.bootstrap_client_key,
            f"virtual-buyer-{uuid.uuid4().hex[:8]}",
        )
        assert principal.tenant_id == tenant_id
        answer = self.service.chat(
            principal,
            f"virtual-presale-{uuid.uuid4().hex}",
            "晴川 AF5 空气炸锅保修多久？",
            {"shop_id": fixture["store"]["store_id"], "sku_id": "QC-AF5-WHITE"},
            source_type="simulation",
            source_reference=run_id,
        )
        assert answer.sources
        assert answer.context_snapshot_id
        if answer.requires_human:
            assert answer.reason == "model_unavailable"
        else:
            assert "12" in answer.answer
            assert answer.reason == "knowledge_answer_allowed"
        return {
            "intent": answer.intent,
            "requires_human": answer.requires_human,
            "sources": len(answer.sources),
            "context_snapshot": True,
            "safe_model_fallback": bool(
                answer.requires_human and answer.reason == "model_unavailable"
            ),
            "approved_knowledge_answer": bool(
                not answer.requires_human and "12" in answer.answer
            ),
            "agent_response": answer.model_dump(mode="json"),
        }

    def _verify_customer_service_evaluation(
        self, fixture: dict[str, Any], tenant_id: str, actor: str
    ) -> dict[str, Any]:
        suite_key = "virtual-store.customer-service-v1"
        suite = next(
            (
                item
                for item in self.service.evaluations.list_suites(
                    tenant_id, limit=500
                )
                if item["suite_key"] == suite_key
            ),
            None,
        )
        if suite is None:
            suite = self.service.evaluations.create_suite(
                tenant_id,
                EvaluationSuiteCreateRequest(
                    suite_key=suite_key,
                    name="晴川生活电器虚拟客服回归集",
                    description="明确标记为虚拟数据的预售知识问答回归场景。",
                    source_type="synthetic",
                    source_ref=f"virtual://{fixture['fixture_id']}/evaluation",
                    deidentified=True,
                    required_scenarios=["product"],
                    thresholds=EvaluationThresholds(
                        min_cases=1,
                        min_pass_rate=1,
                        min_intent_accuracy=1,
                        min_handoff_recall=1,
                        min_evidence_coverage=1,
                        max_severe_failures=0,
                        max_regression_rate=0,
                    ),
                ),
                actor,
            )
        suite = self.service.evaluations.get_suite(tenant_id, suite["id"])
        if suite["status"] == "draft":
            suite = self.service.evaluations.replace_cases(
                tenant_id,
                suite["id"],
                EvaluationCaseReplaceRequest(
                    expected_record_version=suite["record_version"],
                    cases=[
                        EvaluationCaseCreate(
                            case_key="af5-warranty",
                            scenario="product",
                            source_ref=f"virtual://{fixture['fixture_id']}/case/af5-warranty",
                            turns=[
                                EvaluationTurn(
                                    message="晴川 AF5 空气炸锅保修多久？",
                                    context={
                                        "shop_id": fixture["store"]["store_id"],
                                        "sku_id": "QC-AF5-WHITE",
                                    },
                                    expectation=EvaluationExpectation(
                                        require_sources=True,
                                        forbidden_answer_terms=["已经完成退款"],
                                        expected_context_readiness="ready",
                                    ),
                                )
                            ],
                        )
                    ],
                ),
                actor,
            )
            suite = self.service.evaluations.freeze_suite(
                tenant_id,
                suite["id"],
                EvaluationSuiteTransition(
                    expected_record_version=suite["record_version"],
                    note="虚拟店铺验收数据集冻结。",
                ),
                actor,
            )
        assert suite["status"] == "frozen"
        with self.service.db.connect() as conn:
            before = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("sessions", "messages", "handoff_tasks")
            }
        report = self.service.run_evaluation_suite(
            tenant_id,
            suite["id"],
            EvaluationRunRequest(run_key="virtual-store-v1"),
            actor,
        )
        with self.service.db.connect() as conn:
            after = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("sessions", "messages", "handoff_tasks")
            }
        assert report["status"] == "passed"
        assert report["gate"]["passed"] is True
        assert before == after
        return {
            "suite_id": suite["id"],
            "suite_status": suite["status"],
            "dataset_hash": suite["dataset_hash"],
            "run_id": report["id"],
            "run_status": report["status"],
            "runner_version": report["runner_version"],
            "primary_runtime_unchanged": True,
            "idempotent_run_key": report["run_key"],
            "suite": suite,
            "evaluation_report": report,
            "primary_runtime_counts": {"before": before, "after": after},
        }

    def _verify_order_tool_scope(
        self, fixture: dict[str, Any], tenant_id: str
    ) -> dict[str, Any]:
        store_id = fixture["store"]["store_id"]
        context = ToolExecutionContext(
            tenant_id=tenant_id,
            client_id="simulation",
            session_id="simulation-order",
            trace_id="simulation-order",
            trusted_context={
                "authorized": True,
                "order_id": "QC-ORDER-1005",
                "shop_id": store_id,
            },
        )
        spec, arguments = self.service.tools.validate_selection(
            name="get_order_facts",
            arguments={"order_id": "QC-ORDER-1005", "store_id": store_id},
            requested_mode="observe",
            context=context,
        )
        result = self.service.tools.execute(
            spec=spec, arguments=arguments, context=context
        )
        assert result.output["orders"][0]["logistics"]["status"] == "exception"
        mismatch_blocked = False
        blocked_error = None
        try:
            self.service.tools.validate_selection(
                name="get_order_facts",
                arguments={"order_id": "QC-ORDER-1002", "store_id": store_id},
                requested_mode="observe",
                context=context,
            )
        except ValueError as exc:
            mismatch_blocked = "order_scope_mismatch" in str(exc)
            blocked_error = {
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        assert mismatch_blocked
        return {
            "authorized_order_returned": True,
            "scope_mismatch_blocked": True,
            "allowed_tool_output": result.output,
            "blocked_probe_result": blocked_error,
        }

    def _verify_handoff_dispatch(
        self, fixture: dict[str, Any], tenant_id: str, actor: str, run_id: str
    ) -> dict[str, Any]:
        operator_id = self.service.settings.bootstrap_admin_id
        profile = self.service.handoff_staffing.get(
            tenant_id=tenant_id, operator_id=operator_id
        )
        assert profile is not None
        assignments = [
            HandoffOperatorQueueAssignment(
                queue_key=item.queue_key,
                skill_level=max(item.skill_level, 4),
                is_primary=item.is_primary,
            )
            for item in profile.queue_assignments
        ]
        if not assignments:
            queue = self.service.handoffs.list_queues(tenant_id=tenant_id)[0]
            assignments = [
                HandoffOperatorQueueAssignment(
                    queue_key=queue.queue_key, skill_level=5, is_primary=True
                )
            ]
        profile = self.service.handoff_staffing.upsert(
            tenant_id=tenant_id,
            operator_id=operator_id,
            value=HandoffOperatorUpsert(
                display_name=profile.display_name,
                status="active",
                presence="available",
                dispatch_mode="automatic",
                schedule_mode="unrestricted",
                max_active_tasks=100,
                skills=sorted(set([*profile.skills, "售后", "物流异常", "价保"])),
                queue_assignments=assignments,
                presence_ttl_seconds=3600,
                expected_record_version=profile.record_version,
            ),
            actor=actor,
        )
        assert profile.available_for_claim
        principal = self.service.auth.authenticate(
            self.service.settings.bootstrap_client_id,
            self.service.settings.bootstrap_client_key,
            f"virtual-after-sale-{uuid.uuid4().hex[:8]}",
        )
        answer = self.service.chat(
            principal,
            f"virtual-handoff-{uuid.uuid4().hex}",
            "物流地址异常，我要转人工处理",
            {"shop_id": fixture["store"]["store_id"]},
            source_type="simulation",
            source_reference=run_id,
        )
        assert answer.handoff_id
        self.service.handoff_dispatch.run_once(
            worker_id=f"simulation-{actor}",
            tenant_id=tenant_id,
            limit=20,
            scope="simulation",
        )
        task = self.service.handoffs.get(
            tenant_id=tenant_id, handoff_id=answer.handoff_id
        )
        assert task.assigned_to
        assigned_profile = self.service.handoff_staffing.get(
            tenant_id=tenant_id, operator_id=task.assigned_to
        )
        assert assigned_profile is not None
        assert assigned_profile.available_for_claim
        job = next(
            item
            for item in self.service.handoff_dispatch.list_jobs(
                tenant_id=tenant_id, limit=100, scope="simulation"
            )
            if item.handoff_id == task.id
        )
        assert job.status == "assigned"
        return {
            "handoff_id": task.id,
            "queue_key": task.queue_key,
            "assigned_to": task.assigned_to,
            "assigned_operator_available": True,
            "prepared_operator": operator_id,
            "dispatch_job_status": job.status,
            "agent_response": answer.model_dump(mode="json"),
            "handoff_task": task.model_dump(mode="json"),
            "dispatch_job": job.model_dump(mode="json"),
        }

    def _verify_admin_console(self, tenant_id: str) -> dict[str, Any]:
        overview = self.service.admin.overview(tenant_id, scope="simulation")
        assert overview["counts"]["active_tenant_knowledge"] >= 4
        assert overview["counts"]["conversations"] >= 1
        assert overview["counts"]["messages"] >= 2
        assert overview["recent_activity"]
        return {
            "counts": overview["counts"],
            "recent_activity_visible": True,
            "overview": overview,
        }

    def _verify_tenant_isolation(
        self, fixture: dict[str, Any], tenant_id: str
    ) -> dict[str, Any]:
        other = f"{tenant_id}-isolation-check"
        store_id = fixture["store"]["store_id"]
        probes = {
            "catalog": self.service.operations.catalog.list_items(
                other, store_id=store_id
            ),
            "orders": self.service.operations.orders.list_orders(
                other, store_id=store_id
            ),
            "inventory": self.service.operations.inventory.list_balances(
                other, store_id=store_id
            ),
            "competitive": self.service.operations.competitive.list_entity_matches(
                other, store_id=store_id
            ),
            "marketing": self.service.operations.marketing.list_performance(
                other, store_id=store_id
            ),
            "finance": self.service.operations.finance.list_expenses(
                other, store_id=store_id
            ),
        }
        assert all(not records for records in probes.values())
        return {
            "isolated_domains": list(probes),
            "probe_tenant": other,
            "query_results": probes,
        }

    def _verify_connector_contract(self, fixture: dict[str, Any]) -> dict[str, Any]:
        connector = self.service.operations.connectors.get("virtual_taobao")
        action = ExternalAction(
            action="update_safety_stock_buffer",
            idempotency_key=f"{fixture['fixture_id']}:safety-stock",
            payload={"sku_id": "QC-AF5-WHITE", "buffer": 20},
            dry_run=False,
        )
        first = connector.execute(action)
        replay = connector.execute(action)
        verification = connector.verify(action, first)
        assert first.external_request_id == replay.external_request_id
        assert verification.verified is True
        assert first.output["virtual"] is True
        return {
            "virtual": True,
            "idempotent": True,
            "verified": verification.verified,
            "action": action.model_dump(mode="json"),
            "first_result": first.model_dump(mode="json"),
            "replay_result": replay.model_dump(mode="json"),
            "verification": verification.model_dump(mode="json"),
        }

    @staticmethod
    def _scenario(
        scenarios: list[dict[str, Any]],
        demand: dict[str, Any],
        check: Callable[[], dict[str, Any]],
    ) -> None:
        base = {
            "id": demand["id"],
            "module": demand["module"],
            "title": demand["title"],
            "input": demand["input"],
            "expected": demand["expected"],
        }
        try:
            output = check()
        except Exception as exc:
            output = {
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            }
            scenarios.append(
                {
                    **base,
                    "status": "failed",
                    "assertions": [
                        {"label": item, "passed": False, "status": "not_confirmed"}
                        for item in demand["expected"]
                    ],
                    "output": output,
                    "detail": output,
                }
            )
        else:
            scenarios.append(
                {
                    **base,
                    "status": "passed",
                    "assertions": [
                        {"label": item, "passed": True, "status": "passed"}
                        for item in demand["expected"]
                    ],
                    "output": output,
                    "detail": output,
                }
            )

    @staticmethod
    def _skipped(
        scenarios: list[dict[str, Any]], demand: dict[str, Any], reason: str
    ) -> None:
        output = {"reason": reason}
        scenarios.append(
            {
                "id": demand["id"],
                "module": demand["module"],
                "title": demand["title"],
                "input": demand["input"],
                "expected": demand["expected"],
                "status": "skipped",
                "assertions": [
                    {"label": item, "passed": None, "status": "skipped"}
                    for item in demand["expected"]
                ],
                "output": output,
                "detail": output,
            }
        )
