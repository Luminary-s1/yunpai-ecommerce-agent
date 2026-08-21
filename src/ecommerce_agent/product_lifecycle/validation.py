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

# 越权输出禁止键（同 WP2，含平台权重/效果类词/竞品对标）
FORBIDDEN_OUTPUT_KEYS: frozenset[str] = frozenset({
    "effect",
    "interval",
    "sample_size",
    "gate",
    "平台权重",
    "平台算法",
    "效果提升",
    "权重提升",
    "流量扶持",
    "对标",
    "竞品",
    "行业",
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
    """完整校验：类型事实 + 前置校验 + B3 alternatives + 越权输出扫描。

    - 非 degraded 建议缺必需事实 → 抛（validate_recommendation）
    - alternatives 必须含「上新」或「受控实验」（B3 硬边界：始终保留替代方案）
    - 递归扫描建议内容（rationale/facts_snapshot/missing_evidence/alternatives）：
      命中越权词（effect/平台权重/效果提升/竞品对标等）→ 整体拒绝（C5 接线）
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
    # C5：建议内容递归扫越权词（不依赖 validate_model_output 被单独调用）
    content: dict[str, Any] = {
        "rationale": recommendation.rationale,
        "facts_snapshot": recommendation.facts_snapshot,
        "missing_evidence": list(recommendation.missing_evidence),
        "alternatives": [a.value for a in recommendation.alternatives],
    }
    if contains_forbidden_token(content, FORBIDDEN_OUTPUT_KEYS):
        raise ValueError("forbidden_output_key_recursive")
    # B2（盲点 #4 修复）：缺成本时不得输出正式价格结论（任务书 WP3 L364
    # "缺成本时不能输出正式利润安全价格"）。missing_evidence 含 cost_ready 且
    # rationale 含价格动作结论（提价/降价/安全价/定价）→ 确定性拒绝——不能只靠
    # prompt 软约束，必须是代码硬校验（模型输出经此校验才落库 DRAFT）。
    if "cost_ready" in recommendation.missing_evidence and _contains_price_conclusion(
        recommendation.rationale
    ):
        raise ValueError("price_conclusion_without_cost_ready")


# B2：价格动作结论词（缺成本时 rationale 不得含这些**动作结论**）。
# 注意：不含"安全价/定价"——那些常出现在诚实声明里（"无法输出正式安全价格"），
# 只有动作词（提价/降价/涨价/调价）才表示已给出价格结论。
_PRICE_CONCLUSION_TOKENS: tuple[str, ...] = (
    "提价", "降价", "涨价", "调价",
)


def _contains_price_conclusion(text: str) -> bool:
    """rationale 是否含价格动作结论（缺成本时禁止）。"""
    return any(token in text for token in _PRICE_CONCLUSION_TOKENS)


__all__ = [
    "ALLOWED_INTERNAL_WRITES",
    "FORBIDDEN_OUTPUT_KEYS",
    "WriteBarrier",
    "validate_full_recommendation",
    "validate_model_output",
]
