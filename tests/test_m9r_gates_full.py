"""M9-R WP2 GateEngine 全部门禁测试（G5 反假绿）。

验证 run_all 返回 7 个 gate（evidence/freshness/quality_gate/aa/sample_size/
window/control_variables），并复用 M5-R issue 码反查对应 gate。
"""
from __future__ import annotations

from ecommerce_agent.product_diagnosis.gates import GateEngine


def _view(issues: list[str] | None = None, status: str = "passed") -> dict:
    return {
        "evidence_state": "actual",
        "freshness": {"usable_as_current": True},
        "quality_gate": {"status": status, "issues": issues or []},
    }


def test_run_all_returns_seven_gates() -> None:
    """run_all 返回 7 个 gate（显式枚举 A/A/样本/窗口/控制变量）。"""
    engine = GateEngine()
    all_passed, gates = engine.run_all(_view())
    names = {g.name for g in gates}
    assert names == {
        "evidence", "freshness", "quality_gate", "aa",
        "sample_size", "window", "control_variables",
    }
    assert all_passed is True


def test_aa_gate_detects_missing() -> None:
    """quality_gate.issues 含 aa_gate_missing → check_aa failed。"""
    engine = GateEngine()
    gate = engine.check_aa(_view(issues=["aa_gate_missing"]))
    assert gate.passed is False
    assert "aa_gate_missing" in gate.reason


def test_sample_size_gate_detects_insufficient() -> None:
    """quality_gate.issues 含 minimum_exposure_not_met → check_sample_size failed。"""
    engine = GateEngine()
    gate = engine.check_sample_size(_view(issues=["minimum_exposure_not_met"]))
    assert gate.passed is False
    assert "minimum_exposure_not_met" in gate.reason


def test_window_gate_detects_gap() -> None:
    """quality_gate.issues 含 experiment_window_gap → check_window failed。"""
    engine = GateEngine()
    gate = engine.check_window(_view(issues=["experiment_window_gap"]))
    assert gate.passed is False


def test_control_variable_gate_detects_change() -> None:
    """quality_gate.issues 含 control_variable_changed → check_control_variables failed。"""
    engine = GateEngine()
    gate = engine.check_control_variables(_view(issues=["control_variable_changed"]))
    assert gate.passed is False


def test_run_all_fails_on_aa_issue() -> None:
    """任一 issue 命中 → run_all 返回 all_passed=False（fail-closed）。"""
    engine = GateEngine()
    all_passed, gates = engine.run_all(_view(issues=["aa_gate_missing"]))
    assert all_passed is False
    aa_gate = next(g for g in gates if g.name == "aa")
    assert aa_gate.passed is False


def test_missing_quality_gate_fail_closed() -> None:
    """quality_gate 缺失 → 7 gate 全 failed（不复现 M5-R 未给的结论）。"""
    engine = GateEngine()
    view = {
        "evidence_state": "actual",
        "freshness": {"usable_as_current": True},
        # 无 quality_gate
    }
    all_passed, gates = engine.run_all(view)
    assert all_passed is False
    assert any(g.name == "quality_gate" and g.passed is False for g in gates)
