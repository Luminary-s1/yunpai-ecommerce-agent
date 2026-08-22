"""M9-R WP3「诊断 → 建议」生成引擎：闭环补缺。

边界声明：
- 输入：tenant_id、diagnosis（Diagnosis，含校验后语义类型与固化事实）、
  sku（SKUReadModel，读模型事实）、recommendation_id、created_at（调用方注入）。
- 输出：Recommendation（强制 DRAFT）。调用方决定是否落库
  （走 RecommendationPersistenceService.create，不直插 SQL）。
- 副作用：零——本模块不写库、不触发任何平台动作（B4 平台写=0）；
  语义（类型/理由）由解释器产出，模型解释器通过注入的 ModelGateway 调模型。
- 写屏障：只产 DRAFT 建议；不自动 APPROVED（B2）；不自动填供给方字段
  （supplier_ref/promised_delivery_at 由人工在 M10-R 订购单侧补齐——M10-R 契约约束，
  本引擎只填数量类事实）。
- D-034 分工：确定性代码组装可执行建议候选 + 校验；语义（类型/理由）由解释器
  产出。`RecommendationModelInterpreter` 为模型生产路径（复用 ModelGateway 三件套：
  系统 prompt「无执行权 + 按 output_schema 返回」；模型失败时只返回明确的
  `KEEP_OBSERVE/model_unavailable`，不由规则替模型重做语义决策。
- 失败暴露：required_facts 缺 → degraded=True + missing_evidence
  （validate_recommendation 强制）；越权词 → validate_full_recommendation 递归拒绝；
  诊断类型不可映射 → 抛 ValueError。
- 确定性：无时间/随机源；created_at 由调用方传入。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping, Protocol

from pydantic import BaseModel, ConfigDict

from ..product_semantics import semantic_provenance
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

    生产使用模型解释器（确定性事实 → 模型 → Pydantic 校验 → 失败降级）；
    Ruleset 只服务隔离测试、Eval 和模型关闭时的保守占位。
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
    semantic_provenance: Mapping[str, str] = field(default_factory=dict)


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


# 建议模型输出 schema：只允许模型产 type + rationale + degraded，不产事实快照/越权字段。
_RECOMMENDATION_SYSTEM_PROMPT = """\
你是商品生命周期建议的语义解释器。确定性代码已经固化诊断事实与证据，你没有执行权，且不得修改、替代或重算 effect、confidence interval、sample size、quality gate 或任何证据引用，也不得把建议变成平台动作。

只做两件事：
1. 根据给出的诊断选择唯一建议类型（type）；
2. 用谨慎语言给出建议理由（rationale），把收益/风险描述为待验证假设，不宣称平台内部权重或因果机制。

可选建议类型（严格取值）：
- 选品候选 / 上新准备 / 曝光/点击诊断 / 受控实验 / 保持观察 / 定价候选 / 活动候选 / 补货联动 / 清仓预警

缺成本时不得输出"一定提价 N 元"一类的正式利润安全价格；缺少参照证据时不得虚构对标价格或参照结论，如实说明缺少哪些参照数据即可。严格按用户消息中的 output_schema 返回一个 JSON object。\
"""
RECOMMENDATION_PROMPT_VERSION = "m9r-recommendation-v1"


class _RecommendationModelOutput(BaseModel):
    """模型建议输出契约（仅类型 + 理由 + 降级标记）。"""

    model_config = ConfigDict(extra="forbid")

    type: RecommendationType
    rationale: str
    degraded: bool = False


class RecommendationModelInterpreter:
    """模型建议解释器（D-034 生产路径）：复用 ModelGateway 三件套。

    失败/超时/非法输出 → 明确降级为 KEEP_OBSERVE/model_unavailable。
    """

    def __init__(self, gateway: Any) -> None:
        self.gateway = gateway

    def interpret(self, diagnosis: Diagnosis) -> RecommendationCandidate:
        output_schema = _RecommendationModelOutput.model_json_schema()
        request = {
            "facts_authority": "deterministic_code",
            "diagnosis": {
                "diagnosis_type": diagnosis.diagnosis_type.value,
                "reason": diagnosis.reason,
                "evidence_facts": diagnosis.evidence_facts,
                "degraded": diagnosis.degraded,
            },
            "prompt_version": RECOMMENDATION_PROMPT_VERSION,
            "output_schema": output_schema,
        }
        try:
            raw = self.gateway.generate_json(
                [
                    {"role": "system", "content": _RECOMMENDATION_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(request, ensure_ascii=False, sort_keys=True),
                    },
                ],
                thinking_enabled=False,
            )
            # 模型输出经 Pydantic 校验（type 必须合法）。
            parsed = _RecommendationModelOutput.model_validate(raw)
            return RecommendationCandidate(
                type=parsed.type,
                rationale=parsed.rationale,
                rationale_evidence_refs=tuple(diagnosis.evidence_facts.keys()),
                degraded=parsed.degraded,
                semantic_provenance=semantic_provenance(
                    self.gateway,
                    decision_source="model",
                    prompt_version=RECOMMENDATION_PROMPT_VERSION,
                ),
            )
        except Exception:  # noqa: BLE001 — 模型故障/输出非法 → 明确安全降级
            return RecommendationCandidate(
                type=RecommendationType.KEEP_OBSERVE,
                rationale="model_unavailable",
                rationale_evidence_refs=tuple(diagnosis.evidence_facts.keys()),
                degraded=True,
                semantic_provenance=semantic_provenance(
                    self.gateway,
                    decision_source="model_unavailable",
                    prompt_version=RECOMMENDATION_PROMPT_VERSION,
                ),
            )


class RulesetRecommendationInterpreter:
    """固定表测试解释器：按映射表把诊断类型转为建议类型。

    仅用于隔离测试/Eval 或模型明确禁用的保守占位；模型失败时
    RecommendationModelInterpreter 自身返回 KEEP_OBSERVE，不调用本解释器。
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
            semantic_provenance={"decision_source": "fixed_ruleset"},
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
        evidence_references: dict[str, Any] = {}
        if sku.listing_revision is not None:
            listing_evidence = sku.listing_revision.model_dump(mode="json")
            evidence_references["listing_revision"] = listing_evidence
            if rtype is RecommendationType.EXPERIMENT:
                facts_snapshot = {
                    **facts_snapshot,
                    "revision_evidence": listing_evidence,
                }
        if sku.product_identity_evidence is not None:
            evidence_references["product_identity"] = (
                sku.product_identity_evidence.model_dump(mode="json")
            )
        if evidence_references:
            facts_snapshot = {
                **facts_snapshot,
                "evidence_references": evidence_references,
            }
        semantic_sources: dict[str, Any] = {}
        diagnosis_source = diagnosis.evidence_facts.get("semantic_provenance")
        if isinstance(diagnosis_source, Mapping):
            semantic_sources["diagnosis"] = dict(diagnosis_source)
        if candidate.semantic_provenance:
            semantic_sources["recommendation"] = dict(candidate.semantic_provenance)
        if semantic_sources:
            facts_snapshot = {
                **facts_snapshot,
                "semantic_provenance": semantic_sources,
            }
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
        """按建议类型填前置事实（缺则键缺失 → required_facts 触发降级）。

        T3.1（P4 修复）：覆盖任务书全部 9 类建议——不再对 SELECTION/NEW_LAUNCH/
        PROMOTION/CLEARANCE 抛 recommendation_type_not_supported。缺信号的方向
        返回空 dict（required_facts 键缺失 → 引擎显式降级 degraded + missing_evidence），
        而非崩溃——Eval 场景可断言真实方向可达。
        """
        if rtype is RecommendationType.RESTOCK:
            return self._stock_facts(tenant_id, sku, diagnosis)
        if rtype is RecommendationType.PRICING:
            # 引擎无成本数据：cost_ready 键缺失 → REQUIRED_FACTS 触发降级
            # （对齐「缺成本 → 不出正式利润安全价格」验收条目 4）。
            return {}
        if rtype is RecommendationType.DIAGNOSIS:
            return self._traffic_facts(sku)
        if rtype is RecommendationType.KEEP_OBSERVE:
            # P3 修复：evidence_facts 含 quality_gate/quality_gate_issues 等键名，
            # 递归越权扫描（FORBIDDEN_OUTPUT_KEYS 含 "gate"）会误杀"保持观察"建议。
            # 只保留确定性业务事实（不含越权词命中的证据门禁键）。
            _ALLOWED_EVIDENCE_KEYS = {
                "evidence_state", "exposures", "clicks", "conversions",
                "stockout", "pollution", "reason",
            }
            return {
                "diagnosis_facts": {
                    k: v for k, v in diagnosis.evidence_facts.items()
                    if k in _ALLOWED_EVIDENCE_KEYS
                }
            }
        # R5（C-lite，负责人阻断项 5 修复）：SELECTION/NEW_LAUNCH/CLEARANCE 需要
        # 对应信号域（demand/竞品/上新就绪/库存就绪/清仓信号）。V1 引擎不编造信号
        # （D-034 边界：确定性代码不写经营语义），但从 diagnosis.evidence_facts 透传
        # 同名信号键到 facts_snapshot 顶层——信号由语义层/调用方注入，REQUIRED_FACTS
        # 满足时产出非降级真实方向，缺时仍显式降级（degraded + missing_evidence）。
        # REQUIRED_FACTS 键是顶层的（schemas.py validate_recommendation 要求 facts.get(key)），
        # 所以不能套在 "selection_facts" 等子 dict 里。
        # ⚠️ V1 生产边界（诚实标注）：生产诊断链（diagnose/validate_diagnosis_output）
        # 的 evidence_facts 只含固定 9 键（evidence_state/freshness/quality_gate/exposures
        # 等），不含 demand_signal/competitor_evidence 等信号键——因此 SELECTION/
        # NEW_LAUNCH/CLEARANCE/EXPERIMENT/PROMOTION 在生产恒走降级路径（missing_evidence
        # 列明缺键）。
        # 非降级真实方向仅在信号被注入时可达（Eval 场景注入 required_signals 验证引擎
        # 能力），生产可达需后续扩展信号源（如 M 期接入 demand/竞品数据）。不得在
        # 无信号源时把裸布尔当证据满足（无来源/引用校验）——这正是透传的边界。
        if rtype in (
            RecommendationType.SELECTION,
            RecommendationType.NEW_LAUNCH,
            RecommendationType.CLEARANCE,
            RecommendationType.EXPERIMENT,
            RecommendationType.PROMOTION,
        ):
            return {
                key: diagnosis.evidence_facts.get(key)
                for key in REQUIRED_FACTS[rtype]
                if diagnosis.evidence_facts.get(key) not in (None, False)
            }
        return {}

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
    "RecommendationModelInterpreter",
    "RulesetRecommendationInterpreter",
]
