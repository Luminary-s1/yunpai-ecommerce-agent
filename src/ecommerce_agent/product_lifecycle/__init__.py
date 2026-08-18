"""M9-R WP3 生命周期建议包。

公开 API 边界：
- 类型注册表：RecommendationType / RecommendationState / TargetObject / Recommendation
- 状态机：StateMachine / TransitionAction / AuditRecord
- 校验：WriteBarrier / validate_full_recommendation / validate_model_output
- M10-R 契约：RecommendationOutput / to_m10_contract / M10_CONTRACT_VERSION
- 持久化：RecommendationPersistenceService / RecommendationError
"""
from .interface import (
    M10_CONTRACT_VERSION,
    FactSnapshot,
    M10RecommendationType,
    RecommendationOutput,
    RestockPayload,
    m10_type_from_recommendation,
    to_m10_contract,
)
from .schemas import (
    REQUIRED_FACTS,
    Recommendation,
    RecommendationState,
    RecommendationType,
    TargetObject,
    validate_recommendation,
)
from .service import RecommendationError, RecommendationPersistenceService
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
    "FactSnapshot",
    "M10_CONTRACT_VERSION",
    "M10RecommendationType",
    "REQUIRED_FACTS",
    "Recommendation",
    "RecommendationError",
    "RecommendationOutput",
    "RecommendationPersistenceService",
    "RecommendationState",
    "RecommendationType",
    "RestockPayload",
    "StateMachine",
    "TargetObject",
    "TransitionAction",
    "WriteBarrier",
    "m10_type_from_recommendation",
    "to_m10_contract",
    "validate_full_recommendation",
    "validate_model_output",
    "validate_recommendation",
]

