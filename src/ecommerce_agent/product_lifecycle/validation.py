"""M9-R WP3 建议校验 + 写屏障。

边界声明：
- 输入：Recommendation 对象 + 模型输出（可选）。
- 输出：校验结果（通过 / 拒绝原因）。
- 副作用：零——不触发任何平台写操作（B4 平台写=0）。
- 失败暴露：缺 alternatives / 越权键 / 缺必需事实 → 抛 ValueError（不静默）。
- 确定性：校验规则固定，无时间/随机依赖。

写屏障语义（B4 双语义）：
- 平台写=0：本模块任何路径不调用平台写 API（改价/换图/报名/调广告）。
- 内部写白名单：仅建议记录 + 状态机流转 + 审计记录（本包允许）。
"""
from __future__ import annotations

from typing import Any, Mapping

from ecommerce_agent.text_utils import contains_forbidden_token

from .schemas import Recommendation, RecommendationType, validate_recommendation

# 内部写白名单：本包允许的写动作（其余写 = 禁止）
ALLOWED_INTERNAL_WRITES: frozenset[str] = frozenset({
    "recommendation.create",
    "recommendation.state_transition",
    "recommendation.audit",
})

# 越权输出禁止键（同 WP2，含平台权重）
FORBIDDEN_OUTPUT_KEYS: frozenset[str] = frozenset({
    "effect",
    "interval",
    "sample_size",
    "平台权重",
    "平台算法",
})


class WriteBarrier:
    """写屏障：只允许白名单内写，白名单外一律拒绝。

    用法：
      barrier = WriteBarrier()
      barrier.assert_write_allowed("recommendation.create")   # 通过
      barrier.assert_write_allowed("platform.change_price")    # 抛
    """

    def assert_write_allowed(self, write_action: str) -> None:
        if write_action not in ALLOWED_INTERNAL_WRITES:
            raise ValueError(f"write_not_allowlisted:{write_action}")


def validate_model_output(recommendation: Recommendation, output: Mapping[str, Any]) -> None:
    """模型输出校验：越权键命中 → 抛；否则通过。

    失败暴露：禁止键（effect/平台权重等）出现在模型输出（含嵌套/自然语言）→ 整体拒绝。
    """
    if contains_forbidden_token(output, FORBIDDEN_OUTPUT_KEYS):
        raise ValueError("forbidden_output_key_recursive")


def validate_full_recommendation(recommendation: Recommendation) -> None:
    """完整校验：类型事实 + 前置校验 + B3 alternatives。

    - 非 degraded 建议缺必需事实 → 抛（validate_recommendation）
    - alternatives 必须含「上新」或「受控实验」（B3 硬边界：始终保留替代方案）
    """
    validate_recommendation(recommendation)
    if not recommendation.alternatives:
        raise ValueError("recommendation_requires_alternatives")
    # B3：备选路径必须含上新准备 或 受控实验（优先替代动作）
    b3_compatible = any(
        alt in (RecommendationType.NEW_LAUNCH, RecommendationType.EXPERIMENT)
        for alt in recommendation.alternatives
    )
    if not b3_compatible:
        raise ValueError("alternatives_must_include_launch_or_experiment")


__all__ = [
    "ALLOWED_INTERNAL_WRITES",
    "FORBIDDEN_OUTPUT_KEYS",
    "WriteBarrier",
    "validate_full_recommendation",
    "validate_model_output",
]
