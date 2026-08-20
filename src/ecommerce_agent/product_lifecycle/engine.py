"""M9-R WP3「诊断 → 建议」生成引擎：闭环补缺。

边界声明：
- 输入：tenant_id、diagnosis（Diagnosis，含校验后语义类型与固化事实）、
  sku（SKUReadModel，读模型事实）、recommendation_id、created_at（调用方注入）。
- 输出：Recommendation（强制 DRAFT）。调用方决定是否落库
  （走 RecommendationPersistenceService.create，不直插 SQL）。
- 副作用：零——本模块不写库、不调用模型（模型解释器本期不接，接口已预留）、
  不触发任何平台动作（B4 平台写=0）。
- 写屏障：只产 DRAFT 建议；不自动 APPROVED（B2）；不自动填供给方字段
  （supplier_ref/promised_delivery_at 由人工在 M10-R 订购单侧补齐——M10-R 契约约束，
  本引擎只填数量类事实）。
- D-034 分工：确定性代码组装可执行建议候选 + 校验；语义（类型/理由）由解释器
  产出。本期用 RulesetRecommendationInterpreter 确定性占位；生产可替换为模型
  解释器（对齐 TrafficAnalysisModelInterpreter 三件套：系统 prompt「无执行权 +
  按 output_schema 返回」，模型返回经 Pydantic 校验，失败降级为 Ruleset）。
- 失败暴露：required_facts 缺 → degraded=True + missing_evidence
  （validate_recommendation 强制）；越权词 → validate_full_recommendation 递归拒绝；
  诊断类型不可映射 → 抛 ValueError。
- 确定性：无时间/随机源；ruleset 映射固定；created_at 由调用方传入。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from ..product_diagnosis.diagnosis import Diagnosis, DiagnosisType
from ..product_read_model.models import MetricValue, SKUReadModel
from ..readonly_data.contracts import EvidenceState
from .schemas import (
    REQUIRED_FACTS,
    Recommendation,
    RecommendationState,
    RecommendationType,
    TargetObject,
)
from .validation import validate_full_recommendation

if TYPE_CHECKING:
    from ..business.inventory import InventoryService


class RecommendationInterpreter(Protocol):
    """语义解释器：输入诊断，产出建议候选（类型 + 理由）。

    生产替换为模型解释器（对齐 TrafficAnalysisModelInterpreter：确定性事实 →
    模型 → Pydantic 校验 → 失败降级）。本期仅 Ruleset 实现。
    """

    def interpret(self, diagnosis: Diagnosis) -> "RecommendationCandidate": ...


@dataclass(frozen=True)
class RecommendationCandidate:
    """解释器产出的建议候选（对齐 TrafficAnalysisInterpretation 哲学）。

    degraded 表达「语义层降级」；required_facts 缺失导致的降级由引擎确定性推导。
    """

    type: RecommendationType
    rationale: str
    rationale_evidence_refs: tuple[str, ...] = ()
    degraded: bool = False


# 诊断类型 → 建议类型 权威映射（Ruleset 占位，确定性；枚举变化须同步此处 + 测试）
_TYPE_BY_DIAGNOSIS: dict[DiagnosisType, RecommendationType] = {
    DiagnosisType.STOCKOUT_POLLUTION: RecommendationType.RESTOCK,
    DiagnosisType.AD_PRICE_POLLUTION: RecommendationType.PRICING,
    DiagnosisType.EXPOSURE_INSUFFICIENT: RecommendationType.DIAGNOSIS,
    DiagnosisType.CLICK_INSUFFICIENT: RecommendationType.DIAGNOSIS,
    DiagnosisType.CONVERSION_INSUFFICIENT: RecommendationType.DIAGNOSIS,
    DiagnosisType.EVIDENCE_INSUFFICIENT: RecommendationType.KEEP_OBSERVE,
}

# 固定理由（刻意避开 FORBIDDEN_OUTPUT_KEYS 全部词，防止越权词递归拒绝）
_RATIONALE_BY_TYPE: dict[RecommendationType, str] = {
    RecommendationType.RESTOCK: (
        "库存售罄，建议补货联动；备选：先在单个 SKU 上受控实验验证需求。"
    ),
    RecommendationType.PRICING: (
        "存在广告/价格类污染，且缺成本数据，无法输出正式安全价格，建议先补齐成本事实。"
    ),
    RecommendationType.DIAGNOSIS: (
        "曝光/点击/转化未达健康阈值，建议进一步曝光/点击诊断，定位原因。"
    ),
    RecommendationType.KEEP_OBSERVE: (
        "证据不足，暂不输出强方向结论，建议保持观察，待数据补齐后再判断。"
    ),
}


class RulesetRecommendationInterpreter:
    """确定性占位：按映射表把诊断类型 → 建议类型。

    注意：占位不等于验收依据——本实现只让「诊断→建议」链路端到端可测；
    生产语义决策应由模型解释器承担（见 RecommendationInterpreter）。
    """

    def interpret(self, diagnosis: Diagnosis) -> RecommendationCandidate:
        rtype = _TYPE_BY_DIAGNOSIS.get(diagnosis.diagnosis_type)
        if rtype is None:
            raise ValueError(
                f"diagnosis_type_not_mappable:{diagnosis.diagnosis_type.value}"
            )
        # KEEP_OBSERVE（证据不足）与 PRICING（缺成本）在语义层即降级；
        # required_facts 缺失导致的降级由引擎确定性推导，不在此处。
        degraded = rtype in (
            RecommendationType.KEEP_OBSERVE,
            RecommendationType.PRICING,
        )
        return RecommendationCandidate(
            type=rtype,
            rationale=_RATIONALE_BY_TYPE[rtype],
            rationale_evidence_refs=tuple(diagnosis.evidence_facts.keys()),
            degraded=degraded,
        )


class RecommendationEngine:
    """诊断 → 建议 生成引擎（D-034：确定性组装 + 语义可替换）。

    用法：
      engine = RecommendationEngine(inventory=inventory_service)
      rec = engine.generate(
          tenant_id="t1", diagnosis=diag, sku=sku_model,
          recommendation_id="rec-1", created_at=now,
      )
      # rec.state == DRAFT；落库由调用方走 RecommendationPersistenceService.create
    """

    def __init__(
        self,
        inventory: InventoryService | None = None,
        interpreter: RecommendationInterpreter | None = None,
    ) -> None:
        self.inventory = inventory
        self.interpreter = interpreter or RulesetRecommendationInterpreter()

    def generate(
        self,
        *,
        tenant_id: str,
        diagnosis: Diagnosis,
        sku: SKUReadModel,
        recommendation_id: str,
        created_at: datetime,
    ) -> Recommendation:
        """诊断事实 → 建议候选（DRAFT）。

        步骤（确定性）：
        1. 解释器产出候选（type/rationale/degraded）——模型可替换层。
        2. 按类型组装 facts_snapshot（含读模型事实 + 库存事实）。
        3. required_facts 缺失 → degraded=True + missing_evidence。
        4. validate_full_recommendation（B3 alternatives + 越权词递归扫描）。
        5. 返回 DRAFT Recommendation（零平台写）。
        """
        candidate = self.interpreter.interpret(diagnosis)
        rtype = candidate.type
        facts_snapshot = self._build_facts_snapshot(tenant_id, diagnosis, sku, rtype)
        missing = [
            key
            for key in REQUIRED_FACTS[rtype]
            if key not in facts_snapshot or facts_snapshot.get(key) in (None, False)
        ]
        degraded = candidate.degraded or diagnosis.degraded or bool(missing)
        recommendation = Recommendation(
            recommendation_id=recommendation_id,
            type=rtype,
            target=TargetObject(
                store_id=sku.store_id,
                item_id=sku.item_id,
                sku_id=sku.sku_id,
            ),
            facts_snapshot=facts_snapshot,
            rationale=candidate.rationale,
            missing_evidence=list(missing),
            alternatives=[RecommendationType.EXPERIMENT],  # B3：常备受控实验备选
            state=RecommendationState.DRAFT,
            degraded=degraded,
            created_at=created_at,
            updated_at=created_at,
        )
        validate_full_recommendation(recommendation)  # B3 + required_facts + 越权词
        return recommendation

    # ── 内部：facts_snapshot 确定性组装 ──

    def _build_facts_snapshot(
        self,
        tenant_id: str,
        diagnosis: Diagnosis,
        sku: SKUReadModel,
        rtype: RecommendationType,
    ) -> dict[str, Any]:
        """按建议类型填前置事实（缺则键缺失 → required_facts 触发降级）。"""
        if rtype is RecommendationType.RESTOCK:
            return self._stock_facts(tenant_id, sku, diagnosis)
        if rtype is RecommendationType.PRICING:
            # 引擎无成本数据：cost_ready 键缺失 → REQUIRED_FACTS 触发降级
            # （对齐「缺成本 → 不出正式利润安全价格」验收条目 4）。
            return {}
        if rtype is RecommendationType.DIAGNOSIS:
            return self._traffic_facts(sku)
        if rtype is RecommendationType.KEEP_OBSERVE:
            return {"diagnosis_facts": dict(diagnosis.evidence_facts)}
        raise ValueError(f"recommendation_type_not_supported:{rtype.value}")

    def _stock_facts(
        self,
        tenant_id: str,
        sku: SKUReadModel,
        diagnosis: Diagnosis,
    ) -> dict[str, Any]:
        """补货事实：读模型库存 + InventoryService 补货数量。

        供给方字段（supplier_ref/promised_delivery_at）不在此填充——
        M10-R 契约约束：缺供给方信息时契约停在 draft，由人工在订购单侧补齐。
        """
        sellable, _ = _metric_value(sku.sellable_stock)
        in_transit, _ = _metric_value(sku.in_transit_stock)
        return {
            "stock_facts": {
                "sellable_stock": sellable,
                "in_transit_stock": in_transit,
                "recommended_replenishment": self._replenishment(tenant_id, sku),
                "diagnosis_type": diagnosis.diagnosis_type.value,
            }
        }

    def _traffic_facts(self, sku: SKUReadModel) -> dict[str, Any]:
        """流量诊断事实（DIAGNOSIS 类型前置）。"""
        impressions, _ = _metric_value(sku.impressions)
        clicks, _ = _metric_value(sku.clicks)
        add_to_cart, _ = _metric_value(sku.add_to_cart)
        conversions, _ = _metric_value(sku.payments)  # 转化口径用支付
        return {
            "traffic_facts": {
                "impressions": impressions,
                "clicks": clicks,
                "add_to_cart": add_to_cart,
                "conversions": conversions,
            }
        }

    def _replenishment(self, tenant_id: str, sku: SKUReadModel) -> float | None:
        """从 InventoryService.risks() 取 recommended_replenishment。

        服务未注入/无匹配行/异常 → None（stock_facts 键仍在，required_facts
        仍满足；数量缺失不阻断建议，仅缺失该数量）。
        """
        if self.inventory is None:
            return None
        try:
            risks = self.inventory.risks(
                tenant_id, store_id=sku.store_id, sku_id=sku.sku_id
            )
        except Exception:  # noqa: BLE001 — 库存服务异常 → 数量缺失，不静默 0
            return None
        for row in risks:
            if row.get("sku_id") == sku.sku_id:
                value = row.get("recommended_replenishment")
                if value is None:
                    return None
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        return None


def _metric_value(metric: MetricValue) -> tuple[float | None, bool]:
    """取指标值；MISSING → (None, True)（缺失是合法降级信号，不抛）。

    与 MetricValue.safe_value（fail-fast 抛错）不同：此处缺失要进入 facts_snapshot
    供降级判定，而非中断链路。
    """
    if metric.evidence_state is EvidenceState.MISSING:
        return None, True
    return metric.value, False


__all__ = [
    "RecommendationCandidate",
    "RecommendationEngine",
    "RecommendationInterpreter",
    "RulesetRecommendationInterpreter",
]
