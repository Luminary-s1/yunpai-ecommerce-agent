"""SOP channel gray rollout (F-105): approved versions ramp per session bucket.

In-bucket sessions resolve and pin the approved candidate version while every
other session keeps the active baseline; completing activates the candidate
for all traffic, rolling back returns everyone to the baseline, and already
pinned runs never switch versions mid-conversation.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.rollouts import stable_rollout_bucket
from ecommerce_agent.service import AgentService
from ecommerce_agent.sops import (
    SopCreateRequest,
    SopDsl,
    SopError,
    SopReviseRequest,
    SopRolloutBeginRequest,
    SopRolloutTransitionRequest,
    SopRolloutUpdateRequest,
    SopTransitionRequest,
)

from conftest import make_settings

ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}


def _dsl(postcondition: str = "case_created") -> SopDsl:
    return SopDsl.model_validate(
        {
            "trigger": {"intents": ["complaint"]},
            "required_context": ["shop_id"],
            "steps": [
                {"observe": "get_order_facts"},
                {"clarify_if_missing": "complaint_reason"},
                {"propose": "create_handoff_task"},
            ],
            "guards": {"max_auto_compensation_cents": 0},
            "handoff": {"when": ["customer_escalation", "evidence_conflict"]},
            "success": {"postcondition": postcondition},
        }
    )


def _activate_v1_and_approve_v2(service: AgentService) -> tuple[str, str, str, dict]:
    sops = service.sops
    created = sops.create(
        "tenant-test",
        SopCreateRequest(
            sop_key="complaint.gray",
            name="投诉灰度处理",
            intent="complaint",
            risk_level="high",
            dsl=_dsl(),
        ),
        "admin-test",
    )
    definition_id = created["definition"]["id"]
    version1_id = created["versions"][0]["id"]
    evaluated = sops.evaluate(
        "tenant-test",
        version1_id,
        SopTransitionRequest(expected_record_version=1),
        "reviewer-a",
    )
    approved = sops.approve(
        "tenant-test",
        version1_id,
        SopTransitionRequest(
            expected_record_version=evaluated["definition"]["record_version"]
        ),
        "reviewer-a",
    )
    active_v1 = sops.activate(
        "tenant-test",
        version1_id,
        SopTransitionRequest(
            expected_record_version=approved["definition"]["record_version"]
        ),
        "release-admin",
    )
    draft_v2 = sops.revise(
        "tenant-test",
        definition_id,
        SopReviseRequest(
            expected_record_version=active_v1["definition"]["record_version"],
            dsl=_dsl("supervisor_task_created"),
        ),
        "editor-a",
    )
    version2_id = draft_v2["versions"][0]["id"]
    evaluated_v2 = sops.evaluate(
        "tenant-test",
        version2_id,
        SopTransitionRequest(
            expected_record_version=draft_v2["definition"]["record_version"]
        ),
        "reviewer-a",
    )
    approved_v2 = sops.approve(
        "tenant-test",
        version2_id,
        SopTransitionRequest(
            expected_record_version=evaluated_v2["definition"]["record_version"]
        ),
        "reviewer-a",
    )
    return definition_id, version1_id, version2_id, approved_v2["definition"]


def _session(service: AgentService, external_id: str) -> str:
    return service.db.resolve_session(
        tenant_id="tenant-test",
        client_id="client-test",
        external_session_id=external_id,
        subject_hash="subject-gray",
    )


def _bucket_sessions(service: AgentService, salt: str) -> tuple[str, str]:
    session_in = next(
        _session(service, f"gray-in-{index}")
        for index in range(500)
        if stable_rollout_bucket(
            salt, _session(service, f"gray-in-{index}")
        ) < 5000
    )
    session_out = next(
        _session(service, f"gray-out-{index}")
        for index in range(500)
        if stable_rollout_bucket(
            salt, _session(service, f"gray-out-{index}")
        ) >= 5000
    )
    return session_in, session_out


def test_sop_rollout_pins_candidate_per_bucket_and_survives_rollback(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        sops = service.sops
        definition_id, version1_id, version2_id, definition = (
            _activate_v1_and_approve_v2(service)
        )
        rollout = sops.begin_rollout(
            "tenant-test",
            version2_id,
            SopRolloutBeginRequest(
                expected_record_version=definition["record_version"],
                traffic_percentage=50,
            ),
            "release-admin",
        )
        assert rollout["status"] == "active"
        assert rollout["baseline_id"] == version1_id
        session_in, session_out = _bucket_sessions(service, rollout["rollout_salt"])

        resolved_in = sops.resolve_for_session(
            "tenant-test", session_in, "complaint", create_run=True
        )
        resolved_out = sops.resolve_for_session(
            "tenant-test", session_out, "complaint", create_run=False
        )
        assert resolved_in["version"] == 2
        assert resolved_in["version_id"] == version2_id
        assert resolved_out["version"] == 1
        assert resolved_out["version_id"] == version1_id

        rolled_back = sops.rollback_rollout(
            "tenant-test",
            rollout["id"],
            SopRolloutTransitionRequest(
                expected_record_version=rollout["record_version"],
                note="灰度版本触发升级异常，回退",
            ),
            "release-admin",
        )
        assert rolled_back["status"] == "rolled_back"
        # The in-bucket session already pinned v2: pinned runs never switch.
        still_pinned = sops.resolve_for_session(
            "tenant-test", session_in, "complaint", create_run=False
        )
        assert still_pinned["version"] == 2
        fresh = sops.resolve_for_session(
            "tenant-test",
            _session(service, "gray-after-rollback"),
            "complaint",
            create_run=False,
        )
        assert fresh["version"] == 1
    finally:
        service.close()


def test_sop_rollout_ramp_complete_and_guards(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        sops = service.sops
        definition_id, version1_id, version2_id, definition = (
            _activate_v1_and_approve_v2(service)
        )
        with pytest.raises(SopError, match="approved candidate"):
            sops.begin_rollout(
                "tenant-test",
                version1_id,
                SopRolloutBeginRequest(
                    expected_record_version=definition["record_version"],
                    traffic_percentage=30,
                ),
                "release-admin",
            )
        rollout = sops.begin_rollout(
            "tenant-test",
            version2_id,
            SopRolloutBeginRequest(
                expected_record_version=definition["record_version"],
                traffic_percentage=10,
            ),
            "release-admin",
        )
        with pytest.raises(SopError, match="active rollout"):
            sops.begin_rollout(
                "tenant-test",
                version2_id,
                SopRolloutBeginRequest(
                    expected_record_version=definition["record_version"],
                    traffic_percentage=50,
                ),
                "release-admin",
            )
        ramped = sops.update_rollout(
            "tenant-test",
            rollout["id"],
            SopRolloutUpdateRequest(
                expected_record_version=rollout["record_version"],
                traffic_percentage=100,
            ),
            "release-admin",
        )
        session = _session(service, "gray-full")
        assert (
            sops.resolve_for_session(
                "tenant-test", session, "complaint", create_run=False
            )["version"]
            == 2
        )
        completed = sops.complete_rollout(
            "tenant-test",
            ramped["id"],
            SopRolloutTransitionRequest(
                expected_record_version=ramped["record_version"]
            ),
            "release-admin",
        )
        assert completed["status"] == "completed"
        detail = sops.detail("tenant-test", definition_id)
        assert detail["definition"]["current_active_version"] == 2
        statuses = {
            item["id"]: item["status"] for item in detail["versions"]
        }
        assert statuses[version2_id] == "active"
        assert statuses[version1_id] == "retired"
        with pytest.raises(SopError, match="not found"):
            sops.get_rollout("other-tenant", rollout["id"])
    finally:
        service.close()


def test_sop_rollout_admin_api(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        service = app.state.agent
        _, _, version2_id, definition = _activate_v1_and_approve_v2(service)
        created = client.post(
            f"/v1/admin/sop-versions/{version2_id}/rollouts",
            json={
                "expected_record_version": definition["record_version"],
                "traffic_percentage": 25,
            },
            headers=ADMIN_HEADERS,
        )
        assert created.status_code == 201
        rollout = created.json()
        listing = client.get(
            "/v1/admin/sop-rollouts?status=active", headers=ADMIN_HEADERS
        )
        assert [item["id"] for item in listing.json()] == [rollout["id"]]
        stale = client.post(
            f"/v1/admin/sop-rollouts/{rollout['id']}/rollback",
            json={"expected_record_version": 99},
            headers=ADMIN_HEADERS,
        )
        assert stale.status_code == 409
        done = client.post(
            f"/v1/admin/sop-rollouts/{rollout['id']}/complete",
            json={"expected_record_version": rollout["record_version"]},
            headers=ADMIN_HEADERS,
        )
        assert done.status_code == 200
        assert done.json()["status"] == "completed"
        assert client.get("/v1/admin/sop-rollouts").status_code == 401
