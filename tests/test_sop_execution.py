from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from conftest import make_settings
from ecommerce_agent.api import create_app
from ecommerce_agent.service import AgentService
from ecommerce_agent.sops import (
    SopCompensationRequest,
    SopCreateRequest,
    SopDsl,
    SopError,
    SopService,
    SopStepResolutionRequest,
    SopTransitionRequest,
)
from ecommerce_agent.tools import (
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


class OrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str


def workflow_registry(
    calls: list[str], compensation_result: ToolResult | None = None
) -> ToolRegistry:
    registry = ToolRegistry()

    def read_order(args: BaseModel, _context: ToolExecutionContext) -> ToolResult:
        calls.append(f"read:{args.model_dump()['order_id']}")
        return ToolResult(status="success", output={"state": "paid"})

    def update_order(args: BaseModel, _context: ToolExecutionContext) -> ToolResult:
        calls.append(f"update:{args.model_dump()['order_id']}")
        return ToolResult(status="success", output={"state": "updated"})

    def restore_order(args: BaseModel, _context: ToolExecutionContext) -> ToolResult:
        calls.append(f"restore:{args.model_dump()['order_id']}")
        return compensation_result or ToolResult(
            status="success", output={"state": "restored"}
        )

    registry.register(
        ToolSpec(
            name="lookup_order",
            description="读取测试订单",
            kind="read",
            input_model=OrderInput,
            handler=read_order,
            max_retries=1,
        )
    )
    registry.register(
        ToolSpec(
            name="update_order_test",
            description="更新测试订单",
            kind="write",
            input_model=OrderInput,
            handler=update_order,
            idempotency_fields=("order_id",),
            verifier=lambda _args, result, _context: result.output.get("state") == "updated",
        )
    )
    registry.register(
        ToolSpec(
            name="restore_order_test",
            description="补偿测试订单",
            kind="write",
            input_model=OrderInput,
            handler=restore_order,
            idempotency_fields=("order_id",),
            verifier=lambda _args, result, _context: result.output.get("state") == "restored",
        )
    )
    return registry


def activate_sop(
    service: AgentService,
    *,
    key: str,
    intent: str,
    steps: list[dict],
    risk_level: str = "high",
    required_context: list[str] | None = None,
) -> dict:
    created = service.sops.create(
        "tenant-test",
        SopCreateRequest(
            sop_key=key,
            name=f"{key} workflow",
            intent=intent,
            risk_level=risk_level,
            dsl=SopDsl.model_validate(
                {
                    "trigger": {"intents": [intent]},
                    "required_context": required_context or [],
                    "steps": steps,
                    "guards": {"allow_external_write": any("act" in step for step in steps)},
                    "handoff": {"when": ["operator_required", "tool_failure"]},
                    "success": {"postcondition": "workflow_completed"},
                }
            ),
        ),
        "author",
    )
    version_id = created["versions"][0]["id"]
    evaluated = service.sops.evaluate(
        "tenant-test",
        version_id,
        SopTransitionRequest(expected_record_version=1),
        "reviewer",
    )
    approved = service.sops.approve(
        "tenant-test",
        version_id,
        SopTransitionRequest(
            expected_record_version=evaluated["definition"]["record_version"]
        ),
        "reviewer",
    )
    return service.sops.activate(
        "tenant-test",
        version_id,
        SopTransitionRequest(
            expected_record_version=approved["definition"]["record_version"]
        ),
        "release-admin",
    )


def session_for(service: AgentService, name: str) -> str:
    return service.db.resolve_session(
        tenant_id="tenant-test",
        client_id="client-test",
        external_session_id=name,
        subject_hash=f"subject:{name}",
    )


def execute_registered(
    registry: ToolRegistry,
    name: str,
    arguments: dict,
    *,
    session_id: str,
) -> ToolResult:
    context = ToolExecutionContext(
        tenant_id="tenant-test",
        client_id="client-test",
        session_id=session_id,
        trace_id="trace-test",
        trusted_context={"order_id": arguments.get("order_id")},
    )
    spec, validated = registry.validate_selection(
        name=name, arguments=arguments, requested_mode="act" if name != "lookup_order" else "observe",
        context=context,
    )
    return registry.execute(spec=spec, arguments=validated, context=context)


def test_typed_dsl_assigns_stable_ids_and_rejects_unsafe_retry_metadata() -> None:
    dsl = SopDsl.model_validate(
        {
            "trigger": {"intents": ["test"]},
            "steps": [{"observe": "lookup_order", "max_attempts": 3}, {"evaluate": "policy"}],
            "guards": {"allow_external_write": False},
            "handoff": {"when": ["conflict"]},
            "success": {"postcondition": "checked"},
        }
    )
    assert [step.id for step in dsl.steps] == ["step_01", "step_02"]
    with pytest.raises(ValueError, match="cannot be retried automatically"):
        SopDsl.model_validate(
            {
                "trigger": {"intents": ["test"]},
                "steps": [{"act": "write_tool", "max_attempts": 2}],
                "success": {"postcondition": "done"},
            }
        )
    with pytest.raises(ValueError, match="ids must be unique"):
        SopDsl.model_validate(
            {
                "trigger": {"intents": ["test"]},
                "steps": [
                    {"id": "same", "observe": "one"},
                    {"id": "same", "observe": "two"},
                ],
                "success": {"postcondition": "done"},
            }
        )


def test_high_risk_action_cannot_be_approved_without_step_approval(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        created = service.sops.create(
            "tenant-test",
            SopCreateRequest(
                sop_key="unsafe.high.action",
                name="未审批高风险动作",
                intent="unsafe_action",
                risk_level="high",
                dsl=SopDsl.model_validate(
                    {
                        "trigger": {"intents": ["unsafe_action"]},
                        "steps": [{"act": "unsafe_write"}],
                        "guards": {"allow_external_write": True},
                        "handoff": {"when": ["failure"]},
                        "success": {"postcondition": "written"},
                    }
                ),
            ),
            "author",
        )
        version_id = created["versions"][0]["id"]
        evaluated = service.sops.evaluate(
            "tenant-test",
            version_id,
            SopTransitionRequest(expected_record_version=1),
            "reviewer",
        )
        report = evaluated["versions"][0]["evaluation"]
        assert report["passed"] is False
        assert report["paths"]["high_risk_approval"] is False
        with pytest.raises(SopError, match="evaluation must pass"):
            service.sops.approve(
                "tenant-test",
                version_id,
                SopTransitionRequest(
                    expected_record_version=evaluated["definition"]["record_version"]
                ),
                "reviewer",
            )
    finally:
        service.close()
def test_multistep_run_requires_context_and_approval_then_can_compensate(tmp_path) -> None:
    calls: list[str] = []
    registry = workflow_registry(calls)
    service = AgentService(make_settings(tmp_path), tool_registry=registry)
    try:
        activate_sop(
            service,
            key="workflow.multistep",
            intent="workflow_test",
            steps=[
                {"clarify_if_missing": "order_id"},
                {"observe": "lookup_order", "max_attempts": 2},
                {"evaluate": "order_change_policy"},
                {
                    "act": "update_order_test",
                    "requires_approval": True,
                    "compensate_with": "restore_order_test",
                },
            ],
        )
        session_id = session_for(service, "workflow-multistep")
        sop = service.sops.resolve_for_session("tenant-test", session_id, "workflow_test")
        assert sop and sop["run_status"] == "active"
        run_id = sop["run_id"]

        missing = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=run_id,
            requested_mode="observe",
            tool_name="lookup_order",
            arguments={"order_id": "order-1"},
            context={},
        )
        assert missing["reason"] == "sop_step_context_missing"
        assert missing["missing_fields"] == ["order_id"]

        started = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=run_id,
            requested_mode="observe",
            tool_name="lookup_order",
            arguments={"order_id": "order-1"},
            context={"order_id": "order-1"},
        )
        assert started["allowed"] is True
        read_result = execute_registered(
            registry, "lookup_order", {"order_id": "order-1"}, session_id=session_id
        )
        run = service.sops.record_step_result(
            tenant_id="tenant-test",
            run_id=run_id,
            step_run_id=started["step"]["id"],
            result=read_result,
        )
        assert run["steps"][1]["status"] == "succeeded"

        approval_gate = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=run_id,
            requested_mode="act",
            tool_name="update_order_test",
            arguments={"order_id": "order-1"},
            context={"order_id": "order-1"},
        )
        assert approval_gate["reason"] == "sop_step_approval_required"
        run = service.sops.get_run("tenant-test", run_id)
        evaluation = run["steps"][2]
        run = service.sops.resolve_step(
            "tenant-test",
            run_id,
            evaluation["step_id"],
            SopStepResolutionRequest(
                expected_record_version=evaluation["record_version"],
                resolution="approve",
                note="策略检查通过",
            ),
            "operator-a",
        )
        assert run["steps"][2]["status"] == "succeeded"

        action_gate = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=run_id,
            requested_mode="act",
            tool_name="update_order_test",
            arguments={"order_id": "order-1"},
            context={"order_id": "order-1"},
        )
        assert action_gate["reason"] == "sop_step_approval_required"
        action = service.sops.get_run("tenant-test", run_id)["steps"][3]
        service.sops.resolve_step(
            "tenant-test",
            run_id,
            action["step_id"],
            SopStepResolutionRequest(
                expected_record_version=action["record_version"],
                resolution="approve",
                note="已核对订单状态并批准",
            ),
            "operator-b",
        )
        started_action = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=run_id,
            requested_mode="act",
            tool_name="update_order_test",
            arguments={"order_id": "order-1"},
            context={"order_id": "order-1"},
        )
        action_result = execute_registered(
            registry, "update_order_test", {"order_id": "order-1"}, session_id=session_id
        )
        completed = service.sops.record_step_result(
            tenant_id="tenant-test",
            run_id=run_id,
            step_run_id=started_action["step"]["id"],
            result=action_result,
        )
        assert completed["status"] == "completed"
        assert calls == ["read:order-1", "update:order-1"]

        action = completed["steps"][3]
        compensated = service.sops.compensate_step(
            "tenant-test",
            run_id,
            action["step_id"],
            SopCompensationRequest(
                expected_record_version=action["record_version"],
                arguments={"order_id": "order-1"},
                note="客户撤销变更，执行已批准补偿",
            ),
            "operator-b",
        )
        assert compensated["status"] == "failed"
        assert compensated["last_error"] == "action_compensated"
        assert compensated["steps"][3]["status"] == "compensated"
        assert calls == ["read:order-1", "update:order-1", "restore:order-1"]
        assert compensated["steps"][3]["compensation_input_hash"]
    finally:
        service.close()


def test_uncertain_action_is_never_retried_and_requires_reconciliation(tmp_path) -> None:
    calls: list[str] = []
    registry = workflow_registry(calls)
    service = AgentService(make_settings(tmp_path), tool_registry=registry)
    try:
        activate_sop(
            service,
            key="workflow.uncertain",
            intent="uncertain_test",
            risk_level="low",
            steps=[{"act": "update_order_test"}],
        )
        session_id = session_for(service, "workflow-uncertain")
        sop = service.sops.resolve_for_session("tenant-test", session_id, "uncertain_test")
        started = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            requested_mode="act",
            tool_name="update_order_test",
            arguments={"order_id": "order-u"},
            context={},
        )
        run = service.sops.record_step_result(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            step_run_id=started["step"]["id"],
            result=ToolResult(status="uncertain", error_code="network_after_dispatch"),
        )
        assert run["status"] == "handoff"
        assert run["steps"][0]["status"] == "uncertain"
        denied = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            requested_mode="act",
            tool_name="update_order_test",
            arguments={"order_id": "order-u"},
            context={},
        )
        assert denied["reason"] == "sop_run_not_active"
        step = run["steps"][0]
        reconciled = service.sops.resolve_step(
            "tenant-test",
            sop["run_id"],
            step["step_id"],
            SopStepResolutionRequest(
                expected_record_version=step["record_version"],
                resolution="confirm_succeeded",
                note="已从业务系统读回确认变更成功",
            ),
            "operator-a",
        )
        assert reconciled["status"] == "completed"
        assert reconciled["steps"][0]["status"] == "succeeded"
        assert calls == []
    finally:
        service.close()


def test_uncertain_compensation_is_frozen_until_operator_readback(tmp_path) -> None:
    calls: list[str] = []
    registry = workflow_registry(
        calls, ToolResult(status="uncertain", error_code="compensation_timeout")
    )
    service = AgentService(make_settings(tmp_path), tool_registry=registry)
    try:
        activate_sop(
            service,
            key="workflow.compensation.uncertain",
            intent="compensation_uncertain",
            risk_level="low",
            steps=[
                {
                    "act": "update_order_test",
                    "compensate_with": "restore_order_test",
                }
            ],
        )
        session_id = session_for(service, "compensation-uncertain")
        sop = service.sops.resolve_for_session(
            "tenant-test", session_id, "compensation_uncertain"
        )
        started = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            requested_mode="act",
            tool_name="update_order_test",
            arguments={"order_id": "order-cu"},
            context={},
        )
        action_result = execute_registered(
            registry, "update_order_test", {"order_id": "order-cu"}, session_id=session_id
        )
        completed = service.sops.record_step_result(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            step_run_id=started["step"]["id"],
            result=action_result,
        )
        step = completed["steps"][0]
        uncertain = service.sops.compensate_step(
            "tenant-test",
            sop["run_id"],
            step["step_id"],
            SopCompensationRequest(
                expected_record_version=step["record_version"],
                arguments={"order_id": "order-cu"},
                note="执行逆向恢复",
            ),
            "operator-a",
        )
        assert uncertain["status"] == "handoff"
        assert uncertain["steps"][0]["status"] == "compensation_uncertain"
        assert calls == ["update:order-cu", "restore:order-cu"]

        uncertain_step = uncertain["steps"][0]
        reconciled = service.sops.resolve_step(
            "tenant-test",
            sop["run_id"],
            uncertain_step["step_id"],
            SopStepResolutionRequest(
                expected_record_version=uncertain_step["record_version"],
                resolution="confirm_succeeded",
                note="已读回确认原状态恢复成功",
            ),
            "operator-b",
        )
        assert reconciled["status"] == "failed"
        assert reconciled["steps"][0]["status"] == "compensated"
    finally:
        service.close()


def test_restart_recovers_reads_but_freezes_actions_and_compensations(tmp_path) -> None:
    calls: list[str] = []
    registry = workflow_registry(calls)
    service = AgentService(make_settings(tmp_path), tool_registry=registry)
    try:
        activate_sop(
            service,
            key="workflow.restart.read",
            intent="restart_read",
            risk_level="low",
            steps=[{"observe": "lookup_order", "max_attempts": 2}],
        )
        read_session = session_for(service, "restart-read")
        read_sop = service.sops.resolve_for_session("tenant-test", read_session, "restart_read")
        service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=read_sop["run_id"],
            requested_mode="observe",
            tool_name="lookup_order",
            arguments={"order_id": "order-r"},
            context={},
        )

        activate_sop(
            service,
            key="workflow.restart.action",
            intent="restart_action",
            risk_level="low",
            steps=[{"act": "update_order_test", "compensate_with": "restore_order_test"}],
        )
        action_session = session_for(service, "restart-action")
        action_sop = service.sops.resolve_for_session(
            "tenant-test", action_session, "restart_action"
        )
        service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=action_sop["run_id"],
            requested_mode="act",
            tool_name="update_order_test",
            arguments={"order_id": "order-a"},
            context={},
        )

        report = SopService(service.db, registry).recover_interrupted_runs()
        assert report == {
            "observations_requeued": 1,
            "actions_uncertain": 1,
            "compensations_uncertain": 0,
        }
        read_run = service.sops.get_run("tenant-test", read_sop["run_id"])
        action_run = service.sops.get_run("tenant-test", action_sop["run_id"])
        assert read_run["status"] == "active"
        assert read_run["steps"][0]["status"] == "pending"
        assert action_run["status"] == "handoff"
        assert action_run["steps"][0]["status"] == "uncertain"
    finally:
        service.close()


def test_concurrent_step_claim_allows_one_executor(tmp_path) -> None:
    calls: list[str] = []
    registry = workflow_registry(calls)
    service = AgentService(make_settings(tmp_path), tool_registry=registry)
    try:
        activate_sop(
            service,
            key="workflow.concurrent",
            intent="concurrent_test",
            risk_level="low",
            steps=[{"observe": "lookup_order", "max_attempts": 2}],
        )
        session_id = session_for(service, "workflow-concurrent")
        sop = service.sops.resolve_for_session("tenant-test", session_id, "concurrent_test")

        def claim() -> dict:
            return service.sops.begin_step(
                tenant_id="tenant-test",
                run_id=sop["run_id"],
                requested_mode="observe",
                tool_name="lookup_order",
                arguments={"order_id": "order-c"},
                context={},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _value: claim(), range(2)))
        assert sum(result["allowed"] for result in results) == 1
        assert {result["reason"] for result in results} == {
            "sop_step_started",
            "sop_step_requires_resolution",
        }
    finally:
        service.close()


def test_sop_run_admin_api_is_tenant_scoped_and_versioned(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    headers = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}
    with TestClient(app) as client:
        service = app.state.agent
        activate_sop(
            service,
            key="workflow.api",
            intent="api_workflow",
            risk_level="low",
            steps=[{"evaluate": "operator_policy"}],
        )
        session_id = session_for(service, "workflow-api")
        sop = service.sops.resolve_for_session("tenant-test", session_id, "api_workflow")
        gate = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            requested_mode="observe",
            tool_name="unused",
            arguments={},
            context={},
        )
        assert gate["reason"] == "sop_step_approval_required"

        listed = client.get("/v1/admin/sop-runs", headers=headers)
        assert listed.status_code == 200
        assert any(item["id"] == sop["run_id"] for item in listed.json())
        detail = client.get(f"/v1/admin/sop-runs/{sop['run_id']}", headers=headers)
        assert detail.status_code == 200
        step = detail.json()["steps"][0]
        approved = client.post(
            f"/v1/admin/sop-runs/{sop['run_id']}/steps/{step['step_id']}/resolve",
            headers=headers,
            json={
                "expected_record_version": step["record_version"],
                "resolution": "approve",
                "note": "人工策略检查通过",
            },
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "completed"
        stale = client.post(
            f"/v1/admin/sop-runs/{sop['run_id']}/steps/{step['step_id']}/resolve",
            headers=headers,
            json={
                "expected_record_version": step["record_version"],
                "resolution": "approve",
                "note": "重复审批必须被拒绝",
            },
        )
        assert stale.status_code == 409
        assert client.get(
            "/v1/admin/sop-runs/not-owned", headers=headers
        ).status_code == 404


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"observe": "lookup_order", "evaluate": "policy"}, "one supported operation"),
        ({"observe": "lookup_order", "requires_approval": True}, "only valid for act"),
        ({"observe": "lookup_order", "compensate_with": "restore"}, "only valid for act"),
        ({"evaluate": "policy", "max_attempts": 2}, "only observe steps"),
    ],
)
def test_step_contract_rejects_ambiguous_or_unsafe_metadata(payload, message) -> None:
    with pytest.raises(ValueError, match=message):
        SopDsl.model_validate(
            {
                "trigger": {"intents": ["contract_test"]},
                "steps": [payload],
                "success": {"postcondition": "done"},
            }
        )


def test_waiting_gate_returns_fresh_version_and_enforces_step_order(tmp_path) -> None:
    calls: list[str] = []
    registry = workflow_registry(calls)
    service = AgentService(make_settings(tmp_path), tool_registry=registry)
    try:
        activate_sop(
            service,
            key="workflow.fresh-gate",
            intent="fresh_gate",
            risk_level="low",
            steps=[
                {"clarify_if_missing": "order_id"},
                {"observe": "lookup_order", "max_attempts": 2},
            ],
        )
        session_id = session_for(service, "fresh-gate")
        sop = service.sops.resolve_for_session("tenant-test", session_id, "fresh_gate")

        waiting = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            requested_mode="observe",
            tool_name="lookup_order",
            arguments={"order_id": "order-g"},
            context={},
        )
        assert waiting["step"]["status"] == "waiting_input"
        assert waiting["step"]["record_version"] == 2

        mismatch = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            requested_mode="act",
            tool_name="update_order_test",
            arguments={"order_id": "order-g"},
            context={"order_id": "order-g"},
        )
        assert mismatch["reason"] == "sop_step_order_mismatch"
        assert mismatch["step"]["operation"] == "observe"
        assert mismatch["step"]["status"] == "pending"
    finally:
        service.close()


def test_retryable_observation_requeues_then_exhausts_and_redacts_result(tmp_path) -> None:
    calls: list[str] = []
    registry = workflow_registry(calls)
    service = AgentService(make_settings(tmp_path), tool_registry=registry)
    try:
        activate_sop(
            service,
            key="workflow.retry-exhaustion",
            intent="retry_exhaustion",
            risk_level="low",
            steps=[{"observe": "lookup_order", "max_attempts": 2}],
        )
        session_id = session_for(service, "retry-exhaustion")
        sop = service.sops.resolve_for_session(
            "tenant-test", session_id, "retry_exhaustion"
        )

        first = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            requested_mode="observe",
            tool_name="lookup_order",
            arguments={"order_id": "order-r1"},
            context={},
        )
        requeued = service.sops.record_step_result(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            step_run_id=first["step"]["id"],
            result=ToolResult(
                status="failed",
                retryable=True,
                error_code="upstream_busy",
                output={
                    "phone": "13800138000",
                    "password": "never-persist-this",
                    "nested": {"access_token": "token-value"},
                },
            ),
        )
        step = requeued["steps"][0]
        assert requeued["status"] == "active"
        assert step["status"] == "pending"
        assert step["attempt_count"] == 1
        persisted = step["result"]["output_redacted"]
        assert "never-persist-this" not in persisted
        assert "token-value" not in persisted
        assert "13800138000" not in persisted
        assert "138****8000" in persisted
        assert step["result"]["redacted"] is True

        second = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            requested_mode="observe",
            tool_name="lookup_order",
            arguments={"order_id": "order-r1"},
            context={},
        )
        exhausted = service.sops.record_step_result(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            step_run_id=second["step"]["id"],
            result=ToolResult(
                status="failed", retryable=True, error_code="upstream_busy"
            ),
        )
        assert exhausted["status"] == "handoff"
        assert exhausted["steps"][0]["status"] == "failed"
        assert exhausted["steps"][0]["attempt_count"] == 2
    finally:
        service.close()


def test_failed_observation_can_be_retried_by_operator(tmp_path) -> None:
    calls: list[str] = []
    registry = workflow_registry(calls)
    service = AgentService(make_settings(tmp_path), tool_registry=registry)
    try:
        activate_sop(
            service,
            key="workflow.operator-retry",
            intent="operator_retry",
            risk_level="low",
            steps=[{"observe": "lookup_order", "max_attempts": 2}],
        )
        session_id = session_for(service, "operator-retry")
        sop = service.sops.resolve_for_session("tenant-test", session_id, "operator_retry")
        started = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            requested_mode="observe",
            tool_name="lookup_order",
            arguments={"order_id": "order-or"},
            context={},
        )
        failed = service.sops.record_step_result(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            step_run_id=started["step"]["id"],
            result=ToolResult(status="failed", error_code="operator_check_required"),
        )
        step = failed["steps"][0]
        retried = service.sops.resolve_step(
            "tenant-test",
            sop["run_id"],
            step["step_id"],
            SopStepResolutionRequest(
                expected_record_version=step["record_version"],
                resolution="retry",
                note="Upstream recovered; allow one read-only retry",
            ),
            "operator-a",
        )
        assert retried["status"] == "active"
        assert retried["steps"][0]["status"] == "pending"

        restarted = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            requested_mode="observe",
            tool_name="lookup_order",
            arguments={"order_id": "order-or"},
            context={},
        )
        completed = service.sops.record_step_result(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            step_run_id=restarted["step"]["id"],
            result=ToolResult(status="success", output={"state": "paid"}, postcondition_met=True),
        )
        assert completed["status"] == "completed"
        assert service.sops.list_runs("tenant-test", status="completed")[0]["id"] == sop["run_id"]
    finally:
        service.close()


def test_uncertain_action_and_compensation_can_be_confirmed_failed(tmp_path) -> None:
    calls: list[str] = []
    registry = workflow_registry(
        calls, ToolResult(status="uncertain", error_code="compensation_timeout")
    )
    service = AgentService(make_settings(tmp_path), tool_registry=registry)
    try:
        activate_sop(
            service,
            key="workflow.confirm-action-failed",
            intent="confirm_action_failed",
            risk_level="low",
            steps=[{"act": "update_order_test"}],
        )
        action_session = session_for(service, "confirm-action-failed")
        action_sop = service.sops.resolve_for_session(
            "tenant-test", action_session, "confirm_action_failed"
        )
        action_started = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=action_sop["run_id"],
            requested_mode="act",
            tool_name="update_order_test",
            arguments={"order_id": "order-af"},
            context={},
        )
        uncertain_action = service.sops.record_step_result(
            tenant_id="tenant-test",
            run_id=action_sop["run_id"],
            step_run_id=action_started["step"]["id"],
            result=ToolResult(status="uncertain", error_code="dispatch_unknown"),
        )
        action_step = uncertain_action["steps"][0]
        action_failed = service.sops.resolve_step(
            "tenant-test",
            action_sop["run_id"],
            action_step["step_id"],
            SopStepResolutionRequest(
                expected_record_version=action_step["record_version"],
                resolution="confirm_failed",
                note="Platform readback confirms action was not applied",
            ),
            "operator-a",
        )
        assert action_failed["status"] == "failed"
        assert action_failed["steps"][0]["status"] == "failed"

        activate_sop(
            service,
            key="workflow.confirm-compensation-failed",
            intent="confirm_compensation_failed",
            risk_level="low",
            steps=[{"act": "update_order_test", "compensate_with": "restore_order_test"}],
        )
        comp_session = session_for(service, "confirm-compensation-failed")
        comp_sop = service.sops.resolve_for_session(
            "tenant-test", comp_session, "confirm_compensation_failed"
        )
        comp_started = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=comp_sop["run_id"],
            requested_mode="act",
            tool_name="update_order_test",
            arguments={"order_id": "order-cf"},
            context={},
        )
        comp_completed = service.sops.record_step_result(
            tenant_id="tenant-test",
            run_id=comp_sop["run_id"],
            step_run_id=comp_started["step"]["id"],
            result=ToolResult(status="success", postcondition_met=True),
        )
        comp_step = comp_completed["steps"][0]
        comp_uncertain = service.sops.compensate_step(
            "tenant-test",
            comp_sop["run_id"],
            comp_step["step_id"],
            SopCompensationRequest(
                expected_record_version=comp_step["record_version"],
                arguments={"order_id": "order-cf"},
                note="Reverse the approved order update",
            ),
            "operator-b",
        )
        uncertain_step = comp_uncertain["steps"][0]
        confirmed = service.sops.resolve_step(
            "tenant-test",
            comp_sop["run_id"],
            uncertain_step["step_id"],
            SopStepResolutionRequest(
                expected_record_version=uncertain_step["record_version"],
                resolution="confirm_failed",
                note="Platform readback confirms compensation was not applied",
            ),
            "operator-b",
        )
        assert confirmed["status"] == "failed"
        assert confirmed["steps"][0]["status"] == "compensation_failed"
    finally:
        service.close()


def test_restart_freezes_interrupted_compensation_and_exhausted_observation(tmp_path) -> None:
    calls: list[str] = []
    registry = workflow_registry(calls)
    service = AgentService(make_settings(tmp_path), tool_registry=registry)
    try:
        activate_sop(
            service,
            key="workflow.restart-exhausted",
            intent="restart_exhausted",
            risk_level="low",
            steps=[{"observe": "lookup_order"}],
        )
        read_session = session_for(service, "restart-exhausted")
        read_sop = service.sops.resolve_for_session(
            "tenant-test", read_session, "restart_exhausted"
        )
        service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=read_sop["run_id"],
            requested_mode="observe",
            tool_name="lookup_order",
            arguments={"order_id": "order-re"},
            context={},
        )

        activate_sop(
            service,
            key="workflow.restart-compensating",
            intent="restart_compensating",
            risk_level="low",
            steps=[{"act": "update_order_test", "compensate_with": "restore_order_test"}],
        )
        comp_session = session_for(service, "restart-compensating")
        comp_sop = service.sops.resolve_for_session(
            "tenant-test", comp_session, "restart_compensating"
        )
        comp_started = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=comp_sop["run_id"],
            requested_mode="act",
            tool_name="update_order_test",
            arguments={"order_id": "order-rc"},
            context={},
        )
        service.sops.record_step_result(
            tenant_id="tenant-test",
            run_id=comp_sop["run_id"],
            step_run_id=comp_started["step"]["id"],
            result=ToolResult(status="success", postcondition_met=True),
        )
        with service.db._write_lock, service.db.connect() as conn:
            conn.execute(
                "UPDATE sop_step_runs SET status='compensating' WHERE run_id=?",
                (comp_sop["run_id"],),
            )

        report = SopService(service.db, registry).recover_interrupted_runs()
        assert report == {
            "observations_requeued": 0,
            "actions_uncertain": 0,
            "compensations_uncertain": 1,
        }
        read_run = service.sops.get_run("tenant-test", read_sop["run_id"])
        comp_run = service.sops.get_run("tenant-test", comp_sop["run_id"])
        assert read_run["status"] == "handoff"
        assert read_run["steps"][0]["status"] == "failed"
        assert read_run["last_error"] == "observation_attempts_exhausted_after_restart"
        assert comp_run["status"] == "handoff"
        assert comp_run["steps"][0]["status"] == "compensation_uncertain"
    finally:
        service.close()


def test_evaluation_rejects_unknown_observe_tool(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        created = service.sops.create(
            "tenant-test",
            SopCreateRequest(
                sop_key="workflow.unknown-tool",
                name="Unknown tool workflow",
                intent="unknown_tool",
                risk_level="low",
                dsl=SopDsl.model_validate(
                    {
                        "trigger": {"intents": ["unknown_tool"]},
                        "steps": [{"observe": "tool_not_registered"}],
                        "guards": {"allow_external_write": False},
                        "handoff": {"when": ["tool_unavailable"]},
                        "success": {"postcondition": "checked"},
                    }
                ),
            ),
            "author",
        )
        version_id = created["versions"][0]["id"]
        evaluated = service.sops.evaluate(
            "tenant-test",
            version_id,
            SopTransitionRequest(expected_record_version=1),
            "reviewer",
        )
        report = evaluated["versions"][0]["evaluation"]
        assert report["passed"] is False
        assert report["paths"]["observe_tools_registered"] is False
    finally:
        service.close()


def test_compensation_failure_is_persisted_and_validation_does_not_claim_step(tmp_path) -> None:
    calls: list[str] = []
    registry = workflow_registry(
        calls,
        ToolResult(status="failed", error_code="restore_policy_denied"),
    )
    service = AgentService(make_settings(tmp_path), tool_registry=registry)
    try:
        activate_sop(
            service,
            key="workflow.compensation-failed",
            intent="compensation_failed",
            risk_level="low",
            steps=[{"act": "update_order_test", "compensate_with": "restore_order_test"}],
        )
        session_id = session_for(service, "compensation-failed")
        sop = service.sops.resolve_for_session(
            "tenant-test", session_id, "compensation_failed"
        )
        started = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            requested_mode="act",
            tool_name="update_order_test",
            arguments={"order_id": "order-fc"},
            context={},
        )
        completed = service.sops.record_step_result(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            step_run_id=started["step"]["id"],
            result=ToolResult(status="success", postcondition_met=True),
        )
        step = completed["steps"][0]
        request = SopCompensationRequest(
            expected_record_version=step["record_version"],
            arguments={"order_id": "order-fc"},
            note="Approved reverse operation",
        )

        with pytest.raises(SopError, match="tools are not available"):
            SopService(service.db).compensate_step(
                "tenant-test", sop["run_id"], step["step_id"], request, "operator-a"
            )
        with pytest.raises(SopError, match="compensation_tool_invalid"):
            service.sops.compensate_step(
                "tenant-test",
                sop["run_id"],
                step["step_id"],
                request.model_copy(update={"arguments": {}}),
                "operator-a",
            )
        unchanged = service.sops.get_run("tenant-test", sop["run_id"])["steps"][0]
        assert unchanged["status"] == "succeeded"
        assert unchanged["compensation_attempt_count"] == 0

        failed = service.sops.compensate_step(
            "tenant-test", sop["run_id"], step["step_id"], request, "operator-a"
        )
        assert failed["status"] == "handoff"
        assert failed["steps"][0]["status"] == "compensation_failed"
        assert failed["steps"][0]["compensation_error_code"] == "restore_policy_denied"
        assert failed["steps"][0]["compensation_attempt_count"] == 1
        with pytest.raises(SopError, match="only a succeeded action"):
            service.sops.compensate_step(
                "tenant-test", sop["run_id"], step["step_id"], request, "operator-a"
            )
    finally:
        service.close()


def test_attempt_budget_and_result_recording_contracts_are_enforced(tmp_path) -> None:
    calls: list[str] = []
    registry = workflow_registry(calls)
    service = AgentService(make_settings(tmp_path), tool_registry=registry)
    try:
        activate_sop(
            service,
            key="workflow.attempt-contract",
            intent="attempt_contract",
            risk_level="low",
            steps=[{"observe": "lookup_order"}],
        )
        session_id = session_for(service, "attempt-contract")
        sop = service.sops.resolve_for_session(
            "tenant-test", session_id, "attempt_contract"
        )
        with pytest.raises(SopError, match="step run not found"):
            service.sops.record_step_result(
                tenant_id="tenant-test",
                run_id=sop["run_id"],
                step_run_id="missing-step-run",
                result=ToolResult(status="success", postcondition_met=True),
            )
        with service.db._write_lock, service.db.connect() as conn:
            conn.execute(
                "UPDATE sop_step_runs SET attempt_count=max_attempts WHERE run_id=?",
                (sop["run_id"],),
            )
        exhausted = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=sop["run_id"],
            requested_mode="observe",
            tool_name="lookup_order",
            arguments={"order_id": "order-budget"},
            context={},
        )
        assert exhausted["reason"] == "sop_step_attempts_exhausted"
        run = service.sops.get_run("tenant-test", sop["run_id"])
        assert run["status"] == "handoff"
        assert run["steps"][0]["error_code"] == "attempts_exhausted"

        activate_sop(
            service,
            key="workflow.double-record",
            intent="double_record",
            risk_level="low",
            steps=[{"observe": "lookup_order"}],
        )
        double_session = session_for(service, "double-record")
        double_sop = service.sops.resolve_for_session(
            "tenant-test", double_session, "double_record"
        )
        started = service.sops.begin_step(
            tenant_id="tenant-test",
            run_id=double_sop["run_id"],
            requested_mode="observe",
            tool_name="lookup_order",
            arguments={"order_id": "order-double"},
            context={},
        )
        service.sops.record_step_result(
            tenant_id="tenant-test",
            run_id=double_sop["run_id"],
            step_run_id=started["step"]["id"],
            result=ToolResult(status="success", postcondition_met=True),
        )
        with pytest.raises(SopError, match="not running"):
            service.sops.record_step_result(
                tenant_id="tenant-test",
                run_id=double_sop["run_id"],
                step_run_id=started["step"]["id"],
                result=ToolResult(status="success", postcondition_met=True),
            )
    finally:
        service.close()


def test_safe_result_projection_redacts_suffix_keys_and_bounds_nested_output() -> None:
    nested: dict = {"value": "visible"}
    for _index in range(10):
        nested = {"next": nested}
    payload = json.loads(
        SopService._safe_result_json(
            ToolResult(
                status="success",
                output={
                    "client_secret": "secret-value",
                    "provider-token": "token-value",
                    "items": ["13800138000", nested],
                },
                postcondition_met=True,
            )
        )
    )
    projected = payload["output_redacted"]
    assert "secret-value" not in projected
    assert "token-value" not in projected
    assert "13800138000" not in projected
    assert "[TRUNCATED]" in projected
    assert payload["redacted"] is True
