"""M9-R WP4 机制 Eval runner：复用冻结场景 + 确定性事实 + 语义校验（D-034）。

边界声明：
- 输入：冻结场景集 + 诊断解释器 + 建议引擎。
- 输出：EvalResult（每个场景的通过/失败）。
- 副作用：零——纯派生，不写库、不调用模型。
- 复用边界：场景 runner 复用 F-121/F-122 的 simulation-evidence-v1 契约精神
  （输入/预期/断言），不另建第二套通用 runner；M9 领域场景在此新增。
- 失败暴露：诊断校验抛异常 → 场景记为失败（不静默）。
- 确定性：场景输入固定 → 输出确定性断言。
- D-034：确定性代码只产出可执行事实 + 校验；语义类型由解释器产出。
- 生产链路：诊断 → 建议 全链断言（diagnosis_type + recommendation_type），
  防止自洽假绿（只断言 degraded 不锁方向）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
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
from ecommerce_agent.product_lifecycle.engine import RecommendationEngine

from .scenes import FrozenScene

# 确定性：Eval 固定时点，无时间源（对齐 eval 纯派生边界）。
_EVAL_CREATED_AT = datetime(2026, 8, 20, tzinfo=UTC)


@dataclass
class EvalResult:
    """单个场景的 Eval 结果（不可变数据）。"""

    scene_name: str
    passed: bool
    failures: list[str] = field(default_factory=list)


class MechanismEvalRunner:
    """机制 Eval：对每个冻结场景跑 oracle 断言（诊断 + 建议全链）。

    用法：
      runner = MechanismEvalRunner()
      results = runner.run_all()   # 对 FROZEN_SCENES 逐场景断言
    """

    def __init__(
        self,
        scenes: list[FrozenScene] | None = None,
        interpreter: DiagnosisInterpreter | None = None,
        facts_fn: Callable = build_diagnosis_facts,
        recommendation_interpreter: Any = None,
    ) -> None:
        self.scenes = scenes or _default_scenes()
        self.interpreter = interpreter or RulesetDiagnosisInterpreter()
        self.facts_fn = facts_fn
        # R5（C-lite）：建议解释器可注入（默认 Ruleset），方向场景用 mock 建议
        # 解释器产出 SELECTION/NEW_LAUNCH/CLEARANCE → REQUIRED_FACTS 满足 → 非降级真实方向。
        self.recommendation_engine = RecommendationEngine(
            interpreter=recommendation_interpreter
        )

    def run_scene(self, scene: FrozenScene) -> EvalResult:
        """跑单场景：输入 → 确定性事实 → 解释器 → 建议引擎 → oracle 断言。"""
        input_data = scene.input_data
        try:
            # P2 fail-closed 契约（WP5 反例①）：场景必须显式声明 freshness，
            # 缺失即记失败——不让"没传 freshness"变成"跳过 freshness 检查"。
            if "freshness" not in input_data:
                return EvalResult(scene.name, False, ["scene_missing_freshness"])
            facts: DiagnosisFacts = self.facts_fn(
                input_data["sku_id"],
                {
                    "evidence_state": input_data.get("evidence_state"),
                    "exposures": input_data.get("exposures"),
                    "clicks": input_data.get("clicks"),
                    "conversions": input_data.get("conversions"),
                    "quality_gate": input_data.get("quality_gate"),
                    "freshness": input_data.get("freshness"),
                },
                stockout=input_data.get("stockout", False),
                pollution=input_data.get("pollution"),
            )
            diag = run_interpretation(facts, self.interpreter)
            # R5（C-lite）：场景可显式声明 required_signals（REQUIRED_FACTS 信号键），
            # 注入 Diagnosis.evidence_facts → _build_facts_snapshot 透传 → 满足 REQUIRED_FACTS
            # → 产出非降级真实方向。信号由场景/调用方注入（引擎不编造，D-034）；
            # 未声明 → 不注入（缺信号 → 显式降级）。
            required_signals = input_data.get("required_signals")
            if required_signals:
                diag.evidence_facts.update(
                    {k: v for k, v in required_signals.items() if v not in (None, False)}
                )
            produced = {
                "diagnosis_type": diag.diagnosis_type.value,
                "degraded": diag.degraded,
                "reason": diag.reason,
            }
            # 诊断 → 建议 生产链路：引擎产出建议候选（类型 + 理由），
            # 使 Eval 断言实际建议方向而非只验证 degraded。
            from ecommerce_agent.product_read_model.models import (
                AggregateRule,
                Granularity,
                MetricValue,
                SKUReadModel,
            )
            from ecommerce_agent.readonly_data.contracts import EvidenceState

            _missing = MetricValue.missing(
                Granularity.DAILY, AggregateRule.SUM, "2026-08-17", "eval"
            )
            sku = SKUReadModel(
                tenant_id="t1",
                store_id=input_data.get("store_id", "store-eval"),
                item_id="item-eval",
                sku_id=input_data["sku_id"],
                revision=1,
                impressions=_missing,
                clicks=_missing,
                add_to_cart=_missing,
                orders=_missing,
                payments=_missing,
                refunds=_missing,
                net_sales=_missing,
                sellable_stock=_missing,
                in_transit_stock=_missing,
            )
            rec = self.recommendation_engine.generate(
                tenant_id="t1",
                diagnosis=diag,
                sku=sku,
                recommendation_id="eval-rec",
                created_at=_EVAL_CREATED_AT,
            )
            produced["recommendation_type"] = rec.type.value
            # R5（C-lite）：追加建议层降级态 + 缺失证据键，供方向场景 oracle 锁
            # 真实方向（recommendation_type != 保持观察 + recommendation_degraded +
            # missing_evidence 精确键）。多余键对既有 oracle 无副作用（run_oracle 只遍历
            # expected 键）。
            produced["recommendation_degraded"] = rec.degraded
            produced["missing_evidence"] = list(rec.missing_evidence)
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
