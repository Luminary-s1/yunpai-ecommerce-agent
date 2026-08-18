"""M9-R WP3 生命周期建议包。

公开 API 边界：
- 类型注册表：RecommendationType / RecommendationState / TargetObject / Recommendation
- 状态机：StateMachine / TransitionAction / AuditRecord
- 校验：WriteBarrier / validate_full_recommendation / validate_model_output
- M10-R 契约：RecommendationOutput / to_m10_contract / M10_CONTRACT_VERSION
"""
from .interface import M10_CONTRACT_VERSION, RecommendationOutput, to_m10_contract
from .schemas import (
    REQUIRED_FACTS,
    Recommendation,
    RecommendationState,
    RecommendationType,
    TargetObject,
    validate_recommendation,
)
from .state_machine import AuditRecord, StateMachine, TransitionAction
from .validation import (
    ALLOWED_INTERNAL_WRITES,
    FORBIDDEN_OUTPUT_KEYS,
    WriteBarrier,
    validate_full_recommendation,
    validate_model_output,
)

__all__ = [
    "ALLOWED_INTERNAL_WRITES",
    "AuditRecord",
    "FORBIDDEN_OUTPUT_KEYS",
    "M10_CONTRACT_VERSION",
    "REQUIRED_FACTS",
    "Recommendation",
    "RecommendationOutput",
    "RecommendationState",
    "RecommendationType",
    "StateMachine",
    "TargetObject",
    "TransitionAction",
    "WriteBarrier",
    "to_m10_contract",
    "validate_full_recommendation",
    "validate_model_output",
    "validate_recommendation",
]
