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
from ..text_utils import contains_forbidden_token
from .schemas import (
    REQUIRED_FACTS,
    Recommendation,
    RecommendationState,
    RecommendationType,
    TargetObject,
)
from .validation import FORBIDDEN_OUTPUT_KEYS, validate_full_recommendation

if TYPE_CHECKING:
    from ..business.inventory import InventoryService


class RecommendationInterpreter(Protocol):
    """语义解释器：输入诊断，产出建议候选（类型 + 理由）。

    生产使用模型解释器（确定性事实 → 模型 → Pydantic 校验 → 失败降级）；
    Ruleset 只服务隔离测试、Eval 和模型关闭时的保守占位。
    """

    def interpret(
        self,
        diagnosis: Diagnosis,
        decision_facts: Mapping[str, Any],
    ) -> "RecommendationCandidate": ...


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


_RECOMMENDATION_DECISION_POLICY: dict[str, str] = {
    RecommendationType.SELECTION.value: (
        "需求指标较强、外部价格事实可用且 listing revision 缺失时，必须选择选品候选；"
        "中性商品标题不改变判断，不得因 conversion diagnosis 改成实验或流量诊断，"
        "缺广告或库存不阻断该候选方向"
    ),
    RecommendationType.NEW_LAUNCH.value: (
        "listing revision 刚生效且库存已准备，曝光或订单仍为零/缺失；"
        "新 listing 的零流量不是存量曝光问题"
    ),
    RecommendationType.CLEARANCE.value: (
        "支付需求相对可售库存极弱且有外部价格事实时，必须选择清仓预警；"
        "payments_per_sellable_unit 接近零，尤其零支付与大量正库存，是明确的需求库存失衡，"
        "优先于实验、流量诊断或定价，"
        "缺广告或实验状态不阻断该预警方向"
    ),
    RecommendationType.EXPERIMENT.value: (
        "当前 listing 有大样本观测且点击或转化问题需要通过 revision 变更验证时，"
        "必须选择受控实验而不是停在曝光/点击诊断；"
        "结构化 diagnosis 已经确认 click_insufficient/conversion_insufficient 时无需重复诊断。"
        "listing revision 缺失时禁止选择，已有 revision 和漏斗事实时不要求已有实验状态"
    ),
    RecommendationType.PROMOTION.value: (
        "曝光、点击、支付均较强且 ad_spend 严格大于零；ad_spend=0 表示没有广告投入，"
        "不能选择活动候选。满足正广告投入时，即使 diagnosis 为 conversion_insufficient"
        "也进入人工活动评审，活动窗口缺失由代码降级而不是阻断候选方向"
    ),
    RecommendationType.DIAGNOSIS.value: (
        "存在流量问题但 listing revision 缺失、样本不足或不满足更具体生命周期方向；"
        "可信 revision、大样本漏斗与结构化流量诊断同时存在时不得重复停在诊断"
    ),
    RecommendationType.KEEP_OBSERVE.value: "证据不足或其它方向均不成立",
    RecommendationType.PRICING.value: "广告或价格污染需要人工评审，且不得在缺成本时给价格动作",
    RecommendationType.RESTOCK.value: "有可追溯的缺货污染或库存不足事实",
}


# 建议模型输出 schema：只允许模型产 type + rationale，不产事实快照或越权字段。
_RECOMMENDATION_SYSTEM_PROMPT = """\
你是商品生命周期建议的语义解释器。确定性代码已经固化诊断事实与证据，你没有执行权，且不得修改、替代或重算 effect、confidence interval、sample size、quality gate 或任何证据引用，也不得把建议变成平台动作。

只做两件事：
1. 综合 diagnosis 与 business_facts 选择唯一建议类型（type）。diagnosis 只是当前观测问题，不是建议类型的固定映射；
2. 用谨慎语言给出建议理由（rationale），把收益/风险描述为待验证假设，不宣称平台内部权重或因果机制。

选择建议类型时先应用用户消息中的 decision_policy，再参考 diagnosis。diagnosis 描述当前观测，不能覆盖更具体的生命周期事实。decision_policy 是适用于所有请求的完整候选集合，不是当前请求的目标标签。不把 SKU、revision ID、来源 ID 或字段名当作答案标签。

所有建议类型都只会创建待人工审核的 DRAFT，不会直接修改 listing 或创建实验。不得因为担心自动执行而把事实充分的受控实验候选降成重复诊断；执行安全由确定性代码和人工审核保证。

可选建议类型（严格取值）：
- 选品候选 / 上新准备 / 曝光/点击诊断 / 受控实验 / 保持观察 / 定价候选 / 活动候选 / 补货联动 / 清仓预警

缺成本时不得输出"一定提价 N 元"一类的正式利润安全价格；缺少参照证据时不得虚构参照价格或参照结论，如实说明缺少哪些参照数据即可。rationale 不得包含 effect、interval、sample_size、gate、平台权重、平台算法、效果提升、权重提升、流量扶持、对标、竞品、行业。证据是否齐全和是否降级由确定性代码校验，不由你输出。严格按用户消息中的 output_schema 返回一个 JSON object。\
"""
RECOMMENDATION_PROMPT_VERSION = "m9r-recommendation-v2"


class _RecommendationModelOutput(BaseModel):
    """模型建议输出契约（仅类型 + 理由）。"""

    model_config = ConfigDict(extra="forbid")

    type: RecommendationType
    rationale: str


class RecommendationModelInterpreter:
    """模型建议解释器（D-034 生产路径）：复用 ModelGateway 三件套。

    失败/超时/非法输出 → 明确降级为 KEEP_OBSERVE/model_unavailable。
    """

    def __init__(self, gateway: Any) -> None:
        self.gateway = gateway

    def interpret(
        self,
        diagnosis: Diagnosis,
        decision_facts: Mapping[str, Any] | None = None,
    ) -> RecommendationCandidate:
        trusted_facts = dict(decision_facts or {})
        output_schema = _RecommendationModelOutput.model_json_schema()
        request = {
            "facts_authority": "deterministic_code",
            "decision_policy": _RECOMMENDATION_DECISION_POLICY,
            "diagnosis": {
                "diagnosis_type": diagnosis.diagnosis_type.value,
                "evidence_facts": diagnosis.evidence_facts,
                "degraded": diagnosis.degraded,
            },
            "business_facts": trusted_facts,
            "prompt_version": RECOMMENDATION_PROMPT_VERSION,
            "output_schema": output_schema,
        }
        try:
            for attempt in range(3):
                raw = self.gateway.generate_json(
                    [
                        {"role": "system", "content": _RECOMMENDATION_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": json.dumps(
                                request, ensure_ascii=False, sort_keys=True
                            ),
                        },
                    ],
                    thinking_enabled=False,
                )
                # 模型输出经 Pydantic 校验（type 必须合法）。
                parsed = _RecommendationModelOutput.model_validate(raw)
                precondition_failure = self._verified_precondition_failure(
                    parsed.type, trusted_facts
                )
                if precondition_failure is None and contains_forbidden_token(
                    parsed.rationale, FORBIDDEN_OUTPUT_KEYS
                ):
                    precondition_failure = "forbidden_rationale_content"
                if precondition_failure is not None and attempt < 2:
                    instruction = (
                        "保持仍受事实支持的语义类型，仅用谨慎且不含禁用表述的理由重写。"
                        if precondition_failure == "forbidden_rationale_content"
                        else (
                            "该类型缺少可信执行前提，请重新综合全部事实选择语义类型；"
                            "反馈不是目标标签。"
                        )
                    )
                    request = {
                        **request,
                        "execution_feedback": {
                            "rejected_type": parsed.type.value,
                            "verified_failure": precondition_failure,
                            "instruction": instruction,
                        },
                    }
                    continue
                if precondition_failure is not None:
                    return RecommendationCandidate(
                        type=RecommendationType.KEEP_OBSERVE,
                        rationale=f"model_output_rejected:{precondition_failure}",
                        rationale_evidence_refs=tuple(
                            diagnosis.evidence_facts.keys()
                        ),
                        degraded=True,
                        semantic_provenance=semantic_provenance(
                            self.gateway,
                            decision_source="model_output_rejected",
                            prompt_version=RECOMMENDATION_PROMPT_VERSION,
                        ),
                    )
                if attempt == 0 and parsed.type in (
                    RecommendationType.KEEP_OBSERVE,
                    RecommendationType.DIAGNOSIS,
                ):
                    request = {
                        **request,
                        "semantic_self_review": {
                            "proposed_type": parsed.type.value,
                            "proposed_rationale": parsed.rationale,
                            "instruction": (
                                "最终定案前逐项比较完整 decision_policy 与原始数值事实。"
                                "若存在更具体且受事实支持的生命周期方向则修正；否则保留。"
                                "这是对模型草稿的复核，不是目标标签。"
                            ),
                        },
                    }
                    continue
                break
            return RecommendationCandidate(
                type=parsed.type,
                rationale=parsed.rationale,
                rationale_evidence_refs=tuple(diagnosis.evidence_facts.keys()),
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

    @staticmethod
    def _verified_precondition_failure(
        recommendation_type: RecommendationType,
        decision_facts: Mapping[str, Any],
    ) -> str | None:
        product = decision_facts.get("product")
        listing_revision = (
            product.get("listing_revision") if isinstance(product, Mapping) else None
        )
        if (
            recommendation_type is RecommendationType.EXPERIMENT
            and listing_revision is None
        ):
            return "trusted_listing_revision_missing"
        if (
            recommendation_type is RecommendationType.SELECTION
            and listing_revision is not None
        ):
            return "trusted_listing_revision_present"
        if recommendation_type is RecommendationType.PROMOTION:
            metric_values = decision_facts.get("metric_values")
            ad_spend = (
                metric_values.get("ad_spend")
                if isinstance(metric_values, Mapping) else None
            )
            if not isinstance(ad_spend, (int, float)) or ad_spend <= 0:
                return "positive_ad_spend_missing"
        return None


class RulesetRecommendationInterpreter:
    """固定表测试解释器：按映射表把诊断类型转为建议类型。

    仅用于隔离测试/Eval 或模型明确禁用的保守占位；模型失败时
    RecommendationModelInterpreter 自身返回 KEEP_OBSERVE，不调用本解释器。
    """

    def interpret(
        self,
        diagnosis: Diagnosis,
        decision_facts: Mapping[str, Any] | None = None,
    ) -> RecommendationCandidate:
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
        decision_facts = self._decision_facts(diagnosis, sku)
        candidate = self.interpreter.interpret(diagnosis, decision_facts)
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

    @staticmethod
    def _decision_facts(
        diagnosis: Diagnosis,
        sku: SKUReadModel,
    ) -> dict[str, Any]:
        """Build the exact sourced fact snapshot shown to the semantic model."""

        metric_names = (
            "impressions", "clicks", "add_to_cart", "orders", "payments",
            "refunds", "net_sales", "sellable_stock", "in_transit_stock",
            "ad_spend", "competitor_price", "experiment_state",
        )
        metrics = {
            name: getattr(sku, name).model_dump(mode="json")
            for name in metric_names
        }
        metric_values = {
            name: metric["value"] for name, metric in metrics.items()
        }
        derived_rates: dict[str, float] = {}
        impressions = metric_values["impressions"]
        clicks = metric_values["clicks"]
        payments = metric_values["payments"]
        sellable_stock = metric_values["sellable_stock"]
        if isinstance(impressions, (int, float)) and impressions > 0 and isinstance(clicks, (int, float)):
            derived_rates["click_through_rate"] = clicks / impressions
        if isinstance(clicks, (int, float)) and clicks > 0 and isinstance(payments, (int, float)):
            derived_rates["payment_per_click"] = payments / clicks
        if (
            isinstance(sellable_stock, (int, float))
            and sellable_stock > 0
            and isinstance(payments, (int, float))
        ):
            derived_rates["payments_per_sellable_unit"] = payments / sellable_stock
        return {
            "scope": {
                "tenant_id": sku.tenant_id,
                "store_id": sku.store_id,
                "item_id": sku.item_id,
                "sku_id": sku.sku_id,
                "revision": sku.revision,
            },
            "product": {
                "title": sku.title,
                "merchant_code": sku.merchant_code,
                "material_code": sku.material_code,
                "listing_revision": (
                    sku.listing_revision.model_dump(mode="json")
                    if sku.listing_revision is not None else None
                ),
                "product_identity": (
                    sku.product_identity_evidence.model_dump(mode="json")
                    if sku.product_identity_evidence is not None else None
                ),
            },
            "metrics": metrics,
            "metric_values": metric_values,
            "derived_rates": derived_rates,
        }

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
        PROMOTION/CLEARANCE 抛 recommendation_type_not_supported。缺来源事实的方向
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
        # 模型选择语义方向后，代码仅从 SKUReadModel 提取带来源的原始事实，校验
        # REQUIRED_FACTS 并决定是否降级。不存在由场景或调用方注入的答案布尔值。
        if rtype in (
            RecommendationType.SELECTION,
            RecommendationType.NEW_LAUNCH,
            RecommendationType.CLEARANCE,
            RecommendationType.EXPERIMENT,
            RecommendationType.PROMOTION,
        ):
            return self._direction_facts(sku, diagnosis, rtype)
        return {}

    def _direction_facts(
        self,
        sku: SKUReadModel,
        diagnosis: Diagnosis,
        rtype: RecommendationType,
    ) -> dict[str, Any]:
        """Expose sourced facts required by a model-selected direction."""

        def available(metric: MetricValue) -> dict[str, Any] | None:
            if metric.evidence_state is EvidenceState.MISSING:
                return None
            return metric.model_dump(mode="json")

        demand_facts = {
            name: fact
            for name in ("orders", "payments", "net_sales")
            if (fact := available(getattr(sku, name))) is not None
        }
        stock_facts = {
            name: fact
            for name in ("sellable_stock", "in_transit_stock")
            if (fact := available(getattr(sku, name))) is not None
        }
        competitor = available(sku.competitor_price)
        if rtype is RecommendationType.SELECTION:
            return {
                **({"demand_signal": demand_facts} if demand_facts else {}),
                **({"competitor_evidence": competitor} if competitor else {}),
            }
        if rtype is RecommendationType.NEW_LAUNCH:
            item_facts = (
                {
                    "listing_revision": sku.listing_revision.model_dump(mode="json"),
                    "title": sku.title,
                    "material_code": sku.material_code,
                }
                if sku.listing_revision is not None else None
            )
            return {
                **({"item_ready": item_facts} if item_facts else {}),
                **({"stock_ready": stock_facts} if stock_facts else {}),
            }
        if rtype is RecommendationType.CLEARANCE:
            return {
                **(
                    {"clearance_signal": {
                        "demand": demand_facts,
                        "stock": stock_facts,
                    }}
                    if demand_facts and stock_facts else {}
                ),
                **({"competitor_evidence": competitor} if competitor else {}),
            }
        if rtype is RecommendationType.EXPERIMENT:
            return (
                {"revision_evidence": sku.listing_revision.model_dump(mode="json")}
                if sku.listing_revision is not None else {}
            )
        if rtype is RecommendationType.PROMOTION:
            campaign_window = diagnosis.evidence_facts.get("campaign_window")
            return (
                {"campaign_window": dict(campaign_window)}
                if isinstance(campaign_window, Mapping) else {}
            )
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
