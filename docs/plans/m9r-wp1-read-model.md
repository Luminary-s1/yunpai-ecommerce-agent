# M9-R WP1：Listing/SKU 经营读模型骨架 — 实施计划 v4（对齐任务书）

> 存放位置：项目目录 `docs/plans/`（用户要求）
> 状态：**已落地**（分支 `feature/m9r-read-model`，5 源文件 + 5 测试文件，26 passed）
> 分支：`feature/m9r-read-model`
> 前置：M7-R WP1 已合入 main（0b54a24，SCHEMA_VERSION=34）
> 负责人：胡磊（WP1～WP4）；本计划只覆盖 WP1 骨架，WP2～WP5 见 `m9r-wp2-wp5-next-steps.md`
>
> **文档-代码关系声明**：本文件是 WP1 的设计文档，含历史设计（v3→v4 修正对照）与代码参考。
> **已实现代码以 `src/ecommerce_agent/product_read_model/` 为准**（5 源文件全部含边界声明
> docstring）；测试以 `tests/test_m9r_*.py` 5 文件为准（26 passed，见本文件「验证」节）。
> 本文中的代码块为**设计示意**，若与实现有出入，以实际代码为准（尤其：`FunnelState` 枚举
> 已删除改 `funnel_availability` 派生字段；`MetricValue.missing` 签名含 `period_key`；
> `MetricValue` 含 `data_trust`/`authoritative_service` 字段）。

## Context

M7-R WP1 冻结统一只读契约（`readonly_data/contracts.py` + `service.py`）。M9-R WP1 基于它构建
`store + item + SKU` 经营读模型骨架。**本版 v4 对齐 `M9R_PRODUCT_TRAFFIC_LIFECYCLE_WORKBENCH.md`
WP1 任务书逐条验收标准**，修复 v3 的 5 处偏差。

## v3 → v4 修正对照

| # | v3 偏差 | 修正落地 |
|---|---|---|
| 1 | 工厂对缺失指标字段直接 `ValidationError`，整行崩溃——违反「缺失字段仍显示基础事实、阻断结论」 | 工厂对**每层全部必需指标**逐字段投影：payload 缺失 → `evidence_state=MISSING` 的 `MetricValue`，行照常构建 |
| 2 | `MetricValue` MISSING 携带 `import_manifest_id`/`data_as_of`——与 M7-R `FieldEvidenceInput`「MISSING 不得引用导入」冲突 | MISSING 时两者必须 `None`，改带必填 `reason`（缺失原因），与 M7-R 契约语义完全一致 |
| 3 | SKU 层物理无任何流量字段——无法表达「SKU 自身流量为 missing/blocked」（任务书要求） | SKU 层增加**自身粒度**漏斗字段（impressions/clicks/add_to_cart/orders/payments/refunds），真实缺数据时 MISSING；店铺级字段仍 `extra="forbid"` 物理拒绝 |
| 4 | 无「数据准备度」——WP1 任务名一半缺失 | 新增 `readiness.py`：`MetricReadiness` + `SKUReadiness` + `funnel_availability` 派生字段（complete/partial/unavailable；**非第二套枚举**，全模块唯一状态枚举 = M7-R `EvidenceState`） |
| 5 | 临时溯源 id 不接入真实 `record_import` | 工厂支持可选 `import_id` 参数；未传时用占位 id 且文档标注替换触发点（WP2 对接 service 时） |

## 隔离铁律（数据结构保证，四条不变）

1. **绝不串数**：复合主键 `(tenant, store, item, sku, revision)`，无自增 ID 关联。
2. **绝不静默相加**：`MetricValue` 必带 `granularity + period_key`，底层无 `total_sales`。
3. **绝不广播**：店铺级字段（store_impressions 等）物理不出现在 SKU/Item 层（`extra="forbid"` 类型层拒绝）；SKU 层**自有粒度**的流量字段可存在，但真实数据缺失时必须是 `MISSING`，不许用店铺值推导。
4. **溯源与 fail-fast**：非 MISSING 指标绑定 `import_manifest_id + data_as_of`；MISSING 读取抛 `DataUnavailableError`。

## 文件结构

```
src/ecommerce_agent/product_read_model/
  __init__.py     # 导出全部
  errors.py       # DataUnavailableError
  models.py       # Granularity / AggregateRule / DataTrust / MetricValue / Store/Item/SKUReadModel
  readiness.py    # MetricReadiness / SKUReadiness / funnel_availability（派生字段）/ build_sku_readiness
  factory.py      # build_read_model_from_manifest（强类型返回，MISSING 投影）
tests/test_m9r_read_model_isolation.py   # 破坏性隔离测试（13 个）
tests/test_m9r_readiness.py              # 数据准备度测试（3 个）
tests/test_m9r_evidence_state_boundary.py # B5/B6 边界反证测试（2 个）
tests/test_m9r_data_trust.py             # data_trust 测试（5 个，B7 + 非法组合）
tests/test_m9r_privacy.py                # 隐私红线测试（3 个）
```

---

## 文件 1：`src/ecommerce_agent/product_read_model/errors.py`

```python
"""M9-R 读模型领域错误。"""


class DataUnavailableError(ValueError):
    """访问证据状态为 missing 的指标时抛出，阻断下游计算（fail-fast）。"""
```

---

## 文件 2：`src/ecommerce_agent/product_read_model/models.py`

```python
"""M9-R WP1 读模型骨架：用数据结构保证四条隔离铁律。

1. 绝不串数：复合主键 (tenant, store, item, sku, revision)，无自增 ID 关联。
2. 绝不静默相加：MetricValue 必带 granularity + period_key，底层无 total_sales。
3. 绝不广播：SKU 层物理无店铺级曝光/点击/广告字段（extra="forbid" 类型层拒绝）；
   SKU 层自有粒度流量字段可存在，但缺数据必须是 MISSING，不许用店铺值推导。
4. 溯源与 fail-fast：非 MISSING 指标绑定 import_manifest_id + data_as_of；
   MISSING 指标与 M7-R FieldEvidenceInput 对齐——不得引用导入，带 reason。
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ecommerce_agent.readonly_data.contracts import EvidenceState

from .errors import DataUnavailableError


class Granularity(StrEnum):
    DAILY = "daily"
    MONTHLY = "monthly"


class AggregateRule(StrEnum):
    SUM = "sum"        # 可跨粒度求和（销量/销售额）
    LATEST = "latest"  # 最新快照（库存）
    NONE = "none"      # 不可聚合（单价/状态）


class MetricValue(BaseModel):
    """单个指标的带证据值。

    证据语义与 M7-R FieldEvidenceInput 完全一致（v4 修正 2）：
    - MISSING：value 必须 None，且 import_manifest_id/data_as_of 必须 None
      （缺失不是任何一次导入造成的），reason 必填说明缺失原因。
    - 非 MISSING：value 非 None，且 import_manifest_id/data_as_of 必填（溯源）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_state: EvidenceState
    granularity: Granularity
    aggregate_rule: AggregateRule
    period_key: str = Field(min_length=6, max_length=10)
    value: float | None = None
    import_manifest_id: str | None = None
    data_as_of: datetime | None = None
    authoritative_service: str | None = None  # 溯源到权威领域服务（问题③补充；None=MISSING 或未定）
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_evidence(self) -> Self:
        if self.evidence_state is EvidenceState.MISSING:
            if self.value is not None:
                raise ValueError("missing_evidence_must_not_have_value")
            if self.import_manifest_id is not None or self.data_as_of is not None:
                # 与 M7-R FieldEvidenceInput 对齐：MISSING 不得引用导入
                raise ValueError("missing_evidence_cannot_reference_import")
            if not self.reason:
                raise ValueError("missing_evidence_requires_reason")
        else:
            if self.value is None:
                raise ValueError("evidenced_value_required")
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
        return self.value  # type: ignore[return-value]

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
        reason: str | None = None,
    ) -> "MetricValue":
        """便捷构造器：委托 Pydantic __init__，validator 必然触发。"""
        return cls(
            evidence_state=state,
            granularity=granularity,
            aggregate_rule=aggregate_rule,
            period_key=period_key,
            value=value,
            import_manifest_id=import_manifest_id,
            data_as_of=data_as_of,
            authoritative_service=authoritative_service,
            reason=reason,
        )

    @classmethod
    def missing(cls, granularity: Granularity, aggregate_rule: AggregateRule, reason: str) -> "MetricValue":
        """便捷构造器：无证据占位（v4 修正 1/2），不带任何导入引用。"""
        return cls(
            evidence_state=EvidenceState.MISSING,
            granularity=granularity,
            aggregate_rule=aggregate_rule,
            period_key=granularity.value,
            value=None,
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

    料号引用（问题①降级）：material_code 为"待 M7-R WP3 交付后接入"的字段。
    当前 M7-R WP3（身份映射）未交付，本字段**默认 None**；payload 提供则透传。
    解锁条件：M7-R WP3 canonical 映射合入 main 后，从权威服务填充。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    store_id: str
    item_id: str
    material_code: str | None = None  # 问题①：料号引用（待 M7-R WP3 交付）
    price: MetricValue
    item_impressions: MetricValue
    item_clicks: MetricValue

    def composite_key(self) -> tuple[str, str, str]:
        return (self.tenant_id, self.store_id, self.item_id)


class SKUReadModel(BaseModel):
    """SKU 层读模型。

    隔离铁律 3（绝不广播）：
    - 店铺级字段（store_impressions/store_visitors/store_clicks/store_add_to_cart/ad_spend）
      **物理不出现在本模型**，任何注入尝试因 extra="forbid" 抛 ValidationError。
    - 本模型含**自身粒度**的流量/交易漏斗字段（impressions/clicks/add_to_cart/
      orders/payments/refunds）。真实报表无 SKU 级流量时，这些字段必须是
      EvidenceState.MISSING（v4 修正 3）——允许「SKU 流量漏斗 = missing/blocked」，
      但绝不接受用店铺值推导出的伪 SKU 指标。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    store_id: str
    item_id: str
    sku_id: str
    revision: int
    material_code: str | None = None  # 问题①：料号引用（待 M7-R WP3 交付）
    impressions: MetricValue
    clicks: MetricValue
    add_to_cart: MetricValue
    orders: MetricValue
    payments: MetricValue
    refunds: MetricValue
    net_sales: MetricValue
    sellable_stock: MetricValue
    in_transit_stock: MetricValue

    def composite_key(self) -> tuple[str, str, str, str, int]:
        """固定结构契约：长度恒为 5，槽位含义固定。

        idx0=tenant_id, idx1=store_id, idx2=item_id, idx3=sku_id, idx4=revision。
        后续重构不得改变顺序——改变即跨租户/跨店串数。
        """
        return (self.tenant_id, self.store_id, self.item_id, self.sku_id, self.revision)
```

---

## 文件 3：`src/ecommerce_agent/product_read_model/factory.py`

```python
"""M9-R WP1 契约对接层：manifest → 强类型读模型列表（v4：MISSING 投影）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from ecommerce_agent.readonly_data.contracts import (
    EvidenceState,
    ImportManifestInput,
    ReportFieldPolicy,
    SourceKind,
    sanitize_report_row,
)

from .models import (
    AggregateRule,
    Granularity,
    ItemReadModel,
    MetricValue,
    SKUReadModel,
    StoreReadModel,
)

# 指标字段 → (粒度, 聚合规则)
METRIC_SPECS: dict[str, tuple[Granularity, AggregateRule]] = {
    "store_impressions": (Granularity.DAILY, AggregateRule.SUM),
    "store_visitors": (Granularity.DAILY, AggregateRule.SUM),
    "store_clicks": (Granularity.DAILY, AggregateRule.SUM),
    "store_add_to_cart": (Granularity.DAILY, AggregateRule.SUM),
    "ad_spend": (Granularity.MONTHLY, AggregateRule.SUM),
    "price": (Granularity.DAILY, AggregateRule.NONE),
    "item_impressions": (Granularity.DAILY, AggregateRule.SUM),
    "item_clicks": (Granularity.DAILY, AggregateRule.SUM),
    "impressions": (Granularity.DAILY, AggregateRule.SUM),
    "clicks": (Granularity.DAILY, AggregateRule.SUM),
    "add_to_cart": (Granularity.DAILY, AggregateRule.SUM),
    "orders": (Granularity.DAILY, AggregateRule.SUM),
    "payments": (Granularity.DAILY, AggregateRule.SUM),
    "refunds": (Granularity.DAILY, AggregateRule.SUM),
    "net_sales": (Granularity.DAILY, AggregateRule.SUM),
    "sellable_stock": (Granularity.DAILY, AggregateRule.LATEST),
    "in_transit_stock": (Granularity.DAILY, AggregateRule.LATEST),
}

# 各层级模型需要的指标字段（构造时逐字段投影，缺失→MISSING）
_LEVEL_METRIC_FIELDS: dict[str, set[str]] = {
    "store": {"store_impressions", "store_visitors", "store_clicks", "store_add_to_cart", "ad_spend"},
    "item": {"price", "item_impressions", "item_clicks"},
    "sku": {
        "impressions", "clicks", "add_to_cart",
        "orders", "payments", "refunds", "net_sales",
        "sellable_stock", "in_transit_stock",
    },
}


def _evidence_state_for(source_kind: SourceKind) -> EvidenceState:
    """source_kind → evidence_state（readonly 契约：两者 value 一致）。"""
    return EvidenceState(source_kind.value)


def _period_key(value: datetime, granularity: Granularity) -> str:
    """按粒度生成 period_key：daily→"YYYY-MM-DD"，monthly→"YYYY-MM"。"""
    if granularity is Granularity.MONTHLY:
        return value.strftime("%Y-%m")
    return value.strftime("%Y-%m-%d")


def _detect_level(payload: Mapping[str, Any]) -> str:
    """路由优先级：基于明确 ID 字段，不靠模糊"含某层级字段"。

    1. 含 sku_id         → "sku"  （SKU 优先级最高，同含 item_id 也归 SKU）
    2. 含 item_id 无 sku → "item"
    3. 否则              → "store"（store_id 来自 manifest，payload 不含）
    """
    if "sku_id" in payload:
        return "sku"
    if "item_id" in payload:
        return "item"
    return "store"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _to_model_kwargs(
    payload: Mapping[str, Any],
    manifest: ImportManifestInput,
    state: EvidenceState,
    tenant_id: str,
    level: str,
    import_id: str,
) -> dict[str, Any]:
    """脱敏 payload → 读模型构造参数（v4 修正 1：逐字段投影，缺失→MISSING）。

    - 指标字段：payload 提供 → 带证据值 MetricValue（绑定 import_id/data_as_of）；
      payload 缺失 → MetricValue.missing(reason="field_not_in_row")，行不崩溃。
    - 非指标字段：透传。
    - required_fields 缺失不会到这一步——sanitize_report_row 已按 M7-R 契约拒绝整行。
    """
    kwargs: dict[str, Any] = {}
    kwargs["tenant_id"] = tenant_id
    kwargs["store_id"] = manifest.store_id
    data_as_of = manifest.data_as_of or manifest.exported_at
    for field in _LEVEL_METRIC_FIELDS[level]:
        granularity, rule = METRIC_SPECS[field]
        if field in payload:
            kwargs[field] = MetricValue.from_value(
                state=state,
                granularity=granularity,
                aggregate_rule=rule,
                period_key=_period_key(data_as_of, granularity),
                value=_to_float(payload[field]),
                import_manifest_id=import_id,
                data_as_of=data_as_of,
                authoritative_service=manifest.source_system,  # 问题③：权威服务溯源（见 build 文档）
            )
        else:
            kwargs[field] = MetricValue.missing(granularity, rule, reason="field_not_in_row")
    # 非指标字段（如 sku_id/item_id/material_code）透传
    for field, raw in payload.items():
        if field not in METRIC_SPECS and field not in kwargs:
            kwargs[field] = raw
    if "revision" not in kwargs:
        kwargs["revision"] = 1
    return kwargs


def build_read_model_from_manifest(
    manifest: ImportManifestInput,
    policy: ReportFieldPolicy,
    rows: list[Mapping[str, Any]],
    *,
    tenant_id: str,
    import_id: str | None = None,
) -> list[StoreReadModel | ItemReadModel | SKUReadModel]:
    """从 manifest 构建强类型读模型列表（v4：支持真实 import_id）。

    import_id：优先传 `ReadonlyDataService.record_import` 返回的真实 import_id
    （幂等时复用既有 id）；None 时用 `manifest-{content_digest[:12]}` 占位，
    仅用于内存/测试——WP2 对接 service 后必须替换为真实 id 再消费。

    职责边界：sanitize_report_row 只做脱敏 + 白名单提取；本工厂**不额外剔除**
    合法字段——整份 downstream_payload() 传给对应层级 Pydantic，由 extra="forbid"
    决定字段是否属于该层级（物理隔离）。例：SKU 行含 store_impressions（白名单
    内被保留）→ SKUReadModel(**kwargs) 抛 ValidationError，而非被工厂静默丢弃。
    """
    models: list[StoreReadModel | ItemReadModel | SKUReadModel] = []
    state = _evidence_state_for(manifest.source_kind)
    import_id = import_id or f"manifest-{manifest.content_digest[:12]}"
    for row in rows:
        payload = sanitize_report_row(policy, row).downstream_payload()
        level = _detect_level(payload)
        kwargs = _to_model_kwargs(payload, manifest, state, tenant_id, level, import_id)
        if level == "store":
            models.append(StoreReadModel(**kwargs))
        elif level == "item":
            models.append(ItemReadModel(**kwargs))
        else:
            models.append(SKUReadModel(**kwargs))
    return models
```

---

## 文件 4：`src/ecommerce_agent/product_read_model/readiness.py`

```python
"""M9-R WP1 数据准备度（v4 修正 4）：评估读模型的缺失/可用性状态。

WP1 任务书要求"数据准备度、缺失项、时间覆盖…只读"；本模块提供最小组件：
- MetricReadiness：单指标的就绪描述（证据状态/粒度/缺失原因）。
- SKUReadiness：SKU 指标就绪矩阵 + 派生漏斗可用性。
- build_sku_readiness：从 SKUReadModel 计算（纯派生，无副作用）。

唯一证据状态枚举 = M7-R EvidenceState；funnel_availability 是它的派生视图
（complete/partial/unavailable），不是第二套状态标签。派生规则确定性：
任一漏斗字段非 MISSING 计可用；全可用=complete；部分=partial；全 MISSING=unavailable。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ecommerce_agent.readonly_data.contracts import EvidenceState

from .models import AggregateRule, Granularity, SKUReadModel

# 漏斗关键字段（顺序即漏斗顺序：曝光→点击→加购→支付→退款→净销，对齐任务书漏斗）
FUNNEL_FIELDS: tuple[str, ...] = (
    "impressions",
    "clicks",
    "add_to_cart",
    "orders",
    "payments",
    "refunds",
    "net_sales",
)

# 派生值约定（字符串，非枚举——避免第二套状态标签）
FUNNEL_COMPLETE = "complete"
FUNNEL_PARTIAL = "partial"
FUNNEL_UNAVAILABLE = "unavailable"


class MetricReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field_key: str
    evidence_state: EvidenceState
    granularity: Granularity
    aggregate_rule: AggregateRule
    reason: str | None = None


class SKUReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    composite_key: tuple[str, str, str, str, int]
    metrics: tuple[MetricReadiness, ...]
    funnel_availability: str  # complete / partial / unavailable（派生值）

    def readiness_for(self, field_key: str) -> MetricReadiness | None:
        for metric in self.metrics:
            if metric.field_key == field_key:
                return metric
        return None


def build_sku_readiness(sku: SKUReadModel) -> SKUReadiness:
    """从 SKUReadModel 计算准备度矩阵与漏斗可用性（确定性派生）。"""
    observed_fields: tuple[str, ...] = FUNNEL_FIELDS + ("sellable_stock", "in_transit_stock")
    metrics: list[MetricReadiness] = []
    funnel_available = 0
    for field in observed_fields:
        metric_value = getattr(sku, field)
        metrics.append(
            MetricReadiness(
                field_key=field,
                evidence_state=metric_value.evidence_state,
                granularity=metric_value.granularity,
                aggregate_rule=metric_value.aggregate_rule,
                reason=metric_value.reason,
            )
        )
        if field in FUNNEL_FIELDS and metric_value.evidence_state is not EvidenceState.MISSING:
            funnel_available += 1
    if funnel_available == 0:
        availability = FUNNEL_UNAVAILABLE
    elif funnel_available == len(FUNNEL_FIELDS):
        availability = FUNNEL_COMPLETE
    else:
        availability = FUNNEL_PARTIAL
    return SKUReadiness(
        composite_key=sku.composite_key(),
        metrics=tuple(metrics),
        funnel_availability=availability,
    )
```

> 说明：原 v4 计划的 `FunnelState` 枚举（含 BLOCKED）**已删除**——全模块唯一证据状态
> 枚举是 M7-R `EvidenceState`，漏斗可用性是派生字符串。`blocked` 语义由 WP2 诊断层
> 通过把 SKU 流量字段显式标 `missing` + reason 表达，不新增第二套状态枚举。

---

## 文件 5：`src/ecommerce_agent/product_read_model/__init__.py`

```python
"""M9-R WP1 读模型骨架：数据结构保证隔离铁律 + 数据准备度。"""
from .errors import DataUnavailableError
from .factory import METRIC_SPECS, build_read_model_from_manifest
from .models import (
    AggregateRule,
    DataTrust,
    Granularity,
    ItemReadModel,
    MetricValue,
    SKUReadModel,
    StoreReadModel,
)
from .readiness import (
    FUNNEL_COMPLETE,
    FUNNEL_FIELDS,
    FUNNEL_PARTIAL,
    FUNNEL_UNAVAILABLE,
    MetricReadiness,
    SKUReadiness,
    build_sku_readiness,
)

__all__ = [
    "AggregateRule",
    "DataTrust",
    "DataUnavailableError",
    "FUNNEL_COMPLETE",
    "FUNNEL_FIELDS",
    "FUNNEL_PARTIAL",
    "FUNNEL_UNAVAILABLE",
    "Granularity",
    "ItemReadModel",
    "METRIC_SPECS",
    "MetricReadiness",
    "MetricValue",
    "SKUReadModel",
    "SKUReadiness",
    "StoreReadModel",
    "build_read_model_from_manifest",
    "build_sku_readiness",
]
```

---

## 文件 6：`tests/test_m9r_read_model_isolation.py`（13 个破坏性测试）

```python
"""M9-R WP1 破坏性隔离测试：锁隔离规则 + MISSING 投影，不写 CRUD。

13 个测试：
1. SKU 拒绝店铺级指标（extra="forbid" 物理拒绝，工厂路径验证——非伪测试）
2. 复合主键跨店/跨SKU/跨revision 隔离 + 槽位契约防呆
3. 无时间维度 total_sales 不存在 + 不同 period_key 在 set 中不合并
4. MISSING fail-fast + 与 M7-R 对齐（不携带导入引用）+ reason 保留
5. from_value(MISSING, value=非空) 构造即抛（锁修正 1）
6. _detect_level 路由优先级（锁修正 3）
7. 工厂 MISSING 投影：SKU 行缺漏斗字段 → 行照常构建，字段 MISSING，读抛错
8. 工厂接真实 import_id → MetricValue 溯源到该 id
8b. authoritative_service best-effort + material_code 占位（问题③/①）
9. MISSING 不能携带导入引用（构造即抛，锁修正 2）
10. Item 层拒绝店铺级字段
11. Store 层接收店铺字段、拒绝 SKU 字段
12. 同租户多店 composite_key 隔离（并入 2 已覆盖，此处锁 SKU 层店级派生禁止）
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ecommerce_agent.product_read_model.errors import DataUnavailableError
from ecommerce_agent.product_read_model.factory import (
    _detect_level,
    build_read_model_from_manifest,
)
from ecommerce_agent.product_read_model.models import (
    AggregateRule,
    Granularity,
    ItemReadModel,
    MetricValue,
    SKUReadModel,
    StoreReadModel,
)
from ecommerce_agent.readonly_data.contracts import (
    EvidenceState,
    ImportManifestInput,
    ImportReference,
    ReferenceKind,
    ReportFieldPolicy,
    SourceKind,
)

DATA_AS_OF = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
DIGEST = "a" * 64


def _metric(
    state: EvidenceState, value: float | None, period_key: str = "2026-08-17"
) -> MetricValue:
    return MetricValue.from_value(
        state=state, granularity=Granularity.DAILY, aggregate_rule=AggregateRule.SUM,
        period_key=period_key, value=value,
        import_manifest_id="import-test-1", data_as_of=DATA_AS_OF,
    )


def _missing(reason: str = "field_not_in_row") -> MetricValue:
    return MetricValue.missing(Granularity.DAILY, AggregateRule.SUM, reason=reason)


def _sku(
    state: EvidenceState = EvidenceState.ACTUAL,
    store_id: str = "store-a",
    item_id: str = "i1",
    sku_id: str = "sku1",
    revision: int = 1,
    period_key: str = "2026-08-17",
    missing_flow: bool = False,
) -> SKUReadModel:
    flow = _missing("sku_traffic_blocked") if missing_flow else _metric(state, 10.0, period_key)
    return SKUReadModel(
        tenant_id="t1", store_id=store_id, item_id=item_id, sku_id=sku_id, revision=revision,
        impressions=flow,
        clicks=flow,
        add_to_cart=_metric(state, 5.0, period_key),
        orders=_metric(state, 4.0, period_key),
        payments=_metric(state, 3.0, period_key),
        refunds=_metric(state, 0.0, period_key),
        net_sales=_metric(state, 100.0, period_key),
        sellable_stock=_metric(state, 50.0, period_key),
        in_transit_stock=_metric(state, 20.0, period_key),
    )


def _policy() -> ReportFieldPolicy:
    return ReportFieldPolicy(
        report_type="store_traffic", mapping_version="v1",
        allowed_fields=frozenset({
            "store_impressions", "store_visitors", "store_clicks", "store_add_to_cart",
            "ad_spend", "item_id", "sku_id",
            "impressions", "clicks", "add_to_cart",
            "orders", "payments", "refunds", "net_sales",
            "sellable_stock", "in_transit_stock",
        }),
        required_fields=frozenset({"item_id", "sku_id", "net_sales"}),
    )


def _manifest() -> ImportManifestInput:
    return ImportManifestInput(
        store_id="s1", source_kind=SourceKind.ACTUAL, source_system="taobao",
        report_type="store_traffic", report_period="2026-08-17",
        exported_at=DATA_AS_OF, schema_fingerprint=DIGEST, content_digest=DIGEST,
        mapping_version="v1", parsed_rows=1, data_as_of=DATA_AS_OF,
        references=(ImportReference(
            kind=ReferenceKind.RAW_FILE,
            reference="objects/readonly-imports/x.csv", content_digest=DIGEST,
        ),),
    )


# ── 1. SKU 拒绝店铺级指标 ──────────────────────────────────────────────
def test_sku_model_rejects_store_level_metrics() -> None:
    """v4 修正 3 验证：SKU 层物理拒绝店铺级字段，工厂路径真实触发（非伪测试）。"""
    rows = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
             "sellable_stock": 50.0, "in_transit_stock": 20.0,
             "store_impressions": 9999.0}]  # 白名单内被保留 → SKU 层被拒
    with pytest.raises(ValidationError):
        build_read_model_from_manifest(_manifest(), _policy(), rows, tenant_id="t1")
    with pytest.raises(ValidationError):
        SKUReadModel(
            tenant_id="t1", store_id="s1", item_id="i1", sku_id="sku1", revision=1,
            impressions=_metric(EvidenceState.ACTUAL, 10.0),
            clicks=_metric(EvidenceState.ACTUAL, 5.0),
            add_to_cart=_metric(EvidenceState.ACTUAL, 5.0),
            orders=_metric(EvidenceState.ACTUAL, 4.0),
            payments=_metric(EvidenceState.ACTUAL, 3.0),
            refunds=_metric(EvidenceState.ACTUAL, 0.0),
            net_sales=_metric(EvidenceState.ACTUAL, 100.0),
            sellable_stock=_metric(EvidenceState.ACTUAL, 50.0),
            in_transit_stock=_metric(EvidenceState.ACTUAL, 20.0),
            store_impressions=_metric(EvidenceState.ACTUAL, 9999.0),
        )


# ── 2. 复合主键跨店/跨SKU/跨revision 隔离 ──────────────────────────────
def test_composite_key_prevents_cross_scope_leak() -> None:
    """v4 验证：同一 item 多 SKU、同一 SKU 多 revision、同租户多店均不串数。"""
    a = _sku(store_id="store-a")
    b = _sku(store_id="store-b")
    c = _sku(item_id="i2", sku_id="sku9")
    d = _sku(sku_id="sku1", revision=2)
    assert len(a.composite_key()) == 5
    assert a.composite_key() == ("t1", "store-a", "i1", "sku1", 1)
    assert a.composite_key() != b.composite_key()  # 跨店
    assert a.composite_key() != c.composite_key()  # 跨 item/SKU
    assert a.composite_key() != d.composite_key()  # 跨 revision
    assert len({a.composite_key(), b.composite_key(), c.composite_key(), d.composite_key()}) == 4
    assert a.composite_key()[1] == "store-a"  # 槽位契约：idx1 = store_id


# ── 3. 无时间维度汇总 + 跨周期不合并 ────────────────────────────────────
def test_silent_aggregation_blocked() -> None:
    """v4 验证：无时间维度汇总字段物理不存在；不同 period_key 在 set 中不合并。"""
    sku = _sku()
    assert not hasattr(sku, "total_sales"), "无时间维度汇总字段物理不存在"
    assert sku.net_sales.granularity is Granularity.DAILY
    assert sku.net_sales.aggregate_rule is AggregateRule.SUM

    daily = _metric(EvidenceState.ACTUAL, 100.0, period_key="2026-08-17")
    monthly = MetricValue.from_value(
        state=EvidenceState.ACTUAL, granularity=Granularity.MONTHLY,
        aggregate_rule=AggregateRule.SUM, period_key="2026-08", value=3000.0,
        import_manifest_id="import-test-1", data_as_of=DATA_AS_OF,
    )
    assert daily.period_key != monthly.period_key
    assert len({daily, monthly}) == 2, "不同 period_key 的值在 set 中不合并"
    assert daily.period_key == "2026-08-17"
    assert monthly.period_key == "2026-08"


# ── 4. MISSING fail-fast + 与 M7-R 对齐 ─────────────────────────────────
def test_missing_evidence_fails_fast_and_rejects_import_ref() -> None:
    """v4 修正 2 验证：MISSING 不携带导入引用（对齐 FieldEvidenceInput）+ 读取抛错。"""
    missing = MetricValue.missing(Granularity.DAILY, AggregateRule.SUM, reason="field_not_in_row")
    assert missing.import_manifest_id is None
    assert missing.data_as_of is None
    assert missing.reason == "field_not_in_row"
    with pytest.raises(DataUnavailableError):
        missing.safe_value
    # MISSING + 导入引用 → 构造即抛（锁死修正 2）
    with pytest.raises(ValidationError, match="missing_evidence_cannot_reference_import"):
        MetricValue.from_value(
            state=EvidenceState.MISSING, granularity=Granularity.DAILY,
            aggregate_rule=AggregateRule.SUM, period_key="2026-08-17", value=None,
            import_manifest_id="import-test-1", data_as_of=DATA_AS_OF, reason="x",
        )


# ── 5. from_value(MISSING, value=非空) 构造即抛 ─────────────────────────
def test_from_value_rejects_missing_with_value() -> None:
    """锁修正 1：MISSING + 非空 value → 构造即抛 ValidationError（from_value 无法绕过）。"""
    with pytest.raises(ValidationError, match="missing_evidence_must_not_have_value"):
        MetricValue.from_value(
            state=EvidenceState.MISSING, granularity=Granularity.DAILY,
            aggregate_rule=AggregateRule.SUM, period_key="2026-08-17", value=100.0,
            import_manifest_id="import-test-1", data_as_of=DATA_AS_OF, reason="x",
        )


# ── 6. _detect_level 路由优先级 ─────────────────────────────────────────
def test_detect_level_routing_priority() -> None:
    """锁修正 3：基于明确 ID 字段的优先级路由（store 由"无 id 字段"决定，不依赖 store_id）。"""
    assert _detect_level({"item_id": "i1", "sku_id": "sku1"}) == "sku"
    assert _detect_level({"item_id": "i1", "sku_id": "sku1", "net_sales": 1.0}) == "sku"
    assert _detect_level({"item_id": "i1"}) == "item"
    assert _detect_level({"store_impressions": 100.0}) == "store"
    # 工厂端到端：SKU 行（同含 item_id+sku_id）→ 正确路由到 SKUReadModel
    rows = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
             "sellable_stock": 50.0, "in_transit_stock": 20.0}]
    models = build_read_model_from_manifest(_manifest(), _policy(), rows, tenant_id="t1")
    assert isinstance(models[0], SKUReadModel)


# ── 7. 工厂 MISSING 投影：缺字段不崩行 ──────────────────────────────────
def test_factory_projects_missing_fields() -> None:
    """v4 修正 1 验证：SKU 行缺漏斗字段 → 行照常构建，缺字段 MISSING，读抛错。"""
    rows = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
             "sellable_stock": 50.0, "in_transit_stock": 20.0}]
    models = build_read_model_from_manifest(_manifest(), _policy(), rows, tenant_id="t1")
    sku = models[0]
    assert isinstance(sku, SKUReadModel)
    # 提供的字段有值
    assert sku.net_sales.safe_value == 100.0
    # 缺失字段投影为 MISSING，可安全读取状态而不崩
    assert sku.impressions.evidence_state is EvidenceState.MISSING
    assert sku.impressions.reason == "field_not_in_row"
    assert sku.orders.evidence_state is EvidenceState.MISSING
    # 读取 MISSING 值 fail-fast
    with pytest.raises(DataUnavailableError):
        sku.impressions.safe_value
    # 不同行缺不同字段互不影响
    rows2 = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
              "impressions": 300.0, "orders": 5.0}]
    models2 = build_read_model_from_manifest(_manifest(), _policy(), rows2, tenant_id="t1")
    sku2 = models2[0]
    assert sku2.impressions.safe_value == 300.0
    assert sku2.orders.safe_value == 5.0
    assert sku2.clicks.evidence_state is EvidenceState.MISSING


# ── 8. 工厂接真实 import_id ────────────────────────────────────────────
def test_factory_accepts_real_import_id() -> None:
    """v4 修正 5 验证：传入 record_import 返回的真实 import_id 后，溯源到该 id。"""
    rows = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
             "sellable_stock": 50.0, "in_transit_stock": 20.0}]
    models = build_read_model_from_manifest(
        _manifest(), _policy(), rows, tenant_id="t1", import_id="import-real-uuid"
    )
    assert models[0].net_sales.import_manifest_id == "import-real-uuid"  # type: ignore[union-attr]
    assert models[0].net_sales.data_as_of == DATA_AS_OF  # type: ignore[union-attr]


# ── 8b. 权威服务 best-effort + 料号引用占位（问题③/①） ────────────────
def test_metric_authoritative_service_and_material_code() -> None:
    """v4 补充：authoritative_service 为来源系统 best-effort；material_code 占位 None。"""
    rows = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
             "sellable_stock": 50.0, "in_transit_stock": 20.0}]
    models = build_read_model_from_manifest(_manifest(), _policy(), rows, tenant_id="t1")
    sku = models[0]
    assert sku.net_sales.authoritative_service == "taobao"  # type: ignore[union-attr]  # 来源系统 best-effort
    assert sku.material_code is None  # 料号引用占位：待 M7-R WP3 交付（问题①）
    assert sku.impressions.authoritative_service is None  # MISSING 不携带权威服务


# ── 9. MISSING 不能携带导入引用 ─────────────────────────────────────────
def test_missing_metric_cannot_reference_import() -> None:
    """锁修正 2：MetricValue 缺 evidence 引用构造即抛（见测试 4 第二断言，此处留独立锁）。"""
    with pytest.raises(ValidationError, match="missing_evidence_cannot_reference_import"):
        MetricValue.from_value(
            state=EvidenceState.MISSING, granularity=Granularity.DAILY,
            aggregate_rule=AggregateRule.SUM, period_key="2026-08-17", value=None,
            import_manifest_id="import-x", data_as_of=DATA_AS_OF, reason="r",
        )
    # 无 reason 也拒绝
    with pytest.raises(ValidationError, match="missing_evidence_requires_reason"):
        MetricValue.missing(Granularity.DAILY, AggregateRule.SUM, reason="")


# ── 10. Item 层拒绝店铺级字段 ──────────────────────────────────────────
def test_item_model_rejects_store_level_metrics() -> None:
    """v4 验证：Item 层同样物理拒绝店铺级字段（铁律 3 延伸到 item）。"""
    with pytest.raises(ValidationError):
        ItemReadModel(
            tenant_id="t1", store_id="s1", item_id="i1",
            price=_metric(EvidenceState.ACTUAL, 99.0),
            item_impressions=_metric(EvidenceState.ACTUAL, 500.0),
            item_clicks=_metric(EvidenceState.ACTUAL, 30.0),
            store_impressions=_metric(EvidenceState.ACTUAL, 9999.0),
        )


# ── 11. Store 层接收店铺字段、拒绝 SKU 字段 ────────────────────────────
def test_store_model_rejects_sku_metrics() -> None:
    """v4 验证：Store 层接收店铺字段，但拒绝 SKU 级净销量字段（反向广播）。"""
    rows = [{"store_impressions": 1000.0, "store_visitors": 800.0,
             "store_clicks": 200.0, "store_add_to_cart": 30.0,
             "ad_spend": 500.0, "net_sales": 9000.0}]  # net_sales 是 SKU/交易字段
    with pytest.raises(ValidationError):
        build_read_model_from_manifest(_manifest(), _policy(), rows, tenant_id="t1")
    good = build_read_model_from_manifest(
        _manifest(), _policy(),
        [{"store_impressions": 1000.0, "store_visitors": 800.0,
          "store_clicks": 200.0, "store_add_to_cart": 30.0, "ad_spend": 500.0}],
        tenant_id="t1",
    )
    assert isinstance(good[0], StoreReadModel)


# ── 12. SKU 层店级派生禁止 ────────────────────────────────────────────
def test_sku_traffic_cannot_be_derived_from_store() -> None:
    """v4 修正 3 验证：SKU 流量字段物理存在但缺数据时是 MISSING，不是店铺推导值。"""
    sku = _sku(missing_flow=True)  # impressions/clicks 为 MISSING
    assert sku.impressions.evidence_state is EvidenceState.MISSING
    assert sku.impressions.reason == "sku_traffic_blocked"
    with pytest.raises(DataUnavailableError):
        sku.impressions.safe_value
```

---

## 文件 7：`tests/test_m9r_readiness.py`（3 个准备度测试）

```python
"""M9-R WP1 数据准备度测试（funnel_availability 派生视图，非第二套枚举）。"""
from __future__ import annotations

from datetime import UTC, datetime

from ecommerce_agent.product_read_model.models import (
    AggregateRule, Granularity, MetricValue, SKUReadModel,
)
from ecommerce_agent.product_read_model.readiness import (
    FUNNEL_COMPLETE, FUNNEL_PARTIAL, FUNNEL_UNAVAILABLE, build_sku_readiness,
)
from ecommerce_agent.readonly_data.contracts import EvidenceState

DATA_AS_OF = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)


def _mv(value: float) -> MetricValue:
    return MetricValue.from_value(
        state=EvidenceState.ACTUAL, granularity=Granularity.DAILY,
        aggregate_rule=AggregateRule.SUM, period_key="2026-08-17", value=value,
        import_manifest_id="import-1", data_as_of=DATA_AS_OF,
    )


def _missing(reason: str = "field_not_in_row") -> MetricValue:
    return MetricValue.missing(
        Granularity.DAILY, AggregateRule.SUM, "2026-08-17", reason=reason
    )


def _sku(flow: MetricValue, sales: MetricValue) -> SKUReadModel:
    return SKUReadModel(
        tenant_id="t1", store_id="s1", item_id="i1", sku_id="sku1", revision=1,
        impressions=flow, clicks=flow, add_to_cart=flow, orders=flow,
        payments=flow, refunds=_missing(), net_sales=sales,
        sellable_stock=_mv(50.0), in_transit_stock=_mv(20.0),
    )


def test_readiness_complete_funnel() -> None:
    flow = _mv(10.0)
    readiness = build_sku_readiness(_sku(flow, _mv(100.0)))
    # refunds 恒为 missing（_sku 夹具），所以是 partial 而非 complete
    assert readiness.funnel_availability == FUNNEL_PARTIAL
    assert readiness.composite_key == ("t1", "s1", "i1", "sku1", 1)
    assert readiness.readiness_for("net_sales") is not None
    assert readiness.readiness_for("refunds").reason == "field_not_in_row"  # type: ignore[union-attr]


def test_readiness_all_funnel_fields_available() -> None:
    """全部 7 个漏斗字段可用 → complete。"""
    flow = _mv(10.0)
    sku = SKUReadModel(
        tenant_id="t1", store_id="s1", item_id="i1", sku_id="sku1", revision=1,
        impressions=flow, clicks=flow, add_to_cart=flow, orders=flow,
        payments=flow, refunds=flow, net_sales=_mv(100.0),
        sellable_stock=_mv(50.0), in_transit_stock=_mv(20.0),
    )
    readiness = build_sku_readiness(sku)
    assert readiness.funnel_availability == FUNNEL_COMPLETE


def test_readiness_missing_funnel() -> None:
    flow = _missing()
    sales = _missing()
    readiness = build_sku_readiness(_sku(flow, sales))
    assert readiness.funnel_availability == FUNNEL_UNAVAILABLE
```

---

## 验证

```
cd /d/yunpai-ecommerce-agent
git checkout -b feature/m9r-read-model origin/main      # 建分支
python -m pytest tests/test_m9r_read_model_isolation.py tests/test_m9r_readiness.py tests/test_m9r_evidence_state_boundary.py tests/test_m9r_data_trust.py tests/test_m9r_privacy.py -q --no-header -p no:cacheprovider  # 红→绿（26 passed）
python -m pytest tests/test_readonly_data_contract.py tests/test_traffic_lab.py -q --no-header -p no:cacheprovider  # 上游契约不回归（25 passed）
```

## 全量回归证据（WP1 收口）

> 格式规范见 `m9r-complete-plan.md` 第十节「回归证据规范」。

- 执行时间：2026-08-18 15:57（全量回归完成时间戳）
- 命令：`python scripts/run_full_regression.py --allow-dirty`
- M9-R WP1 测试：`27 passed`（5 文件：isolation 13 / readiness 3 / boundary 2 / data_trust 6 / privacy 3）
- **M9-R WP1-WP4 全部测试：`92 passed`（21 个测试文件，1.21s）**——含 WP2/3/4 的 65 个
- 上游契约回归：`test_readonly_data_contract.py` + `test_traffic_lab.py` → `25 passed`（17.69s）
- WP1 验收：18 条标准逐条断言验证（15 ✅ PASS + 3 依赖项 SKIP，见 `tests/verify_wp1_acceptance.py`）
- 全量回归：**超时（900s）——非 WP1-4 回归**
  - 原因：项目既有测试慢（如 `test_agent.py` 7 测试 48s，每个构造完整 AgentService；全量 1042 测试 15 分钟只跑 7%）
  - WP1-4 的 92 测试全部 1.21s 秒级通过，未引入任何慢测试
  - 证据：`test_agent.py` 未被我方修改（git diff 确认）；慢为既有设计
- 报告：`pytest_debug_report.json`（含系统快照）
- 状态：✅ WP1-4 无回归（局部 92 passed + 上游契约 25 passed）；全量超时为既有问题，如实标注

## WP1 验收对照（对齐任务书，诚实标注状态）

> 状态标记：✅ 已达标（本计划已覆盖）；⚠️ 部分达标（依赖未满足，见降级/解锁条件）；❌ 不达标（当前做不到，已降级）

| WP1 验收标准 | 落地 | 状态 |
|---|---|---|
| 同一 item 多 SKU / 同 SKU 多 revision / 同租户多店不串数 | composite_key 五元组 + 测试 2 | ✅ 已达标 |
| 日/月、店铺/商品、支付/退款等不同粒度不静默相加 | period_key+granularity 物理隔离 + 测试 3 | ✅ 已达标 |
| 店铺级曝光/点击/广告不广播、均摊或推导成 SKU 指标 | extra="forbid" 物理拒绝 + 测试 1/10/11/12 | ✅ 已达标 |
| 缺广告/竞品/退款明细时仍显示基础流量事实，阻断依赖结论 | 工厂 MISSING 投影 + safe_value fail-fast + 测试 7 | ⚠️ 部分达标（见下：问题②） |
| 每个值回溯到权威服务、import manifest 和 data_as_of | 部分：import_id + data_as_of + 测试 8；权威服务为 best-effort（见下：问题③） | ⚠️ 部分达标（见下：问题③） |
| 数据准备度、漏斗可用性、缺失阻断语义 | readiness.py + funnel_availability 派生字段 + 测试 1-3（readiness 文件） | ✅ 已达标 |
| 保留字段原始粒度、料号引用、来源和 data_as_of（WP1 任务原文） | 粒度/来源/data_as_of 已覆盖；料号引用为字段占位、默认 None（见下：问题①） | ⚠️ 部分达标（见下：问题①） |

### 问题① 料号引用 — 降级为"待 M7-R WP3 交付后接入"

- **当前状态**：`material_code` 字段已加入 Item/SKUReadModel（`str | None = None`），payload 提供则透传，缺省为 None。M7-R WP3（canonical 商品/SKU/料号映射）**未合入 main**（`readonly_data/` 仅 contracts.py + service.py，无映射表/接口），本字段当前无法从权威服务填充。
- **降级措辞**：`material_code` = `None` 即表示"料号引用不可用：M7-R WP3 映射未交付"；读取非 None 值前应视来源判断，不作为已达标依据。
- **解锁条件**：当 **M7-R WP3 canonical 商品/SKU/商家编码/料号映射合入 main 且有权威查询接口** 时，WP1 从该服务填充 material_code，此时本项升级为已达标。

### 问题② 缺竞品/退款仍显示基础事实 — 达标假设未成立，需确认 M7-R WP2 交付范围

- **当前状态**：工厂 MISSING 投影保证"行缺字段不崩、缺失指标可安全读取"。但"竞品/退款明细缺失"的真实边界，取决于 **M7-R WP2 已交付的数据域范围**，而 WP2 交付状态未在 main 上核实。
- **降级措辞**：本项标注为"**取决于 M7-R WP2 交付范围，需确认**"。在确认前，测试只断言"同行缺字段投影 MISSING"（机械事实），**不**断言"竞品/退款数据域可用"。
- **解锁条件**：当 **M7-R WP2 交付范围清单确认（含竞品/退款域是否交付）** 时，按实际范围补场景测试，本项升级为已达标；若 WP2 未交付对应数据域，则本项继续以"缺数据时显示基础事实+阻断"的机械语义成立，但不得声称覆盖了竞品/退款业务域。

### 问题③ 回溯到权威服务 — 缺失一半，验收不标全额达标

- **当前状态**：`MetricValue` 增加 `authoritative_service: str | None`（溯源到权威领域服务）。工厂现以 `manifest.source_system` 作 best-effort 映射（`authoritative_service=manifest.source_system`）。**该映射仅是来源系统，不是"哪个领域服务产出该值"的权威声明**——真正的权威服务解析（如 `store_read_model` 由哪一服务投影）未实现。
- **降级措辞**：验收条目改写为"每个值可回溯到 **import manifest 和 data_as_of**（已达标）；**权威服务**为 best-effort 来源系统标记，权威服务解析待 WP2 桥接层实现"。
- **解锁条件**：当 **WP2 桥接层确立权威服务投影规则（每个读模型值绑定产出它的领域服务）** 时，回填各字段的 authoritative_service 并补断言，本项升级为已达标。

## 待负责人 / 待外部交付（不阻塞本阶段，但影响验收状态）

- **真实 import_id 替换触发点**：WP2 对接 `ReadonlyDataService` 时，工厂调用改为
  `import_id=record_import(tenant_id, manifest)["import_id"]`，占位 id 停止使用。
  替换后补一条「同 manifest 重复导入幂等复用 id」的测试。
- **落库（v35 迁移）**：内存骨架满足 WP1「逐值可回溯」；若需跨请求查询/工作台展示，
  再按 CONTRIBUTING 占 v35。任务书明确「本文不预占」，故不在此占号。
- **`BLOCKED` 语义**：由 WP2 诊断层在「店铺级流量无法拆 SKU / revision 窗口缺失」时
  显式写入；骨架已保留枚举位。
- **料号引用（问题①）**：`material_code` 占位为 None。解锁条件 = **M7-R WP3 canonical
  映射合入 main 且有权威查询接口**。在解锁前，本项不标已达标。
- **权威服务解析（问题③）**：`authoritative_service` 现为来源系统 best-effort。解锁条件 =
  **WP2 桥接层确立权威服务投影规则**后回填并补断言。
