"""M9-R WP2 Gate 确定性测试：全部门判定不依赖时间/随机/外部状态。

对齐验收标准：条目 4（Gate 通过才给强方向结论）、条目 5（freshness Gate）、
条目 7（模型越权输出整份拒绝）、条目 10（写屏障平台写=0）。
"""
from __future__ import annotations

from ecommerce_agent.product_diagnosis.gates import FORBIDDEN_KEYS, GateEngine, GateResult


def test_gate_evidence_passes_when_actual() -> None:
    engine = GateEngine()
    result = engine.check_evidence({"evidence_state": "actual"})
    assert result.passed is True


def test_gate_evidence_fails_when_missing() -> None:
    engine = GateEngine()
    result = engine.check_evidence({"evidence_state": "missing"})
    assert result.passed is False
    assert result.reason == "evidence_missing"


def test_gate_freshness_passes_when_usable() -> None:
    engine = GateEngine()
    result = engine.check_freshness({
        "freshness": {
            "policy_version": "evidence-freshness-v1",
            "status": "current",
            "usable_as_current": True,
            "reason_codes": [],
        }
    })
    assert result.passed is True


def test_gate_freshness_fails_when_stale() -> None:
    engine = GateEngine()
    result = engine.check_freshness({
        "freshness": {
            "policy_version": "evidence-freshness-v1",
            "status": "stale",
            "usable_as_current": False,
            "reason_codes": ["age"],
        }
    })
    assert result.passed is False
    assert "freshness_not_current" in (result.reason or "")


def test_gate_freshness_fails_when_missing() -> None:
    engine = GateEngine()
    result = engine.check_freshness({"freshness": None})
    assert result.passed is False
    assert result.reason == "freshness_missing"


def test_gate_forbidden_output_rejected() -> None:
    """越权输出：禁止键命中即整体拒绝（对齐验收条目 7）。"""
    engine = GateEngine()
    # 模型想声称「平台权重」或「效果区间」→ 必须拒绝
    result = engine.check_no_forbidden_output({"effect": 0.5, "diagnosis": "x"})
    assert result.passed is False
    assert "forbidden_output_key" in (result.reason or "")
    clean = engine.check_no_forbidden_output({"diagnosis_type": "x"})
    assert clean.passed is True


def test_gate_run_all_gates_passed() -> None:
    engine = GateEngine()
    all_passed, results = engine.run_all({
        "evidence_state": "actual",
        "freshness": {"usable_as_current": True},
    })
    assert all_passed is True
    assert all(isinstance(r, GateResult) for r in results)
    assert len(results) == 2


def test_gate_run_all_fails_on_missing_evidence() -> None:
    engine = GateEngine()
    all_passed, results = engine.run_all({"evidence_state": "missing"})
    assert all_passed is False
    assert results[0].name == "evidence"
    assert results[0].passed is False


def test_forbidden_keys_include_platform_claims() -> None:
    """禁止键清单确定性：平台权重/算法必须在列。"""
    assert "平台权重" in FORBIDDEN_KEYS
    assert "平台算法" in FORBIDDEN_KEYS
