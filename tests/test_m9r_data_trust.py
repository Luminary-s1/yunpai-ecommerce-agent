"""M9-R WP1 data_trust 测试：样本 vs 生产口径（B7 载体）+ 非法组合校验。

每个测试确定性：不依赖时间/随机/外部状态。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ecommerce_agent.product_read_model.factory import build_read_model_from_manifest
from ecommerce_agent.product_read_model.models import (
    AggregateRule,
    DataTrust,
    Granularity,
    MetricValue,
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


def test_data_trust_defaults_to_production() -> None:
    """默认（未显式传 row_data_trust）→ ACTUAL 状态推导 production。"""
    rows = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
             "sellable_stock": 50.0, "in_transit_stock": 20.0}]
    models = build_read_model_from_manifest(
        _manifest(), _sku_policy(), rows, tenant_id="t1"
    )
    sku = models[0]
    assert sku.net_sales.data_trust is DataTrust.PRODUCTION  # type: ignore[union-attr]


def test_data_trust_sample_marks_whole_row() -> None:
    """显式 row_data_trust=SAMPLE → 整行指标 data_trust=sample（B7 载体）。"""
    rows = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
             "sellable_stock": 50.0, "in_transit_stock": 20.0}]
    models = build_read_model_from_manifest(
        _manifest(), _sku_policy(), rows, tenant_id="t1",
        row_data_trust=DataTrust.SAMPLE,
    )
    sku = models[0]
    assert sku.net_sales.data_trust is DataTrust.SAMPLE  # type: ignore[union-attr]
    assert sku.net_sales.evidence_state is EvidenceState.ACTUAL  # type: ignore[union-attr]


def test_sample_trust_rejected_for_missing() -> None:
    """MISSING 字段必须 data_trust=missing；显式给 SAMPLE → 构造即抛。"""
    with pytest.raises(ValidationError, match="data_trust_evidence_state_mismatch"):
        MetricValue.from_value(
            state=EvidenceState.MISSING, granularity=Granularity.DAILY,
            aggregate_rule=AggregateRule.SUM, period_key="2026-08-17", value=None,
            data_trust=DataTrust.SAMPLE, reason="x",
        )


def test_production_trust_rejected_for_demo() -> None:
    """DEMO 状态显式给 production → 构造即抛（demo 绝不冒充生产口径）。"""
    with pytest.raises(ValidationError, match="data_trust_evidence_state_mismatch"):
        MetricValue.from_value(
            state=EvidenceState.DEMO, granularity=Granularity.DAILY,
            aggregate_rule=AggregateRule.SUM, period_key="2026-08-17", value=10.0,
            import_manifest_id="import-1", data_as_of=DATA_AS_OF,
            data_trust=DataTrust.PRODUCTION,
        )


def test_period_key_format_enforced() -> None:
    """period_key 必须匹配粒度格式（日=YYYY-MM-DD，月=YYYY-MM）。"""
    with pytest.raises(ValidationError, match="period_key_format_invalid"):
        MetricValue.from_value(
            state=EvidenceState.ACTUAL, granularity=Granularity.DAILY,
            aggregate_rule=AggregateRule.SUM, period_key="2026-08", value=10.0,
            import_manifest_id="import-1", data_as_of=DATA_AS_OF,
        )


def test_non_finite_value_rejected() -> None:
    """非有限浮点（NaN/Inf）构造即抛——防静默传播（自审补充）。"""
    for bad_value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError, match="non_finite_metric_value"):
            MetricValue.from_value(
                state=EvidenceState.ACTUAL, granularity=Granularity.DAILY,
                aggregate_rule=AggregateRule.SUM, period_key="2026-08-17",
                value=bad_value,
                import_manifest_id="import-1", data_as_of=DATA_AS_OF,
            )
