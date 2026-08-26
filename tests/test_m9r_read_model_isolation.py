"""M9-R WP1 破坏性隔离测试：锁隔离规则 + MISSING 投影，不写 CRUD。

13 个测试（每行一个测试函数，命名即锁定内容）：
1. SKU 拒绝店铺级指标（extra="forbid" 物理拒绝，工厂路径验证——非伪测试）
2. 复合主键跨店/跨SKU/跨revision 隔离 + 槽位契约防呆
3. 无时间维度 total_sales 不存在 + 不同 period_key 在 set 中不合并
4. MISSING fail-fast + 与 M7-R 对齐（不携带导入引用）+ reason 保留
5. from_value(MISSING, value=非空) 构造即抛
6. _detect_level 路由优先级
7. 工厂 MISSING 投影：SKU 行缺漏斗字段 → 行照常构建，字段 MISSING，读抛错
8. 工厂接真实 import_id → MetricValue 溯源到该 id
9. authoritative_service best-effort + material_code 占位
10. MISSING 不能携带导入引用（构造即抛）
11. Item 层拒绝店铺级字段
12. Store 层接收店铺字段、拒绝 SKU 字段
13. SKU 层店级派生禁止（手工构造：缺数据= MISSING，非店铺推导值）
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

# 全部 17 个指标字段（白名单必须包含，否则 sanitize 剥离后测不到 extra="forbid" 层）
_ALL_METRIC_FIELDS = frozenset({
    "store_impressions", "store_visitors", "store_clicks", "store_add_to_cart",
    "ad_spend", "price", "item_impressions", "item_clicks",
    "impressions", "clicks", "add_to_cart", "orders", "payments", "refunds",
    "net_sales", "sellable_stock", "in_transit_stock",
})


def _metric(
    state: EvidenceState,
    value: float | None,
    period_key: str = "2026-08-17",
) -> MetricValue:
    return MetricValue.from_value(
        state=state, granularity=Granularity.DAILY, aggregate_rule=AggregateRule.SUM,
        period_key=period_key, value=value,
        import_manifest_id="import-test-1", data_as_of=DATA_AS_OF,
    )


def _sku(
    state: EvidenceState = EvidenceState.ACTUAL,
    store_id: str = "store-a",
    item_id: str = "i1",
    sku_id: str = "sku1",
    revision: int = 1,
    period_key: str = "2026-08-17",
    missing_flow: bool = False,
) -> SKUReadModel:
    flow = (
        MetricValue.missing(
            Granularity.DAILY, AggregateRule.SUM, period_key,
            reason="sku_traffic_blocked",
        )
        if missing_flow
        else _metric(state, 10.0, period_key)
    )
    return SKUReadModel(
        tenant_id="t1", store_id=store_id, item_id=item_id, sku_id=sku_id,
        revision=revision,
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


def _sku_policy() -> ReportFieldPolicy:
    """SKU 行 policy：白名单含全部指标 + ID 字段，required 锁 SKU 行必备项。"""
    return ReportFieldPolicy(
        report_type="store_traffic", mapping_version="v1",
        allowed_fields=_ALL_METRIC_FIELDS
        | frozenset({"item_id", "sku_id", "material_code"}),
        required_fields=frozenset({"item_id", "sku_id", "net_sales"}),
    )


def _store_policy() -> ReportFieldPolicy:
    """store 行 policy：required 为空（store 行无 ID 字段）；白名单含店铺指标 + net_sales
    （net_sales 用于测试 12 的越界拒绝——必须留在白名单里才能走到 extra="forbid" 层）。"""
    return ReportFieldPolicy(
        report_type="store_traffic", mapping_version="v1",
        allowed_fields=frozenset({
            "store_impressions", "store_visitors", "store_clicks",
            "store_add_to_cart", "ad_spend", "net_sales",
        }),
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
    """工厂不剔除合法字段，extra="forbid" 真实触发（非伪测试）。"""
    rows = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
             "sellable_stock": 50.0, "in_transit_stock": 20.0,
             "store_impressions": 9999.0}]  # 白名单内被保留 → SKU 层被拒
    with pytest.raises(ValidationError):
        build_read_model_from_manifest(
            _manifest(), _sku_policy(), rows, tenant_id="t1"
        )
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
    """同一 item 多 SKU、同一 SKU 多 revision、同租户多店均不串数。"""
    a = _sku(store_id="store-a")
    b = _sku(store_id="store-b")
    c = _sku(item_id="i2", sku_id="sku9")
    d = _sku(sku_id="sku1", revision=2)
    assert len(a.composite_key()) == 5
    assert a.composite_key() == ("t1", "store-a", "i1", "sku1", 1)
    assert a.composite_key() != b.composite_key()  # 跨店
    assert a.composite_key() != c.composite_key()  # 跨 item/SKU
    assert a.composite_key() != d.composite_key()  # 跨 revision
    assert len({a.composite_key(), b.composite_key(), c.composite_key(),
                d.composite_key()}) == 4
    assert a.composite_key()[1] == "store-a"  # 槽位契约：idx1 = store_id


# ── 3. 无时间维度汇总 + 跨周期不合并 ────────────────────────────────────
def test_silent_aggregation_blocked() -> None:
    """无时间维度汇总字段物理不存在；不同 period_key 在 set 中不合并。"""
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
    """MISSING 不携带导入引用（对齐 FieldEvidenceInput）+ 读取抛错 + reason 保留。"""
    missing = MetricValue.missing(
        Granularity.DAILY, AggregateRule.SUM, "2026-08-17", reason="field_not_in_row"
    )
    assert missing.import_manifest_id is None
    assert missing.data_as_of is None
    assert missing.reason == "field_not_in_row"
    assert missing.data_trust.value == "missing"
    with pytest.raises(DataUnavailableError):
        missing.safe_value
    # MISSING + 导入引用 → 构造即抛
    with pytest.raises(ValidationError, match="missing_evidence_cannot_reference_import"):
        MetricValue.from_value(
            state=EvidenceState.MISSING, granularity=Granularity.DAILY,
            aggregate_rule=AggregateRule.SUM, period_key="2026-08-17", value=None,
            import_manifest_id="import-test-1", data_as_of=DATA_AS_OF, reason="x",
        )


# ── 5. from_value(MISSING, value=非空) 构造即抛 ─────────────────────────
def test_from_value_rejects_missing_with_value() -> None:
    """MISSING + 非空 value → 构造即抛 ValidationError（from_value 无法绕过）。"""
    with pytest.raises(ValidationError, match="missing_evidence_must_not_have_value"):
        MetricValue.from_value(
            state=EvidenceState.MISSING, granularity=Granularity.DAILY,
            aggregate_rule=AggregateRule.SUM, period_key="2026-08-17", value=100.0,
            import_manifest_id="import-test-1", data_as_of=DATA_AS_OF, reason="x",
        )


# ── 6. _detect_level 路由优先级 ─────────────────────────────────────────
def test_detect_level_routing_priority() -> None:
    """基于明确 ID 字段的优先级路由（store 由"无 id 字段"决定，不依赖 store_id）。"""
    assert _detect_level({"item_id": "i1", "sku_id": "sku1"}) == "sku"
    assert _detect_level({"item_id": "i1", "sku_id": "sku1", "net_sales": 1.0}) == "sku"
    assert _detect_level({"item_id": "i1"}) == "item"
    assert _detect_level({"store_impressions": 100.0}) == "store"
    # 工厂端到端：SKU 行（同含 item_id+sku_id）→ 正确路由到 SKUReadModel
    rows = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
             "sellable_stock": 50.0, "in_transit_stock": 20.0}]
    models = build_read_model_from_manifest(
        _manifest(), _sku_policy(), rows, tenant_id="t1"
    )
    assert isinstance(models[0], SKUReadModel)


# ── 7. 工厂 MISSING 投影：缺字段不崩行 ──────────────────────────────────
def test_factory_projects_missing_fields() -> None:
    """SKU 行缺漏斗字段 → 行照常构建，缺字段 MISSING，读抛错。"""
    rows = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
             "sellable_stock": 50.0, "in_transit_stock": 20.0}]
    models = build_read_model_from_manifest(
        _manifest(), _sku_policy(), rows, tenant_id="t1"
    )
    sku = models[0]
    assert isinstance(sku, SKUReadModel)
    assert sku.net_sales.safe_value == 100.0
    assert sku.impressions.evidence_state is EvidenceState.MISSING
    assert sku.impressions.reason == "field_not_in_row"
    assert sku.orders.evidence_state is EvidenceState.MISSING
    with pytest.raises(DataUnavailableError):
        sku.impressions.safe_value
    # 不同行缺不同字段互不影响
    rows2 = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
              "impressions": 300.0, "orders": 5.0}]
    models2 = build_read_model_from_manifest(
        _manifest(), _sku_policy(), rows2, tenant_id="t1"
    )
    sku2 = models2[0]
    assert sku2.impressions.safe_value == 300.0
    assert sku2.orders.safe_value == 5.0
    assert sku2.clicks.evidence_state is EvidenceState.MISSING


# ── 8. 工厂接真实 import_id ────────────────────────────────────────────
def test_factory_accepts_real_import_id() -> None:
    """传入 record_import 返回的真实 import_id 后，溯源到该 id。"""
    rows = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
             "sellable_stock": 50.0, "in_transit_stock": 20.0}]
    models = build_read_model_from_manifest(
        _manifest(), _sku_policy(), rows, tenant_id="t1", import_id="import-real-uuid"
    )
    assert models[0].net_sales.import_manifest_id == "import-real-uuid"  # type: ignore[union-attr]
    assert models[0].net_sales.data_as_of == DATA_AS_OF  # type: ignore[union-attr]


# ── 9. 权威服务 best-effort + 料号引用占位 ──────────────────────────────
def test_metric_authoritative_service_and_material_code() -> None:
    """authoritative_service 为来源系统 best-effort；material_code 占位 None。"""
    rows = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
             "sellable_stock": 50.0, "in_transit_stock": 20.0}]
    models = build_read_model_from_manifest(
        _manifest(), _sku_policy(), rows, tenant_id="t1"
    )
    sku = models[0]
    assert sku.net_sales.authoritative_service == "taobao"  # type: ignore[union-attr]
    assert sku.material_code is None  # 待 M7-R WP3 交付
    assert sku.impressions.authoritative_service is None  # MISSING 不携带权威服务


# ── 10. MISSING 不能携带导入引用 ────────────────────────────────────────
def test_missing_metric_cannot_reference_import() -> None:
    """缺证据引用构造即抛；无 reason 也拒绝。"""
    with pytest.raises(ValidationError, match="missing_evidence_cannot_reference_import"):
        MetricValue.from_value(
            state=EvidenceState.MISSING, granularity=Granularity.DAILY,
            aggregate_rule=AggregateRule.SUM, period_key="2026-08-17", value=None,
            import_manifest_id="import-x", data_as_of=DATA_AS_OF, reason="r",
        )
    with pytest.raises(ValidationError, match="missing_evidence_requires_reason"):
        MetricValue.missing(
            Granularity.DAILY, AggregateRule.SUM, "2026-08-17", reason=""
        )


# ── 11. Item 层拒绝店铺级字段 ──────────────────────────────────────────
def test_item_model_rejects_store_level_metrics() -> None:
    """Item 层同样物理拒绝店铺级字段（铁律 3 延伸到 item）。"""
    with pytest.raises(ValidationError):
        ItemReadModel(
            tenant_id="t1", store_id="s1", item_id="i1",
            price=_metric(EvidenceState.ACTUAL, 99.0),
            item_impressions=_metric(EvidenceState.ACTUAL, 500.0),
            item_clicks=_metric(EvidenceState.ACTUAL, 30.0),
            store_impressions=_metric(EvidenceState.ACTUAL, 9999.0),
        )


# ── 12. Store 层接收店铺字段、拒绝 SKU 字段 ────────────────────────────
def test_store_model_rejects_sku_metrics() -> None:
    """Store 层接收店铺字段，但拒绝 SKU 级净销量字段（反向广播）。"""
    rows = [{"store_impressions": 1000.0, "store_visitors": 800.0,
             "store_clicks": 200.0, "store_add_to_cart": 30.0,
             "ad_spend": 500.0, "net_sales": 9000.0}]  # net_sales 越界
    with pytest.raises(ValidationError):
        build_read_model_from_manifest(
            _manifest(), _store_policy(), rows, tenant_id="t1"
        )
    good = build_read_model_from_manifest(
        _manifest(), _store_policy(),
        [{"store_impressions": 1000.0, "store_visitors": 800.0,
          "store_clicks": 200.0, "store_add_to_cart": 30.0, "ad_spend": 500.0}],
        tenant_id="t1",
    )
    assert isinstance(good[0], StoreReadModel)


# ── 13. SKU 层店级派生禁止 ────────────────────────────────────────────
def test_sku_traffic_cannot_be_derived_from_store() -> None:
    """SKU 流量字段物理存在但缺数据时是 MISSING，不是店铺推导值。"""
    sku = _sku(missing_flow=True)  # impressions/clicks 为 MISSING
    assert sku.impressions.evidence_state is EvidenceState.MISSING
    assert sku.impressions.reason == "sku_traffic_blocked"
    with pytest.raises(DataUnavailableError):
        sku.impressions.safe_value
