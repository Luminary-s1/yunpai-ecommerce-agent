from __future__ import annotations

import argparse
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from ecommerce_agent.customer_service_loop import (
    build_customer_service_response_policy,
    validate_customer_service_draft,
)
from ecommerce_agent.graph import verify_response
from ecommerce_agent.policy import review_output
from ecommerce_agent.service import AgentService


def _load_wp3_helpers() -> ModuleType:
    helper_path = Path(__file__).with_name("WP3_人工验收场景.py")
    spec = importlib.util.spec_from_file_location("m8r_wp3_manual_helpers", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("WP3 manual acceptance helpers could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verified_delivery_tool_result() -> dict[str, Any]:
    output = {
        "contract_version": "customer-service-facts-v1",
        "domain": "after_sales",
        "state": "available",
        "scope": {
            "tenant_id": "tenant-test",
            "store_id": "store-a",
            "order_id": "ORDER-1",
        },
        "facts": {
            "order": {"state": "available"},
            "logistics": {
                "state": "available",
                "status": "pending_shipment",
                "last_event": "承诺明天发货",
            },
            "after_sales": [],
        },
        "missing": [],
        "data_as_of": "2026-08-25T08:00:00+00:00",
        "freshness": {"status": "current", "usable_as_current": True},
        "source_provenance": {"source_type": "virtual", "virtual": True},
        "evidence": [{"evidence_id": "cs-fact-verified-delivery"}],
    }
    return {
        "tool_name": "get_customer_after_sales_facts",
        "tool_kind": "read",
        "status": "success",
        "output": {
            **output,
            "response_policy": build_customer_service_response_policy(
                "get_customer_after_sales_facts",
                output,
            ),
        },
        "postcondition_met": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--tester", default="谢良璇")
    parser.add_argument("--auto-confirm", action="store_true")
    args = parser.parse_args()

    wp3 = _load_wp3_helpers()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    service = AgentService(wp3._settings(args.data_dir))
    service.knowledge.retrieve = lambda *_args, **_kwargs: []  # type: ignore[method-assign]
    principal = wp3._principal(service)
    observations: list[dict[str, Any]] = []
    automatic_checks = "passed"
    now = datetime.now(UTC)

    try:
        wp3._catalog(
            service,
            sku_id="SKU-1",
            title="恒温水壶",
            source_time=now,
        )
        wp3._inventory(service, sku_id="SKU-1", source_time=now)
        wp3._order(service, now)

        wp3._install_model(
            service,
            tool_name="get_customer_sales_facts",
            intent="product_inquiry",
            arguments=wp3._sales_arguments,
            answer="今天下单的话，明天一定发货",
        )
        nonstream = service.chat(
            principal,
            "pr25-p1-delivery-nonstream",
            "今天下单什么时候发货",
            {"shop_id": "store-a", "sku_id": "SKU-1"},
        )
        wp3._install_model(
            service,
            tool_name="get_customer_sales_facts",
            intent="product_inquiry",
            arguments=wp3._sales_arguments,
            answer="今天下单的话，明天一定发货",
        )
        service.model.stream_generate = lambda _messages: iter(  # type: ignore[method-assign]
            ("今天下单的话，", "明天一定发货")
        )
        stream_events = list(
            service.chat_stream(
                principal,
                "pr25-p1-delivery-stream",
                "今天下单什么时候发货",
                {"shop_id": "store-a", "sku_id": "SKU-1"},
                idempotency_key=None,
            )
        )
        streamed_answer = "".join(
            event["text"] for event in stream_events if event["event"] == "delta"
        )
        stream_response = stream_events[-1]["response"]
        assert nonstream.requires_human
        assert nonstream.reason == "customer_service_unverified_delivery_commitment"
        assert stream_response["requires_human"] is True
        assert stream_response["reason"] == (
            "customer_service_unverified_delivery_commitment"
        )
        assert "明天一定发货" not in nonstream.answer
        assert "明天一定发货" not in streamed_answer
        delivery_variants = {
            draft: review_output(draft, "", question="什么时候发货？")
            for draft in (
                "一般48小时发货。",
                "两天后寄出。",
                "大概三天到货。",
                "1-2个工作日发货。",
                "明早发货。",
                "8/26发货。",
                "明天安排。",
                "明早给您安排。",
                "无法确认具体时间同时明天发货。",
                "明天。",
                "预计后天。",
                "最晚48小时内。",
                "明天肯定可以。",
                "明天没问题。",
                "明天就可以了。",
                "肯定是明天。",
                "发货时间定在明天。",
                "我看就是明天。",
            )
        }
        no_fact_delivery = verify_response(
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
        verified_fact_delivery = verify_response(
            {
                "draft": "承诺明天发货",
                "normalized_input": "什么时候发货",
                "retrieved": [],
                "customer_service_content": {},
                "context_bundle": {},
                "tool_result": _verified_delivery_tool_result(),
                "model_fallback": False,
                "model_retry_advised": False,
                "trace": [],
            }
        )
        tentative_fact_result = _verified_delivery_tool_result()
        tentative_fact_result["output"]["facts"]["logistics"]["last_event"] = (
            "预计明天送达"
        )
        strengthened_tentative_fact = validate_customer_service_draft(
            "明天送达。",
            tentative_fact_result,
            question="什么时候能收到？",
        )
        equally_cautious_fact = validate_customer_service_draft(
            "预计明天送达。",
            tentative_fact_result,
            question="什么时候能收到？",
        )
        safe_uncertainty = review_output("明天能否发货需要人工核对。", "")
        unsupported_extra_delivery = {
            draft: validate_customer_service_draft(
                draft,
                _verified_delivery_tool_result(),
                question="什么时候能收到？",
            )
            for draft in (
                "承诺明天发货，但后天一定送达。",
                "承诺明天发货后天送达。",
            )
        }
        assert all(
            result == (False, "forbidden_commitment_in_output")
            for result in delivery_variants.values()
        )
        assert no_fact_delivery["review_route"] == "handoff"
        assert no_fact_delivery["route_reason"] == (
            "customer_service_unverified_delivery_commitment"
        )
        assert verified_fact_delivery["review_route"] == "pass"
        assert strengthened_tentative_fact == (
            False,
            "customer_service_unverified_delivery_commitment",
        )
        assert equally_cautious_fact == (
            True,
            "customer_service_output_policy_passed",
        )
        assert safe_uncertainty == (True, "output_policy_passed")
        assert all(
            result
            == (False, "customer_service_unverified_delivery_commitment")
            for result in unsupported_extra_delivery.values()
        )
        wp3._show(
            "第 1 步：未经证实的发货承诺",
            "具体时效必须来自批准政策或已验证事实；无事实和口语变体都要拦截，安全核对文案不能被误伤。",
            {
                "nonstream": nonstream.model_dump(),
                "stream_response": stream_response,
                "streamed_answer": streamed_answer,
                "delivery_variants": delivery_variants,
                "no_fact_delivery": no_fact_delivery,
                "verified_fact_delivery": verified_fact_delivery,
                "strengthened_tentative_fact": strengthened_tentative_fact,
                "equally_cautious_fact": equally_cautious_fact,
                "safe_uncertainty": safe_uncertainty,
                "unsupported_extra_delivery": unsupported_extra_delivery,
            },
        )
        wp3._confirm(
            observations,
            item_id="delivery-commitment",
            expected=(
                "流式和非流式都转人工，原因是 unverified_delivery_commitment；"
                "48小时、两天后、明早和纯时间短答等变体也被拦截；"
                "一条物流事实不能授权另一句承诺，预计事实也不能升级成确定答复；"
                "同等谨慎的事实复述、已验证事实正例和人工核对文案正常放行。"
            ),
            auto_confirm=args.auto_confirm,
        )

        wp3._install_model(
            service,
            tool_name="get_customer_after_sales_facts",
            intent="after_sales",
            arguments=wp3._after_sales_arguments,
            answer="已经帮您办理退款了",
        )
        refund_action = service.chat(
            principal,
            "pr25-p1-refund-action",
            "帮我看看退款进度",
            {"shop_id": "store-a", "order_id": "ORDER-1"},
        )
        assert refund_action.requires_human
        assert refund_action.reason == (
            "customer_service_unverified_business_action_claim"
        )
        assert "已经帮您办理退款了" not in refund_action.answer
        colloquial_action_guards = {
            draft: review_output(draft, "")
            for draft in (
                "您的退款我这边处理好了。",
                "退款给你办妥了。",
                "订单给您取消好了。",
                "已经替你取消订单了。",
                "我把退款提交了。",
                "我把订单取消了。",
                "订单地址已修改。",
            )
        }
        contextual_action_guards = {
            f"{question} -> {answer}": review_output(
                answer,
                "",
                question=question,
            )
            for question, answer in (
                ("请帮我退款。", "钱退你了。"),
                ("请帮我退款。", "已经退掉了。"),
                ("请取消订单。", "已经撤掉了。"),
                ("请修改地址。", "已经改过来了。"),
                ("请补发商品。", "已重新寄了。"),
                ("请帮我催发货。", "已经加急了。"),
            )
        }
        natural_order_action_guards = {
            f"{question} -> {answer}": review_output(
                answer,
                "",
                question=question,
            )
            for question, answer in (
                ("请帮我换货。", "已经给你换新的了。"),
                ("请帮我补发。", "已经把新货发给你了。"),
                ("请帮我修改手机号。", "号码已经换成新的了。"),
                ("请帮我修改发票抬头。", "抬头改成公司了。"),
                ("请帮我拦截快递。", "快递已经给你截住了。"),
                ("请帮我添加订单备注。", "备注已经加上了。"),
                ("请帮我补发优惠券。", "优惠券已经到账了。"),
                ("请帮我删除订单。", "订单已经替你删掉了。"),
                ("请帮我确认收货。", "已经替你收货了。"),
                ("请帮我延长收货时间。", "收货期限延到下周了。"),
            )
        }
        bare_action_guards = {
            answer: review_output(
                answer,
                "",
                question="请帮我办理退款。",
            )
            for answer in (
                "好了。",
                "成功了。",
                "退了。",
                "已退款。",
                "取消了。",
                "已取消。",
            )
        }
        status_reports = {
            draft: review_output(draft, draft)
            for draft in (
                "退款已完成。",
                "系统显示退款成功。",
                "订单已取消。",
            )
        }
        completed_refund_fact = _verified_delivery_tool_result()
        completed_refund_fact["output"]["facts"]["after_sales"] = [
            {"status": "completed"}
        ]
        status_lookup = validate_customer_service_draft(
            "退款已完成。",
            completed_refund_fact,
            question="请帮我查询退款状态。",
        )
        compound_action_requests = {
            question: review_output(
                "已经处理好了。",
                "",
                question=question,
            )
            for question in (
                "请帮我查询并办理退款。",
                "先查询退款状态，再退款。",
            )
        }
        assert all(
            result == (False, "forbidden_commitment_in_output")
            for result in colloquial_action_guards.values()
        )
        assert all(
            result == (False, "forbidden_commitment_in_output")
            for result in contextual_action_guards.values()
        )
        assert all(
            result == (False, "forbidden_commitment_in_output")
            for result in natural_order_action_guards.values()
        )
        assert all(
            result == (False, "forbidden_commitment_in_output")
            for result in bare_action_guards.values()
        )
        assert all(
            result == (False, "forbidden_commitment_in_output")
            for result in compound_action_requests.values()
        )
        assert all(result == (True, "output_policy_passed") for result in status_reports.values())
        assert status_lookup == (True, "customer_service_output_policy_passed")
        wp3._show(
            "第 2 步：只读退款事实不能冒充写动作回执",
            "查到退款记录只说明进度；多种口语化代办声称必须拦截，纯状态说明仍可由可信事实支持。",
            {
                "refund_action": refund_action.model_dump(),
                "colloquial_action_guards": colloquial_action_guards,
                "contextual_action_guards": contextual_action_guards,
                "natural_order_action_guards": natural_order_action_guards,
                "bare_action_guards": bare_action_guards,
                "compound_action_requests": compound_action_requests,
                "status_reports": status_reports,
                "status_lookup": status_lookup,
            },
        )
        wp3._confirm(
            observations,
            item_id="refund-action-claim",
            expected=(
                "草稿被转人工，原因是 unverified_business_action_claim；"
                "处理好了、办妥了、好了、已退款以及换货、补发、改信息、"
                "拦截、备注、优惠券、删单、确认收货和延期等自然口语也被拦截；"
                "只读状态查询和纯状态说明不被误伤。"
            ),
            auto_confirm=args.auto_confirm,
        )

        service.model.generate_json = lambda _messages, **_kwargs: {  # type: ignore[method-assign]
            "intent": "product_info",
            "mode": "clarify",
            "missing_fields": ["收货地区"],
            "response": "今天下单的话，明天一定发货。",
            "reason": "need destination",
            "confidence": 0.9,
        }
        unsafe_clarify = service.chat(
            principal,
            "pr25-p1-unsafe-clarify",
            "今天下单什么时候发货？",
            {},
        )
        service.model.generate_json = lambda _messages, **_kwargs: {  # type: ignore[method-assign]
            "intent": "after_sales",
            "mode": "handoff",
            "response": "已经帮您办理退款了。",
            "reason": "manual review required",
            "confidence": 0.9,
        }
        unsafe_handoff = service.chat(
            principal,
            "pr25-p1-unsafe-handoff",
            "请帮我申请退款",
            {},
        )
        service.model.generate_json = lambda _messages, **_kwargs: {  # type: ignore[method-assign]
            "intent": "after_sales",
            "mode": "refuse",
            "response": "已经替您取消订单了。",
            "reason": "cannot continue automatically",
            "confidence": 0.9,
        }
        unsafe_refuse = service.chat(
            principal,
            "pr25-p1-unsafe-refuse",
            "请取消这个订单",
            {},
        )
        service.model.generate_json = lambda _messages, **_kwargs: {  # type: ignore[method-assign]
            "intent": "general",
            "mode": "handoff",
            "reason": "customer_requested_human",
            "confidence": 1.0,
        }
        normal_handoff = service.chat(
            principal,
            "pr25-p1-normal-handoff",
            "转人工",
            {},
        )
        for guarded, reason, phrase in (
            (
                unsafe_clarify,
                "customer_service_unverified_delivery_commitment",
                "明天一定发货",
            ),
            (
                unsafe_handoff,
                "customer_service_unverified_business_action_claim",
                "已经帮您办理退款",
            ),
            (
                unsafe_refuse,
                "customer_service_unverified_business_action_claim",
                "已经替您取消订单",
            ),
        ):
            assert guarded.requires_human
            assert guarded.reason == reason
            assert phrase not in guarded.answer
            assert guarded.handoff_id is not None
        assert normal_handoff.requires_human
        assert normal_handoff.reason == "customer_requested_human"
        assert "请勿发送密码" in normal_handoff.answer
        wp3._show(
            "第 3 步：所有客户可见出口共用同一安全审查",
            "危险承诺不能藏在澄清、转人工或拒绝文案里；正常转人工的安全提醒不能被误拦截。",
            {
                "unsafe_clarify": unsafe_clarify.model_dump(),
                "unsafe_handoff": unsafe_handoff.model_dump(),
                "unsafe_refuse": unsafe_refuse.model_dump(),
                "normal_handoff": normal_handoff.model_dump(),
            },
        )
        wp3._confirm(
            observations,
            item_id="all-customer-visible-exits",
            expected=(
                "澄清、转人工和拒绝文案中的未验证承诺均转人工并改写；"
                "正常‘转人工’仍保留 customer_requested_human 和请勿发送密码的安全提醒。"
            ),
            auto_confirm=args.auto_confirm,
        )

        wp3._install_model(
            service,
            tool_name="get_customer_after_sales_facts",
            intent="after_sales",
            arguments=wp3._after_sales_arguments,
            answer="订单已发货，物流正在运输中，退款申请当前处于审核中。",
        )
        correct_status = service.chat(
            principal,
            "pr25-p1-status-correct",
            "订单、物流和退款现在是什么状态",
            {"shop_id": "store-a", "order_id": "ORDER-1"},
        )
        assert not correct_status.requires_human
        assert "运输中" in correct_status.answer and "审核中" in correct_status.answer
        wrong_status_cases = []
        for session_id, draft, expected_reason, forbidden_phrase in (
            (
                "pr25-p1-status-wrong-order",
                "订单取消了。",
                "customer_service_order_status_mismatch",
                "取消了",
            ),
            (
                "pr25-p1-status-wrong-logistics",
                "快递签收了。",
                "customer_service_logistics_status_mismatch",
                "签收了",
            ),
            (
                "pr25-p1-status-wrong-refund",
                "退款审核通过了。",
                "customer_service_after_sales_status_mismatch",
                "审核通过了",
            ),
        ):
            wp3._install_model(
                service,
                tool_name="get_customer_after_sales_facts",
                intent="after_sales",
                arguments=wp3._after_sales_arguments,
                answer=draft,
            )
            guarded = service.chat(
                principal,
                session_id,
                "订单、物流和退款现在是什么状态",
                {"shop_id": "store-a", "order_id": "ORDER-1"},
            )
            assert guarded.requires_human
            assert guarded.reason == expected_reason
            assert forbidden_phrase not in guarded.answer
            wrong_status_cases.append(
                {
                    "draft": draft,
                    "expected_reason": expected_reason,
                    "response": guarded.model_dump(),
                }
            )
        no_fact_status = verify_response(
            {
                "draft": "订单已经发货。",
                "normalized_input": "订单现在是什么状态",
                "retrieved": [],
                "customer_service_content": {},
                "context_bundle": {},
                "tool_result": {},
                "model_fallback": False,
                "model_retry_advised": False,
                "trace": [],
            }
        )
        wp3._install_model(
            service,
            tool_name="get_customer_sales_facts",
            intent="product_inquiry",
            arguments=wp3._sales_arguments,
            answer="订单已经发货。",
        )
        wrong_domain_order = service.chat(
            principal,
            "pr25-p1-status-sales-domain",
            "这款商品现在有货吗",
            {"shop_id": "store-a", "sku_id": "SKU-1"},
        )
        wp3._install_model(
            service,
            tool_name="get_customer_after_sales_facts",
            intent="after_sales",
            arguments=wp3._after_sales_arguments,
            answer="这款商品目前有货。",
        )
        wrong_domain_product = service.chat(
            principal,
            "pr25-p1-status-after-sales-domain",
            "订单、物流和退款现在是什么状态",
            {"shop_id": "store-a", "order_id": "ORDER-1"},
        )
        for guarded in (no_fact_status,):
            assert guarded["review_route"] == "handoff"
            assert guarded["route_reason"] == (
                "customer_service_unverified_operational_status_claim"
            )
        for guarded in (wrong_domain_order, wrong_domain_product):
            assert guarded.requires_human
            assert guarded.reason == (
                "customer_service_unverified_operational_status_claim"
            )
        wp3._show(
            "第 4 步：状态回答必须与可信事实逐项一致",
            "事实是运输中和审核中时可以回答；错误状态、没有事实或拿错领域事实都必须被拦截。",
            {
                "correct_status": correct_status.model_dump(),
                "wrong_status_cases": wrong_status_cases,
                "no_fact_status": no_fact_status,
                "wrong_domain_order": wrong_domain_order.model_dump(),
                "wrong_domain_product": wrong_domain_product.model_dump(),
            },
        )
        wp3._confirm(
            observations,
            item_id="status-consistency",
            expected=(
                "已发货/运输中/审核中的正例正常回答；订单取消、物流签收、退款审核通过"
                "三个独立反例分别返回对应 mismatch 并转人工，"
                "无事实和错领域事实也转人工，最终回答不包含错误状态。"
            ),
            auto_confirm=args.auto_confirm,
        )

        inventory_cases = []
        for session_id, question, answer, expected_reason in (
            (
                "pr25-p1-stock-price-question",
                "这个多少钱",
                "这款商品当前可售库存为 5 件。",
                "customer_service_exact_inventory_not_requested",
            ),
            (
                "pr25-p1-stock-negated-question",
                "我不是问库存数量，只想知道有没有货",
                "这款商品当前可售库存为 5 件。",
                "customer_service_exact_inventory_not_requested",
            ),
            (
                "pr25-p1-stock-vague-question",
                "这款商品还剩吗",
                "这款商品还剩 5 件。",
                "customer_service_exact_inventory_not_requested",
            ),
            (
                "pr25-p1-stock-inbound",
                "这款商品有货吗",
                "这款商品有货，另外还有在途库存。",
                "customer_service_inbound_inventory_internal",
            ),
            (
                "pr25-p1-stock-warehouse",
                "这款商品有货吗",
                "库存放在华东仓，目前有货。",
                "customer_service_warehouse_detail_internal",
            ),
            (
                "pr25-p1-product-status",
                "这款商品还能买吗",
                "这款商品已经下架。",
                "customer_service_product_status_mismatch",
            ),
        ):
            wp3._install_model(
                service,
                tool_name="get_customer_sales_facts",
                intent="product_inquiry",
                arguments=wp3._sales_arguments,
                answer=answer,
            )
            response = service.chat(
                principal,
                session_id,
                question,
                {"shop_id": "store-a", "sku_id": "SKU-1"},
            )
            assert response.requires_human
            assert response.reason == expected_reason
            assert answer not in response.answer
            inventory_cases.append(
                {
                    "question": question,
                    "unsafe_draft": answer,
                    "response": response.model_dump(),
                }
            )
        wp3._show(
            "第 5 步：库存隐私声明必须逐条落地",
            "价格、否定或模糊问法不能授权精确库存；在途和仓位属于内部信息，商品状态必须与事实一致。",
            inventory_cases,
        )
        wp3._confirm(
            observations,
            item_id="inventory-disclosure-boundaries",
            expected=(
                "价格、否定和模糊问法不泄露 5 件；在途、华东仓和错误下架状态也被拦截。"
            ),
            auto_confirm=args.auto_confirm,
        )

        unrelated_write = verify_response(
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
        generic_order_claim = verify_response(
            {
                "draft": "已经为您修改订单。",
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
        failed_write = verify_response(
            {
                "draft": "退款已经为您办好了。",
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
        credential_guard = review_output(
            "请提供银行卡密码。",
            "",
            approved_commitment=True,
        )
        off_platform_guard = review_output(
            "加我微信，转到私人账户。",
            "",
            verified_business_action="refund_order",
        )
        substring_tool_guard = review_output(
            "已经为您完成退款。",
            "策略配置写入后置条件已核验。",
            verified_business_action="refund_policy_update",
        )
        matching_tool_guard = review_output(
            "已经为您完成退款。",
            "退款写入后置条件已核验。",
            verified_business_action="refund_order",
        )
        matching_order_action_guards = {
            f"{question} -> {answer} [{tool_name}]": review_output(
                answer,
                "对应写入后置条件已核验。",
                verified_business_action=tool_name,
                question=question,
            )
            for question, answer, tool_name in (
                ("请帮我换货。", "已经给你换新的了。", "exchange_order"),
                ("请帮我补发。", "已经把新货发给你了。", "reship_order"),
                ("请帮我修改手机号。", "号码已经换成新的了。", "update_order_phone"),
                ("请帮我修改发票抬头。", "抬头改成公司了。", "update_invoice_title"),
                ("请帮我拦截快递。", "快递已经给你截住了。", "intercept_shipment"),
                ("请帮我添加订单备注。", "备注已经加上了。", "add_order_note"),
                ("请帮我补发优惠券。", "优惠券已经到账了。", "issue_coupon"),
                ("请帮我删除订单。", "订单已经替你删掉了。", "delete_order"),
                ("请帮我确认收货。", "已经替你收货了。", "confirm_receipt"),
                ("请帮我延长收货时间。", "收货期限延到下周了。", "extend_receipt_deadline"),
            )
        }
        assert unrelated_write["review_route"] == "handoff"
        assert unrelated_write["route_reason"] == (
            "customer_service_unverified_business_action_claim"
        )
        assert generic_order_claim["review_route"] == "handoff"
        assert generic_order_claim["route_reason"] == (
            "customer_service_unverified_business_action_claim"
        )
        assert failed_write["review_route"] == "handoff"
        assert failed_write["route_reason"] == (
            "customer_service_unverified_business_action_claim"
        )
        assert credential_guard == (False, "forbidden_commitment_in_output")
        assert off_platform_guard == (False, "forbidden_commitment_in_output")
        assert substring_tool_guard == (False, "forbidden_commitment_in_output")
        assert matching_tool_guard == (True, "output_policy_passed")
        assert all(
            result == (True, "output_policy_passed")
            for result in matching_order_action_guards.values()
        )
        wp3._show(
            "第 6 步：授权不能扩散到无关话术",
            "修改地址只证明地址修改，失败回执不构成成功；批准话术或写回执也不能放行密码和站外支付。",
            {
                "unrelated_write": unrelated_write,
                "generic_order_claim": generic_order_claim,
                "failed_write": failed_write,
                "credential_guard": credential_guard,
                "off_platform_guard": off_platform_guard,
                "substring_tool_guard": substring_tool_guard,
                "matching_tool_guard": matching_tool_guard,
                "matching_order_action_guards": matching_order_action_guards,
            },
        )
        wp3._confirm(
            observations,
            item_id="authorization-does-not-spread",
            expected=(
                "地址修改回执不能授权退款或泛化订单修改，失败退款回执也不能声称成功；"
                "工具名只含 refund 字样也不能伪授权；只有匹配的退款写回执可放行；"
                "换货、补发、改信息、拦截、备注、优惠券、删单、确认收货和延期"
                "也只接受各自匹配的成功写回执；"
                "银行卡密码和站外支付话术始终被拦截。"
            ),
            auto_confirm=args.auto_confirm,
        )
    except Exception:
        automatic_checks = "failed"
        raise
    finally:
        service.close()

    human_passed = all(item["confirmed"] for item in observations) and len(
        observations
    ) == 6
    confirmation_mode = "auto" if args.auto_confirm else "human"
    final_status = (
        "developer_rehearsal_passed"
        if args.auto_confirm and human_passed and automatic_checks == "passed"
        else "human_accepted"
        if human_passed and automatic_checks == "passed"
        else "failed"
    )
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    result_path = (
        args.evidence_dir / f"{args.tester}_PR25_P1承诺边界人工复验结果_{stamp}.json"
    )
    result = {
        "tester": args.tester,
        "scope": "PR #25 P1 commitment boundary recheck",
        "completed_at": datetime.now().astimezone().isoformat(),
        "data_dir": str(args.data_dir),
        "external_model_called": False,
        "platform_write_performed": False,
        "confirmation_mode": confirmation_mode,
        "automatic_contract_checks": automatic_checks,
        "observations": observations,
        "human_observations_passed": human_passed,
        "final_status": final_status,
        "transcript": str(args.transcript),
    }
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("=" * 76)
    print("复验结束")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"过程记录：{args.transcript}")
    print(f"结果文件：{result_path}")
    return 0 if final_status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
