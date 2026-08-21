from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from ecommerce_agent.auth import Principal
from ecommerce_agent.business.catalog import CatalogItemUpsert, CatalogService
from ecommerce_agent.business.inventory import InventoryBalanceUpsert, InventoryService
from ecommerce_agent.business.orders import (
    AfterSaleCaseInput,
    LogisticsSnapshotInput,
    OrderLineInput,
    OrderService,
    OrderUpsert,
)
from ecommerce_agent.config import Settings
from ecommerce_agent.service import AgentService
from ecommerce_agent.tools import ToolResult, ToolSpec


def _settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        model_provider="controlled-local",
        model_base_url="http://127.0.0.1:9/v1",
        model_name="wp3-manual-controlled-model",
        model_api_key="",
        model_timeout_seconds=0.2,
        model_max_output_tokens=240,
        model_temperature=0.0,
        model_thinking_enabled=False,
        model_streaming=True,
        model_retry_attempts=0,
        model_enabled=False,
        model_mock_mode=True,
        model_context_limit_tokens=128000,
        context_budget_ratio=0.7,
        rag_top_k=5,
        rag_min_score=0.08,
        rag_direct_approved_answer=True,
        rag_direct_approved_min_score=0.6,
        handoff_confidence_threshold=0.6,
        max_input_chars=2000,
        session_history_limit=6,
        admin_api_key="wp3-admin-key-123456",
        admin_auth_required=True,
        bootstrap_admin_id="wp3-admin",
        auth_required=True,
        bootstrap_tenant_id="tenant-test",
        bootstrap_client_id="client-test",
        bootstrap_client_key="wp3-client-key-123456",
        bootstrap_client_can_supply_order_context=True,
        subject_hash_key="wp3-subject-hash-key-123456",
        session_idle_timeout_minutes=120,
        message_retention_days=30,
        audit_retention_days=180,
        max_request_body_bytes=16384,
        rate_limit_requests_per_minute=120,
        min_free_disk_mb=1,
        rag_scene_prompts=False,
        kg_import_enabled=False,
        kg_dream_worker_enabled=False,
    )


def _principal(service: AgentService) -> Principal:
    return service.auth.authenticate(
        service.settings.bootstrap_client_id,
        service.settings.bootstrap_client_key,
        "wp3-manual-buyer",
    )


def _catalog(
    service: AgentService,
    *,
    sku_id: str,
    title: str,
    source_time: datetime,
) -> None:
    CatalogService(service.db).upsert(
        "tenant-test",
        CatalogItemUpsert(
            connector_id="virtual_taobao",
            store_id="store-a",
            item_id=f"ITEM-{sku_id}",
            sku_id=sku_id,
            title=title,
            status="active",
            sale_price=Decimal("129.00"),
            currency="CNY",
            source_updated_at=source_time,
            source_id=f"virtual:wp3:catalog:{sku_id}",
        ),
    )


def _inventory(
    service: AgentService,
    *,
    sku_id: str,
    source_time: datetime,
) -> None:
    InventoryService(service.db).upsert(
        "tenant-test",
        InventoryBalanceUpsert(
            connector_id="virtual_taobao",
            store_id="store-a",
            warehouse_id="INTERNAL-WAREHOUSE",
            sku_id=sku_id,
            on_hand=Decimal("8"),
            reserved=Decimal("3"),
            inbound=Decimal("2"),
            average_daily_sales=Decimal("1"),
            source_updated_at=source_time,
            source_id=f"virtual:wp3:inventory:{sku_id}",
        ),
    )


def _order(service: AgentService, now: datetime) -> None:
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


def _install_model(
    service: AgentService,
    *,
    tool_name: str,
    intent: str,
    arguments: Callable[[dict[str, Any]], dict[str, Any]],
    answer: str,
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []

    def generate_json(messages: list[dict[str, str]], **_kwargs) -> dict[str, Any]:
        payload = json.loads(messages[-1]["content"])
        if payload.get("task_type") != "agent_decision":
            return {"intent": intent, "confidence": 0.98}
        decisions.append(payload)
        if payload.get("latest_observation"):
            return {
                "intent": intent,
                "mode": "finish",
                "reason": "verified_customer_service_fact_ready",
                "confidence": 0.98,
            }
        return {
            "intent": intent,
            "mode": "observe",
            "tool_name": tool_name,
            "arguments": arguments(payload),
            "reason": "controlled_model_selected_fact_tool",
            "confidence": 0.98,
        }

    service.model.generate_json = generate_json  # type: ignore[method-assign]
    service.model.generate = lambda _messages: answer  # type: ignore[method-assign]
    return decisions


def _sales_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    trusted = payload["trusted_context"]
    return {"store_id": trusted["store_id"], "sku_id": trusted["sku_id"]}


def _after_sales_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    trusted = payload["trusted_context"]
    return {
        "store_id": trusted["store_id"],
        "order_id": trusted["order_id"],
        "include_history": True,
    }


def _show(title: str, reason: str, value: Any) -> None:
    print("=" * 76)
    print(title)
    print(f"为什么测：{reason}")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _confirm(
    observations: list[dict[str, Any]],
    *,
    item_id: str,
    expected: str,
    auto_confirm: bool,
) -> None:
    print(f"人工观察重点：{expected}")
    if auto_confirm:
        answer = "Y"
        print("开发侧演练自动确认：Y（不能替代谢良璇正式人工验收）")
    else:
        answer = input("请亲自查看上方结果。符合预期请输入 Y，不符合请输入 N：")
    confirmed = answer.strip().upper() == "Y"
    observations.append(
        {"id": item_id, "expected": expected, "confirmed": confirmed}
    )
    if not confirmed:
        print("已记录为不通过，后续仍继续执行以收集完整问题。")


class RefundInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(min_length=1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--tester", default="谢良璇")
    parser.add_argument("--auto-confirm", action="store_true")
    args = parser.parse_args()

    args.data_dir.mkdir(parents=True, exist_ok=True)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    service = AgentService(_settings(args.data_dir))
    service.knowledge.retrieve = lambda *_args, **_kwargs: []  # type: ignore[method-assign]
    principal = _principal(service)
    observations: list[dict[str, Any]] = []
    automatic_checks = "passed"
    now = datetime.now(UTC)
    old = now - timedelta(days=5)
    try:
        _catalog(service, sku_id="SKU-1", title="恒温水壶", source_time=now)
        _inventory(service, sku_id="SKU-1", source_time=now)
        _catalog(
            service,
            sku_id="SKU-MISSING",
            title="无库存样例商品",
            source_time=now,
        )
        _catalog(service, sku_id="SKU-STALE", title="旧快照商品", source_time=old)
        _inventory(service, sku_id="SKU-STALE", source_time=old)
        _order(service, now)

        _install_model(
            service,
            tool_name="get_customer_sales_facts",
            intent="product_inquiry",
            arguments=_sales_arguments,
            answer="这款商品目前有货，可以正常选购。",
        )
        sync = service.chat(
            principal,
            "wp3-manual-current-sync",
            "这款商品有货吗",
            {"shop_id": "store-a", "sku_id": "SKU-1"},
        )
        service.model.stream_generate = lambda _messages: iter(  # type: ignore[method-assign]
            ("这款商品目前有货，", "可以正常选购。")
        )
        stream_events = list(
            service.chat_stream(
                principal,
                "wp3-manual-current-stream",
                "这款商品有货吗",
                {"shop_id": "store-a", "sku_id": "SKU-1"},
                idempotency_key=None,
            )
        )
        stream_answer = "".join(
            event["text"] for event in stream_events if event["event"] == "delta"
        )
        step1 = {
            "nonstream_answer": sync.answer,
            "stream_answer": stream_answer,
            "suggestion": sync.suggestion.model_dump() if sync.suggestion else None,
        }
        assert stream_answer == sync.answer
        assert "5" not in sync.answer and "在途" not in sync.answer
        assert sync.suggestion and len(sync.suggestion.facts["evidence_ids"]) == 2
        _show("第 1 步：当前库存默认披露与流式一致", "默认不能泄露内部库存结构，两类接口必须同语义。", step1)
        _confirm(
            observations,
            item_id="current-and-stream",
            expected="两类答案一致且只说有货，不出现 5 件或在途 2 件；建议含 2 条事实证据。",
            auto_confirm=args.auto_confirm,
        )

        _install_model(
            service,
            tool_name="get_customer_sales_facts",
            intent="product_inquiry",
            arguments=_sales_arguments,
            answer="这款商品当前可售库存为 5 件。",
        )
        exact = service.chat(
            principal,
            "wp3-manual-exact",
            "这款商品现在还有多少件",
            {"shop_id": "store-a", "sku_id": "SKU-1"},
        )
        assert not exact.requires_human and "5 件" in exact.answer
        assert "在途" not in exact.answer and "2 件" not in exact.answer
        _show("第 2 步：明确询问才披露精确可售量", "区分客户可见可售量和内部在途量。", exact.model_dump())
        _confirm(
            observations,
            item_id="explicit-quantity",
            expected="明确问数量时回答可售 5 件，但不出现内部在途 2 件。",
            auto_confirm=args.auto_confirm,
        )

        _install_model(
            service,
            tool_name="get_customer_sales_facts",
            intent="product_inquiry",
            arguments=_sales_arguments,
            answer="当前库存为 0 件。",
        )
        missing = service.chat(
            principal,
            "wp3-manual-missing",
            "这款商品现在还有多少件",
            {"shop_id": "store-a", "sku_id": "SKU-MISSING"},
        )
        assert missing.requires_human
        assert missing.reason == "customer_service_missing_inventory_fabricated"
        assert "0 件" not in missing.answer
        _show("第 3 步：库存缺失不能补零", "missing/null 与真实零库存不是同一事实。", missing.model_dump())
        _confirm(
            observations,
            item_id="missing-not-zero",
            expected="危险草稿被拦截并转人工，最终答案不出现虚构的库存 0 件。",
            auto_confirm=args.auto_confirm,
        )

        safe_stale_answer = (
            f"根据 {old.date().isoformat()} 的导出快照，当时显示有货，当前库存仍需核对。"
        )
        _install_model(
            service,
            tool_name="get_customer_sales_facts",
            intent="product_inquiry",
            arguments=_sales_arguments,
            answer=safe_stale_answer,
        )
        stale_safe = service.chat(
            principal,
            "wp3-manual-stale-safe",
            "这款商品有货吗",
            {"shop_id": "store-a", "sku_id": "SKU-STALE"},
        )
        _install_model(
            service,
            tool_name="get_customer_sales_facts",
            intent="product_inquiry",
            arguments=_sales_arguments,
            answer="目前有货，可以立即下单。",
        )
        service.model.stream_generate = lambda _messages: iter(  # type: ignore[method-assign]
            ("目前有货，", "可以立即下单。")
        )
        stale_events = list(
            service.chat_stream(
                principal,
                "wp3-manual-stale-unsafe",
                "这款商品有货吗",
                {"shop_id": "store-a", "sku_id": "SKU-STALE"},
                idempotency_key=None,
            )
        )
        stale_stream_answer = "".join(
            event["text"] for event in stale_events if event["event"] == "delta"
        )
        stale_result = stale_events[-1]["response"]
        assert old.date().isoformat() in stale_safe.answer
        assert not stale_safe.requires_human
        assert "目前有货" not in stale_stream_answer
        assert stale_stream_answer == stale_result["answer"]
        assert stale_result["reason"] == "customer_service_data_as_of_required"
        _show(
            "第 4 步：陈旧快照和流式安全",
            "过期事实必须带时间，危险草稿不能在验证前流给客户。",
            {"safe": stale_safe.model_dump(), "unsafe_stream": stale_result, "streamed_answer": stale_stream_answer},
        )
        _confirm(
            observations,
            item_id="stale-stream",
            expected="安全回复显示快照日期；危险流式草稿未出现，只发送最终安全降级文案。",
            auto_confirm=args.auto_confirm,
        )

        decisions = _install_model(
            service,
            tool_name="get_customer_after_sales_facts",
            intent="after_sales",
            arguments=_after_sales_arguments,
            answer="订单已发货，物流正在运输中，退款申请仍在审核。",
        )
        first = service.chat(
            principal,
            "wp3-manual-after-sales",
            "帮我查一下这笔订单的物流和退款进度",
            {"shop_id": "store-a", "order_id": "ORDER-1"},
        )
        second_decision_start = len(decisions)
        second = service.chat(
            principal,
            "wp3-manual-after-sales",
            "它现在处理到哪一步了",
            {"shop_id": "store-a"},
        )
        recovered = next(
            payload["trusted_context"].get("order_id")
            for payload in decisions[second_decision_start:]
            if payload.get("trusted_context")
        )
        serialized = json.dumps(second.model_dump(), ensure_ascii=False)
        assert recovered == "ORDER-1"
        assert not first.requires_human and not second.requires_human
        assert all(secret not in serialized for secret in ("private-buyer-hash", "TRACK", "13800138000"))
        _show(
            "第 5 步：售后多轮与隐私",
            "第二轮必须恢复可信订单，同时只输出客服允许字段。",
            {"first": first.model_dump(), "second": second.model_dump(), "recovered_order_id": recovered},
        )
        _confirm(
            observations,
            item_id="after-sales-multi-turn",
            expected="第二轮恢复 ORDER-1；回复中没有买家 hash、运单号或完整手机号。",
            auto_confirm=args.auto_confirm,
        )

        _install_model(
            service,
            tool_name="get_customer_after_sales_facts",
            intent="after_sales",
            arguments=lambda _payload: {
                "store_id": "store-a",
                "order_id": "ORDER-2",
                "include_history": True,
            },
            answer="不应生成",
        )
        wrong_scope = service.chat(
            principal,
            "wp3-manual-wrong-scope",
            "查询这笔订单",
            {"shop_id": "store-a", "order_id": "ORDER-1"},
            execution_mode="shadow",
        )
        assert wrong_scope.requires_human
        assert wrong_scope.reason == "tool_policy_denied:order_scope_mismatch"
        assert wrong_scope.handoff_id is None
        _show("第 6 步：错误订单范围在执行前阻断", "模型不能越过可信订单与店铺双绑定。", wrong_scope.model_dump())
        _confirm(
            observations,
            item_id="scope-negative",
            expected="错误 ORDER-2 被 order_scope_mismatch 阻断，影子模式不创建人工任务。",
            auto_confirm=args.auto_confirm,
        )

        session_id = service.db.resolve_session(
            tenant_id="tenant-test",
            client_id="client-test",
            external_session_id="wp3-manual-semantics",
            subject_hash=principal.subject_hash,
        )
        gate = service.graph.get_graph().nodes["decision_gate"].data
        semantic_cases = []
        for message, decision in (
            (
                "我不是要退款，只是想了解退款规则",
                {"intent": "product_inquiry", "mode": "answer", "reason": "policy_question", "confidence": 0.95},
            ),
            (
                "如果买了不合适，可以申请退款吗？",
                {"intent": "product_inquiry", "mode": "answer", "reason": "presale_hypothetical", "confidence": 0.95},
            ),
            (
                "先查库存，再说明退款规则",
                {
                    "intent": "product_inquiry",
                    "mode": "observe",
                    "tool_name": "get_customer_sales_facts",
                    "arguments": {"store_id": "store-a", "sku_id": "SKU-1"},
                    "reason": "compound_first_step",
                    "confidence": 0.95,
                },
            ),
        ):
            result = gate.invoke(
                {
                    "decision": decision,
                    "normalized_input": message,
                    "react_step": 0,
                    "tool_result": {},
                    "session_id": session_id,
                    "tenant_id": "tenant-test",
                    "execution_mode": "live",
                    "context": {"authorized": True, "shop_id": "store-a", "sku_id": "SKU-1"},
                    "trace": [],
                }
            )
            semantic_cases.append({"message": message, "model_mode": decision["mode"], "gate_route": result["route"]})
        assert [item["gate_route"] for item in semantic_cases] == ["answer", "answer", "observe"]
        _show("第 7 步：关键词不覆盖模型语义", "否定、假设和复合请求必须保留模型决定。", semantic_cases)
        _confirm(
            observations,
            item_id="semantic-counterexamples",
            expected="两条退款相关问法保持 answer，复合请求保持 observe，没有被关键词统一改成 handoff。",
            auto_confirm=args.auto_confirm,
        )

        write_calls: list[str] = []

        def write_handler(value: BaseModel, _context) -> ToolResult:
            write_calls.append(str(value.model_dump()["order_id"]))
            return ToolResult(status="success", output={"status": "refunded"}, postcondition_met=True)

        service.tools.register(
            ToolSpec(
                name="refund_order_for_wp3_manual",
                description="WP3 manual shadow barrier",
                kind="write",
                input_model=RefundInput,
                handler=write_handler,
                required_context_fields=("authorized", "order_id"),
                idempotency_fields=("order_id",),
                verifier=lambda _args, result, _context: result.status == "success",
            )
        )

        def shadow_decide(messages: list[dict[str, str]], **_kwargs) -> dict[str, Any]:
            payload = json.loads(messages[-1]["content"])
            if payload.get("task_type") != "agent_decision":
                return {"intent": "after_sales", "confidence": 0.98}
            return {
                "intent": "after_sales",
                "mode": "act",
                "tool_name": "refund_order_for_wp3_manual",
                "arguments": {"order_id": "ORDER-1"},
                "reason": "controlled_model_selected_refund",
                "confidence": 0.98,
            }

        service.model.generate_json = shadow_decide  # type: ignore[method-assign]
        with service.db.connect() as conn:
            handoff_before = conn.execute(
                "SELECT COUNT(*) FROM handoff_tasks"
            ).fetchone()[0]
            outbox_before = conn.execute(
                "SELECT COUNT(*) FROM channel_outbox"
            ).fetchone()[0]
        shadow = service.chat(
            principal,
            "wp3-manual-shadow",
            "请帮我立即退款",
            {"shop_id": "store-a", "order_id": "ORDER-1"},
            execution_mode="shadow",
        )
        with service.db.connect() as conn:
            handoff_after = conn.execute(
                "SELECT COUNT(*) FROM handoff_tasks"
            ).fetchone()[0]
            outbox_after = conn.execute(
                "SELECT COUNT(*) FROM channel_outbox"
            ).fetchone()[0]
        handoff_delta = handoff_after - handoff_before
        outbox_delta = outbox_after - outbox_before
        assert write_calls == [] and handoff_delta == 0 and outbox_delta == 0
        assert shadow.reason == "shadow_write_suppressed"
        assert shadow.suggestion and shadow.suggestion.delivery_status == "suggestion_not_sent"
        _show(
            "第 8 步：影子模式零写入",
            "影子建议不能执行退款、创建真实人工任务或写渠道 outbox。",
            {
                "response": shadow.model_dump(),
                "write_calls": write_calls,
                "handoff_before": handoff_before,
                "handoff_after": handoff_after,
                "handoff_delta": handoff_delta,
                "outbox_before": outbox_before,
                "outbox_after": outbox_after,
                "outbox_delta": outbox_delta,
            },
        )
        _confirm(
            observations,
            item_id="shadow-write-barrier",
            expected="写工具调用为 0，人工任务和 outbox 增量均为 0；建议为 suggestion_not_sent。",
            auto_confirm=args.auto_confirm,
        )
    except Exception:
        automatic_checks = "failed"
        raise
    finally:
        service.close()

    human_passed = all(item["confirmed"] for item in observations) and len(observations) == 8
    confirmation_mode = "auto" if args.auto_confirm else "human"
    final_status = (
        "developer_rehearsal_passed"
        if args.auto_confirm and human_passed and automatic_checks == "passed"
        else "human_accepted"
        if human_passed and automatic_checks == "passed"
        else "failed"
    )
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    result_path = args.evidence_dir / f"{args.tester}_WP3人工验收结果_{stamp}.json"
    result = {
        "tester": args.tester,
        "work_package": "M8-R-WP3",
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
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 76)
    print("验收结束")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"过程记录：{args.transcript}")
    print(f"结果文件：{result_path}")
    return 0 if final_status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
