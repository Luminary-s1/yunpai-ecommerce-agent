from __future__ import annotations

import json
from dataclasses import replace

from pydantic import BaseModel, ConfigDict

from ecommerce_agent.llm import ModelUnavailableError
from ecommerce_agent.service import AgentService
from ecommerce_agent.sops import SopCreateRequest, SopDsl, SopTransitionRequest
from ecommerce_agent.tools import ToolRegistry, ToolResult, ToolSpec

from conftest import make_settings, principal_for


class CancelOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str


def cancel_registry(calls: list[str]) -> ToolRegistry:
    registry = ToolRegistry()

    def cancel(args: BaseModel, _context) -> ToolResult:
        order_id = str(args.model_dump()["order_id"])
        calls.append(order_id)
        return ToolResult(
            status="success",
            output={"order_id": order_id, "status": "canceled"},
        )

    registry.register(
        ToolSpec(
            name="cancel_order",
            description="取消已授权且满足店铺规则的订单",
            kind="write",
            input_model=CancelOrderInput,
            handler=cancel,
            required_context_fields=("authorized",),
            idempotency_fields=("order_id",),
            verifier=lambda _args, result, _context: result.output.get("status") == "canceled",
        )
    )
    return registry


def activate_test_action_sop(service: AgentService) -> None:
    created = service.sops.create(
        "tenant-test",
        SopCreateRequest(
            sop_key="test.single_action",
            name="测试低风险单步动作",
            intent="test_action",
            risk_level="low",
            dsl=SopDsl.model_validate(
                {
                    "trigger": {"intents": ["test_action"]},
                    "steps": [{"act": "cancel_order"}],
                    "guards": {"allow_external_write": True},
                    "handoff": {"when": ["tool_failure"]},
                    "success": {"postcondition": "action_verified"},
                }
            ),
        ),
        "test-author",
    )
    version_id = created["versions"][0]["id"]
    evaluated = service.sops.evaluate(
        "tenant-test", version_id, SopTransitionRequest(expected_record_version=1), "test-reviewer"
    )
    approved = service.sops.approve(
        "tenant-test",
        version_id,
        SopTransitionRequest(
            expected_record_version=evaluated["definition"]["record_version"]
        ),
        "test-reviewer",
    )
    service.sops.activate(
        "tenant-test",
        version_id,
        SopTransitionRequest(
            expected_record_version=approved["definition"]["record_version"]
        ),
        "test-release",
    )


def test_llm_drives_registered_tool_then_finishes_after_verified_observation(tmp_path) -> None:
    calls: list[str] = []
    settings = replace(make_settings(tmp_path), bootstrap_client_can_supply_order_context=True)
    service = AgentService(settings, tool_registry=cancel_registry(calls))
    activate_test_action_sop(service)
    decision_payloads: list[dict] = []

    def decide(messages: list[dict[str, str]], **_kwargs) -> dict:
        payload = json.loads(messages[-1]["content"])
        if payload.get("task_type") == "intent_classification":
            return {"intent": "after_sales", "confidence": 0.9}
        decision_payloads.append(payload)
        if payload["latest_observation"]:
            return {
                "intent": "test_action",
                "mode": "finish",
                "reason": "verified cancellation",
                "confidence": 0.99,
            }
        return {
            "intent": "test_action",
            "mode": "act",
            "tool_name": "cancel_order",
            "arguments": {"order_id": "order-1001"},
            "expected_outcome": "order status is canceled",
            "reason": "customer requested cancellation",
            "confidence": 0.95,
        }

    service.model.generate_json = decide  # type: ignore[method-assign]
    service.model.generate = lambda _messages: "已经为您取消订单。"  # type: ignore[method-assign]
    try:
        response = service.chat(
            principal_for(service),
            "react-success",
            "请取消订单 order-1001",
            {"order_status": "paid"},
        )
        assert response.requires_human is False
        assert response.sop_id is not None
        assert response.sop_version == 1
        assert response.reason == "verified_tool_result_complete"
        assert response.answer == "已经为您取消订单。"
        assert calls == ["order-1001"]
        assert len(decision_payloads) == 2
        assert decision_payloads[0]["current_tool_catalog"][0]["name"] == "cancel_order"
        assert decision_payloads[1]["latest_observation"]["postcondition_met"] is True
        runs = service.sops.list_runs("tenant-test", status="completed")
        run = next(item for item in runs if item["intent"] == "test_action")
        detail = service.sops.get_run("tenant-test", run["id"])
        assert detail["steps"][0]["status"] == "succeeded"
        assert detail["steps"][0]["attempt_count"] == 1
        with service.db.connect() as conn:
            snapshots = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, stage, sequence, parent_snapshot_id, evidence_json
                    FROM context_snapshots WHERE trace_id=?
                    ORDER BY sequence, CASE stage WHEN 'decision' THEN 0 ELSE 1 END
                    """,
                    (response.trace_id,),
                ).fetchall()
            ]
        assert [(item["stage"], item["sequence"]) for item in snapshots] == [
            ("decision", 0),
            ("decision", 1),
            ("generation", 1),
        ]
        assert snapshots[1]["parent_snapshot_id"] == snapshots[0]["id"]
        assert snapshots[2]["parent_snapshot_id"] == snapshots[1]["id"]
        assert '"authority":"verified_tool"' in snapshots[2]["evidence_json"]
    finally:
        service.close()


def test_transient_model_outage_after_verified_action_reports_completion(tmp_path) -> None:
    calls: list[str] = []
    settings = replace(make_settings(tmp_path), bootstrap_client_can_supply_order_context=True)
    service = AgentService(settings, tool_registry=cancel_registry(calls))
    activate_test_action_sop(service)
    decision_calls = 0

    def decide(messages: list[dict[str, str]], **_kwargs) -> dict:
        nonlocal decision_calls
        payload = json.loads(messages[-1]["content"])
        if payload.get("task_type") == "intent_classification":
            return {"intent": "after_sales", "confidence": 0.9}
        decision_calls += 1
        if decision_calls == 1:
            return {
                "intent": "test_action",
                "mode": "act",
                "tool_name": "cancel_order",
                "arguments": {"order_id": "order-1001"},
                "expected_outcome": "order status is canceled",
                "reason": "customer requested cancellation",
                "confidence": 0.95,
            }
        raise ModelUnavailableError("model request failed with HTTP 429 (provider code 1302)")

    def unavailable(_messages: list[dict[str, str]]) -> str:
        raise ModelUnavailableError("model request failed with HTTP 429 (provider code 1302)")

    service.model.generate_json = decide  # type: ignore[method-assign]
    service.model.generate = unavailable  # type: ignore[method-assign]
    try:
        response = service.chat(
            principal_for(service),
            "react-outage-after-write",
            "请取消订单 order-1001",
            {"order_status": "paid"},
        )
        assert response.reason == "verified_tool_result_complete"
        assert response.answer == "操作已完成，业务系统已经确认处理结果。"
        assert response.risk_level == "high"
        assert response.requires_human is False
        assert response.handoff_id is None
        assert calls == ["order-1001"]
        assert decision_calls == 2
        runs = service.sops.list_runs("tenant-test", status="completed")
        assert any(item["intent"] == "test_action" for item in runs)
    finally:
        service.close()


def test_missing_tool_arguments_become_a_clarification_not_execution(tmp_path) -> None:
    calls: list[str] = []
    settings = replace(make_settings(tmp_path), bootstrap_client_can_supply_order_context=True)
    service = AgentService(settings, tool_registry=cancel_registry(calls))
    activate_test_action_sop(service)
    service.model.generate_json = lambda _messages, **_kwargs: {  # type: ignore[method-assign]
        "intent": "test_action",
        "mode": "act",
        "tool_name": "cancel_order",
        "arguments": {},
        "reason": "customer requested cancellation",
        "confidence": 0.8,
    }
    try:
        response = service.chat(
            principal_for(service),
            "react-clarify",
            "帮我取消订单",
            {"order_status": "paid"},
        )
        assert response.requires_human is False
        assert response.reason == "tool_arguments_invalid:order_id"
        assert "order_id" in response.answer
        assert calls == []
        run = next(
            item
            for item in service.sops.list_runs("tenant-test", status="active")
            if item["intent"] == "test_action"
        )
        assert service.sops.get_run("tenant-test", run["id"])["steps"][0]["status"] == "pending"
    finally:
        service.close()


def test_sku_demanding_clarification_is_rewritten_for_customers(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    service.model.generate_json = lambda _messages, **_kwargs: {  # type: ignore[method-assign]
        "intent": "product_info",
        "mode": "clarify",
        "missing_fields": ["sku_id"],
        "response": "您好，为了准确查询空气炸锅的参数，请提供具体的 SKU 编号。",
        "reason": "need product identifier",
        "confidence": 0.6,
    }
    try:
        response = service.chat(
            principal_for(service),
            "clarify-sku",
            "你们店铺空气炸锅什么参数？",
            {},
        )
        assert response.reason == "llm_clarification_required"
        assert response.requires_human is False
        assert "SKU" not in response.answer
        assert "sku" not in response.answer.lower()
        assert "商品名称或商品链接" in response.answer
    finally:
        service.close()


def test_unverified_delivery_commitment_in_clarification_is_handed_off(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    service.model.generate_json = lambda _messages, **_kwargs: {  # type: ignore[method-assign]
        "intent": "product_info",
        "mode": "clarify",
        "missing_fields": ["收货地区"],
        "response": "今天下单的话，明天一定发货。",
        "reason": "need destination",
        "confidence": 0.9,
    }
    try:
        response = service.chat(
            principal_for(service),
            "clarify-unsafe-delivery",
            "今天下单什么时候发货？",
            {},
        )
        assert response.requires_human is True
        assert response.reason == "customer_service_unverified_delivery_commitment"
        assert "明天一定发货" not in response.answer
        assert response.handoff_id is not None
    finally:
        service.close()


def test_unverified_refund_claim_in_handoff_copy_is_rewritten(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    service.model.generate_json = lambda _messages, **_kwargs: {  # type: ignore[method-assign]
        "intent": "after_sales",
        "mode": "handoff",
        "response": "已经帮您办理退款了。",
        "reason": "manual review required",
        "confidence": 0.9,
    }
    try:
        response = service.chat(
            principal_for(service),
            "handoff-unsafe-refund",
            "请帮我申请退款",
            {},
        )
        assert response.requires_human is True
        assert response.reason == "customer_service_unverified_business_action_claim"
        assert "已经帮您办理退款" not in response.answer
        assert response.handoff_id is not None
    finally:
        service.close()


def test_unverified_order_action_claim_in_refusal_copy_is_handed_off(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    service.model.generate_json = lambda _messages, **_kwargs: {  # type: ignore[method-assign]
        "intent": "after_sales",
        "mode": "refuse",
        "response": "已经替您取消订单了。",
        "reason": "cannot continue automatically",
        "confidence": 0.9,
    }
    try:
        response = service.chat(
            principal_for(service),
            "refuse-unsafe-order-action",
            "请取消这个订单",
            {},
        )
        assert response.requires_human is True
        assert response.reason == "customer_service_unverified_business_action_claim"
        assert "已经替您取消订单" not in response.answer
        assert response.handoff_id is not None
    finally:
        service.close()
