from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from ecommerce_agent.business.catalog import CatalogItemUpsert, CatalogService
from ecommerce_agent.business.inventory import InventoryBalanceUpsert, InventoryService
from ecommerce_agent.business.orders import (
    AfterSaleCaseInput,
    LogisticsSnapshotInput,
    OrderLineInput,
    OrderService,
    OrderUpsert,
)
from ecommerce_agent.customer_service_loop import (
    CUSTOMER_SERVICE_SUGGESTION_VERSION,
    build_customer_service_response_policy,
    build_customer_service_suggestion,
    validate_customer_service_draft,
)
from ecommerce_agent.graph import prepare_generation, verify_response
from ecommerce_agent.prompts import build_decision_messages, build_messages
from ecommerce_agent.service import AgentService
from ecommerce_agent.tools import ToolRegistry, ToolResult, ToolSpec

from conftest import make_settings, principal_for


def _sales_output(*, freshness: str = "current", usable: bool = True) -> dict:
    return {
        "contract_version": "customer-service-facts-v1",
        "domain": "sales",
        "state": "available",
        "scope": {
            "tenant_id": "tenant-a",
            "store_id": "store-a",
            "sku_id": "SKU-1",
        },
        "facts": {
            "product": {
                "state": "available",
                "sku_id": "SKU-1",
                "title": "恒温水壶",
                "sale_price": "129.00",
                "currency": "CNY",
            },
            "inventory": {
                "state": "available",
                "available_quantity": "5.00",
                "inbound_quantity": "2.00",
            },
        },
        "missing": [],
        "data_as_of": "2026-08-20T08:08:50+00:00",
        "freshness": {
            "status": freshness,
            "usable_as_current": usable,
            "reason_codes": [] if usable else ["snapshot_age_exceeded"],
        },
        "source_provenance": {
            "source_type": "virtual",
            "virtual": True,
            "policy_version": "source-provenance-v1",
        },
        "evidence": [
            {
                "evidence_id": "cs-fact-catalog-1",
                "report_type": "catalog_snapshot",
                "data_as_of": "2026-08-20T08:08:50+00:00",
                "version": 1,
                "current": True,
            },
            {
                "evidence_id": "cs-fact-inventory-1",
                "report_type": "inventory_snapshot",
                "data_as_of": "2026-08-20T08:08:50+00:00",
                "version": 1,
                "current": True,
            },
        ],
    }


def _tool_result(output: dict) -> dict:
    policy = build_customer_service_response_policy(
        "get_customer_sales_facts", output
    )
    return {
        "tool_name": "get_customer_sales_facts",
        "status": "success",
        "output": {**output, "response_policy": policy},
        "postcondition_met": True,
    }


def _node(service: AgentService, name: str):
    return service.graph.get_graph().nodes[name].data


def _seed_sales_facts(
    service: AgentService,
    *,
    source_time: datetime | None = None,
    include_inventory: bool = True,
) -> None:
    observed = source_time or datetime.now(UTC)
    CatalogService(service.db).upsert(
        "tenant-test",
        CatalogItemUpsert(
            connector_id="virtual_taobao",
            store_id="store-a",
            item_id="ITEM-1",
            sku_id="SKU-1",
            title="恒温水壶",
            status="active",
            sale_price=Decimal("129.00"),
            currency="CNY",
            source_updated_at=observed,
            source_id="virtual:wp3:catalog:1",
        ),
    )
    if include_inventory:
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
                source_updated_at=observed,
                source_id="virtual:wp3:inventory:1",
            ),
        )


def _seed_after_sales_facts(service: AgentService) -> None:
    now = datetime.now(UTC)
    OrderService(service.db).upsert(
        "tenant-test",
        OrderUpsert(
            connector_id="virtual_taobao",
            store_id="store-a",
            order_id="ORDER-1",
            order_status="shipped",
            payment_status="paid",
            currency="CNY",
            total_amount=Decimal("129.00"),
            placed_at=now - timedelta(days=1),
            buyer_ref_hash="private-buyer-hash",
            lines=[
                OrderLineInput(
                    line_id="INTERNAL-LINE-1",
                    sku_id="SKU-1",
                    title="恒温水壶",
                    quantity=1,
                    unit_price=Decimal("129.00"),
                )
            ],
            logistics=LogisticsSnapshotInput(
                carrier="测试快递",
                tracking_no_masked="TRACK****0001",
                status="in_transit",
                last_event="运输中，联系电话 13800138000",
                last_event_at=now - timedelta(minutes=10),
            ),
            after_sales=[
                AfterSaleCaseInput(
                    case_id="INTERNAL-CASE-1",
                    case_type="refund",
                    status="reviewing",
                    requested_amount=Decimal("20.00"),
                    approved_amount=Decimal("0"),
                    reason_code="price_protection",
                    opened_at=now - timedelta(hours=2),
                    updated_at=now - timedelta(hours=1),
                )
            ],
            source_updated_at=now,
            source_id="virtual:wp3:order:1",
        ),
    )


def _install_fact_model(
    service: AgentService,
    *,
    tool_name: str,
    intent: str,
    arguments_from_payload,
    answer: str,
) -> tuple[list[dict], list[list[dict[str, str]]]]:
    decision_payloads: list[dict] = []
    generation_messages: list[list[dict[str, str]]] = []

    def generate_json(messages: list[dict[str, str]], **_kwargs) -> dict:
        payload = json.loads(messages[-1]["content"])
        if payload.get("task_type") != "agent_decision":
            return {"intent": intent, "confidence": 0.96}
        decision_payloads.append(payload)
        if payload.get("latest_observation"):
            return {
                "intent": intent,
                "mode": "finish",
                "reason": "verified_customer_service_fact_ready",
                "confidence": 0.96,
            }
        return {
            "intent": intent,
            "mode": "observe",
            "tool_name": tool_name,
            "arguments": arguments_from_payload(payload),
            "expected_outcome": "trusted customer-service facts",
            "reason": "model_selected_customer_service_fact_tool",
            "confidence": 0.96,
        }

    def generate(messages: list[dict[str, str]]) -> str:
        generation_messages.append(messages)
        return answer

    service.model.generate_json = generate_json  # type: ignore[method-assign]
    service.model.generate = generate  # type: ignore[method-assign]
    return decision_payloads, generation_messages


class _RefundActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(min_length=1)


def _write_registry(calls: list[str]) -> ToolRegistry:
    registry = ToolRegistry()

    def execute(value: BaseModel, _context) -> ToolResult:
        order_id = str(value.model_dump()["order_id"])
        calls.append(order_id)
        return ToolResult(
            status="success",
            output={"order_id": order_id, "status": "refunded"},
            postcondition_met=True,
        )

    registry.register(
        ToolSpec(
            name="refund_order_for_wp3_test",
            description="WP3 shadow write barrier test tool",
            kind="write",
            input_model=_RefundActionInput,
            handler=execute,
            required_context_fields=("authorized", "order_id"),
            idempotency_fields=("order_id",),
            verifier=lambda _args, result, _context: result.status == "success",
        )
    )
    return registry


def test_inventory_disclosure_is_customer_policy_not_raw_fact_dump() -> None:
    policy = build_customer_service_response_policy(
        "get_customer_sales_facts", _sales_output()
    )

    assert policy["current_claims_allowed"] is True
    assert policy["inventory"]["default_customer_view"] == "availability_status_only"
    assert policy["inventory"]["exact_available_quantity"] == (
        "explicit_customer_request_and_current_fact_only"
    )
    assert policy["inventory"]["inbound_quantity"] == "internal_only"
    assert policy["inventory"]["warehouse_detail"] == "never_disclose"


def test_stale_fact_policy_requires_source_time_and_forbids_current_wording() -> None:
    output = _sales_output(freshness="stale", usable=False)
    policy = build_customer_service_response_policy(
        "get_customer_sales_facts", output
    )

    assert policy["current_claims_allowed"] is False
    assert policy["must_display_data_as_of"] is True
    assert policy["data_as_of"] == output["data_as_of"]

    unsafe = validate_customer_service_draft(
        "目前有货，可以立即下单。", _tool_result(output)
    )
    safe = validate_customer_service_draft(
        "根据 2026-08-20T08:08:50+00:00 的导出快照，当时显示有货，当前库存仍需核对。",
        _tool_result(output),
    )
    dated_but_current = validate_customer_service_draft(
        "根据 2026-08-20 的数据，目前有货，可以下单。",
        _tool_result(output),
    )
    assert unsafe == (False, "customer_service_data_as_of_required")
    assert safe == (True, "customer_service_output_policy_passed")
    assert dated_but_current == (False, "customer_service_stale_current_claim")


def test_inventory_disclosure_gate_enforces_customer_visible_boundaries() -> None:
    result = _tool_result(_sales_output())

    unasked_exact = validate_customer_service_draft(
        "这款商品目前有货，可售库存为 5 件。",
        result,
        question="这款商品有货吗",
    )
    asked_exact = validate_customer_service_draft(
        "这款商品目前可售库存为 5 件。",
        result,
        question="这款商品现在还有多少件",
    )
    inbound_leak = validate_customer_service_draft(
        "这款商品目前有货，另有 2 件在途。",
        result,
        question="这款商品现在还有多少件",
    )

    assert unasked_exact == (
        False,
        "customer_service_exact_inventory_not_requested",
    )
    assert asked_exact == (True, "customer_service_output_policy_passed")
    assert inbound_leak == (False, "customer_service_inbound_inventory_internal")


def test_missing_inventory_cannot_be_presented_as_zero() -> None:
    output = _sales_output()
    output["state"] = "partial"
    output["facts"]["inventory"] = {
        "state": "missing",
        "available_quantity": None,
        "inbound_quantity": None,
    }
    output["missing"] = ["inventory_snapshot_missing"]

    validated = validate_customer_service_draft(
        "当前库存为 0 件。",
        _tool_result(output),
        question="现在还有多少件",
    )

    assert validated == (False, "customer_service_missing_inventory_fabricated")


@pytest.mark.parametrize(
    ("message", "decision", "expected_route"),
    [
        (
            "我不是要退款，只是想了解退款规则",
            {
                "intent": "product_inquiry",
                "mode": "answer",
                "reason": "model_identified_policy_question",
                "confidence": 0.95,
            },
            "answer",
        ),
        (
            "如果买了不合适，可以申请退款吗？",
            {
                "intent": "product_inquiry",
                "mode": "answer",
                "reason": "model_identified_presale_hypothetical",
                "confidence": 0.95,
            },
            "answer",
        ),
        (
            "先查库存，再说明退款规则",
            {
                "intent": "product_inquiry",
                "mode": "observe",
                "tool_name": "get_customer_sales_facts",
                "arguments": {"store_id": "store-a", "sku_id": "SKU-1"},
                "reason": "model_selected_first_step_for_compound_request",
                "confidence": 0.95,
            },
            "observe",
        ),
    ],
)
def test_keyword_shaped_requests_do_not_override_model_semantic_decisions(
    tmp_path,
    message: str,
    decision: dict,
    expected_route: str,
) -> None:
    settings = replace(
        make_settings(tmp_path),
        bootstrap_client_can_supply_order_context=True,
    )
    service = AgentService(settings)
    try:
        session_id = service.db.resolve_session(
            tenant_id="tenant-test",
            client_id="client-test",
            external_session_id=f"wp3-semantics-{expected_route}",
            subject_hash="subject-a",
        )
        gated = _node(service, "decision_gate").invoke(
            {
                "decision": decision,
                "normalized_input": message,
                "react_step": 0,
                "tool_result": {},
                "session_id": session_id,
                "tenant_id": "tenant-test",
                "execution_mode": "live",
                "context": {
                    "authorized": True,
                    "shop_id": "store-a",
                    "sku_id": "SKU-1",
                },
                "trace": [],
            }
        )

        assert gated["decision_mode"] == decision["mode"]
        assert gated["route"] == expected_route
    finally:
        service.close()


def test_context_snapshot_records_wp1_governance_as_signals_not_routes(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        session_id = service.db.resolve_session(
            tenant_id="tenant-test",
            client_id="client-test",
            external_session_id="wp3-context",
            subject_hash="subject-a",
        )
        content = {
            "scripts": [
                {
                    "id": "kb-script-1",
                    "source": "evolution:m8r-customer-service:row-2",
                    "version": 3,
                    "store_id": "store-a",
                    "sku_id": None,
                    "approved_by": "xie-liangxuan",
                    "effective_from": "2026-08-20T00:00:00+00:00",
                    "effective_to": None,
                }
            ],
            "keyword_signals": [
                {
                    "knowledge_id": "kb-keyword-1",
                    "keyword": "退款",
                    "scenario": "after_sales",
                    "risk_level": "medium",
                    "authority": "advisory_only",
                    "source": "evolution:m8r-customer-service:row-3",
                    "version": 1,
                }
            ],
            "fast_path_eligible": False,
            "fast_path_rule": (
                "human_approved_immutable_exact_normalized_match_only"
            ),
            "keyword_authority": "advisory_only",
        }

        snapshot = service.contexts.build(
            tenant_id="tenant-test",
            session_id=session_id,
            trace_id="trace-a",
            stage="decision",
            sequence=0,
            question="我不是要退款，只是了解规则",
            trusted_context={"shop_id": "store-a"},
            documents=[],
            sops=[],
            tool_catalog=[],
            history=[],
            customer_service_content=content,
        )

        signal = snapshot.bundle["customer_service_content"]["keyword_signals"][0]
        assert signal["authority"] == "advisory_only"
        assert "route" not in signal
        assert "mode" not in signal
        assert any(
            item["type"] == "customer_service_keyword_signal"
            and item["authority"] == "advisory_only"
            for item in snapshot.evidence
        )
    finally:
        service.close()


def test_decision_and_generation_prompts_receive_the_same_wp3_contract() -> None:
    policy = build_customer_service_response_policy(
        "get_customer_sales_facts", _sales_output()
    )
    context = {
        "context_version": "context.v1",
        "trusted_session_state": {
            "business_context_authorized": True,
            "store_id": "store-a",
        },
        "current_subject": {"sku_id": "SKU-1"},
        "product_advisor": {},
        "sop_evidence": [],
        "customer_service_content": {
            "keyword_signals": [
                {
                    "keyword": "库存",
                    "authority": "advisory_only",
                }
            ],
            "keyword_authority": "advisory_only",
        },
        "output_constraints": {"customer_service": policy},
        "recent_history": [],
    }
    tool_result = _tool_result(_sales_output())

    decision = build_decision_messages(
        question="这款商品有货吗",
        documents=[],
        context=context,
        history=[],
        tool_catalog=[],
        observation=tool_result,
        step_count=1,
        max_steps=4,
    )
    generation = build_messages(
        question="这款商品有货吗",
        documents=[],
        context=context,
        history=[],
        verified_tool_result=tool_result,
    )

    assert '"semantic_authority": false' in decision[-1]["content"]
    assert '"customer_service_content"' in decision[-1]["content"]
    assert '"default_customer_view": "availability_status_only"' in generation[-1][
        "content"
    ]
    assert '"inbound_quantity": "internal_only"' in generation[-1]["content"]


def test_prepare_generation_is_the_shared_nonstream_and_stream_contract(tmp_path) -> None:
    settings = make_settings(tmp_path)
    tool_result = _tool_result(_sales_output())
    state = {
        "normalized_input": "这款商品有货吗",
        "retrieved": [],
        "context_bundle": {
            "recent_history": [],
            "output_constraints": {
                "customer_service": tool_result["output"]["response_policy"]
            },
        },
        "tool_result": tool_result,
        "decision": {"reason": "verified inventory observation"},
        "intent_routing": {"prompt_variant": "sales"},
        "intent": "product_inquiry",
    }

    plan = prepare_generation(state, settings)

    assert plan.fixed_text is None
    assert plan.messages
    assert plan.trace_step == "generate:model"
    assert '"response_policy"' in plan.messages[-1]["content"]


def test_shadow_suggestion_is_traceable_and_never_claims_platform_delivery(tmp_path) -> None:
    settings = make_settings(tmp_path)
    tool_result = _tool_result(_sales_output())
    state = {
        "execution_mode": "shadow",
        "decision_mode": "finish",
        "intent": "product_inquiry",
        "risk_level": "low",
        "route_reason": "verified_tool_result_complete",
        "requires_human": False,
        "model_fallback": False,
        "context_snapshot_id": "ctx-1",
        "context_evidence_ids": ["ev-1"],
        "retrieved": [],
        "tool_result": tool_result,
        "context_bundle": {
            "customer_service_content": {
                "keyword_signals": [
                    {"knowledge_id": "kb-keyword-1", "authority": "advisory_only"}
                ]
            }
        },
        "handoff_id": None,
        "handoff_status": None,
    }

    suggestion = build_customer_service_suggestion(state, settings)

    assert suggestion["contract_version"] == CUSTOMER_SERVICE_SUGGESTION_VERSION
    assert suggestion["execution_mode"] == "shadow"
    assert suggestion["delivery_status"] == "suggestion_not_sent"
    assert suggestion["facts"]["evidence_ids"] == [
        "cs-fact-catalog-1",
        "cs-fact-inventory-1",
    ]
    assert suggestion["facts"]["freshness_status"] == "current"
    assert suggestion["facts"]["source_type"] == "virtual"
    assert suggestion["model"]["provider"] == settings.model_provider
    assert suggestion["model"]["name"] == settings.model_name
    assert suggestion["human_task"] is None


def test_sales_fact_tool_runs_end_to_end_and_persists_traceable_suggestion(
    tmp_path,
) -> None:
    settings = replace(
        make_settings(tmp_path),
        bootstrap_client_can_supply_order_context=True,
    )
    service = AgentService(settings)
    try:
        _seed_sales_facts(service)
        service.knowledge.retrieve = lambda *_args, **_kwargs: []  # type: ignore[method-assign]
        decisions, generations = _install_fact_model(
            service,
            tool_name="get_customer_sales_facts",
            intent="product_inquiry",
            arguments_from_payload=lambda payload: {
                "store_id": payload["trusted_context"]["store_id"],
                "sku_id": payload["trusted_context"]["sku_id"],
            },
            answer="这款商品目前有货，可以正常选购。",
        )

        response = service.chat(
            principal_for(service),
            "wp3-sales-e2e",
            "这款商品有货吗",
            {"shop_id": "store-a", "sku_id": "SKU-1"},
        )

        assert response.answer == "这款商品目前有货，可以正常选购。"
        assert response.reason == "verified_tool_result_complete"
        assert response.requires_human is False
        assert "5" not in response.answer
        assert "在途" not in response.answer
        assert len(decisions) == 2
        assert decisions[0]["latest_observation"] == {}
        assert decisions[1]["latest_observation"]["tool_name"] == (
            "get_customer_sales_facts"
        )
        assert len(generations) == 1
        assert response.suggestion is not None
        assert response.suggestion.contract_version == (
            CUSTOMER_SERVICE_SUGGESTION_VERSION
        )
        assert response.suggestion.facts["tool_name"] == (
            "get_customer_sales_facts"
        )
        assert len(response.suggestion.facts["evidence_ids"]) == 2
        assert response.suggestion.facts["freshness_status"] == "current"
        assert response.suggestion.facts["source_type"] == "virtual"
        assert response.suggestion.model["name"] == settings.model_name

        with service.db.connect() as conn:
            row = conn.execute(
                """
                SELECT detail_json FROM audit_log
                WHERE event_type='chat.completed' AND subject_id=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (response.message_id,),
            ).fetchone()
        persisted = json.loads(str(row["detail_json"]))
        assert persisted["suggestion"]["contract_version"] == (
            CUSTOMER_SERVICE_SUGGESTION_VERSION
        )
        assert persisted["suggestion"]["facts"]["evidence_ids"] == (
            response.suggestion.facts["evidence_ids"]
        )
    finally:
        service.close()


def test_after_sales_tool_recovers_order_scope_on_the_second_turn(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        bootstrap_client_can_supply_order_context=True,
    )
    service = AgentService(settings)
    try:
        _seed_after_sales_facts(service)
        service.knowledge.retrieve = lambda *_args, **_kwargs: []  # type: ignore[method-assign]
        decisions, _generations = _install_fact_model(
            service,
            tool_name="get_customer_after_sales_facts",
            intent="after_sales",
            arguments_from_payload=lambda payload: {
                "store_id": payload["trusted_context"]["store_id"],
                "order_id": payload["trusted_context"]["order_id"],
                "include_history": True,
            },
            answer="订单已发货，物流正在运输中，退款申请仍在审核。",
        )

        first = service.chat(
            principal_for(service),
            "wp3-after-sales-multi-turn",
            "帮我查一下这笔订单的物流和退款进度",
            {"shop_id": "store-a", "order_id": "ORDER-1"},
        )
        second = service.chat(
            principal_for(service),
            "wp3-after-sales-multi-turn",
            "它现在处理到哪一步了",
            {"shop_id": "store-a"},
        )

        assert first.requires_human is False
        assert second.requires_human is False
        assert first.suggestion is not None
        assert second.suggestion is not None
        assert first.suggestion.facts["tool_name"] == (
            "get_customer_after_sales_facts"
        )
        assert second.suggestion.facts["tool_name"] == (
            "get_customer_after_sales_facts"
        )
        assert len(second.suggestion.facts["evidence_ids"]) == 3
        second_first_decision = decisions[2]
        assert second_first_decision["latest_observation"] == {}
        assert second_first_decision["trusted_context"]["order_id"] == "ORDER-1"
        assert "private-buyer-hash" not in second.answer
        assert "TRACK" not in second.answer
        assert "13800138000" not in second.answer
    finally:
        service.close()


def test_stream_and_nonstream_use_identical_generation_contracts(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        bootstrap_client_can_supply_order_context=True,
    )
    service = AgentService(settings)
    try:
        _seed_sales_facts(service)
        service.knowledge.retrieve = lambda *_args, **_kwargs: []  # type: ignore[method-assign]
        _decisions, nonstream_generations = _install_fact_model(
            service,
            tool_name="get_customer_sales_facts",
            intent="product_inquiry",
            arguments_from_payload=lambda payload: {
                "store_id": payload["trusted_context"]["store_id"],
                "sku_id": payload["trusted_context"]["sku_id"],
            },
            answer="这款商品目前有货，可以正常选购。",
        )
        nonstream = service.chat(
            principal_for(service),
            "wp3-nonstream-contract",
            "这款商品有货吗",
            {"shop_id": "store-a", "sku_id": "SKU-1"},
        )

        stream_generations: list[list[dict[str, str]]] = []

        def stream_generate(messages: list[dict[str, str]]):
            stream_generations.append(messages)
            yield "这款商品目前有货，"
            yield "可以正常选购。"

        service.model.stream_generate = stream_generate  # type: ignore[method-assign]
        events = list(
            service.chat_stream(
                principal_for(service),
                "wp3-stream-contract",
                "这款商品有货吗",
                {"shop_id": "store-a", "sku_id": "SKU-1"},
                idempotency_key=None,
            )
        )

        streamed_answer = "".join(
            event["text"] for event in events if event["event"] == "delta"
        )
        assert streamed_answer == nonstream.answer
        assert events[-1]["response"]["answer"] == nonstream.answer
        assert len(nonstream_generations) == 1
        assert len(stream_generations) == 1
        assert stream_generations[0] == nonstream_generations[0]
        assert events[-1]["response"]["suggestion"]["facts"]["tool_name"] == (
            "get_customer_sales_facts"
        )
    finally:
        service.close()


def test_stream_never_emits_an_unverified_stale_inventory_draft(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        bootstrap_client_can_supply_order_context=True,
    )
    service = AgentService(settings)
    try:
        _seed_sales_facts(
            service,
            source_time=datetime.now(UTC) - timedelta(days=5),
        )
        service.knowledge.retrieve = lambda *_args, **_kwargs: []  # type: ignore[method-assign]
        _install_fact_model(
            service,
            tool_name="get_customer_sales_facts",
            intent="product_inquiry",
            arguments_from_payload=lambda payload: {
                "store_id": payload["trusted_context"]["store_id"],
                "sku_id": payload["trusted_context"]["sku_id"],
            },
            answer="目前有货，可以立即下单。",
        )
        service.model.stream_generate = lambda _messages: iter(  # type: ignore[method-assign]
            ("目前有货，", "可以立即下单。")
        )

        events = list(
            service.chat_stream(
                principal_for(service),
                "wp3-stale-stream",
                "这款商品有货吗",
                {"shop_id": "store-a", "sku_id": "SKU-1"},
                idempotency_key=None,
            )
        )

        streamed_answer = "".join(
            event["text"] for event in events if event["event"] == "delta"
        )
        response = events[-1]["response"]
        assert "目前有货" not in streamed_answer
        assert streamed_answer == response["answer"]
        assert response["requires_human"] is True
        assert response["reason"] == "customer_service_data_as_of_required"
    finally:
        service.close()


def test_stale_and_missing_inventory_drafts_degrade_to_handoff() -> None:
    stale_output = _sales_output(freshness="stale", usable=False)
    stale_state = {
        "draft": "目前有货，可以立即下单。",
        "normalized_input": "这款商品有货吗",
        "retrieved": [],
        "context_bundle": {},
        "tool_result": _tool_result(stale_output),
        "model_fallback": False,
        "model_retry_advised": False,
        "trace": [],
    }
    missing_output = _sales_output()
    missing_output["state"] = "partial"
    missing_output["facts"]["inventory"] = {
        "state": "missing",
        "available_quantity": None,
        "inbound_quantity": None,
    }
    missing_output["missing"] = ["inventory_snapshot_missing"]
    missing_state = {
        **stale_state,
        "draft": "当前库存为 0 件。",
        "normalized_input": "现在还有多少件",
        "tool_result": _tool_result(missing_output),
    }

    stale = verify_response(stale_state)
    missing = verify_response(missing_state)

    assert stale["review_route"] == "handoff"
    assert stale["route_reason"] == "customer_service_data_as_of_required"
    assert missing["review_route"] == "handoff"
    assert missing["route_reason"] == "customer_service_missing_inventory_fabricated"


def test_shadow_action_creates_only_an_unsent_suggestion(tmp_path) -> None:
    calls: list[str] = []
    settings = replace(
        make_settings(tmp_path),
        bootstrap_client_can_supply_order_context=True,
    )
    service = AgentService(settings, tool_registry=_write_registry(calls))
    try:
        service.knowledge.retrieve = lambda *_args, **_kwargs: []  # type: ignore[method-assign]

        def decide(messages: list[dict[str, str]], **_kwargs) -> dict:
            payload = json.loads(messages[-1]["content"])
            if payload.get("task_type") != "agent_decision":
                return {"intent": "after_sales", "confidence": 0.98}
            return {
                "intent": "after_sales",
                "mode": "act",
                "tool_name": "refund_order_for_wp3_test",
                "arguments": {"order_id": "ORDER-1"},
                "reason": "model_selected_refund_action",
                "confidence": 0.98,
            }

        service.model.generate_json = decide  # type: ignore[method-assign]
        response = service.chat(
            principal_for(service),
            "wp3-shadow-write",
            "请帮我立即退款",
            {"shop_id": "store-a", "order_id": "ORDER-1"},
            execution_mode="shadow",
        )

        assert calls == []
        assert response.requires_human is True
        assert response.reason == "shadow_write_suppressed"
        assert response.handoff_id is None
        assert response.suggestion is not None
        assert response.suggestion.execution_mode == "shadow"
        assert response.suggestion.delivery_status == "suggestion_not_sent"
        assert response.suggestion.human_task == {
            "required": True,
            "task_id": None,
            "status": None,
            "persisted": False,
            "shadow_observation_only": True,
        }
        with service.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM handoff_tasks").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM channel_outbox").fetchone()[0] == 0
    finally:
        service.close()
