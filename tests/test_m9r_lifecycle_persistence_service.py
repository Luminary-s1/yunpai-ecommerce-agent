"""M9-R WP3 持久化读写服务测试：RecommendationPersistenceService。

覆盖：
- create 幂等（同键同内容复用 / 同键异内容冲突）
- record_transition 落 audit + 更新 state，重放幂等
- get/list 过滤与租户隔离
- audit 不可变 + FK 约束
- 行→Recommendation 模型 round-trip
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ecommerce_agent.database import Database
from ecommerce_agent.product_lifecycle import (
    Recommendation,
    RecommendationError,
    RecommendationPersistenceService,
    RecommendationState,
    RecommendationType,
    StateMachine,
    TargetObject,
    TransitionAction,
)

NOW = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)


def _rec(
    *,
    recommendation_id: str = "rec-1",
    rationale: str = "observe current state",
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        type=RecommendationType.KEEP_OBSERVE,
        target=TargetObject(store_id="store-a"),
        facts_snapshot={"traffic_facts": {"impressions": 100}},
        rationale=rationale,
        alternatives=[RecommendationType.EXPERIMENT],
        state=RecommendationState.DRAFT,
        degraded=False,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture()
def service(tmp_path) -> RecommendationPersistenceService:
    db = Database(tmp_path / "persistence-service.sqlite3")
    db.initialize()
    return RecommendationPersistenceService(db)


def test_create_persists_and_get_roundtrips(service) -> None:
    """create → get → 行重建模型 == 原始 Recommendation。"""
    created = service.create("tenant-a", _rec())
    assert created["write_status"] == "applied"
    row = service.get("tenant-a", "rec-1")
    assert row["type"] == RecommendationType.KEEP_OBSERVE.value
    assert row["state"] == RecommendationState.DRAFT.value
    assert len(row["payload_hash"]) == 64
    rebuilt = service._from_row(row)
    assert rebuilt == _rec()


def test_create_same_key_same_payload_idempotent(service) -> None:
    """同键同内容重放 → idempotent，行数不涨。"""
    service.create("tenant-a", _rec())
    again = service.create("tenant-a", _rec())
    assert again["write_status"] == "idempotent"
    assert len(service.list("tenant-a")) == 1


def test_create_same_key_different_payload_conflicts(service) -> None:
    """同键异内容 → recommendation_conflict，不静默覆盖。"""
    service.create("tenant-a", _rec())
    with pytest.raises(RecommendationError) as exc:
        service.create("tenant-a", _rec(rationale="changed"))
    assert exc.value.code == "recommendation_conflict"
    assert len(service.list("tenant-a")) == 1


def test_create_rejects_non_draft_and_invalid(service) -> None:
    """非 DRAFT 落库拒绝；alternatives 缺上新/实验拒绝。"""
    approved = _rec()
    approved = approved.model_copy(update={"state": RecommendationState.APPROVED})
    with pytest.raises(RecommendationError) as exc:
        service.create("tenant-a", approved)
    assert exc.value.code == "recommendation_create_state_not_draft"
    no_alt = _rec()
    no_alt = no_alt.model_copy(update={"alternatives": [RecommendationType.PRICING]})
    with pytest.raises(ValueError, match="alternatives_must_include_launch_or_experiment"):
        service.create("tenant-a", no_alt)


def test_record_transition_updates_state_and_writes_audit(service) -> None:
    """submit → state 更新为 awaiting_review，audit 落 1 行。"""
    service.create("tenant-a", _rec())
    result = service.record_transition(
        "tenant-a", "rec-1", action=TransitionAction.SUBMIT, actor="ops-1", at=NOW
    )
    assert result["recommendation"]["state"] == RecommendationState.AWAITING_REVIEW.value
    assert result["write_status"] == "applied"
    trail = service.audit_trail("tenant-a", "rec-1")
    assert len(trail) == 1
    assert trail[0]["action"] == TransitionAction.SUBMIT.value
    assert trail[0]["from_state"] == RecommendationState.DRAFT.value
    assert trail[0]["to_state"] == RecommendationState.AWAITING_REVIEW.value


def test_create_rolls_back_when_system_audit_fails(service) -> None:
    """通用审计失败时，建议创建不能先提交成无审计状态。"""
    with service.db.connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER fail_m9r_create_system_audit
            BEFORE INSERT ON audit_log
            WHEN NEW.event_type='recommendation.create'
            BEGIN
                SELECT RAISE(ABORT, 'create_system_audit_failed');
            END
            """
        )

    with pytest.raises(Exception, match="create_system_audit_failed"):
        service.create("tenant-a", _rec())

    with service.db.connect() as conn:
        stored = conn.execute(
            "SELECT 1 FROM product_recommendations "
            "WHERE tenant_id='tenant-a' AND recommendation_id='rec-1'"
        ).fetchone()
    assert stored is None


def test_transition_rolls_back_when_system_audit_fails(service) -> None:
    """通用审计失败时，状态更新和领域审计必须一起回滚。"""
    service.create("tenant-a", _rec())
    with service.db.connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER fail_m9r_transition_system_audit
            BEFORE INSERT ON audit_log
            WHEN NEW.event_type='recommendation.state_transition'
            BEGIN
                SELECT RAISE(ABORT, 'transition_system_audit_failed');
            END
            """
        )

    with pytest.raises(Exception, match="transition_system_audit_failed"):
        service.record_transition(
            "tenant-a",
            "rec-1",
            action=TransitionAction.SUBMIT,
            actor="ops-1",
            at=NOW,
        )

    assert service.get("tenant-a", "rec-1")["state"] == RecommendationState.DRAFT.value
    assert service.audit_trail("tenant-a", "rec-1") == []


def test_record_transition_replay_is_idempotent(service) -> None:
    """同 (action, actor, at) 重放 → idempotent，audit 不重复。"""
    service.create("tenant-a", _rec())
    first = service.record_transition(
        "tenant-a", "rec-1", action=TransitionAction.SUBMIT, actor="ops-1", at=NOW
    )
    second = service.record_transition(
        "tenant-a", "rec-1", action=TransitionAction.SUBMIT, actor="ops-1", at=NOW
    )
    assert first["write_status"] == "applied"
    assert second["write_status"] == "idempotent"
    assert len(service.audit_trail("tenant-a", "rec-1")) == 1


def test_record_transition_illegal_from_current_state(service) -> None:
    """submit 后以新时间戳再 submit → 非法转换 ValueError。"""
    service.create("tenant-a", _rec())
    service.record_transition(
        "tenant-a", "rec-1", action=TransitionAction.SUBMIT, actor="ops-1", at=NOW
    )
    with pytest.raises(ValueError, match="invalid_state_transition"):
        service.record_transition(
            "tenant-a", "rec-1", action=TransitionAction.SUBMIT, actor="ops-1", at=datetime(2026, 8, 18, 11, 0, tzinfo=UTC)
        )


def test_record_transition_missing_recommendation(service) -> None:
    """未 create 直接 transition → recommendation_not_found。"""
    with pytest.raises(RecommendationError) as exc:
        service.record_transition(
            "tenant-a", "missing", action=TransitionAction.SUBMIT, actor="ops-1", at=NOW
        )
    assert exc.value.code == "recommendation_not_found"


def test_list_filters_and_tenant_isolation(service) -> None:
    """list 按 store/state 过滤；租户隔离；limit 校验。"""
    # a1/a2 用不同 rationale 使内容不同——同 SKU 同内容的第二条会被内容级幂等
    # 吞掉（B3 任务书 L365「同一证据重放不重复创建」），不在此测试的意图范围内。
    service.create("tenant-a", _rec(recommendation_id="a1", rationale="observe a1"))
    service.create("tenant-a", _rec(recommendation_id="a2", rationale="observe a2"))
    service.create("tenant-b", _rec(recommendation_id="b1"))
    assert {r["recommendation_id"] for r in service.list("tenant-a")} == {"a1", "a2"}
    assert {r["recommendation_id"] for r in service.list("tenant-a", store_id="store-a")} == {"a1", "a2"}
    assert service.list("tenant-a", state=RecommendationState.DRAFT) == [
        r for r in service.list("tenant-a")
    ]
    assert service.list("tenant-a", state=RecommendationState.APPROVED) == []
    assert service.list("tenant-a", limit=1) == [service.list("tenant-a")[0]]
    with pytest.raises(RecommendationError) as exc:
        service.list("tenant-a", limit=0)
    assert exc.value.code == "recommendation_limit_invalid"


def test_audit_immutable_and_fk_via_sql(service) -> None:
    """audit 行不可变（UPDATE/DELETE 拒绝）；孤儿审计 FK 拒绝。"""
    import sqlite3

    service.create("tenant-a", _rec())
    service.record_transition(
        "tenant-a", "rec-1", action=TransitionAction.SUBMIT, actor="ops-1", at=NOW
    )
    db = service.db
    with pytest.raises(sqlite3.IntegrityError, match="product_recommendation_audit_immutable"):
        with db.connect() as conn:
            conn.execute(
                "UPDATE product_recommendation_audit SET actor=? WHERE recommendation_id=?",
                ("changed", "rec-1"),
            )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO product_recommendation_audit(
                    tenant_id, recommendation_id, action, from_state, to_state,
                    actor, occurred_at, payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("tenant-a", "orphan", "submit", "draft", "awaiting_review",
                 "ops-1", NOW.isoformat(), "a" * 64),
            )
