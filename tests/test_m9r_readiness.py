"""M9-R WP1 数据准备度测试（funnel_availability 派生视图，非第二套枚举）。"""
from __future__ import annotations

from datetime import UTC, datetime

from ecommerce_agent.product_read_model.models import (
    AggregateRule,
    Granularity,
    MetricValue,
    SKUReadModel,
)
from ecommerce_agent.product_read_model.readiness import (
    FUNNEL_COMPLETE,
    FUNNEL_PARTIAL,
    FUNNEL_UNAVAILABLE,
    build_sku_readiness,
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
    """漏斗字段全部可用 → complete（refunds 为 missing 时 → partial，见下）。"""
    flow = _mv(10.0)
    readiness = build_sku_readiness(_sku(flow, _mv(100.0)))
    # refunds 恒为 missing（_sku 夹具），所以是 partial 而非 complete
    assert readiness.funnel_availability == FUNNEL_PARTIAL
    assert readiness.composite_key == ("t1", "s1", "i1", "sku1", 1)
    assert readiness.readiness_for("net_sales") is not None
    assert readiness.readiness_for("refunds").reason == "field_not_in_row"  # type: ignore[union-attr]


def test_readiness_complete_all_funnel_fields_available() -> None:
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
    """漏斗字段全部缺失 → unavailable。"""
    flow = _missing()
    sales = _missing()
    readiness = build_sku_readiness(_sku(flow, sales))
    assert readiness.funnel_availability == FUNNEL_UNAVAILABLE
