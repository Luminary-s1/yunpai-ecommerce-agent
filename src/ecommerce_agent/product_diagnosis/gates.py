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


@dataclass(frozen=True)
class GateResult:
    """单个 Gate 判定结果（不可变）。"""

    name: str
    passed: bool
    reason: str | None = None


# 模型越权输出禁止键（命中即整体拒绝）——确定性清单，随输出 schema 演进
FORBIDDEN_KEYS: frozenset[str] = frozenset({
    "effect",
    "interval",
    "sample_size",
    "gate",
    "平台权重",
    "平台算法",
})


class GateEngine:
    """确定性 Gate 组合：全部通过才给强方向结论。

    用法：engine.run_all(view) → (all_passed, [GateResult, ...])
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
        """越权输出 Gate：禁止键命中即整体拒绝。"""
        for key in FORBIDDEN_KEYS:
            if key in output:
                return GateResult("output_scope", False, f"forbidden_output_key:{key}")
        return GateResult("output_scope", True)

    def run_all(self, view: Mapping[str, Any]) -> tuple[bool, list[GateResult]]:
        """组合判定：全部通过 → (True, results)；任一失败 → (False, results)。"""
        results = [
            self.check_evidence(view),
            self.check_freshness(view),
        ]
        all_passed = all(result.passed for result in results)
        return all_passed, results


__all__ = [
    "FORBIDDEN_KEYS",
    "GateEngine",
    "GateResult",
]
