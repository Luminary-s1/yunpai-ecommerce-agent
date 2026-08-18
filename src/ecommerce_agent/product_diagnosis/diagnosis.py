"""M9-R WP2 结构化流量诊断。

边界声明：
- 输入：证据视图（revision/experiment view）+ 可选分析运行。
- 输出：Diagnosis 对象（冻结 pydantic 模型），或 None（无诊断可下时显式返回）。
- 副作用：零——纯派生，不写库、不调用模型。
- 确定性：诊断类型由字段值确定性推导；缺失字段 → reason 明确。

诊断类型（对齐任务书）：
- exposure_insufficient：曝光不足（曝光低且证据可用）
- click_insufficient：点击不足（曝光够但点击率低）
- conversion_insufficient：转化不足（点击够但转化低）
- stockout_pollution：缺货污染（缺货期间指标不可信）
- ad_price_pollution：广告/价格变更污染（实验窗口被污染）
- evidence_insufficient：证据不足（无法下诊断）
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict


class DiagnosisType(StrEnum):
    EXPOSURE_INSUFFICIENT = "exposure_insufficient"
    CLICK_INSUFFICIENT = "click_insufficient"
    CONVERSION_INSUFFICIENT = "conversion_insufficient"
    STOCKOUT_POLLUTION = "stockout_pollution"
    AD_PRICE_POLLUTION = "ad_price_pollution"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"


class Diagnosis(BaseModel):
    """结构化诊断（冻结，可追溯）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    diagnosis_type: DiagnosisType
    sku_id: str
    reason: str | None = None
    evidence_facts: dict[str, Any]  # 固化证据（引用来源，非模型编造）
    degraded: bool = False


# 确定性阈值（可随策略演进，改动须同步测试）
_THRESHOLDS: dict[str, float] = {
    "exposure_low": 100.0,    # 日曝光 < 100 → 曝光不足
    "ctr_low": 0.01,          # CTR < 1% → 点击不足
    "conv_low": 0.02,         # 转化率 < 2% → 转化不足
}


def build_diagnosis(
    sku_id: str,
    view: Mapping[str, Any],
    *,
    stockout: bool = False,
    pollution: str | None = None,
) -> Diagnosis:
    """从证据视图确定性推导诊断类型（无模型）。

    规则（确定性）：
    1. 缺货污染 → STOCKOUT_POLLUTION（优先，污染必须被明确标记）
    2. 广告/价格污染 → AD_PRICE_POLLUTION
    3. 证据不足（视图 evidence_state=missing）→ EVIDENCE_INSUFFICIENT
    4. 曝光 < 阈值 → EXPOSURE_INSUFFICIENT
    5. CTR < 阈值 → CLICK_INSUFFICIENT
    6. 转化 < 阈值 → CONVERSION_INSUFFICIENT
    7. 无命中 → EVIDENCE_INSUFFICIENT（带 reason="no_issue_detected"）
    """
    facts: dict[str, Any] = {
        "evidence_state": view.get("evidence_state"),
        "freshness": view.get("freshness"),
    }
    if stockout:
        return Diagnosis(
            diagnosis_type=DiagnosisType.STOCKOUT_POLLUTION,
            sku_id=sku_id, reason="stockout_period_observed",
            evidence_facts=facts, degraded=True,
        )
    if pollution is not None:
        return Diagnosis(
            diagnosis_type=DiagnosisType.AD_PRICE_POLLUTION,
            sku_id=sku_id, reason=f"pollution:{pollution}",
            evidence_facts=facts, degraded=True,
        )
    if view.get("evidence_state") in (None, "missing"):
        return Diagnosis(
            diagnosis_type=DiagnosisType.EVIDENCE_INSUFFICIENT,
            sku_id=sku_id, reason="evidence_missing",
            evidence_facts=facts,
        )
    exposures = view.get("exposures")
    clicks = view.get("clicks")
    conversions = view.get("conversions")
    if exposures is None or clicks is None:
        return Diagnosis(
            diagnosis_type=DiagnosisType.EVIDENCE_INSUFFICIENT,
            sku_id=sku_id, reason="metrics_fields_missing",
            evidence_facts=facts,
        )
    if exposures < _THRESHOLDS["exposure_low"]:
        return Diagnosis(
            diagnosis_type=DiagnosisType.EXPOSURE_INSUFFICIENT,
            sku_id=sku_id, reason=f"exposures_below_threshold:{exposures}",
            evidence_facts=facts,
        )
    ctr = clicks / exposures if exposures else 0.0
    if ctr < _THRESHOLDS["ctr_low"]:
        return Diagnosis(
            diagnosis_type=DiagnosisType.CLICK_INSUFFICIENT,
            sku_id=sku_id, reason=f"ctr_below_threshold:{ctr:.4f}",
            evidence_facts=facts,
        )
    if conversions is not None:
        conv_rate = conversions / clicks if clicks else 0.0
        if conv_rate < _THRESHOLDS["conv_low"]:
            return Diagnosis(
                diagnosis_type=DiagnosisType.CONVERSION_INSUFFICIENT,
                sku_id=sku_id, reason=f"conv_below_threshold:{conv_rate:.4f}",
                evidence_facts=facts,
            )
    return Diagnosis(
        diagnosis_type=DiagnosisType.EVIDENCE_INSUFFICIENT,
        sku_id=sku_id, reason="no_issue_detected",
        evidence_facts=facts,
    )


__all__ = [
    "Diagnosis",
    "DiagnosisType",
    "build_diagnosis",
]
