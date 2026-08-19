"""M9-R WP4 机制 Eval runner：复用冻结场景 + 确定性事实 + 语义校验（D-034）。

边界声明：
- 输入：冻结场景集 + 诊断解释器。
- 输出：EvalResult（每个场景的通过/失败）。
- 副作用：零——纯派生，不写库、不调用模型。
- 复用边界：场景 runner 复用 F-121/F-122 的 simulation-evidence-v1 契约精神
  （输入/预期/断言），不另建第二套通用 runner；M9 领域场景在此新增。
- 失败暴露：诊断校验抛异常 → 场景记为失败（不静默）。
- 确定性：场景输入固定 → 输出确定性断言。
- D-034：确定性代码只产出可执行事实 + 校验；语义类型由解释器产出。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ecommerce_agent.product_diagnosis.diagnosis import (
    DiagnosisFacts,
    build_diagnosis_facts,
)
from ecommerce_agent.product_diagnosis.interpreter import (
    DiagnosisInterpreter,
    RulesetDiagnosisInterpreter,
    run_interpretation,
)

from .scenes import FrozenScene


@dataclass
class EvalResult:
    """单个场景的 Eval 结果（不可变数据）。"""

    scene_name: str
    passed: bool
    failures: list[str] = field(default_factory=list)


class MechanismEvalRunner:
    """机制 Eval：对每个冻结场景跑 oracle 断言。

    用法：
      runner = MechanismEvalRunner()
      results = runner.run_all()   # 对 FROZEN_SCENES 逐场景断言
    """

    def __init__(
        self,
        scenes: list[FrozenScene] | None = None,
        interpreter: DiagnosisInterpreter | None = None,
        facts_fn: Callable = build_diagnosis_facts,
    ) -> None:
        self.scenes = scenes or _default_scenes()
        self.interpreter = interpreter or RulesetDiagnosisInterpreter()
        self.facts_fn = facts_fn

    def run_scene(self, scene: FrozenScene) -> EvalResult:
        """跑单场景：输入 → 确定性事实 → 解释器 → 校验 → oracle 断言。"""
        input_data = scene.input_data
        try:
            facts: DiagnosisFacts = self.facts_fn(
                input_data["sku_id"],
                {
                    "evidence_state": input_data.get("evidence_state"),
                    "exposures": input_data.get("exposures"),
                    "clicks": input_data.get("clicks"),
                    "conversions": input_data.get("conversions"),
                    "quality_gate": input_data.get("quality_gate"),
                },
                stockout=input_data.get("stockout", False),
                pollution=input_data.get("pollution"),
            )
            diag = run_interpretation(facts, self.interpreter)
            produced = {
                "diagnosis_type": diag.diagnosis_type.value,
                "degraded": diag.degraded,
                "reason": diag.reason,
            }
        except Exception as exc:  # noqa: BLE001
            return EvalResult(scene.name, False, [f"eval_error:{exc}"])
        failures = scene.run_oracle(produced)
        return EvalResult(scene.name, not failures, failures)

    def run_all(self) -> list[EvalResult]:
        """跑全部冻结场景。空场景集 → 返回空（调用方应确保非空）。"""
        return [self.run_scene(scene) for scene in self.scenes]

    def summary(self) -> tuple[int, int]:
        """(passed_count, total_count)——确定性汇总。"""
        results = self.run_all()
        passed = sum(1 for r in results if r.passed)
        return passed, len(results)


def _default_scenes() -> list[FrozenScene]:
    from .scenes import FROZEN_SCENES
    return FROZEN_SCENES


__all__ = [
    "EvalResult",
    "MechanismEvalRunner",
]
