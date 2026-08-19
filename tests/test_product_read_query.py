"""M9-R WP1 权威读模型查询测试：ProductReadQuery 从真实事实表投影。

覆盖：
- 投影 traffic_metric_buckets / inventory_balances / commerce_orders 成 SKUReadModel
- 缺数据 → MISSING（不编造，不广播）
- 接线冒烟：OperationsService.product_read
"""
from __future__ import annotations

from datetime import UTC, datetime

from ecommerce_agent.business.service import OperationsService
from ecommerce_agent.database import Database
from ecommerce_agent.product_read_model.models import DataTrust, EvidenceState
from ecommerce_agent.product_read_model.query import ProductReadQuery


def _seed(db: Database) -> None:
    """种真实事实表：asset + revision + metric bucket + inventory + order。"""
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
                "rev-1", "tenant-a", "virtual_taobao", "store-a", "item-a", "sku-a", 1,
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
                "virtual_taobao",
            ),
        )
        conn.execute(
            """
            INSERT INTO inventory_balances(
                id, tenant_id, connector_id, store_id, warehouse_id, sku_id,
                on_hand, reserved, inbound, average_daily_sales, source_id,
                source_updated_at, payload_hash, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "inv-1", "tenant-a", "virtual_taobao", "store-a", "wh-1", "sku-a",
                "50", "0", "10", "2", "src-inv",
                "2026-08-10T00:00:00+00:00", "c" * 64, 1,
                "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO commerce_orders(
                id, tenant_id, connector_id, store_id, external_order_id, order_status,
                payment_status, currency, total_amount, placed_at, buyer_ref_hash,
                source_id, source_updated_at, payload_hash, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ord-1", "tenant-a", "virtual_taobao", "store-a", "ext-1", "paid",
                "paid", "CNY", "109.00", "2026-08-10T12:00:00+00:00", None,
                "src-ord", "2026-08-10T12:00:00+00:00", "d" * 64, 1,
                "2026-08-10T12:00:00+00:00", "2026-08-10T12:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO commerce_order_lines(
                id, order_id, external_line_id, sku_id, title, quantity, unit_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("line-1", "ord-1", "ext-line-1", "sku-a", "测试商品", 1, "109.00"),
        )


def test_query_projects_real_traffic_and_inventory(tmp_path) -> None:
    """从真实表投影 SKU 读模型：流量/库存有值，来源可追溯。"""
    db = Database(tmp_path / "query.sqlite3")
    db.initialize()
    _seed(db)
    query = ProductReadQuery(db)
    model = query.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a"
    )
    assert model.impressions.value == 1000.0
    assert model.clicks.value == 80.0
    assert model.orders.value == 2.0
    assert model.sellable_stock.value == 50.0
    assert model.in_transit_stock.value == 10.0
    assert model.composite_key() == ("tenant-a", "store-a", "item-a", "sku-a", 1)


def test_query_missing_data_is_missing(tmp_path) -> None:
    """无任何来源 → 读模型全 MISSING（不编造，不广播）。"""
    db = Database(tmp_path / "query-missing.sqlite3")
    db.initialize()
    query = ProductReadQuery(db)
    model = query.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="no-such-sku"
    )
    assert model.impressions.evidence_state is EvidenceState.MISSING
    assert model.sellable_stock.evidence_state is EvidenceState.MISSING
    assert model.impressions.data_trust is DataTrust.MISSING


def test_query_missing_ids_rejected(tmp_path) -> None:
    """缺查询参数 → 抛（不静默返回空）。"""
    db = Database(tmp_path / "query-ids.sqlite3")
    db.initialize()
    query = ProductReadQuery(db)
    try:
        query.sku_read_model("tenant-a", store_id="", item_id="", sku_id="")
        assert False, "should raise"
    except ValueError:
        pass


def test_operations_wires_product_read(tmp_path) -> None:
    """接线冒烟：OperationsService.product_read 可用。"""
    db = Database(tmp_path / "query-wire.sqlite3")
    db.initialize()
    ops = OperationsService(db)
    model = ops.product_read.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a"
    )
    assert model.composite_key() == ("tenant-a", "store-a", "item-a", "sku-a", 1)
