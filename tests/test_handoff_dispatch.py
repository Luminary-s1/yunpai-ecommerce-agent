from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.database import Database
from ecommerce_agent.handoff import HandoffError, HandoffService
from ecommerce_agent.handoff_dispatch import DispatchError, HandoffDispatchService
from ecommerce_agent.handoff_staffing import HandoffStaffingService, StaffingError
from ecommerce_agent.schemas import (
    HandoffDispatchAlertAction,
    HandoffDispatchRetryRequest,
    HandoffOperatorHeartbeat,
    HandoffOperatorPresenceUpdate,
    HandoffOperatorQueueAssignment,
    HandoffOperatorUpsert,
    HandoffPresenceSessionStart,
    HandoffShiftCancelRequest,
    HandoffShiftCreate,
)
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}
CLIENT_HEADERS = {
    "X-Client-Id": "client-test",
    "X-Client-Key": "test-client-key-12345",
    "X-Subject-Id": "buyer-dispatch",
}


def _configure_bootstrap(
    service: AgentService,
    *,
    schedule_mode: str = "unrestricted",
    dispatch_mode: str = "automatic",
    presence: str = "available",
):
    tenant_id = service.settings.bootstrap_tenant_id
    profile = service.handoff_staffing.get(
        tenant_id=tenant_id, operator_id=service.settings.bootstrap_admin_id
    )
    assert profile is not None
    return service.handoff_staffing.upsert(
        tenant_id=tenant_id,
        operator_id=service.settings.bootstrap_admin_id,
        value=HandoffOperatorUpsert(
            display_name=profile.display_name,
            presence=presence,
            dispatch_mode=dispatch_mode,
            schedule_mode=schedule_mode,
            max_active_tasks=profile.max_active_tasks,
            skills=profile.skills,
            queue_assignments=[
                HandoffOperatorQueueAssignment(
                    queue_key=item.queue_key,
                    skill_level=item.skill_level,
                    is_primary=item.is_primary,
                )
                for item in profile.queue_assignments
            ],
            expected_record_version=profile.record_version,
        ),
        actor="admin-test",
    )


def _handoff(service: AgentService, session_id: str = "dispatch-task"):
    answer = service.chat(principal_for(service), session_id, "我要转人工处理")
    assert answer.handoff_id
    return service.handoffs.get(
        tenant_id=service.settings.bootstrap_tenant_id,
        handoff_id=answer.handoff_id,
    )


def test_scheduled_operator_is_ineligible_off_shift_and_dispatch_recovers_on_shift(
    tmp_path,
) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = service.settings.bootstrap_tenant_id
    operator_id = service.settings.bootstrap_admin_id
    try:
        profile = _configure_bootstrap(service, schedule_mode="scheduled")
        assert profile.on_shift is False
        task = _handoff(service)
        with pytest.raises(HandoffError, match="outside the configured shift"):
            service.handoffs.claim(
                tenant_id=tenant_id,
                handoff_id=task.id,
                operator=operator_id,
                expected_version=task.version,
                note="off-shift claim must fail",
            )

        waiting = service.handoff_dispatch.run_once(worker_id="test-dispatch", limit=1)
        assert waiting["waiting"] == 1
        alert = service.handoff_dispatch.list_alerts(
            tenant_id=tenant_id, status="open"
        )[0]
        assert alert.reason == "no_available_operator"

        now = datetime.now(UTC)
        shift = service.handoff_staffing.create_shift(
            tenant_id=tenant_id,
            operator_id=operator_id,
            value=HandoffShiftCreate(
                starts_at=now - timedelta(minutes=5),
                ends_at=now + timedelta(hours=1),
            ),
            actor="admin-test",
        )
        assert shift.status == "scheduled"
        assert service.handoff_staffing.get(
            tenant_id=tenant_id, operator_id=operator_id
        ).on_shift
        assigned = service.handoff_dispatch.run_once(
            worker_id="test-dispatch", limit=1
        )
        assert assigned["assigned"] == 1
        task_after = service.handoffs.get(tenant_id=tenant_id, handoff_id=task.id)
        assert task_after.assigned_to == operator_id
        assert service.handoff_dispatch.list_alerts(tenant_id=tenant_id)[0].status == "resolved"
    finally:
        service.close()


def test_shift_validation_overlap_cancel_and_utc_normalization(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = service.settings.bootstrap_tenant_id
    operator_id = service.settings.bootstrap_admin_id
    try:
        _configure_bootstrap(service, schedule_mode="scheduled")
        start = datetime.now(UTC) + timedelta(hours=1)
        shift = service.handoff_staffing.create_shift(
            tenant_id=tenant_id,
            operator_id=operator_id,
            value=HandoffShiftCreate(starts_at=start, ends_at=start + timedelta(hours=8)),
            actor="admin-test",
        )
        assert shift.starts_at.endswith("+00:00")
        with pytest.raises(StaffingError, match="overlaps"):
            service.handoff_staffing.create_shift(
                tenant_id=tenant_id,
                operator_id=operator_id,
                value=HandoffShiftCreate(
                    starts_at=start + timedelta(hours=1),
                    ends_at=start + timedelta(hours=2),
                ),
                actor="admin-test",
            )
        cancelled = service.handoff_staffing.cancel_shift(
            tenant_id=tenant_id,
            operator_id=operator_id,
            shift_id=shift.id,
            value=HandoffShiftCancelRequest(
                expected_record_version=shift.record_version,
                note="班次调整，手机号 13800138000",
            ),
            actor="admin-test",
        )
        assert cancelled.status == "cancelled"
        replacement = service.handoff_staffing.create_shift(
            tenant_id=tenant_id,
            operator_id=operator_id,
            value=HandoffShiftCreate(starts_at=start, ends_at=start + timedelta(hours=8)),
            actor="admin-test",
        )
        assert replacement.status == "scheduled"
        with pytest.raises(ValueError, match="UTC offset"):
            HandoffShiftCreate(
                starts_at=datetime.now(), ends_at=datetime.now() + timedelta(hours=1)
            )
    finally:
        service.close()


def test_recurring_shift_api_creates_weekly_windows_atomically(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        start = datetime.now(UTC) + timedelta(days=1)
        payload = {
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=8)).isoformat(),
            "repeat_every_weeks": 1,
            "occurrences": 3,
        }

        created = client.post(
            "/v1/handoffs/operators/admin-test/shifts/recurring",
            headers=ADMIN_HEADERS,
            json=payload,
        )

        assert created.status_code == 201
        shifts = created.json()
        assert len(shifts) == 3
        starts = [datetime.fromisoformat(item["starts_at"]) for item in shifts]
        assert starts[1] - starts[0] == timedelta(weeks=1)
        assert starts[2] - starts[1] == timedelta(weeks=1)
        invalid = client.post(
            "/v1/handoffs/operators/admin-test/shifts/recurring",
            headers=ADMIN_HEADERS,
            json={**payload, "occurrences": 1},
        )
        assert invalid.status_code == 422

        conflict = client.post(
            "/v1/handoffs/operators/admin-test/shifts/recurring",
            headers=ADMIN_HEADERS,
            json={
                **payload,
                "starts_at": (start - timedelta(weeks=1)).isoformat(),
                "ends_at": (
                    start - timedelta(weeks=1) + timedelta(hours=8)
                ).isoformat(),
            },
        )
        assert conflict.status_code == 409

        listed = client.get(
            "/v1/handoffs/operators/admin-test/shifts",
            headers=ADMIN_HEADERS,
        )
        assert listed.status_code == 200
        assert len(listed.json()) == 3
        audit = client.get(
            "/v1/admin/audit?event_type=handoff.operator_recurring_shifts_created",
            headers=ADMIN_HEADERS,
        )
        assert audit.status_code == 200
        assert audit.json()[0]["detail"]["occurrences"] == 3


def test_presence_session_heartbeat_is_sequenced_idempotent_and_config_independent(
    tmp_path,
) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = service.settings.bootstrap_tenant_id
    operator_id = service.settings.bootstrap_admin_id
    try:
        profile = service.handoff_staffing.get(
            tenant_id=tenant_id, operator_id=operator_id
        )
        assert profile is not None
        started = service.handoff_staffing.start_presence_session(
            tenant_id=tenant_id,
            operator_id=operator_id,
            value=HandoffPresenceSessionStart(
                session_id="console-session-0001",
                presence="available",
                presence_ttl_seconds=120,
                expected_record_version=profile.record_version,
            ),
            actor=operator_id,
        )
        record_version = started.operator.record_version
        beat = service.handoff_staffing.heartbeat(
            tenant_id=tenant_id,
            operator_id=operator_id,
            value=HandoffOperatorHeartbeat(
                session_id=started.session_id,
                sequence=1,
                presence="available",
                presence_ttl_seconds=120,
                expected_presence_version=started.presence_version,
            ),
            actor=operator_id,
        )
        assert beat.sequence == 1
        assert beat.operator.record_version == record_version
        assert beat.operator.presence_version == started.presence_version + 1

        duplicate = service.handoff_staffing.heartbeat(
            tenant_id=tenant_id,
            operator_id=operator_id,
            value=HandoffOperatorHeartbeat(
                session_id=started.session_id,
                sequence=1,
                presence="available",
                presence_ttl_seconds=120,
                expected_presence_version=started.presence_version,
            ),
            actor=operator_id,
        )
        assert duplicate.presence_version == beat.presence_version
        assert duplicate.presence_expires_at == beat.presence_expires_at
        with pytest.raises(StaffingError, match="contiguous"):
            service.handoff_staffing.heartbeat(
                tenant_id=tenant_id,
                operator_id=operator_id,
                value=HandoffOperatorHeartbeat(
                    session_id=started.session_id,
                    sequence=3,
                    presence="available",
                    presence_ttl_seconds=120,
                    expected_presence_version=beat.presence_version,
                ),
                actor=operator_id,
            )
        with pytest.raises(StaffingError, match="session mismatch"):
            service.handoff_staffing.heartbeat(
                tenant_id=tenant_id,
                operator_id=operator_id,
                value=HandoffOperatorHeartbeat(
                    session_id="different-session-0002",
                    sequence=2,
                    presence="available",
                    presence_ttl_seconds=120,
                    expected_presence_version=beat.presence_version,
                ),
                actor=operator_id,
            )
    finally:
        service.close()


def test_manual_dispatch_mode_is_excluded_from_worker_but_allows_explicit_assignment(
    tmp_path,
) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = service.settings.bootstrap_tenant_id
    try:
        _configure_bootstrap(service, dispatch_mode="manual")
        task = _handoff(service, "manual-mode")
        report = service.handoff_dispatch.run_once(worker_id="test-dispatch", limit=1)
        assert report["waiting"] == 1
        manually_assigned = service.handoffs.auto_assign(
            tenant_id=tenant_id,
            handoff_id=task.id,
            expected_version=task.version,
            actor="admin-test",
            note="supervisor explicitly requested assignment",
        )
        assert manually_assigned.assigned_to == "admin-test"
        job = service.handoff_dispatch.list_jobs(tenant_id=tenant_id)[0]
        assert job.status == "assigned"
        assert job.assigned_to == "admin-test"
    finally:
        service.close()


def test_dispatch_alert_acknowledgement_reopens_and_resolves_without_sensitive_note(
    tmp_path,
) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = service.settings.bootstrap_tenant_id
    operator_id = service.settings.bootstrap_admin_id
    try:
        profile = service.handoff_staffing.get(
            tenant_id=tenant_id, operator_id=operator_id
        )
        assert profile is not None
        away = service.handoff_staffing.update_presence(
            tenant_id=tenant_id,
            operator_id=operator_id,
            value=HandoffOperatorPresenceUpdate(
                presence="away",
                presence_ttl_seconds=600,
                expected_record_version=profile.record_version,
            ),
            actor=operator_id,
        )
        _handoff(service, "alert-lifecycle")
        service.handoff_dispatch.run_once(worker_id="test-dispatch", limit=1)
        alert = service.handoff_dispatch.list_alerts(tenant_id=tenant_id)[0]
        acknowledged = service.handoff_dispatch.acknowledge_alert(
            tenant_id=tenant_id,
            alert_id=alert.id,
            value=HandoffDispatchAlertAction(
                expected_record_version=alert.record_version,
                note="联系值班主管 13800138000",
            ),
            actor="admin-test",
        )
        assert acknowledged.status == "acknowledged"
        assert "13800138000" not in str(acknowledged.detail)

        online = service.handoff_staffing.update_presence(
            tenant_id=tenant_id,
            operator_id=operator_id,
            value=HandoffOperatorPresenceUpdate(
                presence="available",
                presence_ttl_seconds=600,
                expected_record_version=away.record_version,
            ),
            actor=operator_id,
        )
        assert online.available_for_claim
        service.handoff_dispatch.run_once(worker_id="test-dispatch", limit=1)
        assert service.handoff_dispatch.list_alerts(tenant_id=tenant_id)[0].status == "resolved"
    finally:
        service.close()


def test_expired_lease_recovers_and_reconciles_assignment_after_crash(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = service.settings.bootstrap_tenant_id
    try:
        task = _handoff(service, "lease-recovery")
        claimed_job = service.handoff_dispatch._claim(
            worker_id="crashed-worker", tenant_id=tenant_id
        )
        assert claimed_job is not None
        service.handoffs.dispatcher = None
        assigned = service.handoffs.auto_assign(
            tenant_id=tenant_id,
            handoff_id=task.id,
            expected_version=task.version,
            actor="crashed-worker",
            note="assignment committed before worker crash",
        )
        service.handoffs.set_dispatcher(service.handoff_dispatch)
        with service.db._write_lock, service.db.connect() as conn:
            conn.execute(
                """
                UPDATE handoff_dispatch_jobs SET lease_expires_at=?
                WHERE id=?
                """,
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), claimed_job["id"]),
            )
        report = service.handoff_dispatch.run_once(
            worker_id="recovery-worker", tenant_id=tenant_id, limit=1
        )
        assert report["assigned"] == 1
        job = service.handoff_dispatch.list_jobs(tenant_id=tenant_id)[0]
        assert job.status == "assigned"
        assert job.assigned_to == assigned.assigned_to
    finally:
        service.close()


def test_dispatch_claim_is_single_winner_across_database_instances(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = service.settings.bootstrap_tenant_id
    try:
        task = _handoff(service, "concurrent-dispatch")
        results: list[dict] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(8)

        def run(index: int) -> None:
            db = Database(service.settings.app_db_path)
            staffing = HandoffStaffingService(db)
            handoffs = HandoffService(db, staffing)
            dispatch = HandoffDispatchService(db, handoffs, staffing)
            handoffs.set_dispatcher(dispatch)
            try:
                barrier.wait()
                results.append(
                    dispatch.run_once(
                        worker_id=f"parallel-{index}", tenant_id=tenant_id, limit=1
                    )
                )
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert errors == []
        assert sum(item["assigned"] for item in results) == 1
        assert sum(item["claimed"] for item in results) == 1
        history = service.handoffs.history(tenant_id=tenant_id, handoff_id=task.id)
        assert [item.event_type for item in history].count("claimed") == 1
    finally:
        service.close()


def test_dispatch_error_dead_letters_then_manual_retry(tmp_path, monkeypatch) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = service.settings.bootstrap_tenant_id
    service.handoff_dispatch.max_attempts = 2
    service.handoff_dispatch.retry_base_seconds = 1
    try:
        _handoff(service, "dispatch-error")

        def fail(**_kwargs):
            raise HandoffError("synthetic dispatch failure")

        monkeypatch.setattr(service.handoffs, "auto_assign", fail)
        first = service.handoff_dispatch.run_once(
            worker_id="test-dispatch", tenant_id=tenant_id, limit=1
        )
        assert first["waiting"] == 1
        with service.db._write_lock, service.db.connect() as conn:
            conn.execute(
                "UPDATE handoff_dispatch_jobs SET available_at=? WHERE tenant_id=?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), tenant_id),
            )
        second = service.handoff_dispatch.run_once(
            worker_id="test-dispatch", tenant_id=tenant_id, limit=1
        )
        assert second["failed"] == 1
        job = service.handoff_dispatch.list_jobs(tenant_id=tenant_id)[0]
        assert job.status == "failed"
        assert service.handoff_dispatch.list_alerts(tenant_id=tenant_id)[0].reason == "dispatch_error"
        retried = service.handoff_dispatch.retry_job(
            tenant_id=tenant_id,
            job_id=job.id,
            value=HandoffDispatchRetryRequest(
                expected_record_version=job.record_version,
                note="supervisor approved a controlled retry",
            ),
            actor="admin-test",
        )
        assert retried.status == "pending"
    finally:
        service.close()


def test_dispatch_schedule_and_heartbeat_api_contract(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        profile = client.get(
            "/v1/handoffs/operators/admin-test", headers=ADMIN_HEADERS
        ).json()
        configured = client.put(
            "/v1/handoffs/operators/admin-test",
            headers=ADMIN_HEADERS,
            json={
                "display_name": profile["display_name"],
                "status": "active",
                "presence": "available",
                "dispatch_mode": "automatic",
                "schedule_mode": "scheduled",
                "max_active_tasks": profile["max_active_tasks"],
                "skills": profile["skills"],
                "queue_assignments": [
                    {
                        "queue_key": item["queue_key"],
                        "skill_level": item["skill_level"],
                        "is_primary": item["is_primary"],
                    }
                    for item in profile["queue_assignments"]
                ],
                "expected_record_version": profile["record_version"],
            },
        )
        assert configured.status_code == 200
        now = datetime.now(UTC)
        shift = client.post(
            "/v1/handoffs/operators/admin-test/shifts",
            headers=ADMIN_HEADERS,
            json={
                "starts_at": (now - timedelta(minutes=5)).isoformat(),
                "ends_at": (now + timedelta(hours=1)).isoformat(),
            },
        )
        assert shift.status_code == 201
        session = client.post(
            "/v1/handoffs/operators/admin-test/presence-sessions",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "api-console-session-0001",
                "presence": "available",
                "presence_ttl_seconds": 120,
                "expected_record_version": configured.json()["record_version"],
            },
        )
        assert session.status_code == 200
        heartbeat = client.post(
            "/v1/handoffs/operators/admin-test/heartbeat",
            headers=ADMIN_HEADERS,
            json={
                "session_id": session.json()["session_id"],
                "sequence": 1,
                "presence": "available",
                "presence_ttl_seconds": 120,
                "expected_presence_version": session.json()["presence_version"],
            },
        )
        assert heartbeat.status_code == 200
        chat = client.post(
            "/v1/chat",
            headers=CLIENT_HEADERS,
            json={"session_id": "dispatch-api", "message": "转人工", "context": {}},
        ).json()
        run = client.post(
            "/v1/handoffs/dispatch/run?limit=10", headers=ADMIN_HEADERS
        )
        assert run.status_code == 200
        assert run.json()["assigned"] == 1
        task = client.get(
            f"/v1/handoffs/{chat['handoff_id']}", headers=ADMIN_HEADERS
        ).json()
        assert task["assigned_to"] == "admin-test"
        assert client.get(
            "/v1/handoffs/dispatch/jobs", headers=ADMIN_HEADERS
        ).json()[0]["status"] == "assigned"


def test_dispatch_worker_lifecycle_assigns_and_reports_readiness(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        handoff_dispatch_worker_enabled=True,
        handoff_dispatch_poll_seconds=0.05,
        handoff_dispatch_batch_size=5,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            headers=CLIENT_HEADERS,
            json={"session_id": "dispatch-worker", "message": "转人工", "context": {}},
        ).json()
        deadline = time.monotonic() + 4
        task = None
        while time.monotonic() < deadline:
            task = client.get(
                f"/v1/handoffs/{response['handoff_id']}", headers=ADMIN_HEADERS
            ).json()
            if task["assigned_to"]:
                break
            time.sleep(0.05)
        assert task is not None
        assert task["assigned_to"] == "admin-test"
        health = client.get("/health").json()
        ready = client.get("/ready")
        assert health["handoff_dispatch"]["worker"]["running"] is True
        assert health["handoff_dispatch"]["worker"]["assigned"] >= 1
        assert ready.status_code == 200
        assert ready.json()["checks"]["handoff_dispatch_worker"] is True


def test_dispatch_filters_and_actions_fail_closed(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    tenant_id = service.settings.bootstrap_tenant_id
    try:
        with pytest.raises(DispatchError, match="invalid dispatch job status"):
            service.handoff_dispatch.list_jobs(tenant_id=tenant_id, status="unknown")
        with pytest.raises(DispatchError, match="invalid dispatch alert status"):
            service.handoff_dispatch.list_alerts(tenant_id=tenant_id, status="unknown")
        with pytest.raises(DispatchError, match="invalid dispatch worker id"):
            service.handoff_dispatch.run_once(worker_id="bad worker", limit=1)
        with pytest.raises(DispatchError, match="between 1 and 100"):
            service.handoff_dispatch.run_once(worker_id="worker", limit=0)
        with pytest.raises(DispatchError, match="version or status conflict"):
            service.handoff_dispatch.retry_job(
                tenant_id=tenant_id,
                job_id="missing",
                value=HandoffDispatchRetryRequest(
                    expected_record_version=1, note="missing job"
                ),
                actor="admin-test",
            )
    finally:
        service.close()
