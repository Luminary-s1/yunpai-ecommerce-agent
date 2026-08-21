"""M9-R WP1 重叠窗口跨 item 隔离反例（P1 修复验收，WP5 反例③）。

背景：第三轮复验反例③发现——同店、同 SKU、不同 item，两个 revision 时间窗
完全重叠时，只给 item-a 写库存/订单，查询 item-b 仍读到 item-a 的
sellable_stock=50、payments=1。根因：inventory_balances / commerce_orders
数据模型无 item 维度，聚合只能按 SKU + 时间窗过滤，重叠窗口天然串数。

修复（SKU 粒度 + item 展示语义）：
- inventory_balances / commerce_orders 新增可选 item_id 列（v36）。
- 链接专属数据写入时 tag item_id；SKU 级共享数据留 NULL（所有 item 可见）。
- 查询聚合 `AND (item_id = ? OR item_id IS NULL)`：
  * item-a 见 item-a tag 的行 + 所有共享行（NULL）
  * item-b 不见 item-a tag 的行（→ 链接专属数据不串数）

本文件反例：只给 item-a 写链接专属库存/订单，item-b 查询必须 MISSING
（不得读到 item-a 的 50 / 1）；同时验证 SKU 共享数据（NULL）对两 item 可见。
"""
from __future__ import annotations

from pathlib import Path

from ecommerce_agent.database import Database
from ecommerce_agent.product_read_model.query import ProductReadQuery
from ecommerce_agent.readonly_data.contracts import EvidenceState


def _seed(db: Database) -> None:
    """种两个 item（a/b）的重叠 revision 窗口 + item-a 链接专属库存/订单。"""
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
        # item-a revision 1：窗口 08-01 ~ 08-30
        conn.execute(
            """
            INSERT INTO listing_revisions(
                id, tenant_id, connector_id, store_id, item_id, sku_id, revision_no,
                title, main_image_asset_id, sale_price, attributes_json, active_from,
                active_to, source_updated_at, payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rev-a", "tenant-a", "virtual_taobao", "store-a", "item-a", "sku-a", 1,
                "商品A", "asset-1", "109.00", '{"stock_status":"in_stock"}',
                "2026-08-01T00:00:00+00:00", "2026-08-30T00:00:00+00:00",
                "2026-08-10T00:00:00+00:00", "a" * 64,
                "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00",
            ),
        )
        # item-b revision 1：窗口与 item-a 完全重叠 08-01 ~ 08-30
        conn.execute(
            """
            INSERT INTO listing_revisions(
                id, tenant_id, connector_id, store_id, item_id, sku_id, revision_no,
                title, main_image_asset_id, sale_price, attributes_json, active_from,
                active_to, source_updated_at, payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rev-b", "tenant-a", "virtual_taobao", "store-a", "item-b", "sku-a", 1,
                "商品B", "asset-1", "99.00", '{"stock_status":"in_stock"}',
                "2026-08-01T00:00:00+00:00", "2026-08-30T00:00:00+00:00",
                "2026-08-10T00:00:00+00:00", "b" * 64,
                "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00",
            ),
        )
        # item-a 链接专属库存（item_id="item-a"）：on_hand=50
        conn.execute(
            """
            INSERT INTO inventory_balances(
                id, tenant_id, connector_id, store_id, warehouse_id, sku_id, item_id,
                on_hand, reserved, inbound, average_daily_sales, source_id,
                source_updated_at, payload_hash, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "inv-a", "tenant-a", "virtual_taobao", "store-a", "wh-1", "sku-a",
                "item-a", "50", "0", "10", "2", "src-inv-a",
                "2026-08-10T00:00:00+00:00", "c" * 64, 1,
                "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00",
            ),
        )
        # item-a 链接专属订单（item_id="item-a"）：payments=1
        conn.execute(
            """
            INSERT INTO commerce_orders(
                id, tenant_id, connector_id, store_id, external_order_id, item_id,
                order_status, payment_status, currency, total_amount, placed_at,
                buyer_ref_hash, source_id, source_updated_at, payload_hash, version,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ord-a", "tenant-a", "virtual_taobao", "store-a", "ext-1", "item-a",
                "paid", "paid", "CNY", "109.00", "2026-08-10T12:00:00+00:00", None,
                "src-ord-a", "2026-08-10T12:00:00+00:00", "d" * 64, 1,
                "2026-08-10T12:00:00+00:00", "2026-08-10T12:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO commerce_order_lines(
                id, order_id, external_line_id, sku_id, title, quantity, unit_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("line-1", "ord-a", "ext-line-1", "sku-a", "商品A", 1, "109.00"),
        )


def test_overlap_window_item_b_does_not_see_item_a_inventory(tmp_path) -> None:
    """反例③：重叠窗口下 item-b 不得读到 item-a 链接专属库存。"""
    db = Database(tmp_path / "overlap-item-b-inv.sqlite3")
    db.initialize()
    _seed(db)
    query = ProductReadQuery(db)
    model = query.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-b", sku_id="sku-a"
    )
    # item-b 必须 MISSING（item-a 的 item_id 行不匹配 item-b 查询）
    assert model.sellable_stock.evidence_state is EvidenceState.MISSING, (
        f"item-b 串到 item-a 的库存: {model.sellable_stock.value}"
    )
    assert model.in_transit_stock.evidence_state is EvidenceState.MISSING


def test_overlap_window_item_b_does_not_see_item_a_orders(tmp_path) -> None:
    """反例③：重叠窗口下 item-b 不得读到 item-a 链接专属订单。"""
    db = Database(tmp_path / "overlap-item-b-orders.sqlite3")
    db.initialize()
    _seed(db)
    query = ProductReadQuery(db)
    model = query.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-b", sku_id="sku-a"
    )
    assert model.payments.evidence_state is EvidenceState.MISSING, (
        f"item-b 串到 item-a 的订单: {model.payments.value}"
    )


def test_overlap_window_item_a_sees_own_linked_data(tmp_path) -> None:
    """控制组：item-a 仍能看到自己的链接专属库存/订单（过滤不误伤）。"""
    db = Database(tmp_path / "overlap-item-a.sqlite3")
    db.initialize()
    _seed(db)
    query = ProductReadQuery(db)
    model = query.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a"
    )
    assert model.sellable_stock.value == 50.0
    assert model.in_transit_stock.value == 10.0
    assert model.payments.value == 1.0


def test_sku_shared_data_visible_to_both_items(tmp_path) -> None:
    """SKU 共享数据（NULL item_id）对两个 item 都可见（SKU 粒度语义）。

    注：inventory_balances 唯一键 (tenant, connector, store, warehouse, sku)
    不含 item_id，同 SKU 只能有一条库存行（共享 OR 专属，不能并存）——
    这是"加列不重建表"方案的既有约束。因此本测试用独立 seed：只写
    NULL item_id 的共享库存，验证 item-a / item-b 都看到该共享值。
    """
    db = Database(tmp_path / "overlap-shared.sqlite3")
    db.initialize()
    _seed(db)  # 先种两 item 的重叠窗口 + item-a 专属数据（供 revision 定位）
    with db.connect() as conn:
        # 覆盖为共享库存（item_id=NULL）：唯一键同 key 下 UPDATE 而非再 INSERT
        conn.execute(
            """
            UPDATE inventory_balances SET item_id=NULL, on_hand='7',
                reserved='0', inbound='0', source_updated_at='2026-08-10T12:00:00+00:00'
            WHERE id='inv-a'
            """
        )
    query = ProductReadQuery(db)
    # item-a 与 item-b 都看到共享库存 7（SKU 粒度共享，不因 item 不同而异）
    model_a = query.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a"
    )
    assert model_a.sellable_stock.value == 7.0, (
        f"item-a 应见共享库存: {model_a.sellable_stock.value}"
    )
    model_b = query.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-b", sku_id="sku-a"
    )
    assert model_b.sellable_stock.value == 7.0, (
        f"item-b 应见共享库存: {model_b.sellable_stock.value}"
    )
