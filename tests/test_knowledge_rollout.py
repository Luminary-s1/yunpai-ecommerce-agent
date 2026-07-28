"""Knowledge gray release (F-104): staged rollouts with deterministic buckets.

An evaluated candidate version is served to in-bucket sessions while everyone
else stays on the approved baseline; completing the rollout activates the
candidate for all traffic and rolling back restores the baseline everywhere.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.knowledge_management import (
    KnowledgeCreateRequest,
    KnowledgeLifecycleError,
    KnowledgeReviseRequest,
    KnowledgeRolloutBeginRequest,
    KnowledgeRolloutTransitionRequest,
    KnowledgeRolloutUpdateRequest,
    KnowledgeTransitionRequest,
)
from ecommerce_agent.rollouts import stable_rollout_bucket
from ecommerce_agent.service import AgentService

from conftest import make_settings

ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}

BASELINE_ANSWER = "云湃积分每满 100 分可以兑换 5 元优惠券。"
CANDIDATE_ANSWER = "云湃积分每满 100 分可以兑换 8 元优惠券，活动期间翻倍。"
QUERY = "云湃积分怎么兑换优惠券"


def _seed_versions(service: AgentService) -> tuple[dict, dict]:
    management = service.knowledge_management
    created = management.create(
        "tenant-test",
        KnowledgeCreateRequest(
            category="会员",
            intent="membership",
            question="云湃积分怎么兑换优惠券",
            answer=BASELINE_ANSWER,
            keywords="积分 兑换 优惠券",
            source="店铺会员规则 v1",
            layer="store",
            store_id="shop-rollout-1",
        ),
        "admin-test",
    )
    evaluated = management.evaluate(
        "tenant-test",
        created["id"],
        KnowledgeTransitionRequest(expected_record_version=created["record_version"]),
        "admin-test",
    )
    baseline = management.approve(
        "tenant-test",
        evaluated["id"],
        KnowledgeTransitionRequest(expected_record_version=evaluated["record_version"]),
        "admin-test",
    )
    revised = management.revise(
        "tenant-test",
        baseline["id"],
        KnowledgeReviseRequest(
            expected_record_version=baseline["record_version"],
            answer=CANDIDATE_ANSWER,
            source="店铺会员规则 v2",
        ),
        "admin-test",
    )
    candidate = management.evaluate(
        "tenant-test",
        revised["id"],
        KnowledgeTransitionRequest(expected_record_version=revised["record_version"]),
        "admin-test",
    )
    return baseline, candidate


def _served_answer(service: AgentService, unit: str | None) -> str:
    documents = service.knowledge.retrieve(
        QUERY,
        top_k=3,
        min_score=0.05,
        tenant_id="tenant-test",
        store_id="shop-rollout-1",
        rollout_unit=unit,
    )
    assert documents, "retrieval must find the store knowledge"
    return documents[0]["answer"]


def _bucket_units(salt: str) -> tuple[str, str]:
    unit_in = next(
        f"unit-{index}"
        for index in range(500)
        if stable_rollout_bucket(salt, f"unit-{index}") < 5000
    )
    unit_out = next(
        f"unit-{index}"
        for index in range(500)
        if stable_rollout_bucket(salt, f"unit-{index}") >= 5000
    )
    return unit_in, unit_out


def test_rollout_splits_ramps_completes_and_rolls_back(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        management = service.knowledge_management
        baseline, candidate = _seed_versions(service)
        rollout = management.begin_rollout(
            "tenant-test",
            candidate["id"],
            KnowledgeRolloutBeginRequest(
                expected_record_version=candidate["record_version"],
                traffic_percentage=50,
            ),
            "admin-test",
        )
        assert rollout["status"] == "active"
        assert rollout["baseline_id"] == baseline["id"]
        unit_in, unit_out = _bucket_units(rollout["rollout_salt"])

        assert _served_answer(service, unit_in) == CANDIDATE_ANSWER
        assert _served_answer(service, unit_out) == BASELINE_ANSWER
        assert _served_answer(service, None) == BASELINE_ANSWER

        ramped = management.update_rollout(
            "tenant-test",
            rollout["id"],
            KnowledgeRolloutUpdateRequest(
                expected_record_version=rollout["record_version"],
                traffic_percentage=100,
            ),
            "admin-test",
        )
        assert _served_answer(service, unit_out) == CANDIDATE_ANSWER

        rolled_back = management.rollback_rollout(
            "tenant-test",
            ramped["id"],
            KnowledgeRolloutTransitionRequest(
                expected_record_version=ramped["record_version"],
                note="灰度期间发现口径错误，回滚",
            ),
            "admin-test",
        )
        assert rolled_back["status"] == "rolled_back"
        assert _served_answer(service, unit_in) == BASELINE_ANSWER
        still_candidate = management.get_item("tenant-test", candidate["id"])
        assert still_candidate["status"] == "candidate"

        second = management.begin_rollout(
            "tenant-test",
            candidate["id"],
            KnowledgeRolloutBeginRequest(
                expected_record_version=still_candidate["record_version"],
                traffic_percentage=10,
            ),
            "admin-test",
        )
        completed = management.complete_rollout(
            "tenant-test",
            second["id"],
            KnowledgeRolloutTransitionRequest(
                expected_record_version=second["record_version"]
            ),
            "admin-test",
        )
        assert completed["status"] == "completed"
        assert _served_answer(service, None) == CANDIDATE_ANSWER
        promoted = management.get_item("tenant-test", candidate["id"])
        retired = management.get_item("tenant-test", baseline["id"])
        assert promoted["status"] == "active"
        assert promoted["review_status"] == "approved"
        assert retired["status"] == "retired"
    finally:
        service.close()


def test_rollout_requires_evaluated_candidate_and_single_active(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        management = service.knowledge_management
        baseline, candidate = _seed_versions(service)
        draft = management.revise(
            "tenant-test",
            baseline["id"],
            KnowledgeReviseRequest(
                expected_record_version=management.get_item(
                    "tenant-test", baseline["id"]
                )["record_version"],
                answer="未评测的草稿答案，用于验证灰度门槛。",
            ),
            "admin-test",
        )
        with pytest.raises(KnowledgeLifecycleError, match="evaluated candidate"):
            management.begin_rollout(
                "tenant-test",
                draft["id"],
                KnowledgeRolloutBeginRequest(
                    expected_record_version=draft["record_version"],
                    traffic_percentage=20,
                ),
                "admin-test",
            )
        first = management.begin_rollout(
            "tenant-test",
            candidate["id"],
            KnowledgeRolloutBeginRequest(
                expected_record_version=candidate["record_version"],
                traffic_percentage=20,
            ),
            "admin-test",
        )
        refreshed = management.get_item("tenant-test", candidate["id"])
        with pytest.raises(KnowledgeLifecycleError, match="active rollout"):
            management.begin_rollout(
                "tenant-test",
                candidate["id"],
                KnowledgeRolloutBeginRequest(
                    expected_record_version=refreshed["record_version"],
                    traffic_percentage=40,
                ),
                "admin-test",
            )
        with pytest.raises(KnowledgeLifecycleError, match="version conflict"):
            management.rollback_rollout(
                "tenant-test",
                first["id"],
                KnowledgeRolloutTransitionRequest(expected_record_version=99),
                "admin-test",
            )
        with pytest.raises(KnowledgeLifecycleError, match="not found"):
            management.get_rollout("other-tenant", first["id"])
    finally:
        service.close()


def test_chat_serves_rollout_consistently_with_session_bucket(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        from conftest import principal_for

        management = service.knowledge_management
        baseline, candidate = _seed_versions(service)
        rollout = management.begin_rollout(
            "tenant-test",
            candidate["id"],
            KnowledgeRolloutBeginRequest(
                expected_record_version=candidate["record_version"],
                traffic_percentage=50,
            ),
            "admin-test",
        )
        principal = principal_for(service, "buyer-rollout-1")
        response = service.chat(
            principal,
            "rollout-session-1",
            QUERY,
            {"shop_id": "shop-rollout-1"},
        )
        with service.db.connect() as conn:
            internal_session = conn.execute(
                "SELECT id FROM sessions WHERE tenant_id=? AND external_session_id=?",
                ("tenant-test", "rollout-session-1"),
            ).fetchone()[0]
        expected_id = (
            candidate["id"]
            if stable_rollout_bucket(rollout["rollout_salt"], internal_session) < 5000
            else baseline["id"]
        )
        unexpected_id = (
            baseline["id"] if expected_id == candidate["id"] else candidate["id"]
        )
        source_ids = {source.id for source in response.sources}
        assert expected_id in source_ids
        assert unexpected_id not in source_ids
    finally:
        service.close()


def test_rollout_admin_api_lifecycle_and_conflicts(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        service = app.state.agent
        _, candidate = _seed_versions(service)
        created = client.post(
            f"/v1/admin/knowledge/{candidate['id']}/rollouts",
            json={
                "expected_record_version": candidate["record_version"],
                "traffic_percentage": 30,
            },
            headers=ADMIN_HEADERS,
        )
        assert created.status_code == 201
        rollout = created.json()
        assert rollout["status"] == "active"
        listing = client.get(
            "/v1/admin/knowledge-rollouts?status=active", headers=ADMIN_HEADERS
        )
        assert listing.status_code == 200
        assert [item["id"] for item in listing.json()] == [rollout["id"]]
        stale = client.post(
            f"/v1/admin/knowledge-rollouts/{rollout['id']}/complete",
            json={"expected_record_version": 99},
            headers=ADMIN_HEADERS,
        )
        assert stale.status_code == 409
        completed = client.post(
            f"/v1/admin/knowledge-rollouts/{rollout['id']}/complete",
            json={"expected_record_version": rollout["record_version"]},
            headers=ADMIN_HEADERS,
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "completed"
        assert (
            client.get("/v1/admin/knowledge-rollouts").status_code == 401
        )
