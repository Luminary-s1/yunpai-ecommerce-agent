"""M9-R WP4 机制 Eval 测试：冻结场景 oracle 断言。

对齐验收标准：条目 4（Eval 发现真实方向 + 拒绝污染方向）、
条目 3（页面浏览无隐式写动作）。
"""
from __future__ import annotations

from ecommerce_agent.product_workbench.eval import EvalResult, MechanismEvalRunner
from ecommerce_agent.product_workbench.scenes import FROZEN_SCENES


def test_frozen_scenes_non_empty() -> None:
    """至少 7 个冻结场景（覆盖任务书七类方向）。"""
    assert len(FROZEN_SCENES) >= 7


def test_eval_detects_stockout_pollution() -> None:
    """缺货污染场景：诊断必须标记 STOCKOUT_POLLUTION + degraded。"""
    runner = MechanismEvalRunner()
    results = runner.run_all()
    stockout = next(r for r in results if r.scene_name == "缺货污染")
    assert stockout.passed is True, stockout.failures


def test_eval_rejects_pollution_for_clean_data() -> None:
    """合格数据场景：无污染/无不足（不编造问题）。"""
    runner = MechanismEvalRunner()
    results = runner.run_all()
    clean = next(r for r in results if r.scene_name == "存量保持")
    assert clean.passed is True, clean.failures


def test_eval_rejects_pollution_marker_without_pollution() -> None:
    """反证：解释器对无污染的干净数据给 STOCKOUT_POLLUTION → 校验拒绝。"""
    from ecommerce_agent.product_diagnosis.diagnosis import (
        build_diagnosis_facts,
        validate_diagnosis_output,
    )

    facts = build_diagnosis_facts(
        "sku-x",
        {
            "evidence_state": "actual",
            "exposures": 5000,
            "clicks": 400,
            "quality_gate": {"status": "passed", "issues": []},
        },
    )
    try:
        validate_diagnosis_output(
            facts,
            {"diagnosis_type": "stockout_pollution", "reason": "fake"},
        )
        assert False, "should reject pollution marker without pollution"
    except ValueError:
        pass


def test_eval_summary_all_pass() -> None:
    """全部冻结场景通过 oracle。"""
    runner = MechanismEvalRunner()
    passed, total = runner.summary()
    assert passed == total
    assert total >= 7


def test_eval_covers_all_seven_directions() -> None:
    """冻结场景覆盖任务书七类方向。"""
    names = {scene.name for scene in FROZEN_SCENES}
    assert {
        "选品方向", "上新准备", "存量保持", "受控优化",
        "缺货污染", "缺数据", "清仓风险",
    } <= names


def test_eval_result_type() -> None:
    runner = MechanismEvalRunner()
    results = runner.run_all()
    assert all(isinstance(r, EvalResult) for r in results)
