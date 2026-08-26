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


def _tool_result(
    output: dict,
    *,
    tool_name: str = "get_customer_sales_facts",
) -> dict:
    policy = build_customer_service_response_policy(
        tool_name, output
    )
    return {
        "tool_name": tool_name,
        "status": "success",
        "output": {**output, "response_policy": policy},
        "postcondition_met": True,
    }


def _after_sales_output() -> dict:
    return {
        "contract_version": "customer-service-facts-v1",
        "domain": "after_sales",
        "state": "available",
        "scope": {
            "tenant_id": "tenant-a",
            "store_id": "store-a",
            "order_id": "ORDER-1",
        },
        "facts": {
            "order": {
                "state": "available",
                "order_id": "ORDER-1",
                "order_status": "shipped",
                "payment_status": "paid",
            },
            "logistics": {
                "state": "available",
                "status": "in_transit",
                "last_event": "运输中，预计2026-08-25送达",
                "last_event_at": "2026-08-24T08:00:00+00:00",
            },
            "after_sales": [
                {
                    "case_type": "refund",
                    "status": "reviewing",
                    "requested_amount": "20.00",
                    "approved_amount": "0",
                }
            ],
        },
        "missing": [],
        "data_as_of": "2026-08-24T08:00:00+00:00",
        "freshness": {
            "status": "current",
            "usable_as_current": True,
            "reason_codes": [],
        },
        "source_provenance": {
            "source_type": "virtual",
            "virtual": True,
            "policy_version": "source-provenance-v1",
        },
        "evidence": [
            {
                "evidence_id": "cs-fact-order-1",
                "report_type": "order_snapshot",
                "data_as_of": "2026-08-24T08:00:00+00:00",
                "version": 1,
                "current": True,
            }
        ],
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
    non_numeric_inbound_leak = validate_customer_service_draft(
        "这款商品目前有货，另外还有在途库存。",
        result,
        question="这款商品有货吗",
    )
    warehouse_leak = validate_customer_service_draft(
        "这款商品目前有货，将从华东仓发出。",
        result,
        question="这款商品有货吗",
    )
    price_question_leak = validate_customer_service_draft(
        "这款商品目前可售库存为 5 件。",
        result,
        question="这个多少钱",
    )
    negated_quantity_leak = validate_customer_service_draft(
        "这款商品目前可售库存为 5 件。",
        result,
        question="我不是问库存数量，只想知道有没有货",
    )
    hypothetical_quantity_leak = validate_customer_service_draft(
        "这款商品目前可售库存为 5 件。",
        result,
        question="如果之后问还有多少件你会说吗，现在只说有没有货",
    )

    assert unasked_exact == (
        False,
        "customer_service_exact_inventory_not_requested",
    )
    assert asked_exact == (True, "customer_service_output_policy_passed")
    assert inbound_leak == (False, "customer_service_inbound_inventory_internal")
    assert non_numeric_inbound_leak == (
        False,
        "customer_service_inbound_inventory_internal",
    )
    assert warehouse_leak == (False, "customer_service_warehouse_detail_internal")
    assert price_question_leak == (
        False,
        "customer_service_exact_inventory_not_requested",
    )
    assert negated_quantity_leak == (
        False,
        "customer_service_exact_inventory_not_requested",
    )
    assert hypothetical_quantity_leak == (
        False,
        "customer_service_exact_inventory_not_requested",
    )


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


def test_vague_availability_question_does_not_authorize_exact_inventory() -> None:
    validated = validate_customer_service_draft(
        "这款商品还剩 5 件。",
        _tool_result(_sales_output()),
        question="这款商品还剩吗",
    )

    assert validated == (False, "customer_service_exact_inventory_not_requested")


def test_named_warehouse_is_internal_even_without_a_shipping_verb() -> None:
    validated = validate_customer_service_draft(
        "库存放在华东仓，目前有货。",
        _tool_result(_sales_output()),
    )

    assert validated == (False, "customer_service_warehouse_detail_internal")


@pytest.mark.parametrize(
    ("draft", "tool_result", "expected_reason"),
    [
        (
            "今天下单的话，明天一定发货",
            _tool_result(_sales_output()),
            "customer_service_unverified_delivery_commitment",
        ),
        (
            "已经帮您办理退款了",
            _tool_result(
                _after_sales_output(),
                tool_name="get_customer_after_sales_facts",
            ),
            "customer_service_unverified_business_action_claim",
        ),
        (
            "退款已经帮您办理完成",
            _tool_result(
                _after_sales_output(),
                tool_name="get_customer_after_sales_facts",
            ),
            "customer_service_unverified_business_action_claim",
        ),
        (
            "我们现已为您取消订单",
            _tool_result(
                _after_sales_output(),
                tool_name="get_customer_after_sales_facts",
            ),
            "customer_service_unverified_business_action_claim",
        ),
        (
            "已为您补发商品",
            _tool_result(
                _after_sales_output(),
                tool_name="get_customer_after_sales_facts",
            ),
            "customer_service_unverified_business_action_claim",
        ),
    ],
)
def test_response_policy_commitments_are_enforced_deterministically(
    draft: str,
    tool_result: dict,
    expected_reason: str,
) -> None:
    validated = validate_customer_service_draft(draft, tool_result)

    assert validated == (False, expected_reason)


@pytest.mark.parametrize(
    ("draft", "tool_result", "expected_reason"),
    [
        (
            "今天下单的话，明天一定发货",
            {},
            "customer_service_unverified_delivery_commitment",
        ),
        (
            "已经帮您办理退款了",
            {},
            "customer_service_unverified_business_action_claim",
        ),
        (
            "已经帮您办理退款了",
            {
                "tool_name": "refund_order",
                "tool_kind": "write",
                "status": "failed",
                "output": {"order_id": "ORDER-1", "status": "failed"},
                "postcondition_met": True,
            },
            "customer_service_unverified_business_action_claim",
        ),
        (
            "已经帮您办理退款了",
            {
                "tool_name": "update_order_address",
                "tool_kind": "write",
                "status": "success",
                "output": {"order_id": "ORDER-1", "status": "updated"},
                "postcondition_met": True,
            },
            "customer_service_unverified_business_action_claim",
        ),
    ],
)
def test_commitment_gate_applies_without_a_customer_fact_tool(
    draft: str,
    tool_result: dict,
    expected_reason: str,
) -> None:
    assert validate_customer_service_draft(draft, tool_result) == (
        False,
        expected_reason,
    )


@pytest.mark.parametrize(
    ("question", "draft"),
    [
        ("请帮我换货", "已经给你换新的了"),
        ("帮我改收货地址", "已经把地址改成新的了"),
        ("请帮我补发", "已经把货重新给你寄出"),
        ("帮我改手机号", "手机号已经替你换了"),
        ("帮我改发票抬头", "抬头已经更新了"),
        ("帮我拦截快递", "快递已经拦下来了"),
        ("帮我催发货", "给你催过了"),
        ("帮我加订单备注", "备注已经加上了"),
        ("帮我补发优惠券", "券已经发你账户了"),
        ("帮我延长收货时间", "收货时间已经给你延长了"),
        ("请帮我换货", "新货已经给你发出了"),
        ("请帮我补发", "已经把新货发给你了"),
        ("请帮我补发", "新的已经寄出了"),
        ("帮我改手机号", "号码已经换成新的了"),
        ("帮我改发票抬头", "抬头改成公司了"),
        ("帮我拦截快递", "快递已经给你截住了"),
        ("帮我补发优惠券", "优惠券已经到账了"),
        ("帮我删除订单", "订单已经替你删掉了"),
        ("帮我确认收货", "已经替你收货了"),
        ("帮我延长收货时间", "收货期限延到下周了"),
    ],
)
def test_draft_gate_blocks_natural_order_action_completion_without_receipt(
    question: str,
    draft: str,
) -> None:
    assert validate_customer_service_draft(draft, {}, question=question) == (
        False,
        "customer_service_unverified_business_action_claim",
    )


def test_matching_verified_write_receipt_passes_the_draft_gate() -> None:
    tool_result = {
        "tool_name": "refund_order",
        "tool_kind": "write",
        "status": "success",
        "output": {"order_id": "ORDER-1", "status": "refunded"},
        "postcondition_met": True,
    }

    assert validate_customer_service_draft(
        "已经帮您办理退款了",
        tool_result,
    ) == (True, "customer_service_output_policy_not_applicable")


def test_commitment_gate_preserves_supported_status_and_safe_uncertainty() -> None:
    after_sales = _tool_result(
        _after_sales_output(),
        tool_name="get_customer_after_sales_facts",
    )

    unknown_delivery = validate_customer_service_draft(
        "发货时间暂时无法确认，我会转人工核对。",
        _tool_result(_sales_output()),
    )
    refund_status = validate_customer_service_draft(
        "退款申请当前处于审核中。",
        after_sales,
    )
    verified_logistics = validate_customer_service_draft(
        "物流快照显示运输中，预计2026-08-25送达。",
        after_sales,
    )
    strengthened_logistics = validate_customer_service_draft(
        "保证2026-08-25送达。",
        after_sales,
    )

    assert unknown_delivery == (True, "customer_service_output_policy_passed")
    assert refund_status == (True, "customer_service_output_policy_passed")
    assert verified_logistics == (True, "customer_service_output_policy_passed")
    assert strengthened_logistics == (
        False,
        "customer_service_unverified_delivery_commitment",
    )


@pytest.mark.parametrize(
    "draft",
    [
        "这款商品会在48小时内发货。",
        "这款商品下单后两天内发货。",
        "我们会马上安排发出。",
        "最晚明天寄出。",
    ],
)
def test_delivery_commitment_variants_require_policy_or_verified_fact(draft: str) -> None:
    validated = validate_customer_service_draft(draft, _tool_result(_sales_output()))

    assert validated == (False, "customer_service_unverified_delivery_commitment")


@pytest.mark.parametrize(
    "draft",
    [
        "明天一定发货，但具体几点暂时无法确认。",
        "我们今晚给您发。",
        "预计明天能到。",
        "48小时内会揽件。",
    ],
)
def test_delivery_commitment_cannot_hide_behind_wording_variants(draft: str) -> None:
    validated = validate_customer_service_draft(draft, _tool_result(_sales_output()))

    assert validated == (False, "customer_service_unverified_delivery_commitment")


@pytest.mark.parametrize(
    "draft",
    [
        "今天是否发货无法确认，但明天会发货。",
        "无法确认今天发货，预计明天发货。",
    ],
)
def test_safe_delivery_clause_cannot_mask_a_later_unsupported_claim(
    draft: str,
) -> None:
    validated = validate_customer_service_draft(draft, _tool_result(_sales_output()))

    assert validated == (False, "customer_service_unverified_delivery_commitment")


def test_delivery_uncertainty_cannot_mask_a_conjoined_commitment() -> None:
    validated = validate_customer_service_draft(
        "无法确认具体时间同时明天发货。",
        _tool_result(_sales_output()),
    )

    assert validated == (False, "customer_service_unverified_delivery_commitment")


@pytest.mark.parametrize(
    "draft",
    [
        "不保证明天发货。",
        "不承诺明天发货。",
        "明天不一定发货。",
        "您问的是明天能不能发货吗？",
    ],
)
def test_nonassertive_delivery_wording_remains_safe(draft: str) -> None:
    validated = validate_customer_service_draft(draft, _tool_result(_sales_output()))

    assert validated == (True, "customer_service_output_policy_passed")


@pytest.mark.parametrize(
    "draft",
    [
        "一般48小时发货。",
        "两天后寄出。",
        "大概三天到货。",
        "1-2个工作日发货。",
        "3到5个工作日内发货。",
        "明早发货。",
        "8/26发货。",
    ],
)
def test_delivery_commitment_time_expressions_fail_closed(draft: str) -> None:
    validated = validate_customer_service_draft(draft, _tool_result(_sales_output()))

    assert validated == (False, "customer_service_unverified_delivery_commitment")


def test_approved_exact_policy_can_support_a_delivery_commitment() -> None:
    approved_answer = "本店订单会在48小时内发货。"

    validated = validate_customer_service_draft(
        approved_answer,
        _tool_result(_sales_output()),
        approved_answers=[approved_answer],
    )

    assert validated == (True, "customer_service_output_policy_passed")


def test_verified_delivery_fact_can_support_the_same_commitment() -> None:
    output = _after_sales_output()
    output["facts"]["logistics"]["last_event"] = "承诺明天发货"
    result = verify_response(
        {
            "draft": "承诺明天发货",
            "normalized_input": "什么时候发货",
            "retrieved": [],
            "customer_service_content": {},
            "context_bundle": {},
            "tool_result": _tool_result(
                output,
                tool_name="get_customer_after_sales_facts",
            ),
            "model_fallback": False,
            "model_retry_advised": False,
            "trace": [],
        }
    )

    assert result["review_route"] == "pass"
    assert result["answer"] == "承诺明天发货"


@pytest.mark.parametrize(
    ("verified_fact", "stronger_draft"),
    [
        ("预计明天送达", "明天送达"),
        ("大概三天到货", "三天到货"),
        ("最晚48小时内发货", "48小时内发货"),
        ("无法保证明天发货", "明天发货"),
    ],
)
def test_uncertain_delivery_fact_cannot_authorize_a_stronger_claim(
    verified_fact: str,
    stronger_draft: str,
) -> None:
    output = _after_sales_output()
    output["facts"]["logistics"]["last_event"] = verified_fact

    validated = validate_customer_service_draft(
        stronger_draft,
        _tool_result(
            output,
            tool_name="get_customer_after_sales_facts",
        ),
        question="什么时候能收到？",
    )

    assert validated == (
        False,
        "customer_service_unverified_delivery_commitment",
    )


def test_uncertain_delivery_fact_can_support_equally_cautious_wording() -> None:
    output = _after_sales_output()
    output["facts"]["logistics"]["last_event"] = "预计明天送达"

    validated = validate_customer_service_draft(
        "预计明天送达。",
        _tool_result(
            output,
            tool_name="get_customer_after_sales_facts",
        ),
        question="什么时候能收到？",
    )

    assert validated == (True, "customer_service_output_policy_passed")


@pytest.mark.parametrize(
    "draft",
    [
        "承诺明天发货，但后天一定送达。",
        "承诺明天发货后天送达。",
    ],
)
def test_verified_delivery_fact_cannot_authorize_an_unrelated_delivery_clause(
    draft: str,
) -> None:
    output = _after_sales_output()
    output["facts"]["logistics"]["last_event"] = "承诺明天发货"

    validated = validate_customer_service_draft(
        draft,
        _tool_result(
            output,
            tool_name="get_customer_after_sales_facts",
        ),
        question="什么时候能收到？",
    )

    assert validated == (
        False,
        "customer_service_unverified_delivery_commitment",
    )


def test_delivery_time_claim_without_verified_fact_or_policy_is_blocked() -> None:
    result = verify_response(
        {
            "draft": "明天发货。",
            "normalized_input": "什么时候发货",
            "retrieved": [],
            "customer_service_content": {},
            "context_bundle": {},
            "tool_result": {},
            "model_fallback": False,
            "model_retry_advised": False,
            "trace": [],
        }
    )

    assert result["review_route"] == "handoff"
    assert result["route_reason"] == "customer_service_unverified_delivery_commitment"


@pytest.mark.parametrize("draft", ["明天。", "预计后天。", "最晚48小时内。"])
def test_bare_delivery_time_answer_requires_verified_support(draft: str) -> None:
    validated = validate_customer_service_draft(
        draft,
        _tool_result(_sales_output()),
        question="什么时候发货？",
    )

    assert validated == (
        False,
        "customer_service_unverified_delivery_commitment",
    )


@pytest.mark.parametrize(
    ("draft", "tool_result"),
    [
        ("订单已经发货。", {}),
        ("订单已付款。", {}),
        ("物流显示已经签收。", {}),
        ("退款状态显示已完成。", {}),
        ("这款商品目前有货。", {}),
        ("商品当前在售。", {}),
        ("订单已经发货。", _tool_result(_sales_output())),
        (
            "这款商品目前有货。",
            _tool_result(
                _after_sales_output(),
                tool_name="get_customer_after_sales_facts",
            ),
        ),
    ],
)
def test_operational_status_claim_requires_matching_verified_fact_domain(
    draft: str,
    tool_result: dict,
) -> None:
    validated = validate_customer_service_draft(draft, tool_result)

    assert validated == (
        False,
        "customer_service_unverified_operational_status_claim",
    )


@pytest.mark.parametrize(
    "draft",
    [
        "暂时无法确认订单是否已经发货，需要人工核对。",
        "目前无法确认是否有货，我会转人工核对。",
        "退款状态暂时无法核实，请以人工确认结果为准。",
    ],
)
def test_unverified_operational_status_uncertainty_remains_safe(draft: str) -> None:
    validated = validate_customer_service_draft(draft, {})

    assert validated == (True, "customer_service_output_policy_not_applicable")


def test_safe_status_clause_cannot_mask_a_later_unverified_status_claim() -> None:
    validated = validate_customer_service_draft(
        "退款状态无法确认，但订单已经发货。",
        {},
    )

    assert validated == (
        False,
        "customer_service_unverified_operational_status_claim",
    )


def test_status_uncertainty_cannot_mask_a_conjoined_status_claim() -> None:
    validated = validate_customer_service_draft(
        "无法确认订单状态同时退款已经完成。",
        {},
    )

    assert validated == (
        False,
        "customer_service_unverified_operational_status_claim",
    )


@pytest.mark.parametrize(
    "draft",
    [
        "如果订单已经发货，可以在页面查看物流。",
        "订单已经发货了吗？",
    ],
)
def test_hypothetical_or_interrogative_status_wording_is_not_a_fact_claim(
    draft: str,
) -> None:
    assert validate_customer_service_draft(draft, {}) == (
        True,
        "customer_service_output_policy_not_applicable",
    )


@pytest.mark.parametrize(
    "draft",
    [
        "退款申请已受理。",
        "款项已经原路退回。",
        "已原路退回。",
        "退款已原路退还。",
        "订单已经撤销。",
        "订单已经作废。",
        "包裹已经揽件。",
    ],
)
def test_additional_operational_status_variants_require_verified_facts(
    draft: str,
) -> None:
    assert validate_customer_service_draft(draft, {}) == (
        False,
        "customer_service_unverified_operational_status_claim",
    )


def test_additional_operational_status_variants_accept_matching_verified_facts() -> None:
    output = _after_sales_output()
    output["facts"]["order"]["order_status"] = "canceled"
    output["facts"]["logistics"]["status"] = "in_transit"
    output["facts"]["after_sales"][0]["status"] = "reviewing"
    tool_result = _tool_result(
        output,
        tool_name="get_customer_after_sales_facts",
    )

    for draft in (
        "退款申请已受理。",
        "订单已经撤销。",
        "订单已经作废。",
        "包裹已经揽件。",
    ):
        assert validate_customer_service_draft(draft, tool_result) == (
            True,
            "customer_service_output_policy_passed",
        )

    output["facts"]["after_sales"][0]["status"] = "completed"
    completed_result = _tool_result(
        output,
        tool_name="get_customer_after_sales_facts",
    )
    assert validate_customer_service_draft(
        "款项已经原路退回。",
        completed_result,
    ) == (True, "customer_service_output_policy_passed")
    assert validate_customer_service_draft(
        "已原路退回。",
        completed_result,
        question="退款现在什么状态？",
    ) == (True, "customer_service_output_policy_passed")
    assert validate_customer_service_draft(
        "退款已原路退还。",
        completed_result,
    ) == (True, "customer_service_output_policy_passed")


def test_noncurrent_fact_without_timestamp_still_cannot_make_current_claim() -> None:
    output = _sales_output(freshness="stale", usable=False)
    output["data_as_of"] = None

    validated = validate_customer_service_draft(
        "目前有货，可以正常下单。",
        _tool_result(output),
    )

    assert validated == (False, "customer_service_stale_current_claim")


def test_noncurrent_fact_without_timestamp_is_not_usable_for_an_implicit_current_claim() -> None:
    output = _sales_output(freshness="stale", usable=False)
    output["data_as_of"] = None

    validated = validate_customer_service_draft(
        "这款商品有货，可以正常选购。",
        _tool_result(output),
    )

    assert validated == (False, "customer_service_data_as_of_required")


def test_unverified_fact_tool_result_cannot_ground_a_customer_answer() -> None:
    tool_result = _tool_result(_sales_output())
    tool_result.update(status="failed", postcondition_met=False)

    validated = validate_customer_service_draft("这款商品目前有货。", tool_result)

    assert validated == (False, "customer_service_fact_not_verified")


def test_product_status_claim_must_match_the_verified_sales_fact() -> None:
    output = _sales_output()
    output["facts"]["product"]["status"] = "active"
    correct = validate_customer_service_draft(
        "这款商品当前在售。",
        _tool_result(output),
    )
    wrong = validate_customer_service_draft(
        "这款商品已经下架。",
        _tool_result(output),
    )

    assert correct == (True, "customer_service_output_policy_passed")
    assert wrong == (False, "customer_service_product_status_mismatch")


def test_missing_after_sales_subfacts_cannot_be_fabricated() -> None:
    output = _after_sales_output()
    output["state"] = "partial"
    output["facts"]["logistics"] = {
        "state": "missing",
        "status": None,
        "last_event": None,
        "last_event_at": None,
    }
    output["facts"]["after_sales"] = []
    output["missing"] = ["fulfillment_snapshot_missing"]
    tool_result = _tool_result(
        output,
        tool_name="get_customer_after_sales_facts",
    )

    logistics = validate_customer_service_draft("物流目前正在运输中。", tool_result)
    refund = validate_customer_service_draft("退款状态显示已完成。", tool_result)

    assert logistics == (False, "customer_service_missing_logistics_fabricated")
    assert refund == (False, "customer_service_missing_after_sales_fabricated")


def test_missing_order_fact_cannot_be_fabricated() -> None:
    output = _after_sales_output()
    output["state"] = "partial"
    output["facts"]["order"] = {
        "state": "missing",
        "order_id": "ORDER-1",
        "order_status": None,
        "payment_status": None,
    }
    output["missing"] = ["order_snapshot_missing"]
    tool_result = _tool_result(output, tool_name="get_customer_after_sales_facts")

    validated = validate_customer_service_draft("订单当前已发货。", tool_result)

    assert validated == (False, "customer_service_missing_order_fabricated")


def test_after_sales_status_claims_must_match_verified_fact_values() -> None:
    tool_result = _tool_result(
        _after_sales_output(),
        tool_name="get_customer_after_sales_facts",
    )

    correct_refund = validate_customer_service_draft(
        "退款申请当前处于审核中。",
        tool_result,
    )
    wrong_refund = validate_customer_service_draft(
        "退款状态显示已完成。",
        tool_result,
    )
    correct_logistics = validate_customer_service_draft(
        "物流快照显示运输中。",
        tool_result,
    )
    wrong_logistics = validate_customer_service_draft(
        "物流快照显示已经签收。",
        tool_result,
    )
    correct_order = validate_customer_service_draft("订单当前已发货。", tool_result)
    wrong_order = validate_customer_service_draft("订单当前已取消。", tool_result)
    correct_payment = validate_customer_service_draft("订单款项已支付。", tool_result)
    wrong_payment = validate_customer_service_draft("订单款项尚未支付。", tool_result)

    assert correct_refund == (True, "customer_service_output_policy_passed")
    assert wrong_refund == (False, "customer_service_after_sales_status_mismatch")
    assert correct_logistics == (True, "customer_service_output_policy_passed")
    assert wrong_logistics == (False, "customer_service_logistics_status_mismatch")
    assert correct_order == (True, "customer_service_output_policy_passed")
    assert wrong_order == (False, "customer_service_order_status_mismatch")
    assert correct_payment == (True, "customer_service_output_policy_passed")
    assert wrong_payment == (False, "customer_service_payment_status_mismatch")


def test_verified_refund_application_status_is_not_misread_as_agent_action() -> None:
    tool_result = _tool_result(
        _after_sales_output(),
        tool_name="get_customer_after_sales_facts",
    )

    validated = validate_customer_service_draft(
        "退款申请已经提交，当前等待审核。",
        tool_result,
    )

    assert validated == (True, "customer_service_output_policy_passed")


def test_verified_refund_status_remains_reportable_after_an_action_request() -> None:
    tool_result = _tool_result(
        _after_sales_output(),
        tool_name="get_customer_after_sales_facts",
    )

    validated = validate_customer_service_draft(
        "退款申请已经提交，当前等待审核。",
        tool_result,
        question="请帮我办理退款。",
    )

    assert validated == (True, "customer_service_output_policy_passed")


def test_refund_application_status_without_facts_is_blocked() -> None:
    validated = validate_customer_service_draft(
        "退款申请已经提交，当前等待审核。",
        {},
    )

    assert validated == (
        False,
        "customer_service_unverified_operational_status_claim",
    )


@pytest.mark.parametrize(
    "draft",
    [
        "款项已到账。",
        "退款已原路返还。",
        "退款已退到您的账户。",
        "退款已经返到余额。",
        "这笔退款已经退入账户。",
    ],
)
def test_refund_transfer_status_without_facts_is_blocked(draft: str) -> None:
    assert validate_customer_service_draft(
        draft,
        {},
        question="退款到账了吗？",
    ) == (
        False,
        "customer_service_unverified_operational_status_claim",
    )


@pytest.mark.parametrize(
    "draft",
    [
        "款项已到账。",
        "退款已原路返还。",
        "退款已退到您的账户。",
        "退款已经返到余额。",
        "这笔退款已经退入账户。",
    ],
)
def test_refund_transfer_status_must_match_verified_facts(draft: str) -> None:
    reviewing = _tool_result(
        _after_sales_output(),
        tool_name="get_customer_after_sales_facts",
    )
    completed_output = _after_sales_output()
    completed_output["facts"]["after_sales"][0]["status"] = "completed"
    completed = _tool_result(
        completed_output,
        tool_name="get_customer_after_sales_facts",
    )

    assert validate_customer_service_draft(
        draft,
        reviewing,
        question="退款到账了吗？",
    ) == (
        False,
        "customer_service_after_sales_status_mismatch",
    )
    assert validate_customer_service_draft(
        draft,
        completed,
        question="退款到账了吗？",
    ) == (True, "customer_service_output_policy_passed")


def test_refund_status_lookup_is_not_misread_as_a_refund_action() -> None:
    output = _after_sales_output()
    output["facts"]["after_sales"][0]["status"] = "completed"

    validated = validate_customer_service_draft(
        "退款已完成。",
        _tool_result(
            output,
            tool_name="get_customer_after_sales_facts",
        ),
        question="请帮我查询退款状态。",
    )

    assert validated == (True, "customer_service_output_policy_passed")


@pytest.mark.parametrize(
    "draft",
    ["好了。", "成功了。", "退了。", "已退款。", "取消了。", "已取消。"],
)
def test_bare_action_result_requires_a_matching_write_receipt(draft: str) -> None:
    assert validate_customer_service_draft(
        draft,
        {},
        question="请帮我办理退款。",
    ) == (
        False,
        "customer_service_unverified_business_action_claim",
    )


@pytest.mark.parametrize(
    ("draft", "expected"),
    [
        (
            "已经发货。",
            (True, "customer_service_output_policy_passed"),
        ),
        (
            "已经签收。",
            (False, "customer_service_fulfillment_status_mismatch"),
        ),
    ],
)
def test_unscoped_fulfillment_status_must_match_verified_facts(
    draft: str,
    expected: tuple[bool, str],
) -> None:
    tool_result = _tool_result(
        _after_sales_output(),
        tool_name="get_customer_after_sales_facts",
    )

    assert validate_customer_service_draft(draft, tool_result) == expected


@pytest.mark.parametrize(
    "draft",
    [
        "订单状态无法确认，但已经取消。",
        "退款状态无法确认，但已经到账。",
    ],
)
def test_subject_carryover_cannot_hide_a_later_operational_status_claim(
    draft: str,
) -> None:
    assert validate_customer_service_draft(draft, {}) == (
        False,
        "customer_service_unverified_operational_status_claim",
    )


@pytest.mark.parametrize(
    ("draft", "question"),
    [
        ("已付款。", "订单现在什么状态？"),
        ("已支付。", "订单现在什么状态？"),
        ("已经关了。", "订单现在什么状态？"),
        ("完成了。", "订单现在什么状态？"),
        ("通过了。", "退款现在什么状态？"),
        ("退回去了。", "退款现在什么状态？"),
        ("已经揽件。", "物流现在什么状态？"),
        ("到了。", "物流现在什么状态？"),
        ("正在派送。", "物流现在什么状态？"),
    ],
)
def test_question_subject_cannot_hide_an_elliptical_operational_status_claim(
    draft: str,
    question: str,
) -> None:
    assert validate_customer_service_draft(draft, {}, question=question) == (
        False,
        "customer_service_unverified_operational_status_claim",
    )


def test_delivery_time_ellipsis_uses_the_customer_question_context() -> None:
    validated = validate_customer_service_draft(
        "明天会发。",
        _tool_result(_sales_output()),
        question="今天下单什么时候发货？",
    )

    assert validated == (
        False,
        "customer_service_unverified_delivery_commitment",
    )


@pytest.mark.parametrize(
    "draft",
    [
        "支持当日达。",
        "支持当日出货。",
        "支持次日达。",
        "今天拍，明天收货。",
        "支持次日收货。",
        "今晚下单后天收。",
        "今晚下单明早到。",
        "最快后天到手。",
    ],
)
def test_compact_arrival_promise_requires_verified_support(draft: str) -> None:
    assert validate_customer_service_draft(
        draft,
        _tool_result(_sales_output()),
        question="多久能到货？",
    ) == (
        False,
        "customer_service_unverified_delivery_commitment",
    )


def test_generic_completion_uses_the_customer_action_request_context() -> None:
    unverified = validate_customer_service_draft(
        "已经为您处理好了。",
        {},
        question="请帮我办理退款。",
    )
    verified = validate_customer_service_draft(
        "已经为您处理好了。",
        {
            "tool_name": "refund_order",
            "tool_kind": "write",
            "status": "success",
            "output": {"order_id": "ORDER-1", "status": "refunded"},
            "postcondition_met": True,
        },
        question="请帮我办理退款。",
    )

    assert unverified == (
        False,
        "customer_service_unverified_business_action_claim",
    )
    assert verified == (True, "customer_service_output_policy_not_applicable")


def test_short_generic_completion_uses_the_customer_action_request_context() -> None:
    assert validate_customer_service_draft(
        "已经处理好了。",
        {},
        question="请帮我办理退款。",
    ) == (
        False,
        "customer_service_unverified_business_action_claim",
    )


@pytest.mark.parametrize(
    "draft",
    [
        "已经原路返回了。",
        "这笔钱已经返还了。",
        "已退至原支付账户。",
        "已退到原支付方式。",
        "已退到您的账户。",
        "退款已经返到余额。",
        "这笔退款已经退入账户。",
    ],
)
def test_refund_return_synonyms_require_a_verified_write_receipt(
    draft: str,
) -> None:
    unverified = validate_customer_service_draft(
        draft,
        {},
        question="请帮我办理退款。",
    )
    verified = validate_customer_service_draft(
        draft,
        {
            "tool_name": "refund_order",
            "tool_kind": "write",
            "status": "success",
            "output": {"order_id": "ORDER-1", "status": "refunded"},
            "postcondition_met": True,
        },
        question="请帮我办理退款。",
    )

    assert unverified == (
        False,
        "customer_service_unverified_business_action_claim",
    )
    assert verified == (True, "customer_service_output_policy_not_applicable")


def test_matching_write_receipt_does_not_authorize_an_unrelated_status_clause() -> None:
    validated = validate_customer_service_draft(
        "已经原路返回了，并且订单已经取消。",
        {
            "tool_name": "refund_order",
            "tool_kind": "write",
            "status": "success",
            "output": {"order_id": "ORDER-1", "status": "refunded"},
            "postcondition_met": True,
        },
        question="请帮我办理退款。",
    )

    assert validated[0] is False


@pytest.mark.parametrize(
    ("draft", "question", "tool_name"),
    [
        ("已经给您撤了单。", "请取消订单。", "cancel_order"),
        ("订单给您撤了。", "请取消订单。", "cancel_order"),
        ("订单已经作废。", "请取消订单。", "cancel_order"),
        ("地址给您换了。", "请修改地址。", "update_order_address"),
    ],
)
def test_additional_colloquial_order_actions_require_verified_write_receipts(
    draft: str,
    question: str,
    tool_name: str,
) -> None:
    unverified = validate_customer_service_draft(
        draft,
        {},
        question=question,
    )
    verified = validate_customer_service_draft(
        draft,
        {
            "tool_name": tool_name,
            "tool_kind": "write",
            "status": "success",
            "output": {"order_id": "ORDER-1", "status": "updated"},
            "postcondition_met": True,
        },
        question=question,
    )

    assert unverified == (
        False,
        "customer_service_unverified_business_action_claim",
    )
    assert verified == (True, "customer_service_output_policy_not_applicable")


@pytest.mark.parametrize(
    ("question", "draft", "expected"),
    [
        (
            "订单现在是什么状态？",
            "已经取消。",
            (False, "customer_service_order_status_mismatch"),
        ),
        (
            "退款现在是什么状态？",
            "正在审核。",
            (True, "customer_service_output_policy_passed"),
        ),
    ],
)
def test_customer_question_supplies_omitted_operational_subject(
    question: str,
    draft: str,
    expected: tuple[bool, str],
) -> None:
    tool_result = _tool_result(
        _after_sales_output(),
        tool_name="get_customer_after_sales_facts",
    )

    assert validate_customer_service_draft(
        draft,
        tool_result,
        question=question,
    ) == expected


@pytest.mark.parametrize(
    ("draft", "expected_reason"),
    [
        ("订单取消了。", "customer_service_order_status_mismatch"),
        ("订单完成了。", "customer_service_order_status_mismatch"),
        ("快递签收了。", "customer_service_logistics_status_mismatch"),
        ("包裹已经送到了。", "customer_service_logistics_status_mismatch"),
        ("退款审核通过了。", "customer_service_after_sales_status_mismatch"),
        ("退款申请被拒了。", "customer_service_after_sales_status_mismatch"),
        ("订单还没付款。", "customer_service_payment_status_mismatch"),
    ],
)
def test_colloquial_status_claims_still_require_matching_fact(
    draft: str,
    expected_reason: str,
) -> None:
    tool_result = _tool_result(
        _after_sales_output(),
        tool_name="get_customer_after_sales_facts",
    )

    assert validate_customer_service_draft(draft, tool_result) == (
        False,
        expected_reason,
    )


@pytest.mark.parametrize(
    "draft",
    [
        "订单发货了。",
        "订单付款成功。",
        "快递正在运输。",
        "退款还在审核。",
    ],
)
def test_colloquial_status_claims_allow_matching_verified_fact(draft: str) -> None:
    tool_result = _tool_result(
        _after_sales_output(),
        tool_name="get_customer_after_sales_facts",
    )

    assert validate_customer_service_draft(draft, tool_result) == (
        True,
        "customer_service_output_policy_passed",
    )


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


def test_stream_never_emits_an_unverified_delivery_commitment(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        bootstrap_client_can_supply_order_context=True,
    )
    service = AgentService(settings)
    try:
        _seed_sales_facts(service)
        service.knowledge.retrieve = lambda *_args, **_kwargs: []  # type: ignore[method-assign]
        _install_fact_model(
            service,
            tool_name="get_customer_sales_facts",
            intent="product_inquiry",
            arguments_from_payload=lambda payload: {
                "store_id": payload["trusted_context"]["store_id"],
                "sku_id": payload["trusted_context"]["sku_id"],
            },
            answer="今天下单的话，明天一定发货",
        )
        service.model.stream_generate = lambda _messages: iter(  # type: ignore[method-assign]
            ("今天下单的话，", "明天一定发货")
        )

        events = list(
            service.chat_stream(
                principal_for(service),
                "wp3-delivery-commitment-stream",
                "今天下单什么时候发货",
                {"shop_id": "store-a", "sku_id": "SKU-1"},
                idempotency_key=None,
            )
        )

        streamed_answer = "".join(
            event["text"] for event in events if event["event"] == "delta"
        )
        response = events[-1]["response"]
        assert "明天一定发货" not in streamed_answer
        assert streamed_answer == response["answer"]
        assert response["requires_human"] is True
        assert response["reason"] == "customer_service_unverified_delivery_commitment"
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


@pytest.mark.parametrize(
    ("draft", "tool_result", "expected_reason"),
    [
        (
            "今天下单的话，明天一定发货",
            _tool_result(_sales_output()),
            "customer_service_unverified_delivery_commitment",
        ),
        (
            "已经帮您办理退款了",
            _tool_result(
                _after_sales_output(),
                tool_name="get_customer_after_sales_facts",
            ),
            "customer_service_unverified_business_action_claim",
        ),
    ],
)
def test_unverified_commitment_drafts_degrade_to_handoff(
    draft: str,
    tool_result: dict,
    expected_reason: str,
) -> None:
    result = verify_response(
        {
            "draft": draft,
            "normalized_input": "请核对这个问题",
            "retrieved": [],
            "customer_service_content": {},
            "context_bundle": {},
            "tool_result": tool_result,
            "model_fallback": False,
            "model_retry_advised": False,
            "trace": [],
        }
    )

    assert result["review_route"] == "handoff"
    assert result["route_reason"] == expected_reason


def test_exact_approved_delivery_policy_remains_answerable() -> None:
    approved_answer = "本店订单会在48小时内发货。"
    result = verify_response(
        {
            "draft": approved_answer,
            "normalized_input": "这款商品多久发货",
            "retrieved": [
                {
                    "id": "kb-approved-delivery",
                    "answer": approved_answer,
                }
            ],
            "customer_service_content": {
                "scripts": [{"id": "kb-approved-delivery"}],
            },
            "context_bundle": {},
            "tool_result": _tool_result(_sales_output()),
            "model_fallback": False,
            "model_retry_advised": False,
            "trace": [],
        }
    )

    assert result["review_route"] == "pass"
    assert result["answer"] == approved_answer


def test_verified_write_receipt_can_support_a_business_action_claim() -> None:
    result = verify_response(
        {
            "draft": "已经帮您办理退款了",
            "normalized_input": "请帮我退款",
            "retrieved": [],
            "customer_service_content": {},
            "context_bundle": {},
            "tool_result": {
                "tool_name": "refund_order",
                "tool_kind": "write",
                "status": "success",
                "output": {"order_id": "ORDER-1", "status": "refunded"},
                "postcondition_met": True,
            },
            "model_fallback": False,
            "model_retry_advised": False,
            "trace": [],
        }
    )

    assert result["review_route"] == "pass"
    assert result["answer"] == "已经帮您办理退款了"


def test_verified_write_receipt_supports_generic_completion_without_becoming_status() -> None:
    result = verify_response(
        {
            "draft": "操作已完成，业务系统已经确认处理结果。",
            "normalized_input": "请取消订单 order-1001",
            "retrieved": [],
            "customer_service_content": {},
            "context_bundle": {},
            "tool_result": {
                "tool_name": "cancel_order",
                "tool_kind": "write",
                "status": "success",
                "output": {"order_id": "order-1001", "status": "canceled"},
                "postcondition_met": True,
            },
            "model_fallback": True,
            "model_retry_advised": False,
            "trace": [],
        }
    )

    assert result["review_route"] == "pass"
    assert result["answer"] == "操作已完成，业务系统已经确认处理结果。"


def test_unrelated_verified_write_receipt_cannot_support_a_refund_claim() -> None:
    result = verify_response(
        {
            "draft": "已经帮您办理退款了",
            "normalized_input": "请修改订单地址",
            "retrieved": [],
            "customer_service_content": {},
            "context_bundle": {},
            "tool_result": {
                "tool_name": "update_order_address",
                "tool_kind": "write",
                "status": "success",
                "output": {"order_id": "ORDER-1", "status": "updated"},
                "postcondition_met": True,
            },
            "model_fallback": False,
            "model_retry_advised": False,
            "trace": [],
        }
    )

    assert result["review_route"] == "handoff"
    assert result["route_reason"] == "customer_service_unverified_business_action_claim"


def test_failed_write_receipt_cannot_support_a_business_action_claim() -> None:
    result = verify_response(
        {
            "draft": "已经帮您办理退款了",
            "normalized_input": "请帮我退款",
            "retrieved": [],
            "customer_service_content": {},
            "context_bundle": {},
            "tool_result": {
                "tool_name": "refund_order",
                "tool_kind": "write",
                "status": "failed",
                "output": {"order_id": "ORDER-1", "status": "failed"},
                "postcondition_met": True,
            },
            "model_fallback": False,
            "model_retry_advised": False,
            "trace": [],
        }
    )

    assert result["review_route"] == "handoff"
    assert result["route_reason"] == "customer_service_unverified_business_action_claim"


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
