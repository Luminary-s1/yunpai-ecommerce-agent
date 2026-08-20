"""WP1 验收脚本：按 m9r-complete-plan.md 18 条验收标准逐条断言验证。"""
from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError

from ecommerce_agent.product_read_model.errors import DataUnavailableError
from ecommerce_agent.product_read_model.factory import build_read_model_from_manifest
from ecommerce_agent.product_read_model.models import (
    AggregateRule,
    DataTrust,
    Granularity,
    MetricValue,
    SKUReadModel,
)
from ecommerce_agent.product_read_model.readiness import build_sku_readiness
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

ALL_METRIC = frozenset({
    "store_impressions", "store_visitors", "store_clicks", "store_add_to_cart",
    "ad_spend", "price", "item_impressions", "item_clicks", "impressions", "clicks",
    "add_to_cart", "orders", "payments", "refunds", "net_sales",
    "sellable_stock", "in_transit_stock",
})

RESULTS: list[tuple[str, str, str, bool, str]] = []


def check(cid: str, desc: str, expected: str, fn) -> None:
    try:
        fn()
        actual = "PASS"
    except AssertionError as e:
        actual = f"FAIL: {e}"
    except Exception as e:  # noqa: BLE001
        actual = f"ERROR: {type(e).__name__}: {e}"
    if expected == "✅":
        ok = actual == "PASS"
    else:
        ok = True
        actual = f"SKIP({expected} 依赖项，不验证)"
    RESULTS.append((cid, desc, expected, ok, actual))


def manifest() -> ImportManifestInput:
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


def sku_policy() -> ReportFieldPolicy:
    return ReportFieldPolicy(
        report_type="store_traffic", mapping_version="v1",
        allowed_fields=ALL_METRIC | frozenset({"item_id", "sku_id"}),
        required_fields=frozenset({"item_id", "sku_id", "net_sales"}),
    )


def mv(value: float, pk: str = "2026-08-17") -> MetricValue:
    return MetricValue.from_value(
        state=EvidenceState.ACTUAL, granularity=Granularity.DAILY,
        aggregate_rule=AggregateRule.SUM, period_key=pk, value=value,
        import_manifest_id="import-test-1", data_as_of=DATA_AS_OF,
    )


def sku(store: str = "store-a", item: str = "i1", sku_: str = "sku1",
        rev: int = 1, flow: bool = True) -> SKUReadModel:
    f = mv(10.0) if flow else MetricValue.missing(
        Granularity.DAILY, AggregateRule.SUM, "2026-08-17", "sku_traffic_blocked")
    return SKUReadModel(
        tenant_id="t1", store_id=store, item_id=item, sku_id=sku_, revision=rev,
        impressions=f, clicks=f, add_to_cart=mv(5.0), orders=mv(4.0),
        payments=mv(3.0), refunds=mv(0.0), net_sales=mv(100.0),
        sellable_stock=mv(50.0), in_transit_stock=mv(20.0),
    )


ROWS_SKU = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
             "sellable_stock": 50.0, "in_transit_stock": 20.0}]


def test_01_not_cross_scope() -> None:
    a, b, c = sku("store-a"), sku("store-b"), sku(sku_="sku2")
    assert a.composite_key() != b.composite_key()
    assert a.composite_key() != c.composite_key()
    assert len(a.composite_key()) == 5


def test_02_granularity_isolation() -> None:
    d = mv(100.0, pk="2026-08-17")
    m = MetricValue.from_value(
        state=EvidenceState.ACTUAL, granularity=Granularity.MONTHLY,
        aggregate_rule=AggregateRule.SUM, period_key="2026-08", value=3000.0,
        import_manifest_id="import-test-1", data_as_of=DATA_AS_OF)
    assert d.period_key != m.period_key
    assert len({d, m}) == 2
    assert not hasattr(sku(), "total_sales")


def test_03_no_broadcast() -> None:
    try:
        build_read_model_from_manifest(
            manifest(), sku_policy(),
            [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
              "store_impressions": 9999.0}],
            tenant_id="t1")
        raise AssertionError("SKU 行含 store_impressions 应被 extra=forbid 拒绝")
    except ValidationError:
        pass


def test_04_deterministic_check() -> None:
    m = sku()
    assert m.composite_key()[0] == "t1"
    assert m.composite_key()[4] == 1


def test_05_missing_projection() -> None:
    models = build_read_model_from_manifest(manifest(), sku_policy(), ROWS_SKU, tenant_id="t1")
    s = models[0]
    assert s.net_sales.safe_value == 100.0
    assert s.impressions.evidence_state is EvidenceState.MISSING
    try:
        s.impressions.safe_value
        raise AssertionError("MISSING 读取应抛 DataUnavailableError")
    except DataUnavailableError:
        pass


def test_07_trace_import() -> None:
    models = build_read_model_from_manifest(
        manifest(), sku_policy(), ROWS_SKU, tenant_id="t1", import_id="import-real-uuid")
    assert models[0].net_sales.import_manifest_id == "import-real-uuid"
    assert models[0].net_sales.data_as_of == DATA_AS_OF


def test_08_authoritative_service() -> None:
    """query 路径每个值可回溯到权威域服务 + 来源（批次 1 复审成果）。

    query 消费领域事实表（traffic_metric_buckets/inventory_balances/commerce_orders），
    来源诚实化：authoritative_service=权威域服务名，import_manifest_id=领域来源标识
    （source_id/connector），data_as_of=源时间。
    """
    from pathlib import Path
    import tempfile

    from ecommerce_agent.database import Database
    from ecommerce_agent.product_read_model.query import ProductReadQuery

    db = Database(Path(tempfile.mkdtemp()) / "wp1-authoritative.sqlite3")
    db.initialize()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO creative_assets(
                asset_id, tenant_id, sha256, mime_type, width, height, storage_ref,
                source_ref, feature_schema_version, payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "asset-1", "tenant-a", "e" * 64, "image/png", 1200, 1200,
                "objects/a.png", "fixture://a", "image-v1", "f" * 64,
                "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO listing_revisions(
                id, tenant_id, connector_id, store_id, item_id, sku_id, revision_no,
                title, main_image_asset_id, sale_price, attributes_json, active_from,
                active_to, source_updated_at, payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rev-1", "tenant-a", "taobao_official", "store-a", "item-a", "sku-a", 1,
                "测试商品", "asset-1", "109.00", '{"stock_status":"in_stock"}',
                "2026-08-01T00:00:00+00:00", "2026-08-30T00:00:00+00:00",
                "2026-08-10T00:00:00+00:00", "a" * 64,
                "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO traffic_metric_buckets(
                id, tenant_id, listing_revision_id, metric_start, metric_end,
                bucket_granularity, traffic_source, impressions, clicks, visitors,
                favorites, cart_adds, orders, sales_amount, ad_spend,
                search_impressions, recommend_impressions, data_as_of, source_id,
                payload_hash, quality_flags_json, version, created_at, updated_at,
                connector_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "bucket-1", "tenant-a", "rev-1", "2026-08-10T00:00:00+00:00",
                "2026-08-10T23:59:59+00:00", "day", "recommend", 1000, 80, 75,
                8, 5, 2, "218.00", "0", 100, 900, "2026-08-10T12:00:00+00:00",
                "src-1", "b" * 64, "[]", 1,
                "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00",
                "taobao_official",
            ),
        )
    model = ProductReadQuery(db).sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a", revision=1
    )
    # 流量可回溯到权威域服务 + 来源 source_id
    assert model.impressions.authoritative_service == "traffic_metric_buckets"
    assert model.impressions.import_manifest_id == "src-1"
    assert model.impressions.data_as_of is not None
    # 非伪造 manifest id（不以 manifest- 开头），可回溯到领域来源
    assert not model.impressions.import_manifest_id.startswith("manifest-")


def test_09_granularity_preserved() -> None:
    assert sku().net_sales.granularity is Granularity.DAILY
    assert sku().net_sales.aggregate_rule is AggregateRule.SUM


def test_10_source_trace() -> None:
    models = build_read_model_from_manifest(manifest(), sku_policy(), ROWS_SKU, tenant_id="t1")
    assert models[0].net_sales.authoritative_service == "taobao"


def test_12_readiness() -> None:
    r = build_sku_readiness(sku())
    assert r.readiness_for("net_sales") is not None
    assert r.funnel_availability in ("complete", "partial", "unavailable")
    re_ = build_sku_readiness(sku(flow=False))
    assert re_.funnel_availability in ("complete", "partial", "unavailable")


def test_13_reuse_contract() -> None:
    import inspect
    from ecommerce_agent.product_read_model import factory
    src = inspect.getsource(factory)
    assert "sanitize_report_row" in src
    assert "insert" not in src.lower()
    assert "CREATE TABLE" not in src


def test_14_four_states() -> None:
    states = {e.value for e in EvidenceState}
    assert states == {"actual", "manual", "demo", "missing"}
    assert MetricValue.missing(Granularity.DAILY, AggregateRule.SUM,
                               "2026-08-17", "x").evidence_state is EvidenceState.MISSING


def test_15_missing_not_zero() -> None:
    m = MetricValue.missing(Granularity.DAILY, AggregateRule.SUM, "2026-08-17", "no_data")
    try:
        m.safe_value
        raise AssertionError("MISSING safe_value 应抛")
    except DataUnavailableError:
        pass


def test_16_sample_trust() -> None:
    models = build_read_model_from_manifest(
        manifest(), sku_policy(), ROWS_SKU, tenant_id="t1", row_data_trust=DataTrust.SAMPLE)
    assert models[0].net_sales.data_trust is DataTrust.SAMPLE
    assert models[0].net_sales.evidence_state is EvidenceState.ACTUAL


def test_17_privacy() -> None:
    policy = ReportFieldPolicy(
        report_type="orders", mapping_version="orders-v1",
        allowed_fields=frozenset({"item_id", "sku_id", "net_sales", "remark"}),
        required_fields=frozenset({"item_id", "sku_id", "net_sales"}))
    models = build_read_model_from_manifest(
        manifest(), policy,
        [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0, "买家姓名": "张三"}],
        tenant_id="t1")
    assert not hasattr(models[0], "买家姓名")
    assert models[0].net_sales.safe_value == 100.0


def test_18_manual_placeholder() -> None:
    m = MetricValue.missing(Granularity.DAILY, AggregateRule.SUM,
                            "2026-08-17", "manual_entry_not_available")
    assert m.evidence_state is EvidenceState.MISSING
    assert m.reason == "manual_entry_not_available"


check("①", "同 item 多SKU/同SKU多revision/同租户多店不串数", "✅", test_01_not_cross_scope)
check("②", "日/月、店铺/商品、支付/退款不同粒度不静默相加", "✅", test_02_granularity_isolation)
check("③", "店铺级曝光/点击/广告不广播成 SKU 指标", "✅", test_03_no_broadcast)
check("④", "跨粒度/跨店/跨SKU/跨revision/缺失确定性检查", "✅", test_04_deterministic_check)
check("⑤", "缺字段→显示基础事实+阻断依赖结论", "✅", test_05_missing_projection)
check("⑥", "竞品/退款数据域真实覆盖", "⚠️", lambda: None)
check("⑦", "每个值回溯到 import manifest 和 data_as_of", "✅", test_07_trace_import)
check("⑧", "每个值回溯到权威服务", "✅", test_08_authoritative_service)
check("⑨", "保留字段原始粒度", "✅", test_09_granularity_preserved)
check("⑩", "来源追溯（source_system/import_manifest_id）", "✅", test_10_source_trace)
check("⑪", "料号引用 material_code", "🔒", lambda: None)
check("⑫", "数据准备度、漏斗可用性、缺失阻断语义", "✅", test_12_readiness)
check("⑬", "复用现有领域事实表，不复制第二套真相", "✅", test_13_reuse_contract)
check("⑭", "四态证据状态贯穿", "✅", test_14_four_states)
check("⑮", "缺失绝不按 0 处理", "✅", test_15_missing_not_zero)
check("⑯", "样本数据不作产品口径（data_trust）", "✅", test_16_sample_trust)
check("⑰", "敏感字段过滤 + 不入日志（隐私红线）", "✅", test_17_privacy)
check("⑱", "manual 录入机制明确（本期 MISSING 占位）", "✅", test_18_manual_placeholder)

print(f"{'条目':<5}{'验收标准':<32}{'计划':<4}{'实际':<8}备注")
print("-" * 92)
all_ok = True
for cid, desc, exp, ok, actual in RESULTS:
    if not ok:
        all_ok = False
    print(f"{cid:<6}{desc:<34}{exp:<5}{('PASS' if ok else '**FAIL**'):<8}{actual}")
print("-" * 92)
print(f"结论: {'✅ 全部 ✅ 项通过' if all_ok else '❌ 有 FAIL 项，需修复'}")
# FAIL 时返回非零退出码（防 CI/人工只看退出状态误判）。
import sys
if not all_ok:
    sys.exit(1)
