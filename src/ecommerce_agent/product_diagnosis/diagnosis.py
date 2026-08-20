"""M9-R WP2 流量诊断：确定性事实 + 模型语义校验（对齐 D-034）。

边界声明（D-034）：
- 确定性代码回答「这个动作现在能不能安全执行」；模型回答「用户想要什么、下一步做什么」。
- 本模块只产出**可执行事实**（证据状态、门禁、污染旗标、原始漏斗数值），
  不替模型决定语义诊断类型。
- 语义诊断类型（exposure/click/conversion 等）由模型产出，确定性代码只校验：
  1. 类型在 DiagnosisType 白名单内；
  2. 未命中 FORBIDDEN_KEYS（含嵌套/自然语言，递归）；
  3. 可执行前提成立（证据不足/门禁未过时不得给强方向结论）。
- 副作用：零——纯派生，不写库、不调用模型。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict

from ecommerce_agent.text_utils import contains_forbidden_token


class DiagnosisType(StrEnum):
    EXPOSURE_INSUFFICIENT = "exposure_insufficient"
    CLICK_INSUFFICIENT = "click_insufficient"
    CONVERSION_INSUFFICIENT = "conversion_insufficient"
    STOCKOUT_POLLUTION = "stockout_pollution"
    AD_PRICE_POLLUTION = "ad_price_pollution"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"


class Diagnosis(BaseModel):
    """结构化诊断（冻结，可追溯）。由模型产出类型，代码校验后填充。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    diagnosis_type: DiagnosisType
    sku_id: str
    reason: str | None = None
    evidence_facts: dict[str, Any]  # 固化证据（引用来源，非模型编造）
    degraded: bool = False


# 模型越权输出禁止键（含 effect/interval/sample_size/平台权重/平台算法/
# 效果类词/竞品对标——与 WP3 建议门禁共用完整集，安全审查 #2 修复：
# 诊断输出不得含 validation.FORBIDDEN_OUTPUT_KEYS 之外的越权表述）
FORBIDDEN_DIAGNOSIS_KEYS: frozenset[str] = frozenset({
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


@dataclass(frozen=True)
class DiagnosisFacts:
    """确定性可执行事实（D-034：只回答「能不能安全下结论」）。"""

    sku_id: str
    evidence_state: str | None
    freshness: Mapping[str, Any] | None
    quality_gate: str | None  # passed / blocked / None（无门禁信息）
    quality_gate_issues: tuple[str, ...]
    stockout: bool
    pollution: str | None
    exposures: float | None
    clicks: float | None
    conversions: float | None
    degraded: bool = False

    def conclusion_allowed(self) -> bool:
        """能否给强方向结论：证据可用 + 门禁通过 + 无污染。

        确定性：仅依赖固化事实，不依赖模型。
        fail-closed（WP5 复审）：缺门禁信息（None）或 freshness 不可用一律拒绝，
        只有 quality_gate == "passed" 才放行强方向结论（对齐验收「只有满足全部
        Gate 才给强方向结论」）。
        """
        if self.evidence_state in (None, "missing"):
            return False
        if self.quality_gate != "passed":          # 缺门禁/blocked 一律拒绝
            return False
        if self.freshness is not None:
            if self.freshness.get("usable_as_current") is not True:
                return False                       # freshness 不可用 → 拒绝
        if self.stockout or self.pollution is not None:
            return False
        return True


# quality_gate.issues → 污染旗标 权威映射（B6：从证据自动反推污染，不靠调用方硬编码）
# issue 码来自 M5-R TrafficAnalysisEngine._check_control_variables/_check_metric_quality
_POLLUTION_ISSUE_CODES: dict[str, str] = {
    "stock_not_available": "stockout",
    "ad_spend_not_controlled": "ad_change",
    "sale_price_changed": "price_change",
}


def build_diagnosis_facts(
    sku_id: str,
    view: Mapping[str, Any],
    *,
    stockout: bool = False,
    pollution: str | None = None,
) -> DiagnosisFacts:
    """从证据视图提取确定性可执行事实（不做任何语义分类）。

    B6 污染旗标自动反推：若调用方未显式传 stockout/pollution，则从
    quality_gate.issues 反推（stock_not_available→stockout，
    ad_spend_not_controlled/sale_price_changed→pollution）。显式传入优先级更高。
    """
    quality_gate = view.get("quality_gate")
    issues: tuple[str, ...]
    if isinstance(quality_gate, Mapping):
        gate_status = quality_gate.get("status")
        issues = tuple(quality_gate.get("issues") or ())
    else:
        gate_status = quality_gate
        issues = tuple(view.get("quality_gate_issues") or ())
    # B6：未显式传污染旗标时从 issue 码反推
    auto_stockout = any(code == "stock_not_available" for code in issues)
    auto_pollution_codes = [
        _POLLUTION_ISSUE_CODES[code]
        for code in issues
        if code in _POLLUTION_ISSUE_CODES and code != "stock_not_available"
    ]
    resolved_stockout = stockout or auto_stockout
    resolved_pollution = pollution or (
        auto_pollution_codes[0] if auto_pollution_codes else None
    )
    exposures = view.get("exposures")
    clicks = view.get("clicks")
    conversions = view.get("conversions")
    return DiagnosisFacts(
        sku_id=sku_id,
        evidence_state=view.get("evidence_state"),
        freshness=view.get("freshness"),
        quality_gate=gate_status,
        quality_gate_issues=issues,
        stockout=resolved_stockout,
        pollution=resolved_pollution,
        exposures=float(exposures) if exposures is not None else None,
        clicks=float(clicks) if clicks is not None else None,
        conversions=float(conversions) if conversions is not None else None,
        degraded=resolved_stockout or resolved_pollution is not None,
    )


def validate_diagnosis_output(
    facts: DiagnosisFacts,
    produced: Mapping[str, Any],
) -> Diagnosis:
    """校验模型产出的语义诊断（D-034：代码只校验，不替模型选类型）。

    失败暴露（零静默）：
    - 类型不在白名单 → ValueError("diagnosis_type_not_allowlisted")
    - 命中 FORBIDDEN_KEYS（递归，含嵌套/自然语言）→ ValueError("forbidden_output_key_recursive")
    - 可执行前提不成立（结论不允许仍给强方向）→ ValueError("diagnosis_conclusion_not_allowed")
    """
    diagnosis_type_raw = produced.get("diagnosis_type")
    try:
        diagnosis_type = DiagnosisType(diagnosis_type_raw)
    except (ValueError, TypeError):
        raise ValueError(f"diagnosis_type_not_allowlisted:{diagnosis_type_raw}")
    if contains_forbidden_token(produced, FORBIDDEN_DIAGNOSIS_KEYS):
        raise ValueError("forbidden_output_key_recursive")
    strong_types = {
        DiagnosisType.EXPOSURE_INSUFFICIENT,
        DiagnosisType.CLICK_INSUFFICIENT,
        DiagnosisType.CONVERSION_INSUFFICIENT,
    }
    # 污染标记（degraded 降级）不是「强方向结论」，且必须与 facts 的污染旗标一致
    pollution_types = {
        DiagnosisType.STOCKOUT_POLLUTION,
        DiagnosisType.AD_PRICE_POLLUTION,
    }
    if diagnosis_type in strong_types and not facts.conclusion_allowed():
        raise ValueError("diagnosis_conclusion_not_allowed")
    if diagnosis_type in pollution_types:
        if not (facts.stockout or facts.pollution is not None):
            raise ValueError("diagnosis_pollution_marker_without_pollution")
    return Diagnosis(
        diagnosis_type=diagnosis_type,
        sku_id=facts.sku_id,
        reason=produced.get("reason"),
        evidence_facts={
            "evidence_state": facts.evidence_state,
            "freshness": facts.freshness,
            "quality_gate": facts.quality_gate,
            "quality_gate_issues": list(facts.quality_gate_issues),
            "exposures": facts.exposures,
            "clicks": facts.clicks,
            "conversions": facts.conversions,
            "stockout": facts.stockout,
            "pollution": facts.pollution,
        },
        degraded=facts.degraded,
    )


__all__ = [
    "FORBIDDEN_DIAGNOSIS_KEYS",
    "Diagnosis",
    "DiagnosisFacts",
    "DiagnosisType",
    "build_diagnosis_facts",
    "validate_diagnosis_output",
]
