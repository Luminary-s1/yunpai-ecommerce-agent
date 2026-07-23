from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.auth import (
    AdminOperatorCreateRequest,
    AdminOperatorStatusRequest,
    AuthError,
)
from ecommerce_agent.handoff import HandoffError
from ecommerce_agent.handoff_staffing import StaffingError
from ecommerce_agent.schemas import (
    HandoffOperatorPresenceUpdate,
    HandoffOperatorQueueAssignment,
    HandoffOperatorUpsert,
    HandoffQueueUpsert,
)
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


def _add_operator(
    service: AgentService,
    operator_id: str,
    *,
    max_active_tasks: int = 20,
    primary_queue: str = "general",
    skill_level: int = 3,
):
    tenant_id = service.settings.bootstrap_tenant_id
    service.auth.create_admin_operator(
        tenant_id,
        AdminOperatorCreateRequest(
            admin_id=operator_id,
            name=operator_id,
            key=f"staffing-key-{operator_id}-0123456789",
        ),
        "admin-test",
    )
    queues = service.handoffs.list_queues(tenant_id=tenant_id)
    return service.handoff_staffing.upsert(
        tenant_id=tenant_id,
        operator_id=operator_id,
        value=HandoffOperatorUpsert(
            display_name=operator_id,
            presence="available",
            max_active_tasks=max_active_tasks,
            skills=["refund", "complaint"],
            queue_assignments=[
                HandoffOperatorQueueAssignment(
                    queue_key=queue.queue_key,
                    skill_level=skill_level,
                    is_primary=queue.queue_key == primary_queue,
                )
                for queue in queues
                if queue.status == "active"
            ],
        ),
        actor="admin-test",
    )


def test_presence_lease_and_queue_membership_gate_claims(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = service.settings.bootstrap_tenant_id
    try:
        profile = _add_operator(service, "operator-lease")
        expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        with service.db._write_lock, service.db.connect() as conn:
            conn.execute(
                """
                UPDATE handoff_operator_profiles SET presence_expires_at=?
                WHERE tenant_id=? AND admin_id=?
                """,
                (expired, tenant_id, "operator-lease"),
            )
        listed = service.handoff_staffing.get(
            tenant_id=tenant_id, operator_id="operator-lease"
        )
        assert listed is not None
        assert listed.configured_presence == "available"
        assert listed.effective_presence == "offline"
        task = service.chat(principal_for(service), "lease-task", "帮我退款")
        with pytest.raises(HandoffError, match="not currently available"):
            service.handoffs.claim(
                tenant_id=tenant_id,
                handoff_id=task.handoff_id,
                operator="operator-lease",
                expected_version=1,
                note="expired lease must fail closed",
            )
        renewed = service.handoff_staffing.update_presence(
            tenant_id=tenant_id,
            operator_id="operator-lease",
            value=HandoffOperatorPresenceUpdate(
                presence="available",
                presence_ttl_seconds=600,
                expected_record_version=profile.record_version,
            ),
            actor="operator-lease",
        )
        claimed = service.handoffs.claim(
            tenant_id=tenant_id,
            handoff_id=task.handoff_id,
            operator="operator-lease",
            expected_version=1,
            note="lease renewed; customer phone 13800138000",
        )
        assert renewed.effective_presence == "available"
        assert claimed.assigned_operator_name == "operator-lease"
        assert claimed.assigned_operator_presence == "available"
        assert "13800138000" not in (
            service.handoffs.history(
                tenant_id=tenant_id, handoff_id=task.handoff_id
            )[-1].note
            or ""
        )
    finally:
        service.close()


def test_expired_bootstrap_presence_is_not_resurrected_on_restart(tmp_path) -> None:
    settings = make_settings(tmp_path)
    service = AgentService(settings)
    tenant_id = settings.bootstrap_tenant_id
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    try:
        with service.db._write_lock, service.db.connect() as conn:
            conn.execute(
                """
                UPDATE handoff_operator_profiles SET presence_expires_at=?
                WHERE tenant_id=? AND admin_id=?
                """,
                (expired, tenant_id, settings.bootstrap_admin_id),
            )
    finally:
        service.close()

    restarted = AgentService(settings)
    try:
        profile = restarted.handoff_staffing.get(
            tenant_id=tenant_id, operator_id=settings.bootstrap_admin_id
        )
        assert profile is not None
        assert profile.configured_presence == "available"
        assert profile.effective_presence == "offline"
    finally:
        restarted.close()


def test_auto_assignment_is_deterministic_and_load_aware(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = service.settings.bootstrap_tenant_id
    try:
        _add_operator(
            service,
            "operator-specialist",
            max_active_tasks=2,
            primary_queue="after_sales",
            skill_level=5,
        )
        first = service.chat(principal_for(service), "assign-first", "帮我退款")
        selected_first = service.handoffs.auto_assign(
            tenant_id=tenant_id,
            handoff_id=first.handoff_id,
            expected_version=1,
            actor="admin-test",
            note="select best available operator",
        )
        second = service.chat(principal_for(service), "assign-second", "帮我退款")
        selected_second = service.handoffs.auto_assign(
            tenant_id=tenant_id,
            handoff_id=second.handoff_id,
            expected_version=1,
            actor="admin-test",
            note="rebalance by current load",
        )
        assert selected_first.assigned_to == "operator-specialist"
        assert selected_second.assigned_to == "admin-test"
        assert service.handoffs.history(
            tenant_id=tenant_id, handoff_id=first.handoff_id
        )[-1].event_type == "claimed"
        summary = service.handoffs.summary(tenant_id=tenant_id)
        queue = next(item for item in summary["queues"] if item["queue_key"] == "after_sales")
        assert summary["operators"]["available"] == 2
        assert queue["total_operators"] == 2
        assert queue["available_operators"] == 2
    finally:
        service.close()


def test_assigned_operator_credential_cannot_be_disabled(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = service.settings.bootstrap_tenant_id
    try:
        _add_operator(service, "operator-disable")
        task = service.chat(principal_for(service), "disable-task", "转人工")
        claimed = service.handoffs.claim(
            tenant_id=tenant_id,
            handoff_id=task.handoff_id,
            operator="operator-disable",
            expected_version=1,
            note="claim before credential rotation",
        )
        request = AdminOperatorStatusRequest(
            expected_status="active", reason="credential rotation"
        )
        with pytest.raises(AuthError, match="must be reassigned first"):
            service.auth.disable_admin_operator(
                tenant_id, "operator-disable", request, "admin-test"
            )
        service.handoffs.transition(
            tenant_id=tenant_id,
            handoff_id=task.handoff_id,
            target_status="failed",
            operator="operator-disable",
            expected_version=claimed.version,
            note="closed before credential rotation",
        )
        disabled = service.auth.disable_admin_operator(
            tenant_id, "operator-disable", request, "admin-test"
        )
        profile = service.handoff_staffing.get(
            tenant_id=tenant_id, operator_id="operator-disable"
        )
        assert disabled["status"] == "disabled"
        assert profile is not None
        assert profile.status == "inactive"
        assert profile.effective_presence == "offline"
    finally:
        service.close()


def test_staffing_and_auto_assignment_api(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    admin_headers = {
        "X-Admin-Id": "admin-test",
        "X-Admin-Key": "test-admin-key-123456",
    }
    client_headers = {
        "X-Client-Id": "client-test",
        "X-Client-Key": "test-client-key-12345",
        "X-Subject-Id": "buyer-staffing-api",
    }
    with TestClient(app) as client:
        operators = client.get("/v1/handoffs/operators", headers=admin_headers)
        assert operators.status_code == 200
        profile = operators.json()[0]
        assert profile["operator_id"] == "admin-test"
        away = client.post(
            "/v1/handoffs/operators/admin-test/presence",
            headers=admin_headers,
            json={
                "presence": "away",
                "presence_ttl_seconds": 600,
                "expected_record_version": profile["record_version"],
            },
        )
        assert away.status_code == 200
        chat = client.post(
            "/v1/chat",
            headers=client_headers,
            json={"session_id": "staffing-api", "message": "转人工", "context": {}},
        ).json()
        unavailable = client.post(
            f"/v1/handoffs/{chat['handoff_id']}/assign-best",
            headers=admin_headers,
            json={"expected_version": 1, "note": "attempt while away"},
        )
        assert unavailable.status_code == 409
        available = client.post(
            "/v1/handoffs/operators/admin-test/presence",
            headers=admin_headers,
            json={
                "presence": "available",
                "presence_ttl_seconds": 600,
                "expected_record_version": away.json()["record_version"],
            },
        )
        assert available.status_code == 200
        assigned = client.post(
            f"/v1/handoffs/{chat['handoff_id']}/assign-best",
            headers=admin_headers,
            json={"expected_version": 1, "note": "assign after presence renewal"},
        )
        assert assigned.status_code == 200
        assert assigned.json()["assigned_to"] == "admin-test"
        assert assigned.json()["assigned_operator_presence"] == "available"


def test_staffing_configuration_and_presence_fail_closed(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = service.settings.bootstrap_tenant_id
    try:
        general = next(
            queue
            for queue in service.handoffs.list_queues(tenant_id=tenant_id)
            if queue.queue_key == "general"
        )
        assignment = HandoffOperatorQueueAssignment(
            queue_key="general", skill_level=3, is_primary=True
        )
        with pytest.raises(StaffingError, match="invalid operator skill token"):
            service.handoff_staffing.upsert(
                tenant_id=tenant_id,
                operator_id="admin-test",
                value=HandoffOperatorUpsert(
                    display_name="Admin Test",
                    skills=["invalid skill"],
                    queue_assignments=[assignment],
                    expected_record_version=1,
                ),
                actor="admin-test",
            )
        with pytest.raises(StaffingError, match="credential is not active"):
            service.handoff_staffing.ensure_bootstrap_operator(
                tenant_id=tenant_id,
                operator_id="missing-admin",
                display_name="Missing Admin",
            )
        service.auth.create_admin_operator(
            "tenant-empty",
            AdminOperatorCreateRequest(
                admin_id="empty-admin",
                name="Empty Admin",
                key="empty-tenant-admin-key-0123456789",
            ),
            "admin-test",
        )
        with pytest.raises(StaffingError, match="active handoff queue"):
            service.handoff_staffing.ensure_bootstrap_operator(
                tenant_id="tenant-empty",
                operator_id="empty-admin",
                display_name="Empty Admin",
            )
        with pytest.raises(StaffingError, match="administrator credential"):
            service.handoff_staffing.upsert(
                tenant_id=tenant_id,
                operator_id="missing-admin",
                value=HandoffOperatorUpsert(
                    display_name="Missing Admin",
                    queue_assignments=[assignment],
                ),
                actor="admin-test",
            )
        service.auth.create_admin_operator(
            tenant_id,
            AdminOperatorCreateRequest(
                admin_id="operator-unconfigured",
                name="Operator Unconfigured",
                key="unconfigured-operator-key-0123456789",
            ),
            "admin-test",
        )
        with pytest.raises(StaffingError, match="version conflict"):
            service.handoff_staffing.upsert(
                tenant_id=tenant_id,
                operator_id="operator-unconfigured",
                value=HandoffOperatorUpsert(
                    display_name="Operator Unconfigured",
                    queue_assignments=[assignment],
                    expected_record_version=2,
                ),
                actor="admin-test",
            )
        with pytest.raises(StaffingError, match="missing or inactive"):
            service.handoff_staffing.upsert(
                tenant_id=tenant_id,
                operator_id="operator-unconfigured",
                value=HandoffOperatorUpsert(
                    display_name="Operator Unconfigured",
                    queue_assignments=[
                        HandoffOperatorQueueAssignment(queue_key="missing-queue")
                    ],
                ),
                actor="admin-test",
            )
        bootstrap = service.handoff_staffing.get(
            tenant_id=tenant_id, operator_id="admin-test"
        )
        assert bootstrap is not None
        with pytest.raises(StaffingError, match="version conflict"):
            service.handoff_staffing.upsert(
                tenant_id=tenant_id,
                operator_id="admin-test",
                value=HandoffOperatorUpsert(
                    display_name="Admin Test",
                    queue_assignments=[assignment],
                    expected_record_version=bootstrap.record_version + 1,
                ),
                actor="admin-test",
            )
        with pytest.raises(StaffingError, match="profile not found"):
            service.handoff_staffing.update_presence(
                tenant_id=tenant_id,
                operator_id="operator-unconfigured",
                value=HandoffOperatorPresenceUpdate(
                    presence="available", expected_record_version=1
                ),
                actor="operator-unconfigured",
            )
        with pytest.raises(StaffingError, match="invalid operator status"):
            service.handoff_staffing.list(tenant_id=tenant_id, status="blocked")
        with pytest.raises(StaffingError, match="invalid operator presence"):
            service.handoff_staffing.list(tenant_id=tenant_id, presence="busy")

        profile = _add_operator(service, "operator-inactive")
        task = service.chat(principal_for(service), "inactive-block", "转人工")
        service.handoffs.claim(
            tenant_id=tenant_id,
            handoff_id=task.handoff_id,
            operator="operator-inactive",
            expected_version=1,
            note="active assignment blocks profile disable",
        )
        assignments = [
            HandoffOperatorQueueAssignment(
                queue_key=item.queue_key,
                skill_level=item.skill_level,
                is_primary=item.is_primary,
            )
            for item in profile.queue_assignments
        ]
        inactive_value = HandoffOperatorUpsert(
            display_name=profile.display_name,
            status="inactive",
            presence="available",
            max_active_tasks=profile.max_active_tasks,
            skills=["refund", "refund", "complaint"],
            queue_assignments=assignments,
            expected_record_version=profile.record_version,
        )
        with pytest.raises(StaffingError, match="reassign active handoff tasks"):
            service.handoff_staffing.upsert(
                tenant_id=tenant_id,
                operator_id="operator-inactive",
                value=inactive_value,
                actor="admin-test",
            )
        claimed = service.handoffs.get(
            tenant_id=tenant_id, handoff_id=task.handoff_id
        )
        service.handoffs.transition(
            tenant_id=tenant_id,
            handoff_id=task.handoff_id,
            target_status="failed",
            operator="operator-inactive",
            expected_version=claimed.version,
            note="close before disabling staffing profile",
        )
        inactive = service.handoff_staffing.upsert(
            tenant_id=tenant_id,
            operator_id="operator-inactive",
            value=inactive_value,
            actor="admin-test",
        )
        assert inactive.configured_presence == "offline"
        assert inactive.presence_expires_at is None
        assert service.handoff_staffing.list(
            tenant_id=tenant_id, status="inactive", presence="offline"
        )[0].operator_id == "operator-inactive"
        assert service.handoff_staffing.list(
            tenant_id=tenant_id, queue_key=general.queue_key
        )
        with pytest.raises(StaffingError, match="inactive operator"):
            service.handoff_staffing.update_presence(
                tenant_id=tenant_id,
                operator_id="operator-inactive",
                value=HandoffOperatorPresenceUpdate(
                    presence="available",
                    expected_record_version=inactive.record_version,
                ),
                actor="operator-inactive",
            )
        assert service.handoff_staffing.get(
            tenant_id=tenant_id, operator_id="not-found"
        ) is None
        assert service.handoff_staffing._is_expired(None, datetime.now(UTC)) is True
        assert service.handoff_staffing._is_expired(
            "not-a-timestamp", datetime.now(UTC)
        ) is True
        assert service.handoff_staffing._is_expired(
            "2020-01-01T00:00:00", datetime.now(UTC)
        ) is True
        assert service.handoff_staffing._load_list("not-json") == []
        assert service.handoff_staffing._load_list("{}") == []
    finally:
        service.close()


def test_eligibility_rejects_membership_credentials_and_capacity(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = service.settings.bootstrap_tenant_id
    try:
        limited = _add_operator(service, "operator-limited", max_active_tasks=1)
        limited = service.handoff_staffing.upsert(
            tenant_id=tenant_id,
            operator_id="operator-limited",
            value=HandoffOperatorUpsert(
                display_name=limited.display_name,
                presence="available",
                max_active_tasks=1,
                queue_assignments=[
                    HandoffOperatorQueueAssignment(
                        queue_key="general", skill_level=4, is_primary=True
                    )
                ],
                expected_record_version=limited.record_version,
            ),
            actor="admin-test",
        )
        first = service.chat(principal_for(service), "limited-first", "转人工")
        second = service.chat(principal_for(service), "limited-second", "转人工")
        service.handoffs.claim(
            tenant_id=tenant_id,
            handoff_id=first.handoff_id,
            operator="operator-limited",
            expected_version=1,
            note="fill global capacity",
        )
        with pytest.raises(HandoffError, match="global active-task capacity"):
            service.handoffs.claim(
                tenant_id=tenant_id,
                handoff_id=second.handoff_id,
                operator="operator-limited",
                expected_version=1,
                note="must reject global overflow",
            )
        after_sales = service.chat(
            principal_for(service), "limited-membership", "帮我退款"
        )
        with pytest.raises(HandoffError, match="not assigned to this handoff queue"):
            service.handoffs.claim(
                tenant_id=tenant_id,
                handoff_id=after_sales.handoff_id,
                operator="operator-limited",
                expected_version=1,
                note="must reject missing membership",
            )
        general_queue = next(
            queue
            for queue in service.handoffs.list_queues(tenant_id=tenant_id)
            if queue.queue_key == "general"
        )
        with service.db.connect() as conn:
            candidates = service.handoff_staffing.rank_candidates(
                conn, tenant_id=tenant_id, queue_id=general_queue.id
            )
        assert "operator-limited" not in {item["admin_id"] for item in candidates}

        disabled = _add_operator(service, "operator-disabled")
        service.auth.disable_admin_operator(
            tenant_id,
            "operator-disabled",
            AdminOperatorStatusRequest(
                expected_status="active", reason="credential retired"
            ),
            "admin-test",
        )
        disabled_task = service.chat(
            principal_for(service), "disabled-claim", "转人工"
        )
        with pytest.raises(HandoffError, match="operator is disabled"):
            service.handoffs.claim(
                tenant_id=tenant_id,
                handoff_id=disabled_task.handoff_id,
                operator=disabled.operator_id,
                expected_version=1,
                note="disabled credential cannot claim",
            )

        queue_limited = _add_operator(service, "operator-queue-limited")
        service.handoffs.upsert_queue(
            tenant_id=tenant_id,
            value=HandoffQueueUpsert(
                queue_key=general_queue.queue_key,
                name=general_queue.name,
                description=general_queue.description,
                status=general_queue.status,
                default_priority=general_queue.default_priority,
                first_response_sla_minutes=general_queue.first_response_sla_minutes,
                resolution_sla_minutes=general_queue.resolution_sla_minutes,
                max_active_per_operator=1,
                escalation_queue_key=general_queue.escalation_queue_key,
                match_reasons=general_queue.match_reasons,
                match_intents=general_queue.match_intents,
                match_risk_levels=general_queue.match_risk_levels,
                routing_order=general_queue.routing_order,
                expected_record_version=general_queue.record_version,
            ),
            actor="admin-test",
        )
        queue_task = service.chat(principal_for(service), "queue-capacity", "转人工")
        service.handoffs.claim(
            tenant_id=tenant_id,
            handoff_id=queue_task.handoff_id,
            operator=queue_limited.operator_id,
            expected_version=1,
            note="fill queue capacity",
        )
        with service.db.connect() as conn:
            candidates = service.handoff_staffing.rank_candidates(
                conn, tenant_id=tenant_id, queue_id=general_queue.id
            )
        assert "operator-queue-limited" not in {
            item["admin_id"] for item in candidates
        }
    finally:
        service.close()
