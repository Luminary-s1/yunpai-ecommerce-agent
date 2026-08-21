"""M9-R WP2 确定性 Gate：实验/诊断的确定性质量门。

边界声明：
- 输入：实验证据视图（dict）+ 查询参数。
- 输出：GateResult(passed: bool, reason: str | None)——确定性，无模型调用。
- 副作用：零。
- 失败暴露：缺必需字段 → passed=False + 明确 reason（不静默通过）。
- 确定性：不依赖时间源/随机/外部状态；所有判定基于输入字段。

复用边界：统计计算在 TrafficAnalysisEngine（M5-R）；本层只做确定性门判定。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ecommerce_agent.text_utils import contains_forbidden_token


@dataclass(frozen=True)
class GateResult:
    """单个 Gate 判定结果（不可变）。"""

    name: str
    passed: bool
    reason: str | None = None


# 模型越权输出禁止键（命中即整体拒绝）——与 WP3 建议门禁共用完整集
# （安全审查 #2：诊断/建议门禁标准必须一致，防越权表述穿透）
FORBIDDEN_KEYS: frozenset[str] = frozenset({
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


# M5-R issue 码 → M9-R 显式 gate 权威映射（复用 M5-R 预计算结果，不重算统计）。
# 枚举变化须同步此处 + 测试锁定（防 M5-R 改 issue 码后 M9-R 门禁误判）。
_AA_ISSUE_CODES: frozenset[str] = frozenset({
    "aa_gate_missing", "aa_gate_stale", "aa_gate_failed",
    "aa_false_positive_detected",
})
_SAMPLE_SIZE_ISSUE_CODES: frozenset[str] = frozenset({
    "assignment_buckets_insufficient", "minimum_exposure_not_met",
    "normal_approximation_unreliable", "analysis_samples_missing",
})
_WINDOW_ISSUE_CODES: frozenset[str] = frozenset({
    "experiment_windows_missing", "experiment_window_gap", "experiment_window_overlap",
    "revision_window_gap", "revision_window_overlap", "source_receipt_missing",
})
_CONTROL_VARIABLE_ISSUE_CODES: frozenset[str] = frozenset({
    "control_variable_missing", "control_variable_changed",
    "treatment_variable_missing", "multiple_treatment_variables_changed",
    "unplanned_revision_attributes_changed", "stock_not_available",
    "sale_price_changed",
})


def _quality_gate_issues(view: Mapping[str, Any]) -> tuple[str, ...]:
    """从证据视图提取 quality_gate.issues（M5-R 权威 issue 列表）。"""
    gate = view.get("quality_gate")
    if isinstance(gate, Mapping):
        return tuple(gate.get("issues") or ())
    return ()


def _quality_gate_present(view: Mapping[str, Any]) -> bool:
    """quality_gate 是否以 passed 状态存在（子 gate 的前提）。"""
    gate = view.get("quality_gate")
    if isinstance(gate, Mapping):
        return gate.get("status") == "passed"
    return gate == "passed"


class GateEngine:
    """确定性 Gate 组合：全部通过才给强方向结论。

    用法：engine.run_all(view) → (all_passed, [GateResult, ...])

    7 个 gate：evidence / freshness / quality_gate（总开关）+ aa / sample_size /
    window / control_variables（显式枚举，复用 M5-R issue 码反查，不重算统计）。
    """

    def __init__(self) -> None:
        # 确定性：无状态，纯函数组合
        pass

    @staticmethod
    def check_evidence(view: Mapping[str, Any]) -> GateResult:
        """证据 Gate：evidence_state 必须非 missing。"""
        state = view.get("evidence_state")
        if state in (None, "missing"):
            return GateResult("evidence", False, "evidence_missing")
        return GateResult("evidence", True)

    @staticmethod
    def check_freshness(view: Mapping[str, Any]) -> GateResult:
        """freshness Gate：usable_as_current 必须为 true（evidence-freshness-v1）。"""
        freshness = view.get("freshness")
        if freshness is None:
            return GateResult("freshness", False, "freshness_missing")
        if freshness.get("usable_as_current") is not True:
            return GateResult(
                "freshness", False,
                f"freshness_not_current:{freshness.get('status')}",
            )
        return GateResult("freshness", True)

    @staticmethod
    def check_no_forbidden_output(output: Mapping[str, Any]) -> GateResult:
        """越权输出 Gate：禁止键（含嵌套/自然语言）命中即整体拒绝。"""
        if contains_forbidden_token(output, FORBIDDEN_KEYS):
            return GateResult("output_scope", False, "forbidden_output_key_recursive")
        return GateResult("output_scope", True)

    @staticmethod
    def check_quality_gate(view: Mapping[str, Any]) -> GateResult:
        """quality_gate：M5-R 已把 A/A/样本/实际窗口/控制变量/污染折进 status。

        只有 status == "passed" 才允许强方向结论（对齐 strong_conclusion_allowed）。
        """
        gate = view.get("quality_gate")
        if isinstance(gate, Mapping):
            status = gate.get("status")
            issues = gate.get("issues") or ()
        else:
            status = gate
            issues = ()
        if status != "passed":
            detail = ",".join(str(i) for i in issues) or (str(status) if status else "missing")
            return GateResult("quality_gate", False, f"quality_gate_not_passed:{detail}")
        return GateResult("quality_gate", True)

    @staticmethod
    def check_aa(view: Mapping[str, Any]) -> GateResult:
        """A/A Gate：M5-R issue 码反查 aa_gate_*，命中即失败。

        quality_gate 缺失/未通过时 → failed（fail-closed，不假装"检查过且通过"）。
        """
        if not _quality_gate_present(view):
            return GateResult("aa", False, "aa_gate_unchecked_quality_gate_missing")
        issues = _quality_gate_issues(view)
        hit = [i for i in issues if i in _AA_ISSUE_CODES]
        if hit:
            return GateResult("aa", False, f"aa_gate_issue:{','.join(hit)}")
        return GateResult("aa", True)

    @staticmethod
    def check_sample_size(view: Mapping[str, Any]) -> GateResult:
        """样本量 Gate：M5-R issue 码反查样本不足，命中即失败。"""
        if not _quality_gate_present(view):
            return GateResult("sample_size", False, "sample_size_unchecked_quality_gate_missing")
        issues = _quality_gate_issues(view)
        hit = [i for i in issues if i in _SAMPLE_SIZE_ISSUE_CODES]
        if hit:
            return GateResult("sample_size", False, f"sample_size_issue:{','.join(hit)}")
        return GateResult("sample_size", True)

    @staticmethod
    def check_window(view: Mapping[str, Any]) -> GateResult:
        """实际窗口 Gate：M5-R issue 码反查窗口 gap/overlap/缺失，命中即失败。"""
        if not _quality_gate_present(view):
            return GateResult("window", False, "window_unchecked_quality_gate_missing")
        issues = _quality_gate_issues(view)
        hit = [i for i in issues if i in _WINDOW_ISSUE_CODES]
        if hit:
            return GateResult("window", False, f"window_issue:{','.join(hit)}")
        return GateResult("window", True)

    @staticmethod
    def check_control_variables(view: Mapping[str, Any]) -> GateResult:
        """控制变量 Gate：M5-R issue 码反查控制变量缺失/变更，命中即失败。"""
        if not _quality_gate_present(view):
            return GateResult("control_variables", False, "control_variables_unchecked_quality_gate_missing")
        issues = _quality_gate_issues(view)
        hit = [i for i in issues if i in _CONTROL_VARIABLE_ISSUE_CODES]
        if hit:
            return GateResult("control_variables", False, f"control_variable_issue:{','.join(hit)}")
        return GateResult("control_variables", True)

    def run_all(self, view: Mapping[str, Any]) -> tuple[bool, list[GateResult]]:
        """组合判定（7 gate）：全部通过 → (True, results)；任一失败 → (False, results)。

        越权输出检查作用于模型输出（见 EvidenceBridge.run_gates 的 model_output 参数），
        不在此处对证据视图做子串匹配——视图含 quality_gate/effect_estimate 等合法键，
        误杀会破坏门禁。
        """
        results = [
            self.check_evidence(view),
            self.check_freshness(view),
            self.check_quality_gate(view),
            self.check_aa(view),
            self.check_sample_size(view),
            self.check_window(view),
            self.check_control_variables(view),
        ]
        all_passed = all(result.passed for result in results)
        return all_passed, results


__all__ = [
    "FORBIDDEN_KEYS",
    "GateEngine",
    "GateResult",
]
