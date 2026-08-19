"""M9-R WP3 建议状态机：draft → awaiting_review → approved/rejected → observed → closed。

边界声明：
- 输入：(当前状态, 动作, 操作者)。
- 输出：新状态 + 审计记录（actor, at, action, target, from_state, to_state）。
- 副作用：零——纯状态转换，不触发任何平台动作（B2：approved 不写平台）。
- 失败暴露：非法转换 → 抛 ValueError（明确 from→action 不可达）。
- 确定性：状态图固定，无时间源依赖（审计的 at 由调用方传入）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .schemas import RecommendationState


class TransitionAction(StrEnum):
    SUBMIT = "submit"            # draft → awaiting_review
    APPROVE = "approve"          # awaiting_review → approved
    REJECT = "reject"            # awaiting_review → rejected
    OBSERVE = "observe"          # approved → observed
    CLOSE = "close"              # observed/rejected → closed
    MARK_STALE = "mark_stale"    # 任何非终态 → stale（事实更新后旧建议作废，不原地改写历史）


@dataclass(frozen=True)
class AuditRecord:
    """状态流转审计（actor, at, action, target, from_state, to_state）。"""

    actor: str
    at: datetime
    action: TransitionAction
    target: str
    from_state: RecommendationState
    to_state: RecommendationState


# 确定性状态图：state → 允许的动作 → 目标状态
_TRANSITIONS: dict[RecommendationState, dict[TransitionAction, RecommendationState]] = {
    RecommendationState.DRAFT: {
        TransitionAction.SUBMIT: RecommendationState.AWAITING_REVIEW,
        TransitionAction.MARK_STALE: RecommendationState.STALE,
    },
    RecommendationState.AWAITING_REVIEW: {
        TransitionAction.APPROVE: RecommendationState.APPROVED,
        TransitionAction.REJECT: RecommendationState.REJECTED,
        TransitionAction.MARK_STALE: RecommendationState.STALE,
    },
    RecommendationState.APPROVED: {
        TransitionAction.OBSERVE: RecommendationState.OBSERVED,
        TransitionAction.MARK_STALE: RecommendationState.STALE,
    },
    RecommendationState.REJECTED: {
        TransitionAction.CLOSE: RecommendationState.CLOSED,
    },
    RecommendationState.OBSERVED: {
        TransitionAction.CLOSE: RecommendationState.CLOSED,
        TransitionAction.MARK_STALE: RecommendationState.STALE,
    },
    RecommendationState.CLOSED: {},
}


class StateMachine:
    """建议状态机（确定性，无副作用）。

    用法：
      sm = StateMachine(RecommendationState.DRAFT)
      new_state, audit = sm.apply(TransitionAction.SUBMIT, actor="ops-1", at=now, target="r1")
    """

    def __init__(self, initial: RecommendationState = RecommendationState.DRAFT) -> None:
        self._state = initial

    @property
    def state(self) -> RecommendationState:
        return self._state

    def apply(
        self,
        action: TransitionAction,
        *,
        actor: str,
        at: datetime,
        target: str,
    ) -> tuple[RecommendationState, AuditRecord]:
        """执行转换：合法 → 返回 (新状态, 审计)；非法 → 抛 ValueError。

        确定性与失败暴露：状态图固定，非法动作立即抛，不静默跳过。
        """
        allowed = _TRANSITIONS[self._state]
        if action not in allowed:
            raise ValueError(
                f"invalid_state_transition:{self._state.value}:{action.value}"
            )
        to_state = allowed[action]
        audit = AuditRecord(
            actor=actor, at=at, action=action, target=target,
            from_state=self._state, to_state=to_state,
        )
        self._state = to_state
        return to_state, audit

    def can(self, action: TransitionAction) -> bool:
        """确定性判定：当前状态是否允许某动作。"""
        return action in _TRANSITIONS[self._state]


__all__ = [
    "AuditRecord",
    "StateMachine",
    "TransitionAction",
]
