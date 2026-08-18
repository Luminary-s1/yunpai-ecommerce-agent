"""M9-R WP2 结构化诊断测试：build_diagnosis 确定性推导。

对齐验收标准：条目 6（缺货/广告/价格污染不被归因标题/主图）、条目 8（无合格实验不编造 uplift）。
"""
from __future__ import annotations

from ecommerce_agent.product_diagnosis.diagnosis import DiagnosisType, build_diagnosis


def test_stockout_pollution_priority() -> None:
    """缺货污染 → 标记为 STOCKOUT_POLLUTION（不归因标题/主图）。"""
    diag = build_diagnosis(
        "sku1", {"evidence_state": "actual", "exposures": 50, "clicks": 10},
        stockout=True,
    )
    assert diag.diagnosis_type is DiagnosisType.STOCKOUT_POLLUTION
    assert diag.degraded is True


def test_ad_price_pollution() -> None:
    diag = build_diagnosis(
        "sku1", {"evidence_state": "actual"}, pollution="ad_change"
    )
    assert diag.diagnosis_type is DiagnosisType.AD_PRICE_POLLUTION
    assert "ad_change" in (diag.reason or "")


def test_evidence_missing_gives_insufficient() -> None:
    diag = build_diagnosis("sku1", {"evidence_state": "missing"})
    assert diag.diagnosis_type is DiagnosisType.EVIDENCE_INSUFFICIENT


def test_exposure_below_threshold() -> None:
    diag = build_diagnosis(
        "sku1", {"evidence_state": "actual", "exposures": 50, "clicks": 10}
    )
    assert diag.diagnosis_type is DiagnosisType.EXPOSURE_INSUFFICIENT


def test_click_insufficient_when_ctr_low() -> None:
    # 曝光 1000（够），点击 5 → CTR 0.5% < 1% → 点击不足
    diag = build_diagnosis(
        "sku1", {"evidence_state": "actual", "exposures": 1000, "clicks": 5}
    )
    assert diag.diagnosis_type is DiagnosisType.CLICK_INSUFFICIENT


def test_conversion_insufficient_when_conv_low() -> None:
    # 曝光 1000，点击 100（CTR 10%），转化 1 → 转化率 1% < 2% → 转化不足
    diag = build_diagnosis(
        "sku1", {"evidence_state": "actual", "exposures": 1000, "clicks": 100,
                 "conversions": 1}
    )
    assert diag.diagnosis_type is DiagnosisType.CONVERSION_INSUFFICIENT


def test_no_issue_detected_is_evidence_insufficient() -> None:
    """无命中 → EVIDENCE_INSUFFICIENT（不编造问题）。"""
    diag = build_diagnosis(
        "sku1", {"evidence_state": "actual", "exposures": 1000, "clicks": 100,
                 "conversions": 50}
    )
    assert diag.diagnosis_type is DiagnosisType.EVIDENCE_INSUFFICIENT
    assert diag.reason == "no_issue_detected"
