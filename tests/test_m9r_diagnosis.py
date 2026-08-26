"""M9-R WP2 流量诊断测试：确定性事实 + 语义校验（对齐 D-034）。

D-034：确定性代码只产出可执行事实，语义类型由解释器产出、代码只校验。
验收点：事实值与证据一致、门禁能拒绝污染方向、白名单/越权递归生效、
       解释器链路端到端可跑（不把 stub 的类型选择当验收断言）。
"""
from __future__ import annotations

import pytest

from ecommerce_agent.product_diagnosis.diagnosis import (
    FORBIDDEN_DIAGNOSIS_KEYS,
    DiagnosisType,
    build_diagnosis_facts,
    validate_diagnosis_output,
)
from ecommerce_agent.product_diagnosis.interpreter import (
    RulesetDiagnosisInterpreter,
    run_interpretation,
)


def test_stockout_pollution_facts_deny_conclusion() -> None:
    """缺货污染 → 事实 degraded + conclusion_allowed=False（不归因标题/主图）。"""
    facts = build_diagnosis_facts(
        "sku1", {"evidence_state": "actual", "exposures": 50, "clicks": 10},
        stockout=True,
    )
    assert facts.stockout is True
    assert facts.degraded is True
    assert facts.conclusion_allowed() is False


def test_pollution_facts_marked_degraded() -> None:
    """广告/价格污染 → 事实 degraded + 污染旗标。"""
    facts = build_diagnosis_facts(
        "sku1", {"evidence_state": "actual"}, pollution="ad_change"
    )
    assert facts.pollution == "ad_change"
    assert facts.conclusion_allowed() is False


def test_evidence_missing_denies_conclusion() -> None:
    """证据缺失 → conclusion_allowed=False（不能下结论）。"""
    facts = build_diagnosis_facts("sku1", {"evidence_state": "missing"})
    assert facts.evidence_state == "missing"
    assert facts.conclusion_allowed() is False


def test_quality_gate_blocked_denies_conclusion() -> None:
    """quality_gate=blocked（缺 A/A/样本/窗口等）→ 不能给强方向结论。"""
    facts = build_diagnosis_facts(
        "sku1",
        {
            "evidence_state": "actual",
            "exposures": 1000,
            "clicks": 100,
            "quality_gate": {"status": "blocked", "issues": ["aa_gate_missing"]},
        },
    )
    assert facts.quality_gate == "blocked"
    assert facts.conclusion_allowed() is False


def test_clean_facts_allow_conclusion() -> None:
    """证据可用 + 门禁通过 + freshness 可用 + 无污染 → conclusion_allowed=True。"""
    facts = build_diagnosis_facts(
        "sku1",
        {
            "evidence_state": "actual",
            "exposures": 1000,
            "clicks": 100,
            "quality_gate": {"status": "passed", "issues": []},
            "freshness": {"usable_as_current": True},
        },
    )
    assert facts.conclusion_allowed() is True


def test_missing_freshness_rejects_conclusion() -> None:
    """P2 修复（WP5 反例①）：freshness 缺失（None）→ conclusion_allowed=False。

    复验暴露：freshness=None 时旧代码跳过检查返回 True（fail-open），
    反转后缺失一律拒绝（fail-closed）。
    """
    facts = build_diagnosis_facts(
        "sku1",
        {
            "evidence_state": "actual",
            "exposures": 1000,
            "clicks": 100,
            "quality_gate": {"status": "passed", "issues": []},
        },
    )
    assert facts.freshness is None
    assert facts.conclusion_allowed() is False


def test_validate_rejects_not_allowlisted_type() -> None:
    """解释器产出白名单外类型 → 拒绝。"""
    facts = build_diagnosis_facts(
        "sku1", {"evidence_state": "actual", "quality_gate": {"status": "passed"}}
    )
    with pytest.raises(ValueError, match="diagnosis_type_not_allowlisted"):
        validate_diagnosis_output(facts, {"diagnosis_type": "magic_answer"})


def test_validate_rejects_forbidden_recursive() -> None:
    """解释器产出含 effect/平台权重（嵌套/自然语言）→ 拒绝。"""
    facts = build_diagnosis_facts(
        "sku1", {"evidence_state": "actual", "quality_gate": {"status": "passed"}}
    )
    with pytest.raises(ValueError, match="forbidden_output_key"):
        validate_diagnosis_output(
            facts, {"diagnosis_type": "click_insufficient", "details": {"effect": 0.5}}
        )
    with pytest.raises(ValueError, match="forbidden_output_key"):
        validate_diagnosis_output(
            facts,
            {"diagnosis_type": "click_insufficient", "notes": ["平台权重提升20%"]},
        )


def test_validate_rejects_strong_conclusion_when_blocked() -> None:
    """门禁 blocked 时解释器仍给强方向类型 → 拒绝（不编造结论）。"""
    facts = build_diagnosis_facts(
        "sku1",
        {
            "evidence_state": "actual",
            "quality_gate": {"status": "blocked", "issues": ["aa_gate_missing"]},
        },
    )
    with pytest.raises(ValueError, match="diagnosis_conclusion_not_allowed"):
        validate_diagnosis_output(
            facts, {"diagnosis_type": "exposure_insufficient", "reason": "low exp"}
        )


def test_interpreter_chain_end_to_end() -> None:
    """解释器链路端到端可跑（facts → interpret → validate），产出合法诊断。"""
    facts = build_diagnosis_facts(
        "sku1",
        {
            "evidence_state": "actual",
            "exposures": 50,
            "clicks": 10,
            "quality_gate": {"status": "passed", "issues": []},
            "freshness": {"usable_as_current": True},
        },
    )
    diagnosis = run_interpretation(facts, RulesetDiagnosisInterpreter())
    assert diagnosis.diagnosis_type is DiagnosisType.EXPOSURE_INSUFFICIENT
    assert diagnosis.evidence_facts["exposures"] == 50.0
