"""M9-R P2 修复验收：freshness 缺失必须 fail-closed（WP5 反例①）。

复验发现：`conclusion_allowed()` 里 freshness=None 时跳过检查返回 True
（fail-open），与"缺门禁信息一律拒绝"契约相反。修复后缺失一律拒绝。
"""
from __future__ import annotations

import pytest

from ecommerce_agent.product_diagnosis.diagnosis import (
    build_diagnosis_facts,
    validate_diagnosis_output,
)
from ecommerce_agent.product_diagnosis.interpreter import (
    RulesetDiagnosisInterpreter,
    run_interpretation,
)


def test_freshness_none_rejects_conclusion() -> None:
    """P2 修复核心：freshness=None → conclusion_allowed()=False。"""
    facts = build_diagnosis_facts(
        "sku1",
        {
            "evidence_state": "actual",
            "exposures": 1000,
            "clicks": 100,
            "conversions": 10,
            "quality_gate": {"status": "passed", "issues": []},
            # 显式不传 freshness → None
        },
    )
    assert facts.freshness is None
    assert facts.conclusion_allowed() is False


def test_freshness_usable_allows_strong_conclusion() -> None:
    """对照：freshness 可用时强方向结论可放行（不误伤）。"""
    facts = build_diagnosis_facts(
        "sku1",
        {
            "evidence_state": "actual",
            "exposures": 1000,
            "clicks": 100,
            "conversions": 10,
            "quality_gate": {"status": "passed", "issues": []},
            "freshness": {"usable_as_current": True},
        },
    )
    assert facts.conclusion_allowed() is True


def test_freshness_missing_blocks_strong_direction_interpreter() -> None:
    """反例①生产形态：freshness 缺失时解释器给强方向（EXPOSURE_INSUFFICIENT）
    必须被 validate_diagnosis_output 拒绝（fail-closed）。"""
    facts = build_diagnosis_facts(
        "sku1",
        {
            "evidence_state": "actual",
            "exposures": 50,
            "clicks": 10,
            "quality_gate": {"status": "passed", "issues": []},
        },
    )
    # RulesetDiagnosisInterpreter 对低曝光给 EXPOSURE_INSUFFICIENT（强方向）
    with pytest.raises(ValueError, match="diagnosis_conclusion_not_allowed"):
        run_interpretation(facts, RulesetDiagnosisInterpreter())


def test_freshness_stale_rejects_conclusion() -> None:
    """freshness 存在但不可用（stale）→ 拒绝（fail-closed 既有行为保留）。"""
    facts = build_diagnosis_facts(
        "sku1",
        {
            "evidence_state": "actual",
            "exposures": 1000,
            "clicks": 100,
            "quality_gate": {"status": "passed", "issues": []},
            "freshness": {"usable_as_current": False, "status": "stale"},
        },
    )
    assert facts.conclusion_allowed() is False
