"""M9-R WP4 机制 Eval runner：复用冻结场景 + 确定性事实 + 语义校验（D-034）。

边界声明：
- 输入：冻结场景集 + 诊断解释器 + 建议引擎。
- 输出：EvalResult（每个场景的通过/失败）。
- 副作用：不写库；默认固定解释器不调用模型，显式注入生产模型解释器时会发起模型调用。
- 复用边界：场景 runner 复用 F-121/F-122 的 simulation-evidence-v1 契约精神
  （输入/预期/断言），不另建第二套通用 runner；M9 领域场景在此新增。
- 失败暴露：诊断校验抛异常 → 场景记为失败（不静默）。
- 确定性：场景输入固定 → 输出确定性断言。
- D-034：确定性代码只产出可执行事实 + 校验；语义类型由解释器产出。
- 生产链路：诊断 → 建议 全链断言（diagnosis_type + recommendation_type），
  防止自洽假绿（只断言 degraded 不锁方向）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from ecommerce_agent.product_diagnosis.diagnosis import (
    DiagnosisFacts,
    build_diagnosis_facts,
)
from ecommerce_agent.product_diagnosis.interpreter import (
    DiagnosisInterpreter,
    RulesetDiagnosisInterpreter,
    run_interpretation,
)
from ecommerce_agent.product_lifecycle.engine import (
    RecommendationEngine,
    RulesetRecommendationInterpreter,
)
from ecommerce_agent.product_lifecycle.schemas import RecommendationType
from ecommerce_agent.product_read_model.models import (
    AggregateRule,
    Granularity,
    ListingRevisionEvidence,
    MetricValue,
    SKUReadModel,
)
from ecommerce_agent.readonly_data.contracts import EvidenceState

from .scenes import FrozenScene

# 确定性：Eval 固定时点，无时间源（对齐 eval 纯派生边界）。
_EVAL_CREATED_AT = datetime(2026, 8, 20, tzinfo=UTC)
_FORBIDDEN_PRODUCTION_INPUT_KEYS = frozenset({
    "expected",
    "oracle",
    "recommendation_type",
    "recommendation_degraded",
    "required_signals",
})


@dataclass
class EvalResult:
    """单个场景的 Eval 结果（不可变数据）。"""

    scene_name: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    produced: dict[str, Any] = field(default_factory=dict)


def assert_direction_inputs_oracle_free(
    scenes: list[object],
) -> list[dict[str, str]]:
    """Audit direction inputs without accessing a scene's expected result."""

    records: list[dict[str, str]] = []
    answer_values = {member.value for member in RecommendationType}
    for scene in scenes:
        input_data = scene.input_data
        leaked_keys = _all_keys(input_data) & _FORBIDDEN_PRODUCTION_INPUT_KEYS
        if leaked_keys:
            raise ValueError(f"oracle_key_in_production_input:{sorted(leaked_keys)}")
        input_text = json.dumps(input_data, ensure_ascii=False, sort_keys=True)
        leaked_values = {value for value in answer_values if value in input_text}
        if leaked_values:
            raise ValueError(f"oracle_value_in_production_input:{sorted(leaked_values)}")
        records.append({
            "scene": scene.name,
            "production_input_sha256": hashlib.sha256(input_text.encode()).hexdigest(),
        })
    return records


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {
            *(str(key) for key in value),
            *(nested for item in value.values() for nested in _all_keys(item)),
        }
    if isinstance(value, list):
        return {nested for item in value for nested in _all_keys(item)}
    return set()


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
        self.recommendation_engine = RecommendationEngine(
            interpreter=(
                recommendation_interpreter
                if recommendation_interpreter is not None
                else RulesetRecommendationInterpreter()
            )
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
            produced = {
                "diagnosis_type": diag.diagnosis_type.value,
                "degraded": diag.degraded,
                "reason": diag.reason,
            }
            # 诊断 → 建议 生产链路：引擎产出建议候选（类型 + 理由），
            # 使 Eval 断言实际建议方向而非只验证 degraded。
            sku = _sku_from_scene(input_data)
            rec = self.recommendation_engine.generate(
                tenant_id="t1",
                diagnosis=diag,
                sku=sku,
                recommendation_id="eval-rec",
                created_at=_EVAL_CREATED_AT,
            )
            produced["recommendation_type"] = rec.type.value
            produced["recommendation_rationale"] = rec.rationale
            # R5（C-lite）：追加建议层降级态 + 缺失证据键，供方向场景 oracle 锁
            # 真实方向（recommendation_type != 保持观察 + recommendation_degraded +
            # missing_evidence 精确键）。多余键对既有 oracle 无副作用（run_oracle 只遍历
            # expected 键）。
            produced["recommendation_degraded"] = rec.degraded
            produced["missing_evidence"] = list(rec.missing_evidence)
        except Exception as exc:  # noqa: BLE001
            return EvalResult(scene.name, False, [f"eval_error:{exc}"])
        failures = scene.run_oracle(produced)
        return EvalResult(scene.name, not failures, failures, produced)

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

    return list(FROZEN_SCENES)


_AGGREGATE_RULES: dict[str, AggregateRule] = {
    "impressions": AggregateRule.SUM,
    "clicks": AggregateRule.SUM,
    "add_to_cart": AggregateRule.SUM,
    "orders": AggregateRule.SUM,
    "payments": AggregateRule.SUM,
    "refunds": AggregateRule.SUM,
    "net_sales": AggregateRule.SUM,
    "sellable_stock": AggregateRule.LATEST,
    "in_transit_stock": AggregateRule.LATEST,
    "ad_spend": AggregateRule.SUM,
    "competitor_price": AggregateRule.NONE,
    "experiment_state": AggregateRule.NONE,
}


def _sku_from_scene(input_data: dict[str, Any]) -> SKUReadModel:
    """Build a production-shaped SKU model from raw, answer-free scene facts."""

    raw = input_data.get("business_facts")
    business_facts = raw if isinstance(raw, dict) else {}
    raw_metrics = business_facts.get("metrics")
    metrics = raw_metrics if isinstance(raw_metrics, dict) else {}

    def metric(name: str) -> MetricValue:
        if name not in metrics:
            return MetricValue.missing(
                Granularity.DAILY,
                _AGGREGATE_RULES[name],
                "2026-08-17",
                f"eval_{name}_not_provided",
            )
        return MetricValue.from_value(
            state=EvidenceState.ACTUAL,
            granularity=Granularity.DAILY,
            aggregate_rule=_AGGREGATE_RULES[name],
            period_key="2026-08-17",
            value=float(metrics[name]),
            import_manifest_id=f"eval-source-{name}",
            data_as_of=_EVAL_CREATED_AT,
            authoritative_service=f"eval_fixture_{name}",
        )

    raw_revision = business_facts.get("listing_revision")
    listing_revision = None
    revision_no = 1
    if isinstance(raw_revision, dict):
        revision_no = int(raw_revision.get("revision_no", 1))
        listing_revision = ListingRevisionEvidence(
            revision_id=str(raw_revision["revision_id"]),
            revision_no=revision_no,
            connector_id="eval_fixture",
            active_from=datetime.fromisoformat(str(raw_revision["active_from"])),
            active_to=(
                datetime.fromisoformat(str(raw_revision["active_to"]))
                if raw_revision.get("active_to") else None
            ),
            source_updated_at=_EVAL_CREATED_AT,
        )
    return SKUReadModel(
        tenant_id="t1",
        store_id=str(input_data.get("store_id", "store-eval")),
        item_id="item-eval",
        sku_id=str(input_data["sku_id"]),
        revision=revision_no,
        listing_revision=listing_revision,
        title=(str(business_facts["title"]) if business_facts.get("title") else None),
        **{name: metric(name) for name in _AGGREGATE_RULES},
    )


__all__ = [
    "EvalResult",
    "MechanismEvalRunner",
    "assert_direction_inputs_oracle_free",
]
