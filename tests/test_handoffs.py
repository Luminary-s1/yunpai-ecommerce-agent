import pytest

from ecommerce_agent.handoff import HandoffError
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


def test_handoff_has_idempotent_task_and_legal_lifecycle(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        response = service.chat(principal_for(service), "handoff-session", "帮我马上退款")
        assert response.handoff_id
        assert response.handoff_status == "proposed"

        proposed = service.handoffs.get(
            tenant_id=service.settings.bootstrap_tenant_id,
            handoff_id=response.handoff_id,
        )
        assert proposed.version == 1
        accepted = service.handoffs.transition(
            tenant_id=service.settings.bootstrap_tenant_id,
            handoff_id=proposed.id,
            target_status="accepted",
            operator="admin-test",
            expected_version=1,
            note=None,
        )
        working = service.handoffs.transition(
            tenant_id=service.settings.bootstrap_tenant_id,
            handoff_id=proposed.id,
            target_status="working",
            operator="admin-test",
            expected_version=accepted.version,
            note=None,
        )
        review = service.handoffs.transition(
            tenant_id=service.settings.bootstrap_tenant_id,
            handoff_id=proposed.id,
            target_status="review",
            operator="admin-test",
            expected_version=working.version,
            note="resolved",
        )
        completed = service.handoffs.transition(
            tenant_id=service.settings.bootstrap_tenant_id,
            handoff_id=proposed.id,
            target_status="completed",
            operator="reviewer",
            expected_version=review.version,
            note="checked",
        )
        assert completed.status == "completed"
        assert completed.completed_at
    finally:
        service.close()


def test_handoff_rejects_illegal_or_stale_transition(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        response = service.chat(principal_for(service), "handoff-invalid", "转人工")
        with pytest.raises(HandoffError):
            service.handoffs.transition(
                tenant_id=service.settings.bootstrap_tenant_id,
                handoff_id=response.handoff_id,
                target_status="completed",
                operator="admin-test",
                expected_version=1,
                note=None,
            )
        accepted = service.handoffs.transition(
            tenant_id=service.settings.bootstrap_tenant_id,
            handoff_id=response.handoff_id,
            target_status="accepted",
            operator="admin-test",
            expected_version=1,
            note=None,
        )
        with pytest.raises(HandoffError):
            service.handoffs.transition(
                tenant_id=service.settings.bootstrap_tenant_id,
                handoff_id=response.handoff_id,
                target_status="working",
                operator="admin-test",
                expected_version=1,
                note=None,
            )
        assert accepted.version == 2
    finally:
        service.close()
