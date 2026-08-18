"""M9-R WP3 状态机测试：确定性状态转换 + 审计记录。

对齐验收标准：条目 1（建议默认 draft，人工批准才生效）、条目 2（批准不触发平台动作）、
条目 5（重放幂等，旧建议标 stale）。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ecommerce_agent.product_lifecycle.state_machine import (
    AuditRecord,
    StateMachine,
    TransitionAction,
)
from ecommerce_agent.product_lifecycle.schemas import RecommendationState

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def test_starts_draft() -> None:
    """建议默认 draft（对齐验收条目 1）。"""
    sm = StateMachine()
    assert sm.state is RecommendationState.DRAFT


def test_draft_submit_to_awaiting_review() -> None:
    sm = StateMachine(RecommendationState.DRAFT)
    new_state, audit = sm.apply(
        TransitionAction.SUBMIT, actor="ops-1", at=NOW, target="r1"
    )
    assert new_state is RecommendationState.AWAITING_REVIEW
    assert isinstance(audit, AuditRecord)
    assert audit.from_state is RecommendationState.DRAFT
    assert audit.to_state is RecommendationState.AWAITING_REVIEW
    assert audit.actor == "ops-1"
    assert audit.target == "r1"


def test_awaiting_review_approve() -> None:
    sm = StateMachine(RecommendationState.AWAITING_REVIEW)
    new_state, _ = sm.apply(TransitionAction.APPROVE, actor="ops-1", at=NOW, target="r1")
    assert new_state is RecommendationState.APPROVED


def test_awaiting_review_reject() -> None:
    sm = StateMachine(RecommendationState.AWAITING_REVIEW)
    new_state, _ = sm.apply(TransitionAction.REJECT, actor="ops-1", at=NOW, target="r1")
    assert new_state is RecommendationState.REJECTED


def test_approved_observe_to_observed() -> None:
    sm = StateMachine(RecommendationState.APPROVED)
    new_state, _ = sm.apply(TransitionAction.OBSERVE, actor="ops-1", at=NOW, target="r1")
    assert new_state is RecommendationState.OBSERVED


def test_invalid_transition_raises() -> None:
    """非法转换（draft→approve）→ 抛 ValueError（不静默）。"""
    sm = StateMachine(RecommendationState.DRAFT)
    with pytest.raises(ValueError, match="invalid_state_transition"):
        sm.apply(TransitionAction.APPROVE, actor="ops-1", at=NOW, target="r1")


def test_mark_stale_closes() -> None:
    """事实更新 → 旧建议标 stale（closed）（对齐验收条目 5）。"""
    sm = StateMachine(RecommendationState.OBSERVED)
    new_state, audit = sm.apply(
        TransitionAction.MARK_STALE, actor="ops-1", at=NOW, target="r1"
    )
    assert new_state is RecommendationState.CLOSED
    assert audit.action is TransitionAction.MARK_STALE


def test_can_deterministic() -> None:
    sm = StateMachine(RecommendationState.DRAFT)
    assert sm.can(TransitionAction.SUBMIT) is True
    assert sm.can(TransitionAction.APPROVE) is False
