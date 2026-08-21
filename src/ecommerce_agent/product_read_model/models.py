"""M9-R WP1 读模型骨架：用数据结构保证隔离铁律。

铁律（物理层，非约定）：
1. 绝不串数：复合主键 (tenant, store, item, sku, revision)，无自增 ID 关联。
2. 绝不静默相加：MetricValue 必带 granularity + period_key，底层无 total_sales。
3. 绝不广播：SKU 层物理无店铺级字段（extra="forbid" 类型层拒绝）；SKU 层
   自有粒度流量字段可存在，但缺数据必须是 MISSING，不许用店铺值推导。
4. 溯源与 fail-fast：非 MISSING 指标绑定 import_manifest_id + data_as_of；
   MISSING 与 M7-R FieldEvidenceInput 对齐——不得引用导入，带 reason。

边界声明：
- 全部模型 frozen + extra="forbid"：构造即校验，之后不可变；无 I/O、无副作用。
- data_trust（生产/样本/演示/缺失口径）与 evidence_state（来源四态）必须一致：
  production 仅配 actual/manual；sample 仅配 actual；demo 仅配 demo；
  missing 仅配 missing。非法组合构造即抛。
- 确定性：无时间源、无随机数、无外部状态。period_key 由调用方传入，
  factory 由 manifest 确定性派生。
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ecommerce_agent.readonly_data.contracts import EvidenceState

from .errors import DataUnavailableError


class Granularity(StrEnum):
    HOURLY = "hourly"
    DAILY = "daily"
    MONTHLY = "monthly"


class AggregateRule(StrEnum):
    SUM = "sum"        # 可跨期求和（销量/销售额）
    LATEST = "latest"  # 最新快照（库存）
    NONE = "none"      # 不可聚合（单价/状态）


class DataTrust(StrEnum):
    PRODUCTION = "production"  # 可作产品口径
    SAMPLE = "sample"          # 仅字段/粒度线索，不作产品口径
    DEMO = "demo"              # 隔离演示参数，不作产品口径
    MISSING = "missing"        # 无数据，不作产品口径


# 证据状态 × 数据口径 合法组合（唯一权威映射；枚举变化须同步此处 + 测试）
_TRUST_BY_STATE: dict[EvidenceState, set[DataTrust]] = {
    EvidenceState.ACTUAL: {DataTrust.PRODUCTION, DataTrust.SAMPLE},
    EvidenceState.MANUAL: {DataTrust.PRODUCTION},
    EvidenceState.DEMO: {DataTrust.DEMO},
    EvidenceState.MISSING: {DataTrust.MISSING},
}

# period_key 格式校验（确定性：按粒度固定格式，不依赖时间源）
_PERIOD_KEY_PATTERNS: dict[Granularity, str] = {
    Granularity.HOURLY: r"\d{4}-\d{2}-\d{2}T\d{2}",   # YYYY-MM-DDTHH
    Granularity.DAILY: r"\d{4}-\d{2}-\d{2}",   # YYYY-MM-DD
    Granularity.MONTHLY: r"\d{4}-\d{2}",        # YYYY-MM
}


class MetricValue(BaseModel):
    """单个指标的带证据值（不可变）。

    输入边界：evidence_state / granularity / aggregate_rule / period_key 必填；
    value / import_manifest_id / data_as_of / data_trust / reason 按状态约束。
    输出结构：frozen 值对象；safe_value 属性做 fail-fast 读取。
    副作用：零。

    约束（构造即抛，无静默失败）：
    - MISSING：value=None；import_manifest_id/data_as_of 必须 None（对齐 M7-R）；
      data_trust=MISSING；reason 必填。
    - 非 MISSING：value 非 None 且有限；import_manifest_id/data_as_of 必填；
      data_trust 必须与 evidence_state 合法组合。
    - period_key 必须匹配 granularity 的固定格式。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_state: EvidenceState
    granularity: Granularity
    aggregate_rule: AggregateRule
    period_key: str = Field(min_length=1, max_length=13)
    value: float | None = None
    import_manifest_id: str | None = None
    data_as_of: datetime | None = None
    authoritative_service: str | None = None  # 权威服务溯源（WP2 桥接层回填）
    data_trust: DataTrust | None = None        # 口径标签；None 时按状态确定性推导
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_evidence(self) -> Self:
        # 1) 格式校验：period_key 必须匹配粒度（MISSING 允许占位符，不编造时间戳）
        if self.evidence_state is not EvidenceState.MISSING:
            pattern = _PERIOD_KEY_PATTERNS[self.granularity]
            if re.fullmatch(pattern, self.period_key) is None:
                raise ValueError(
                    f"period_key_format_invalid:{self.granularity.value}:{self.period_key}"
                )

        # 2) 口径推导：data_trust 未显式给时，按状态取唯一确定性默认
        if self.data_trust is None:
            if self.evidence_state is EvidenceState.MISSING:
                object.__setattr__(self, "data_trust", DataTrust.MISSING)
            elif self.evidence_state is EvidenceState.DEMO:
                object.__setattr__(self, "data_trust", DataTrust.DEMO)
            else:
                object.__setattr__(self, "data_trust", DataTrust.PRODUCTION)

        # 3) 状态 × 口径一致性（确定性，防静默漂移）
        allowed = _TRUST_BY_STATE[self.evidence_state]
        if self.data_trust not in allowed:
            raise ValueError(
                "data_trust_evidence_state_mismatch:"
                f"{self.evidence_state.value}:{self.data_trust.value}"
            )

        # 4) 证据语义（与 M7-R FieldEvidenceInput 对齐）
        if self.evidence_state is EvidenceState.MISSING:
            if self.value is not None:
                raise ValueError("missing_evidence_must_not_have_value")
            if self.import_manifest_id is not None or self.data_as_of is not None:
                raise ValueError("missing_evidence_cannot_reference_import")
            if not self.reason:
                raise ValueError("missing_evidence_requires_reason")
        else:
            if self.value is None:
                raise ValueError("evidenced_value_required")
            if not math.isfinite(self.value):
                # 非有限浮点（NaN/Inf）静默传播 = 数据污染；构造即抛（自审补充）
                raise ValueError(f"non_finite_metric_value:{self.value!r}")
            if self.import_manifest_id is None:
                raise ValueError("evidenced_metric_requires_import_manifest")
            if self.data_as_of is None:
                raise ValueError("evidenced_metric_requires_data_as_of")
        return self

    @property
    def safe_value(self) -> float:
        """fail-fast 读取：MISSING 抛 DataUnavailableError，阻断下游计算。"""
        if self.evidence_state is EvidenceState.MISSING:
            raise DataUnavailableError(
                f"metric evidence missing: reason={self.reason} period={self.period_key}"
            )
        if self.value is None:
            # 防御性：构造约束下理论不可达；到达即说明校验被绕过，仍 fail-fast
            raise DataUnavailableError("metric value unexpectedly None")
        return self.value

    @classmethod
    def from_value(
        cls,
        *,
        state: EvidenceState,
        granularity: Granularity,
        aggregate_rule: AggregateRule,
        period_key: str,
        value: float | None = None,
        import_manifest_id: str | None = None,
        data_as_of: datetime | None = None,
        authoritative_service: str | None = None,
        data_trust: DataTrust | None = None,
        reason: str | None = None,
    ) -> "MetricValue":
        """便捷构造器：委托 Pydantic __init__，validator 必然触发（无法绕过）。"""
        return cls(
            evidence_state=state,
            granularity=granularity,
            aggregate_rule=aggregate_rule,
            period_key=period_key,
            value=value,
            import_manifest_id=import_manifest_id,
            data_as_of=data_as_of,
            authoritative_service=authoritative_service,
            data_trust=data_trust,
            reason=reason,
        )

    @classmethod
    def missing(
        cls,
        granularity: Granularity,
        aggregate_rule: AggregateRule,
        period_key: str,
        reason: str,
    ) -> "MetricValue":
        """无证据占位：不带任何导入引用，reason 必填（缺失原因可追溯）。"""
        return cls(
            evidence_state=EvidenceState.MISSING,
            granularity=granularity,
            aggregate_rule=aggregate_rule,
            period_key=period_key,
            value=None,
            data_trust=DataTrust.MISSING,
            reason=reason,
        )


class StoreReadModel(BaseModel):
    """店铺层读模型：挂店铺级流量/广告指标（绝不广播到 SKU 层）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    store_id: str
    store_impressions: MetricValue
    store_visitors: MetricValue
    store_clicks: MetricValue
    store_add_to_cart: MetricValue
    ad_spend: MetricValue

    def composite_key(self) -> tuple[str, str]:
        return (self.tenant_id, self.store_id)


class ItemReadModel(BaseModel):
    """商品层读模型：价格、商品级曝光/点击、料号引用。

    料号引用：material_code 为「待 M7-R WP3 交付后接入」字段；当前 None 占位。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    store_id: str
    item_id: str
    material_code: str | None = None
    price: MetricValue
    item_impressions: MetricValue
    item_clicks: MetricValue

    def composite_key(self) -> tuple[str, str, str]:
        return (self.tenant_id, self.store_id, self.item_id)


class SKUReadModel(BaseModel):
    """SKU 层读模型。

    隔离铁律 3（绝不广播）：店铺级字段物理不出现，注入即 ValidationError；
    本模型含自有粒度流量/交易漏斗字段，缺数据必须是 MISSING，不许用店铺值推导。

    证据域（任务书 WP1）：流量/交易/库存（真实查询）+ 商品/竞品（真实查询）+
    广告/实验（SKU 级缺来源 → 显式 MISSING）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    store_id: str
    item_id: str
    sku_id: str
    revision: int
    material_code: str | None = None
    title: str | None = None
    merchant_code: str | None = None
    impressions: MetricValue
    clicks: MetricValue
    add_to_cart: MetricValue
    orders: MetricValue
    payments: MetricValue
    refunds: MetricValue
    net_sales: MetricValue
    sellable_stock: MetricValue
    in_transit_stock: MetricValue
    ad_spend: MetricValue = Field(default_factory=lambda: MetricValue.missing(
        Granularity.DAILY, AggregateRule.SUM, "—", "ad_metric_store_level_only"
    ))
    competitor_price: MetricValue = Field(default_factory=lambda: MetricValue.missing(
        Granularity.DAILY, AggregateRule.NONE, "—", "competitor_evidence_not_found"
    ))
    experiment_state: MetricValue = Field(default_factory=lambda: MetricValue.missing(
        Granularity.DAILY, AggregateRule.NONE, "—", "experiment_state_provided_by_wp2_bridge"
    ))

    def composite_key(self) -> tuple[str, str, str, str, int]:
        """固定结构契约：长度恒为 5，槽位含义固定。

        idx0=tenant_id, idx1=store_id, idx2=item_id, idx3=sku_id, idx4=revision。
        后续重构不得改变顺序——改变即跨租户/跨店串数。
        """
        return (self.tenant_id, self.store_id, self.item_id, self.sku_id, self.revision)


__all__ = [
    "AggregateRule",
    "DataTrust",
    "Granularity",
    "ItemReadModel",
    "MetricValue",
    "SKUReadModel",
    "StoreReadModel",
]
