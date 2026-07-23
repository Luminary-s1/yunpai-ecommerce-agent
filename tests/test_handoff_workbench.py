from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.auth import AdminOperatorCreateRequest
from ecommerce_agent.handoff import HandoffError
from ecommerce_agent.schemas import (
    HandoffOperatorQueueAssignment,
    HandoffOperatorUpsert,
    HandoffQueueUpsert,
)
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


def _tenant(service: AgentService) -> str:
    return service.settings.bootstrap_tenant_id


def _queue_update(queue, **changes) -> HandoffQueueUpsert:
    values = {
        "queue_key": queue.queue_key,
        "name": queue.name,
        "description": queue.description,
        "status": queue.status,
        "default_priority": queue.default_priority,
        "first_response_sla_minutes": queue.first_response_sla_minutes,
        "resolution_sla_minutes": queue.resolution_sla_minutes,
        "max_active_per_operator": queue.max_active_per_operator,
        "escalation_queue_key": queue.escalation_queue_key,
        "match_reasons": queue.match_reasons,
        "match_intents": queue.match_intents,
        "match_risk_levels": queue.match_risk_levels,
        "routing_order": queue.routing_order,
        "expected_record_version": queue.record_version,
    }
    values.update(changes)
    return HandoffQueueUpsert(**values)


def _configure_operator(service: AgentService, operator_id: str) -> None:
    tenant_id = _tenant(service)
    service.auth.create_admin_operator(
        tenant_id,
        AdminOperatorCreateRequest(
            admin_id=operator_id,
            name=operator_id,
            key=f"operator-key-{operator_id}-0123456789",
        ),
        "admin-test",
    )
    queues = service.handoffs.list_queues(tenant_id=tenant_id)
    service.handoff_staffing.upsert(
        tenant_id=tenant_id,
        operator_id=operator_id,
        value=HandoffOperatorUpsert(
            display_name=operator_id,
            presence="available",
            max_active_tasks=20,
            queue_assignments=[
                HandoffOperatorQueueAssignment(
                    queue_key=queue.queue_key,
                    skill_level=3,
                    is_primary=queue.queue_key == "general",
                )
                for queue in queues
                if queue.status == "active"
            ],
        ),
        actor="admin-test",
    )


def test_default_routing_owner_guards_and_event_complete_lifecycle(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = _tenant(service)
    try:
        _configure_operator(service, "operator-a")
        response = service.chat(
            principal_for(service), "handoff-workbench-lifecycle", "帮我马上退款"
        )
        task = service.handoffs.get(
            tenant_id=tenant_id, handoff_id=response.handoff_id
        )
        assert task.queue_key == "after_sales"
        assert task.priority == "high"
        assert task.sla_status == "on_track"
        assert task.sla_first_response_at
        assert task.sla_resolution_at

        claimed = service.handoffs.claim(
            tenant_id=tenant_id,
            handoff_id=task.id,
            operator="operator-a",
            expected_version=task.version,
            note="开始核对退款条件",
        )
        assert claimed.status == "accepted"
        assert claimed.acknowledged_at
        with pytest.raises(HandoffError, match="assigned operator"):
            service.handoffs.transition(
                tenant_id=tenant_id,
                handoff_id=task.id,
                target_status="working",
                operator="operator-b",
                expected_version=claimed.version,
                note="越权处理",
            )
        working = service.handoffs.transition(
            tenant_id=tenant_id,
            handoff_id=task.id,
            target_status="working",
            operator="operator-a",
            expected_version=claimed.version,
            note="已核对订单",
        )
        waiting = service.handoffs.transition(
            tenant_id=tenant_id,
            handoff_id=task.id,
            target_status="input_required",
            operator="operator-a",
            expected_version=working.version,
            note="等待客户补充凭证",
        )
        resumed = service.handoffs.transition(
            tenant_id=tenant_id,
            handoff_id=task.id,
            target_status="working",
            operator="operator-a",
            expected_version=waiting.version,
            note="客户凭证已收到",
        )
        review = service.handoffs.transition(
            tenant_id=tenant_id,
            handoff_id=task.id,
            target_status="review",
            operator="operator-a",
            expected_version=resumed.version,
            note="提交复核",
        )
        with pytest.raises(HandoffError, match="resolution note"):
            service.handoffs.transition(
                tenant_id=tenant_id,
                handoff_id=task.id,
                target_status="completed",
                operator="reviewer-a",
                expected_version=review.version,
                note=None,
            )
        completed = service.handoffs.transition(
            tenant_id=tenant_id,
            handoff_id=task.id,
            target_status="completed",
            operator="reviewer-a",
            expected_version=review.version,
            note="复核通过并完成退款说明",
        )
        history = service.handoffs.history(
            tenant_id=tenant_id, handoff_id=task.id
        )
        assert completed.status == "completed"
        assert completed.retry_count == 1
        assert completed.started_at and completed.review_started_at
        assert [event.task_version for event in history] == list(
            range(1, completed.version + 1)
        )
        assert [event.event_type for event in history] == [
            "created",
            "claimed",
            "transitioned",
            "transitioned",
            "transitioned",
            "transitioned",
            "transitioned",
        ]
    finally:
        service.close()


def test_claim_is_single_winner_under_concurrency(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = _tenant(service)
    try:
        response = service.chat(
            principal_for(service), "handoff-concurrency", "转人工"
        )

        def claim(index: int) -> str:
            try:
                service.handoffs.claim(
                    tenant_id=tenant_id,
                    handoff_id=response.handoff_id,
                    operator="admin-test",
                    expected_version=1,
                    note=f"并发认领 {index}",
                )
                return "claimed"
            except HandoffError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=12) as executor:
            outcomes = list(executor.map(claim, range(12)))
        assert outcomes.count("claimed") == 1
        assert outcomes.count("conflict") == 11
        task = service.handoffs.get(
            tenant_id=tenant_id, handoff_id=response.handoff_id
        )
        assert task.status == "accepted"
        assert task.version == 2
        assert len(
            service.handoffs.history(
                tenant_id=tenant_id, handoff_id=response.handoff_id
            )
        ) == 2
    finally:
        service.close()


def test_queue_capacity_reassignment_routing_and_optimistic_policy_updates(
    tmp_path,
) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = _tenant(service)
    try:
        for operator_id in ("operator-capacity", "operator-other", "operator-third"):
            _configure_operator(service, operator_id)
        queues = service.handoffs.list_queues(tenant_id=tenant_id)
        after_sales = next(queue for queue in queues if queue.queue_key == "after_sales")
        updated_queue = service.handoffs.upsert_queue(
            tenant_id=tenant_id,
            value=_queue_update(after_sales, max_active_per_operator=1),
            actor="admin-a",
        )
        with pytest.raises(HandoffError, match="version conflict"):
            service.handoffs.upsert_queue(
                tenant_id=tenant_id,
                value=_queue_update(after_sales, max_active_per_operator=2),
                actor="admin-b",
            )
        assert updated_queue.max_active_per_operator == 1

        first = service.chat(
            principal_for(service), "handoff-capacity-1", "帮我退款"
        )
        second = service.chat(
            principal_for(service), "handoff-capacity-2", "帮我退款"
        )
        first_claim = service.handoffs.claim(
            tenant_id=tenant_id,
            handoff_id=first.handoff_id,
            operator="operator-capacity",
            expected_version=1,
            note="认领第一个售后任务",
        )
        with pytest.raises(HandoffError, match="capacity"):
            service.handoffs.claim(
                tenant_id=tenant_id,
                handoff_id=second.handoff_id,
                operator="operator-capacity",
                expected_version=1,
                note="超出容量",
            )
        second_claim = service.handoffs.claim(
            tenant_id=tenant_id,
            handoff_id=second.handoff_id,
            operator="operator-other",
            expected_version=1,
            note="由另一坐席认领",
        )
        with pytest.raises(HandoffError, match="capacity"):
            service.handoffs.reassign(
                tenant_id=tenant_id,
                handoff_id=second.handoff_id,
                assigned_to="operator-capacity",
                expected_version=second_claim.version,
                actor="supervisor",
                note="尝试转派至满负载坐席",
            )
        reassigned = service.handoffs.reassign(
            tenant_id=tenant_id,
            handoff_id=second.handoff_id,
            assigned_to="operator-third",
            expected_version=second_claim.version,
            actor="supervisor",
            note="转派给可用坐席，联系电话 13800138000",
        )
        history = service.handoffs.history(
            tenant_id=tenant_id, handoff_id=second.handoff_id
        )
        assert first_claim.assigned_to == "operator-capacity"
        assert reassigned.assigned_to == "operator-third"
        assert history[-1].event_type == "reassigned"
        assert "13800138000" not in (history[-1].note or "")

        custom = service.handoffs.upsert_queue(
            tenant_id=tenant_id,
            value=HandoffQueueUpsert(
                queue_key="vip",
                name="VIP 专席",
                default_priority="urgent",
                first_response_sla_minutes=3,
                resolution_sla_minutes=30,
                max_active_per_operator=5,
                escalation_queue_key="complaints",
                match_reasons=["vip_*"],
                routing_order=5,
            ),
            actor="admin-a",
        )
        session_id = service.db.resolve_session(
            tenant_id=tenant_id,
            client_id=service.settings.bootstrap_client_id,
            external_session_id="vip-routing",
            subject_hash="vip-subject",
        )
        routed = service.handoffs.create(
            tenant_id=tenant_id,
            session_id=session_id,
            message_id="vip-message-1",
            reason="vip_escalation",
            payload={"intent": "human", "risk_level": "medium"},
        )
        assert custom.queue_key == "vip"
        assert routed.queue_key == "vip"
        assert routed.priority == "urgent"
        assert [item.id for item in service.handoffs.list(
            tenant_id=tenant_id, queue_key="vip"
        )] == [routed.id]
    finally:
        service.close()


def test_sla_scan_escalates_once_per_level_and_summary_reflects_breach(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = _tenant(service)
    try:
        response = service.chat(
            principal_for(service), "handoff-sla", "转人工"
        )
        expired = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        with service.db._write_lock, service.db.connect() as conn:
            conn.execute(
                """
                UPDATE handoff_tasks
                SET sla_first_response_at=?, sla_resolution_at=?
                WHERE id=? AND tenant_id=?
                """,
                (expired, expired, response.handoff_id, tenant_id),
            )
        before = service.handoffs.get(
            tenant_id=tenant_id, handoff_id=response.handoff_id
        )
        assert before.sla_status == "breached"

        first_scan = service.handoffs.escalate_due(tenant_id=tenant_id)
        escalated = service.handoffs.get(
            tenant_id=tenant_id, handoff_id=response.handoff_id
        )
        second_scan = service.handoffs.escalate_due(tenant_id=tenant_id)
        summary = service.handoffs.summary(tenant_id=tenant_id)
        assert first_scan["escalated"] == 1
        assert escalated.escalation_level == 2
        assert escalated.priority == "urgent"
        assert escalated.queue_key == "complaints"
        assert second_scan["escalated"] == 0
        assert second_scan["skipped"] == 1
        assert summary["breached"] == 1
        assert summary["escalated"] == 1
        assert [event.event_type for event in service.handoffs.history(
            tenant_id=tenant_id, handoff_id=response.handoff_id
        )] == ["created", "escalated"]
    finally:
        service.close()


def test_handoff_service_enforces_tenant_scope_on_reads_and_mutations(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = _tenant(service)
    try:
        response = service.chat(
            principal_for(service), "handoff-tenant-scope", "转人工"
        )
        assert service.handoffs.list(tenant_id="tenant-other") == []
        with pytest.raises(HandoffError, match="not found"):
            service.handoffs.get(
                tenant_id="tenant-other", handoff_id=response.handoff_id
            )
        with pytest.raises(HandoffError, match="not found"):
            service.handoffs.claim(
                tenant_id="tenant-other",
                handoff_id=response.handoff_id,
                operator="operator-other",
                expected_version=1,
                note="越权认领",
            )
        untouched = service.handoffs.get(
            tenant_id=tenant_id, handoff_id=response.handoff_id
        )
        assert untouched.status == "proposed"
        assert untouched.version == 1
    finally:
        service.close()


def test_handoff_api_exposes_queue_claim_history_notes_and_conflicts(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    admin_headers = {
        "X-Admin-Id": "admin-test",
        "X-Admin-Key": "test-admin-key-123456",
    }
    client_headers = {
        "X-Client-Id": "client-test",
        "X-Client-Key": "test-client-key-12345",
        "X-Subject-Id": "buyer-handoff-api",
    }
    with TestClient(app) as client:
        queues = client.get("/v1/handoffs/queues", headers=admin_headers)
        assert queues.status_code == 200
        assert {item["queue_key"] for item in queues.json()} >= {
            "general",
            "after_sales",
            "technical",
            "complaints",
        }
        chat = client.post(
            "/v1/chat",
            headers=client_headers,
            json={"session_id": "handoff-api-full", "message": "帮我退款", "context": {}},
        )
        handoff_id = chat.json()["handoff_id"]
        claimed = client.post(
            f"/v1/handoffs/{handoff_id}/claim",
            headers=admin_headers,
            json={"expected_version": 1, "note": "后台认领"},
        )
        assert claimed.status_code == 200
        assert claimed.json()["status"] == "accepted"
        stale = client.post(
            f"/v1/handoffs/{handoff_id}/claim",
            headers=admin_headers,
            json={"expected_version": 1, "note": "重复认领"},
        )
        assert stale.status_code == 409
        noted = client.post(
            f"/v1/handoffs/{handoff_id}/notes",
            headers=admin_headers,
            json={"expected_version": 2, "note": "客户电话 13800138000 已脱敏"},
        )
        assert noted.status_code == 200
        history = client.get(
            f"/v1/handoffs/{handoff_id}/history", headers=admin_headers
        )
        assert history.status_code == 200
        assert [item["event_type"] for item in history.json()] == [
            "created",
            "claimed",
            "note_added",
        ]
        assert "13800138000" not in history.text
        summary = client.get("/v1/handoffs/summary", headers=admin_headers)
        assert summary.status_code == 200
        assert summary.json()["open"] == 1
        invalid_filter = client.get(
            "/v1/handoffs?status=not-a-status", headers=admin_headers
        )
        assert invalid_filter.status_code == 422


def test_handoff_sla_worker_runs_and_reports_health(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        handoff_sla_worker_enabled=True,
        handoff_sla_poll_seconds=0.05,
    )
    service = AgentService(settings)
    tenant_id = _tenant(service)
    try:
        response = service.chat(
            principal_for(service), "handoff-worker", "转人工"
        )
        expired = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        with service.db._write_lock, service.db.connect() as conn:
            conn.execute(
                """
                UPDATE handoff_tasks
                SET sla_first_response_at=?, sla_resolution_at=?
                WHERE id=?
                """,
                (expired, expired, response.handoff_id),
            )
        service.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if service.handoff_sla_worker_status()["escalated"] >= 1:
                break
            time.sleep(0.02)
        status = service.handoff_sla_worker_status()
        ready, detail = service.readiness()
        assert status["running"] is True
        assert status["cycles"] >= 1
        assert status["escalated"] == 1
        assert status["last_error"] is None
        assert ready is True
        assert detail["checks"]["handoff_sla_worker"] is True
        task = service.handoffs.get(
            tenant_id=tenant_id, handoff_id=response.handoff_id
        )
        assert task.escalation_level == 2
    finally:
        service.close()


def test_handoff_negative_policy_and_manual_escalation_paths_fail_closed(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = _tenant(service)
    try:
        _configure_operator(service, "operator-negative")
        response = service.chat(
            principal_for(service), "handoff-negative-paths", "转人工"
        )
        assert len(service.handoffs.list(tenant_id=tenant_id, sla="unassigned")) == 1
        with pytest.raises(HandoffError, match="priority"):
            service.handoffs.list(tenant_id=tenant_id, priority="invalid")
        with pytest.raises(HandoffError, match="SLA filter"):
            service.handoffs.list(tenant_id=tenant_id, sla="invalid")
        with pytest.raises(HandoffError, match="priority"):
            service.handoffs.create(
                tenant_id=tenant_id,
                session_id="unused",
                message_id="unused",
                reason="unused",
                payload={},
                priority="invalid",
            )
        accepted = service.handoffs.transition(
            tenant_id=tenant_id,
            handoff_id=response.handoff_id,
            target_status="accepted",
            operator="operator-negative",
            expected_version=1,
            note="通过兼容流转接口认领",
        )
        level_one = service.handoffs.escalate(
            tenant_id=tenant_id,
            handoff_id=response.handoff_id,
            expected_version=accepted.version,
            actor="supervisor",
            note="首次手工升级",
        )
        assert level_one.escalation_level == 1
        assert level_one.priority == "high"
        level_two = service.handoffs.escalate(
            tenant_id=tenant_id,
            handoff_id=response.handoff_id,
            expected_version=level_one.version,
            actor="supervisor",
            note="二次手工升级",
            queue_key="complaints",
        )
        assert level_two.escalation_level == 2
        assert level_two.priority == "urgent"
        with pytest.raises(HandoffError, match="already escalated"):
            service.handoffs.escalate(
                tenant_id=tenant_id,
                handoff_id=response.handoff_id,
                expected_version=level_two.version,
                actor="supervisor",
                note="禁止三级升级",
            )
        with pytest.raises(HandoffError, match="required"):
            service.handoffs.add_note(
                tenant_id=tenant_id,
                handoff_id=response.handoff_id,
                expected_version=level_two.version,
                actor="supervisor",
                note=" ",
            )

        queues = service.handoffs.list_queues(tenant_id=tenant_id)
        general = next(queue for queue in queues if queue.queue_key == "general")
        with pytest.raises(HandoffError, match="itself"):
            service.handoffs.upsert_queue(
                tenant_id=tenant_id,
                value=_queue_update(general, escalation_queue_key="general"),
                actor="admin-negative",
            )
        with pytest.raises(HandoffError, match="not found or inactive"):
            service.handoffs.upsert_queue(
                tenant_id=tenant_id,
                value=_queue_update(general, escalation_queue_key="missing"),
                actor="admin-negative",
            )
        with pytest.raises(HandoffError, match="catch-all"):
            service.handoffs.upsert_queue(
                tenant_id=tenant_id,
                value=_queue_update(general, status="inactive"),
                actor="admin-negative",
            )
        with pytest.raises(HandoffError, match="routing token"):
            service.handoffs.upsert_queue(
                tenant_id=tenant_id,
                value=_queue_update(general, match_reasons=["invalid token"]),
                actor="admin-negative",
            )
        with pytest.raises(HandoffError, match="version conflict"):
            service.handoffs.upsert_queue(
                tenant_id=tenant_id,
                value=HandoffQueueUpsert(
                    queue_key="new-with-version",
                    name="错误版本新队列",
                    expected_record_version=2,
                ),
                actor="admin-negative",
            )
    finally:
        service.close()


def test_handoff_retry_budget_terminal_escalation_and_corrupt_routing_are_blocked(
    tmp_path,
) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = _tenant(service)
    try:
        _configure_operator(service, "operator-retry")
        principal = principal_for(service)
        session_id = service.db.resolve_session(
            tenant_id=tenant_id,
            client_id=principal.client_id,
            external_session_id="handoff-retry-budget",
            subject_hash=principal.subject_hash,
        )
        task = service.handoffs.create(
            tenant_id=tenant_id,
            session_id=session_id,
            message_id="handoff-retry-message",
            reason="customer_requested_human",
            payload={"intent": "human", "risk_level": "medium"},
            max_retries=0,
        )
        with pytest.raises(HandoffError, match="claimed tasks"):
            service.handoffs.reassign(
                tenant_id=tenant_id,
                handoff_id=task.id,
                assigned_to="operator-retry",
                expected_version=task.version,
                actor="supervisor",
                note="未认领任务不可转派",
            )
        claimed = service.handoffs.claim(
            tenant_id=tenant_id,
            handoff_id=task.id,
            operator="operator-retry",
            expected_version=task.version,
            note="认领重试预算任务",
        )
        working = service.handoffs.transition(
            tenant_id=tenant_id,
            handoff_id=task.id,
            target_status="working",
            operator="operator-retry",
            expected_version=claimed.version,
            note="开始处理",
        )
        waiting = service.handoffs.transition(
            tenant_id=tenant_id,
            handoff_id=task.id,
            target_status="input_required",
            operator="operator-retry",
            expected_version=working.version,
            note="等待补充",
        )
        with pytest.raises(HandoffError, match="retry budget exhausted"):
            service.handoffs.transition(
                tenant_id=tenant_id,
                handoff_id=task.id,
                target_status="working",
                operator="operator-retry",
                expected_version=waiting.version,
                note="预算耗尽",
            )
        failed = service.handoffs.transition(
            tenant_id=tenant_id,
            handoff_id=task.id,
            target_status="failed",
            operator="operator-retry",
            expected_version=waiting.version,
            note="补充次数耗尽，转业务复核",
        )
        with pytest.raises(HandoffError, match="terminal"):
            service.handoffs.escalate(
                tenant_id=tenant_id,
                handoff_id=task.id,
                expected_version=failed.version,
                actor="supervisor",
                note="终态禁止升级",
            )
        with pytest.raises(HandoffError, match="idempotency scope"):
            service.handoffs.create(
                tenant_id="tenant-other",
                session_id=session_id,
                message_id="handoff-retry-message",
                reason="customer_requested_human",
                payload={},
            )

        with service.db._write_lock, service.db.connect() as conn:
            conn.execute(
                "UPDATE handoff_queues SET status='inactive' WHERE tenant_id=?",
                (tenant_id,),
            )
        with pytest.raises(HandoffError, match="no active handoff queue"):
            service.handoffs.create(
                tenant_id=tenant_id,
                session_id=session_id,
                message_id="handoff-no-route",
                reason="no_route",
                payload={},
            )
    finally:
        service.close()
