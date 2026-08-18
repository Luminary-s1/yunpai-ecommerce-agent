"""M9-R WP1 边界反证测试：B5（SKU 流量不推导）与 B6（缺失绝不按 0）。

每个测试都是「破坏性」断言：如果实现违反边界，测试必须 FAIL。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ecommerce_agent.product_read_model.errors import DataUnavailableError
from ecommerce_agent.product_read_model.factory import build_read_model_from_manifest
from ecommerce_agent.product_read_model.models import (
    AggregateRule,
    Granularity,
    MetricValue,
    SKUReadModel,
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

_METRIC_FIELDS = frozenset({
    "store_impressions", "store_visitors", "store_clicks", "store_add_to_cart",
    "ad_spend", "price", "item_impressions", "item_clicks",
    "impressions", "clicks", "add_to_cart", "orders", "payments", "refunds",
    "net_sales", "sellable_stock", "in_transit_stock",
})


def _sku_policy() -> ReportFieldPolicy:
    return ReportFieldPolicy(
        report_type="store_traffic", mapping_version="v1",
        allowed_fields=_METRIC_FIELDS | frozenset({"item_id", "sku_id"}),
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


def test_missing_field_returns_error_not_zero() -> None:
    """B6 反例：缺失字段 safe_value 必须抛错，不能静默返回 0。"""
    rows = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
             "sellable_stock": 50.0, "in_transit_stock": 20.0}]
    models = build_read_model_from_manifest(
        _manifest(), _sku_policy(), rows, tenant_id="t1"
    )
    sku = models[0]
    assert isinstance(sku, SKUReadModel)
    # impressions 未在行中 → MISSING → 必须抛错，绝不返回 0.0
    with pytest.raises(DataUnavailableError):
        sku.impressions.safe_value


def test_sku_traffic_blocked_without_real_data() -> None:
    """B5 反例：真实模式缺 SKU 级流量，必须 MISSING + 原因，不能假装是实际值。"""
    rows = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
             "sellable_stock": 50.0, "in_transit_stock": 20.0}]
    models = build_read_model_from_manifest(
        _manifest(), _sku_policy(), rows, tenant_id="t1"
    )
    sku = models[0]
    assert sku.impressions.evidence_state is EvidenceState.MISSING
    assert sku.impressions.reason == "field_not_in_row"
    # 绝不能把店铺级值推导进来——物理上没有该字段的赋值路径
    assert sku.impressions.value is None
