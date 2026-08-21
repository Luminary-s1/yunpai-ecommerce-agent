from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient

from conftest import make_settings
from ecommerce_agent.api import create_app
from ecommerce_agent.business.catalog import CatalogItemUpsert, CatalogService
from ecommerce_agent.business.inventory import InventoryBalanceUpsert, InventoryService
from ecommerce_agent.customer_service_workbench import (
    ensure_m8r_frozen_suite,
    load_m8r_eval_definition,
)
from ecommerce_agent.evaluation import EvaluationRunRequest
from ecommerce_agent.service import AgentService


ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}


def _counts(service: AgentService) -> dict[str, int]:
    with service.db.connect() as conn:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("sessions", "messages", "handoff_tasks", "channel_outbox")
        }


def _seed_sales(service: AgentService) -> None:
    now = datetime.now(UTC)
    CatalogService(service.db).upsert(
        "tenant-test",
        CatalogItemUpsert(
            connector_id="virtual_taobao",
            store_id="store-a",
            item_id="ITEM-SKU-1",
            sku_id="SKU-1",
            title="恒温水壶",
            status="active",
            sale_price=Decimal("129.00"),
            currency="CNY",
            source_updated_at=now,
            source_id="virtual:m8r-wp4:catalog:SKU-1",
        ),
    )
    InventoryService(service.db).upsert(
        "tenant-test",
        InventoryBalanceUpsert(
            connector_id="virtual_taobao",
            store_id="store-a",
            warehouse_id="INTERNAL-WAREHOUSE",
            sku_id="SKU-1",
            on_hand=Decimal("8"),
            reserved=Decimal("3"),
            inbound=Decimal("2"),
            average_daily_sales=Decimal("1"),
            source_updated_at=now,
            source_id="virtual:m8r-wp4:inventory:SKU-1",
        ),
    )


def _install_sales_model(service: AgentService) -> None:
    def generate_json(messages, **_kwargs):
        payload = json.loads(messages[-1]["content"])
        if payload.get("task_type") != "agent_decision":
            return {"intent": "product_inquiry", "confidence": 0.98}
        if payload.get("latest_observation"):
            return {
                "intent": "product_inquiry",
                "mode": "finish",
                "reason": "verified_tool_result_complete",
                "confidence": 0.98,
            }
        trusted = payload["trusted_context"]
        return {
            "intent": "product_inquiry",
            "mode": "observe",
            "tool_name": "get_customer_sales_facts",
            "arguments": {
                "store_id": trusted["store_id"],
                "sku_id": trusted["sku_id"],
            },
            "reason": "controlled_model_selected_fact_tool",
            "confidence": 0.98,
        }

    service.model.generate_json = generate_json
    service.model.generate = lambda _messages: "这款商品当前可售库存为 5 件。"


def test_m8r_input_and_oracle_are_physically_separate() -> None:
    definition = load_m8r_eval_definition()

    assert len(definition["cases"]) >= 8
    assert definition["input_hash"] != definition["oracle_hash"]
    assert definition["runner_contract"]["oracle_fields_visible_to_runner"] == []
    assert any(item["partition"] == "holdout" for item in definition["cases"])
    for case in definition["evaluation_cases"]:
        for turn in case["turns"]:
            assert set(turn) <= {"message", "context", "expectation"}


def test_shadow_catalog_browsing_is_read_only_and_feedback_reuses_governance(
    tmp_path,
) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        service = app.state.agent
        _seed_sales(service)
        _install_sales_model(service)
        before = _counts(service)

        catalog = client.get(
            "/v1/admin/customer-service-shadow/scenarios", headers=ADMIN_HEADERS
        )
        assert catalog.status_code == 200
        assert client.get(
            "/v1/admin/customer-service-shadow/runs", headers=ADMIN_HEADERS
        ).status_code == 200
        assert client.get(
            "/v1/admin/customer-service-shadow/feedback", headers=ADMIN_HEADERS
        ).status_code == 200
        assert client.get("/admin").status_code == 200
        assert client.get(
            "/v1/admin/evaluations/suites", headers=ADMIN_HEADERS
        ).status_code == 200
        assert client.get(
            "/v1/admin/evaluations/runs", headers=ADMIN_HEADERS
        ).status_code == 200
        assert _counts(service) == before

        run = client.post(
            "/v1/admin/customer-service-shadow/scenarios/sales-exact-quantity/runs",
            headers=ADMIN_HEADERS,
            json={"run_key": "manual-1"},
        )
        assert run.status_code == 201, run.text
        response = run.json()["responses"][-1]
        assert response["answer"] == "这款商品当前可售库存为 5 件。"
        assert response["suggestion"]["execution_mode"] == "shadow"
        assert response["suggestion"]["delivery_status"] == "suggestion_not_sent"
        assert len(response["suggestion"]["facts"]["evidence_ids"]) == 2
        assert run.json()["assertion"]["passed"] is True
        assert run.json()["assertion"]["actual"]["turns"][0]["violations"] == []

        after_run = _counts(service)
        assert after_run["sessions"] == before["sessions"] + 1
        assert after_run["messages"] == before["messages"] + 2
        assert after_run["handoff_tasks"] == before["handoff_tasks"]
        assert after_run["channel_outbox"] == before["channel_outbox"]

        feedback = client.post(
            f"/v1/admin/customer-service-shadow/messages/{response['message_id']}/feedback",
            headers=ADMIN_HEADERS,
            json={
                "rating": -1,
                "corrected_answer": "这款商品当前可售库存为 5 件，请以页面实时状态为准。",
                "note": "人工核对后补充实时口径",
                "evidence_source": "M8-R WP4 人工审阅",
            },
        )
        assert feedback.status_code == 201, feedback.text
        assert feedback.json()["status"] == "candidate_pending"
        history = client.get(
            "/v1/admin/customer-service-shadow/feedback", headers=ADMIN_HEADERS
        ).json()
        assert history[0]["candidate_status"] == "pending"


def test_wp4_evaluation_endpoint_explicitly_uses_shadow_mode(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        service = app.state.agent
        before = _counts(service)
        captured = {}

        def run_evaluation_suite(
            tenant_id, suite_id, request, actor, *, execution_mode="live"
        ):
            captured.update(
                {
                    "tenant_id": tenant_id,
                    "suite_id": suite_id,
                    "run_key": request.run_key,
                    "actor": actor,
                    "execution_mode": execution_mode,
                }
            )
            return {
                "id": "eval-wp4-shadow",
                "run_key": request.run_key,
                "status": "passed",
                "metrics": {"passed_cases": 8, "total_cases": 8},
                "results": [],
            }

        service.run_evaluation_suite = run_evaluation_suite
        response = client.post(
            "/v1/admin/customer-service-shadow/evaluations/runs",
            headers=ADMIN_HEADERS,
            json={"run_key": "wp4-shadow-contract"},
        )

        assert response.status_code == 201, response.text
        assert captured["execution_mode"] == "shadow"
        assert captured["run_key"] == "wp4-shadow-contract"
        assert _counts(service) == before


def test_evaluation_runner_never_receives_oracle_fields(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        suite = ensure_m8r_frozen_suite(service, "tenant-test", "admin-test")
        seen = []

        def runner(case):
            seen.append(case)
            assert all("expectation" not in turn for turn in case["turns"])
            return [
                SimpleNamespace(
                    answer="已安全降级。",
                    intent="after_sales",
                    risk_level="medium",
                    requires_human=True,
                    reason="shadow_write_suppressed",
                    sources=[],
                    evidence_ids=[],
                    model_fallback=False,
                    context_readiness="ready",
                    suggestion={
                        "decision": {"mode": "handoff"},
                        "delivery_status": "suggestion_not_sent",
                        "facts": {},
                        "human_task": {
                            "required": True,
                            "persisted": False,
                            "shadow_observation_only": True,
                        },
                    },
                )
                for _turn in case["turns"]
            ]

        run = service.evaluations.run_suite(
            "tenant-test",
            suite["id"],
            EvaluationRunRequest(run_key="oracle-separation"),
            "admin-test",
            runner,
        )

        assert len(seen) == len(suite["cases"])
        assert all(
            result["actual"]["runner_contract"]["oracle_fields_visible"] == []
            for result in run["results"]
        )
    finally:
        service.close()


def test_structured_wp4_metrics_cover_sources_privacy_and_handoff(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        case = {
            "id": "structured-case",
            "case_key": "structured-case",
            "scenario": "sales.development",
            "case_hash": "a" * 64,
            "turns": [
                {
                    "message": "有货吗",
                    "context": {},
                    "expectation": {
                        "expected_requires_human": False,
                        "expected_delivery_status": "suggestion_not_sent",
                        "expected_fact_tool": "get_customer_sales_facts",
                        "min_fact_evidence": 2,
                        "expected_freshness_status": "current",
                        "expected_source_type": "virtual",
                        "require_source_completeness": True,
                    },
                }
            ],
        }
        response = SimpleNamespace(
            answer="这款商品目前有货。",
            intent="product_inquiry",
            risk_level="low",
            requires_human=False,
            reason="verified_tool_result_complete",
            sources=[],
            model_fallback=False,
            context_readiness="ready",
            suggestion={
                "decision": {"mode": "finish"},
                "delivery_status": "suggestion_not_sent",
                "facts": {
                    "tool_name": "get_customer_sales_facts",
                    "evidence_ids": ["fact-1", "fact-2"],
                    "data_as_of": "2026-08-21T00:00:00+00:00",
                    "freshness_status": "current",
                    "source_type": "virtual",
                },
                "human_task": None,
            },
        )

        result = service.evaluations._evaluate_case(case, [response], None)
        metrics = service.evaluations._metrics([result], {})

        assert result["passed"] is True
        assert metrics["handoff_reasonableness"] == 1
        assert metrics["source_completeness_rate"] == 1
        assert metrics["sensitive_output_rate"] == 0
    finally:
        service.close()
