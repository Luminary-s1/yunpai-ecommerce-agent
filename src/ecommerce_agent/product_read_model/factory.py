"""M9-R WP1 契约对接层：manifest → 强类型读模型列表。

边界声明：
- 输入：manifest（ImportManifestInput）、policy（ReportFieldPolicy）、
  rows（list[Mapping]）、tenant_id、import_id（可选）、row_data_trust（可选）。
- 输出：list[StoreReadModel | ItemReadModel | SKUReadModel]（强类型）。
- 副作用：零——纯内存投影，不插库、不写文件、不网络。
- 失败快速暴露（无静默失败）：
  * sanitize_report_row 的 required_fields 缺失 → ValueError（M7-R 契约行为，透传）
  * 行内 tenant_id/store_id 与 manifest/参数冲突 → ValueError（显式，不静默覆盖）
  * 指标值非有限浮点 → ValueError(non_finite_metric_value)
  * 层级字段越界 → 对应模型 ValidationError（extra="forbid" 物理拒绝）
  * 工厂收口确定性断言（composite_key 槽位 spot-check）——触达即 AssertionError
- 确定性：import_id 占位由 content_digest 派生；period_key 由 data_as_of 派生；
  无时间源、无随机、无外部状态。
"""
from __future__ import annotations

import math
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
    DataTrust,
    Granularity,
    ItemReadModel,
    MetricValue,
    SKUReadModel,
    StoreReadModel,
)

# 指标字段 → (粒度, 聚合规则) 权威映射（WP5 按此表逐字段核对）
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
    "store": {"store_impressions", "store_visitors", "store_clicks",
              "store_add_to_cart", "ad_spend"},
    "item": {"price", "item_impressions", "item_clicks"},
    "sku": {
        "impressions", "clicks", "add_to_cart",
        "orders", "payments", "refunds", "net_sales",
        "sellable_stock", "in_transit_stock",
    },
}


def _evidence_state_for(source_kind: SourceKind) -> EvidenceState:
    """source_kind → evidence_state（M7-R 契约：两者 value 一致）。"""
    return EvidenceState(source_kind.value)


def _data_trust_for(evidence_state: EvidenceState, override: DataTrust | None) -> DataTrust:
    """口径标签：显式 override 优先（由 MetricValue 校验合法性）；否则确定性推导。"""
    if override is not None:
        return override
    if evidence_state is EvidenceState.MISSING:
        return DataTrust.MISSING
    if evidence_state is EvidenceState.DEMO:
        return DataTrust.DEMO
    return DataTrust.PRODUCTION


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
    """显式数值转换：None→None；非有限浮点→抛 ValueError（不静默）。"""
    if value is None:
        return None
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"non_finite_metric_value:{value!r}")
    return converted


def _to_model_kwargs(
    payload: Mapping[str, Any],
    manifest: ImportManifestInput,
    state: EvidenceState,
    tenant_id: str,
    level: str,
    import_id: str,
    data_trust: DataTrust | None,
) -> dict[str, Any]:
    """脱敏 payload → 读模型构造参数。

    行为（确定性）：
    - 指标字段：payload 提供 → 带证据值 MetricValue（绑定 import_id/data_as_of）；
      payload 缺失 → MetricValue.missing（reason="field_not_in_row"），行不崩溃。
    - 非指标字段（ID/料号/revision）透传；revision 缺省 1。
    - 透传语义：工厂不额外剔除合法字段——整份 downstream_payload() 传给对应层级
      Pydantic，由 extra="forbid" 决定字段是否属于该层级（层级越界 → ValidationError）。
    """
    kwargs: dict[str, Any] = {}
    kwargs["tenant_id"] = tenant_id
    kwargs["store_id"] = manifest.store_id
    data_as_of = manifest.data_as_of or manifest.exported_at
    allowed_metric = _LEVEL_METRIC_FIELDS[level]
    for field in allowed_metric:
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
                authoritative_service=manifest.source_system,
                data_trust=data_trust,
            )
        else:
            kwargs[field] = MetricValue.missing(
                granularity,
                rule,
                _period_key(data_as_of, granularity),
                reason="field_not_in_row",
            )
    for field, raw in payload.items():
        if field in kwargs:
            continue
        # 设计意图（计划原文）：工厂不额外剔除合法字段——整份 downstream_payload()
        # 传给对应层级 Pydantic，由 extra="forbid" 决定字段是否属于该层级。
        # 因此层级越界的指标字段（如 SKU 行含 store_impressions）原样透传，
        # 由模型构造抛 ValidationError 物理拒绝，而非工厂静默丢弃。
        kwargs[field] = raw
    if "revision" not in kwargs and level == "sku":
        kwargs["revision"] = 1
    return kwargs


def build_read_model_from_manifest(
    manifest: ImportManifestInput,
    policy: ReportFieldPolicy,
    rows: list[Mapping[str, Any]],
    *,
    tenant_id: str,
    import_id: str | None = None,
    row_data_trust: DataTrust | None = None,
) -> list[StoreReadModel | ItemReadModel | SKUReadModel]:
    """从 manifest 构建强类型读模型列表（纯内存投影）。

    参数边界：
    - import_id：真实 import_id 优先（record_import 返回）；None 时用
      manifest-{content_digest[:12]} 占位（仅内存/测试，WP2 接 service 后替换）。
    - row_data_trust：整批行的口径覆盖；None 时按 evidence_state 确定性推导。
      样本数据导入时显式传 DataTrust.SAMPLE（B7）。
    失败暴露：
    - sanitize_report_row 抛 ValueError（白名单外/敏感/required 缺失）→ 透传，不吞。
    - 行内 tenant_id/store_id 与参数/manifest 冲突 → ValueError，不静默覆盖。
    - 层级字段越界 → 对应模型 ValidationError（extra="forbid"）。
    """
    models: list[StoreReadModel | ItemReadModel | SKUReadModel] = []
    state = _evidence_state_for(manifest.source_kind)
    trust = _data_trust_for(state, row_data_trust)
    import_id = import_id or f"manifest-{manifest.content_digest[:12]}"
    for row in rows:
        payload = sanitize_report_row(policy, row).downstream_payload()
        # 显式冲突检查（防静默覆盖）：行内身份字段必须与 manifest/调用方一致
        if "tenant_id" in payload and payload["tenant_id"] != tenant_id:
            raise ValueError(
                f"row_tenant_conflicts_with_scope:{payload['tenant_id']}:{tenant_id}"
            )
        if "store_id" in payload and payload["store_id"] != manifest.store_id:
            raise ValueError(
                f"row_store_conflicts_with_manifest:{payload['store_id']}:{manifest.store_id}"
            )
        level = _detect_level(payload)
        kwargs = _to_model_kwargs(
            payload, manifest, state, tenant_id, level, import_id, trust
        )
        if level == "store":
            models.append(StoreReadModel(**kwargs))
        elif level == "item":
            models.append(ItemReadModel(**kwargs))
        else:
            models.append(SKUReadModel(**kwargs))
    # 确定性自检：复合主键槽位契约 spot-check（防止未来重构悄悄改变槽位语义）
    if models:
        first_key = models[0].composite_key()
        assert first_key[0] == tenant_id, "composite_key_slot0_must_be_tenant_id"
    return models


__all__ = [
    "METRIC_SPECS",
    "build_read_model_from_manifest",
]
