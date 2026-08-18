"""M9-R WP4 机制 Eval 测试：冻结场景 oracle 断言。

对齐验收标准：条目 4（Eval 发现真实方向 + 拒绝污染方向）、
条目 3（页面浏览无隐式写动作）。
"""
from __future__ import annotations

from ecommerce_agent.product_workbench.eval import EvalResult, MechanismEvalRunner
from ecommerce_agent.product_workbench.scenes import FROZEN_SCENES


def test_frozen_scenes_non_empty() -> None:
    """至少 2 个冻结场景（缺货污染/合格实验）。"""
    assert len(FROZEN_SCENES) >= 2


def test_eval_detects_stockout_pollution() -> None:
    """缺货污染场景：诊断必须标记 STOCKOUT_POLLUTION + degraded。"""
    runner = MechanismEvalRunner()
    results = runner.run_all()
    stockout = next(r for r in results if r.scene_name == "缺货污染")
    assert stockout.passed is True, stockout.failures


def test_eval_rejects_pollution_for_clean_data() -> None:
    """合格实验场景：无污染/无不足（不编造问题）。"""
    runner = MechanismEvalRunner()
    results = runner.run_all()
    clean = next(r for r in results if r.scene_name == "合格实验")
    assert clean.passed is True, clean.failures


def test_eval_summary_all_pass() -> None:
    """全部冻结场景通过 oracle。"""
    runner = MechanismEvalRunner()
    passed, total = runner.summary()
    assert passed == total
    assert total >= 2


def test_eval_result_type() -> None:
    runner = MechanismEvalRunner()
    results = runner.run_all()
    assert all(isinstance(r, EvalResult) for r in results)
