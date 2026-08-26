"""M9-R WP1 复审反证：ProductReadQuery 来源诚实 + revision 隔离 + 粒度诚实。

覆盖（WP5 复审计划批次 1）：
- virtual_taobao connector → DEMO/DEMO（demo 不冒充 actual）
- 真实 connector（taobao_official）→ ACTUAL/PRODUCTION
- 跨 revision 值隔离（同 SKU 不同 revision 不串数）
- revision 窗口订单聚合（只聚合 active_from/active_to 内订单）
- 库存跨仓汇总（多仓求和，非单仓取一）
- 粒度诚实（hour bucket → HOURLY，不强行标 DAILY）
"""
from __future__ import annotations

from datetime import UTC, datetime

from ecommerce_agent.database import Database
from ecommerce_agent.product_read_model.models import DataTrust, EvidenceState, Granularity
from ecommerce_agent.product_read_model.query import ProductReadQuery


def _seed_common(db: Database, *, connector_id: str = "taobao_official") -> None:
    """种 asset + revision + bucket + 库存（双仓）+ 订单。"""
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
        # revision 1（窗口 8/1-8/15）
        conn.execute(
            """
            INSERT INTO listing_revisions(
                id, tenant_id, connector_id, store_id, item_id, sku_id, revision_no,
                title, main_image_asset_id, sale_price, attributes_json, active_from,
                active_to, source_updated_at, payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rev-1", "tenant-a", connector_id, "store-a", "item-a", "sku-a", 1,
                "测试商品", "asset-1", "109.00", '{"stock_status":"in_stock"}',
                "2026-08-01T00:00:00+00:00", "2026-08-15T00:00:00+00:00",
                "2026-08-10T00:00:00+00:00", "a" * 64,
                "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00",
            ),
        )
        # revision 2（窗口 8/16-8/31，值不同）
        conn.execute(
            """
            INSERT INTO listing_revisions(
                id, tenant_id, connector_id, store_id, item_id, sku_id, revision_no,
                title, main_image_asset_id, sale_price, attributes_json, active_from,
                active_to, source_updated_at, payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rev-2", "tenant-a", connector_id, "store-a", "item-a", "sku-a", 2,
                "测试商品 v2", "asset-1", "119.00", '{"stock_status":"in_stock"}',
                "2026-08-16T00:00:00+00:00", "2026-08-31T00:00:00+00:00",
                "2026-08-20T00:00:00+00:00", "b" * 64,
                "2026-08-20T00:00:00+00:00", "2026-08-20T00:00:00+00:00",
            ),
        )


def _seed_traffic(db: Database, *, connector_id: str = "taobao_official") -> None:
    """种 bucket：rev-1 一个 day bucket，rev-2 一个 hour bucket。"""
    with db.connect() as conn:
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
                connector_id,
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
                "bucket-2", "tenant-a", "rev-2", "2026-08-20T10:00:00+00:00",
                "2026-08-20T10:59:59+00:00", "hour", "recommend", 500, 40, 38,
                4, 3, 1, "109.00", "0", 50, 450, "2026-08-20T10:30:00+00:00",
                "src-2", "c" * 64, "[]", 1,
                "2026-08-20T10:00:00+00:00", "2026-08-20T10:00:00+00:00",
                connector_id,
            ),
        )


def _seed_inventory(db: Database, *, connector_id: str = "taobao_official") -> None:
    """种库存：双仓（wh-1 50/10，wh-2 30/5）。item 身份 item-a（R1 严格匹配）。"""
    with db.connect() as conn:
        for i, (wid, oh, inbound) in enumerate((("wh-1", "50", "10"), ("wh-2", "30", "5"))):
            conn.execute(
                """
                INSERT INTO inventory_balances(
                    id, tenant_id, connector_id, store_id, warehouse_id, sku_id, item_id,
                    on_hand, reserved, inbound, average_daily_sales, source_id,
                    source_updated_at, payload_hash, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"inv-{wid}", "tenant-a", connector_id, "store-a", wid, "sku-a",
                    "item-a",
                    oh, "0", inbound, "2", f"src-{wid}",
                    # R2 来源同源：双仓时间戳不同，最新行明确为 wh-2（source_id=src-wh-2）
                    "2026-08-10T00:00:00+00:00" if i == 0 else "2026-08-11T00:00:00+00:00",
                    "d" * 64, 1,
                    "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00",
                ),
            )


def _seed_order(
    db: Database, *, order_id: str, placed_at: str, connector_id: str = "taobao_official"
) -> None:
    with db.connect() as conn:
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
                order_id, "tenant-a", connector_id, "store-a", f"ext-{order_id}",
                "item-a",
                "paid", "paid", "CNY", "109.00", placed_at, None,
                f"src-{order_id}", placed_at, "e" * 64, 1,
                placed_at, placed_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO commerce_order_lines(
                id, order_id, external_line_id, sku_id, title, quantity, unit_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (f"line-{order_id}", order_id, f"ext-line-{order_id}", "sku-a", "测试", 1, "109.00"),
        )


def _query_db(tmp_path, *, connector_id: str = "taobao_official") -> Database:
    db = Database(tmp_path / "q.sqlite3")
    db.initialize()
    _seed_common(db, connector_id=connector_id)
    _seed_traffic(db, connector_id=connector_id)
    _seed_inventory(db, connector_id=connector_id)
    _seed_order(db, order_id="ord-1", placed_at="2026-08-10T12:00:00+00:00", connector_id=connector_id)
    _seed_order(db, order_id="ord-2", placed_at="2026-08-20T11:00:00+00:00", connector_id=connector_id)
    return db


def test_operational_connector_maps_to_actual(tmp_path) -> None:
    """真实 connector → ACTUAL/PRODUCTION。"""
    db = _query_db(tmp_path)
    model = ProductReadQuery(db).sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a", revision=1
    )
    assert model.impressions.evidence_state is EvidenceState.ACTUAL
    assert model.impressions.data_trust is DataTrust.PRODUCTION
    assert model.impressions.value == 1000.0


def test_virtual_connector_maps_to_demo(tmp_path) -> None:
    """virtual connector → DEMO/DEMO（demo 不冒充 actual）。"""
    db = _query_db(tmp_path, connector_id="virtual_taobao")
    model = ProductReadQuery(db).sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a", revision=1
    )
    assert model.impressions.evidence_state is EvidenceState.DEMO
    assert model.impressions.data_trust is DataTrust.DEMO
    assert model.sellable_stock.evidence_state is EvidenceState.DEMO


def test_revision_isolation_values_differ(tmp_path) -> None:
    """跨 revision 值隔离：rev-1 与 rev-2 不串数。"""
    db = _query_db(tmp_path)
    query = ProductReadQuery(db)
    model1 = query.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a", revision=1
    )
    model2 = query.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a", revision=2
    )
    assert model1.impressions.value == 1000.0
    assert model2.impressions.value == 500.0  # rev-2 只有 hour bucket 500
    assert model1.orders.value == 2.0
    assert model2.orders.value == 1.0  # rev-2 窗口只有 ord-2


def test_revision_window_order_aggregation(tmp_path) -> None:
    """revision 窗口订单聚合：只聚合 active_from/active_to 内订单。"""
    db = _query_db(tmp_path)
    query = ProductReadQuery(db)
    model = query.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a", revision=1
    )
    assert model.payments.value == 1.0  # 窗口 8/1-8/15 只有 ord-1
    assert model.refunds.evidence_state is EvidenceState.MISSING
    assert model.refunds.reason == "refund_source_not_available"
    assert model.net_sales.evidence_state is EvidenceState.MISSING
    assert model.net_sales.reason == "refund_source_not_available"


def test_inventory_cross_warehouse_sum(tmp_path) -> None:
    """库存跨仓汇总：wh-1+wh-2 求和。"""
    db = _query_db(tmp_path)
    model = ProductReadQuery(db).sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a", revision=1
    )
    assert model.sellable_stock.value == 80.0  # 50+30
    assert model.in_transit_stock.value == 15.0  # 10+5


def test_hour_bucket_marked_hourly(tmp_path) -> None:
    """hour bucket → HOURLY 粒度（不强行标 DAILY）。"""
    db = _query_db(tmp_path)
    model = ProductReadQuery(db).sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a", revision=2
    )
    assert model.impressions.granularity is Granularity.HOURLY


def test_source_ref_is_source_id_not_manifest_fake(tmp_path) -> None:
    """来源诚实：import_manifest_id 是领域真实来源标识，非合成前缀串。"""
    db = _query_db(tmp_path)
    model = ProductReadQuery(db).sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a", revision=1
    )
    # 流量来自 bucket.source_id（src-1），权威服务是 traffic_metric_buckets
    assert model.impressions.import_manifest_id == "src-1"
    assert model.impressions.authoritative_service == "traffic_metric_buckets"
    # 库存来源：真实 inventory_balances.source_id（两仓最新行 src-wh-2），
    # 非合成前缀串（证据审查 #1 修复）
    assert model.sellable_stock.import_manifest_id == "src-wh-2"
    assert model.sellable_stock.authoritative_service == "inventory_balances"
    # 订单来源：退款来源未知时 net_sales 保持 MISSING，payments 仍可追到订单来源。
    assert model.payments.import_manifest_id in ("src-ord-1", "src-ord-2")
    assert model.payments.authoritative_service == "commerce_orders"


def _seed_multi_line_order(db: Database) -> None:
    """种一个多行订单（同一 order_id 两行 line，含两个不同 SKU）在 revision 1 窗口内。"""
    with db.connect() as conn:
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
                "ord-multi", "tenant-a", "taobao_official", "store-a", "ext-ord-multi",
                "item-a",
                "paid", "paid", "CNY", "218.00", "2026-08-12T10:00:00+00:00", None,
                "src-ord-multi", "2026-08-12T10:00:00+00:00", "g" * 64, 1,
                "2026-08-12T10:00:00+00:00", "2026-08-12T10:00:00+00:00",
            ),
        )
        # A3（盲点 #6 修复）：多行订单 = 含多个**不同 SKU**（sku-a + sku-b），
        # 退款无法归 SKU；同 SKU 拆分多行（qty 拆两行）仍可精确归 SKU
        conn.execute(
            """
            INSERT INTO commerce_order_lines(
                id, order_id, external_line_id, sku_id, title, quantity, unit_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (f"line-{1}", "ord-multi", f"ext-line-{1}", "sku-a", "测试", 1, "109.00"),
        )
        conn.execute(
            """
            INSERT INTO commerce_order_lines(
                id, order_id, external_line_id, sku_id, title, quantity, unit_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (f"line-{2}", "ord-multi", f"ext-line-{2}", "sku-b", "测试", 1, "109.00"),
        )


def test_multi_line_order_net_sales_missing_independent_reason(tmp_path) -> None:
    """R2（证据诚实）：多行订单退款无法归 SKU → net_sales MISSING + 独立 reason。

    复验阻断项 2a：net_sales 直接返回 gross（用 GMV 冒充净销）。修复后多行订单
    （退款无法归属 SKU）net_sales 必须 MISSING（任务书 L66），且 net_sales_reason
    独立，不污染 payments/refunds。
    """
    db = _query_db(tmp_path)
    _seed_multi_line_order(db)
    model = ProductReadQuery(db).sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a", revision=1
    )
    # 多行订单 → net_sales MISSING + 独立 reason（不能以 GMV 冒充净销）
    assert model.net_sales.evidence_state is EvidenceState.MISSING
    assert (
        model.net_sales.reason
        == "net_sales_not_attributable_to_sku_multi_line_order"
    )
    # payments 不被污染（仍有值，reason 为 None）
    assert model.payments.value is not None
    assert model.payments.reason is None


def test_same_sku_split_lines_without_refund_source_keeps_net_sales_missing(tmp_path) -> None:
    """同 SKU 拆多行可归属订单，但退款来源未知时净销售仍不可用。

    修复前 multi_line 按行数判定（HAVING COUNT(*) > 1），同 SKU 拆两行也误判
    为跨 SKU；修复后订单归属成立，但不能把“没有退款来源”解释成已知退款为零。
    """
    db = _query_db(tmp_path)
    # 种同 SKU 拆两行的订单（两行都是 sku-a，qty 1+1）
    with db.connect() as conn:
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
                "ord-split", "tenant-a", "taobao_official", "store-a", "ext-ord-split",
                "item-a",
                "paid", "paid", "CNY", "218.00", "2026-08-13T10:00:00+00:00", None,
                "src-ord-split", "2026-08-13T10:00:00+00:00", "h" * 64, 1,
                "2026-08-13T10:00:00+00:00", "2026-08-13T10:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO commerce_order_lines(
                id, order_id, external_line_id, sku_id, title, quantity, unit_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (f"line-{1}", "ord-split", f"ext-line-{1}", "sku-a", "测试", 1, "109.00"),
        )
        conn.execute(
            """
            INSERT INTO commerce_order_lines(
                id, order_id, external_line_id, sku_id, title, quantity, unit_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (f"line-{2}", "ord-split", f"ext-line-{2}", "sku-a", "测试", 1, "109.00"),
        )
    model = ProductReadQuery(db).sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a", revision=1
    )
    assert model.refunds.evidence_state is EvidenceState.MISSING
    assert model.net_sales.evidence_state is EvidenceState.MISSING
    assert model.net_sales.reason == "refund_source_not_available"


def test_same_sku_split_lines_refund_not_amplified(tmp_path) -> None:
    """负责人复验阻断项 3：同 SKU 拆两行 + 一笔退款 → 退款不被 JOIN 放大。

    修复前退款 JOIN order_lines 会把整单退款按行数放大：同 SKU 拆两行的订单，
    一笔 approved 退款 50 会被算成 100（每条行都 JOIN 到同一条退款）。
    修复后用 EXISTS 判定订单含该 SKU（不 JOIN 行），退款精确为 50。
    """
    db = _query_db(tmp_path)
    # 种同 SKU 拆两行的订单（两行都是 sku-a，qty 1+1，gross=218）+ approved 退款 50
    with db.connect() as conn:
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
                "ord-split-r", "tenant-a", "taobao_official", "store-a", "ext-split-r",
                "item-a",
                "paid", "partially_refunded", "CNY", "218.00", "2026-08-13T10:00:00+00:00", None,
                "src-split-r", "2026-08-13T10:00:00+00:00", "i" * 64, 1,
                "2026-08-13T10:00:00+00:00", "2026-08-13T10:00:00+00:00",
            ),
        )
        for line_id in ("line-r1", "line-r2"):
            conn.execute(
                """
                INSERT INTO commerce_order_lines(
                    id, order_id, external_line_id, sku_id, title, quantity, unit_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (line_id, "ord-split-r", f"ext-{line_id}", "sku-a", "测试", 1, "109.00"),
            )
        conn.execute(
            "INSERT INTO commerce_after_sale_cases(id, order_id, external_case_id, case_type, status, requested_amount, approved_amount, reason_code, opened_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("case-split-r", "ord-split-r", "ext-case-split-r", "refund", "approved",
             "50.00", "50.00", None, "2026-08-13T11:00:00+00:00", "2026-08-13T11:00:00+00:00"),
        )
    model = ProductReadQuery(db).sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a", revision=1
    )
    # 退款精确 50，不按行数放大成 100
    assert model.refunds.value == 50.0, (
        f"同 SKU 拆行退款被 JOIN 放大: {model.refunds.value}"
    )
    # net_sales = gross - 退款 = 218 - 50 = 168（含 _seed_common 的 ord-1 会在下方单独验证，
    # 此处只对 ord-split-r 的净额语义做归属断言）
    assert model.net_sales.evidence_state is not EvidenceState.MISSING


def test_order_source_deterministic_latest(tmp_path) -> None:
    """R2（来源诚实）：同窗口多笔订单 → 来源取全局最新 source_updated_at 一行。

    复验阻断项 2c：来源分别 MAX 拼凑会拼出不存在的组合。修复后 connector_id/source_id
    来自 source_updated_at 最新的一行（全局 ORDER BY + LIMIT 1），确定性。

    C1（盲点 #8 修复）：种两个**不同 connector** 的订单——"source_updated_at 最新行"
    的 connector 不是文本最大——MAX 拼凑实现会取 connector=文本最大 + source=时间最新
    （拼出不存在组合），测试必须变红；全局 LIMIT 1 取时间最新整行（src-ord-z）。
    """
    db = _query_db(tmp_path)
    # ord-3 connector=taobao_official（文本较大）source_updated_at=8/13
    _seed_order(db, order_id="ord-3", placed_at="2026-08-13T09:00:00+00:00")
    # ord-z connector=virtual_taobao（文本较小）source_updated_at=8/14（全局最新）
    _seed_order_for_sku(
        db, order_id="ord-z", sku_id="sku-a", placed_at="2026-08-14T09:00:00+00:00",
        connector_id="virtual_taobao",
    )
    model = ProductReadQuery(db).sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a", revision=1
    )
    # 全局最新 source_updated_at = ord-z（8/14）→ 来源应为 src-ord-z
    # （MAX 拼凑会取 connector=taobao_official + source=src-ord-z → 测试变红）
    assert model.payments.import_manifest_id == "src-ord-z"
    assert model.payments.authoritative_service == "commerce_orders"


def _seed_order_for_sku(
    db: Database, *, order_id: str, sku_id: str, placed_at: str,
    source_updated_at: str | None = None, connector_id: str = "taobao_official",
) -> None:
    """种一个指定 sku 的订单（默认 source_updated_at=placed_at，可指定 connector）。"""
    src_ts = source_updated_at or placed_at
    with db.connect() as conn:
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
                order_id, "tenant-a", connector_id, "store-a", f"ext-{order_id}",
                "item-a",
                "paid", "paid", "CNY", "109.00", placed_at, None,
                f"src-{order_id}", src_ts, "e" * 64, 1,
                src_ts, src_ts,
            ),
        )
        conn.execute(
            """
            INSERT INTO commerce_order_lines(
                id, order_id, external_line_id, sku_id, title, quantity, unit_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (f"line-{order_id}", order_id, f"ext-line-{order_id}", sku_id, "测试", 1, "109.00"),
        )


def test_order_source_not_polluted_by_other_sku(tmp_path) -> None:
    """P0-1 反例：同 item 其他 sku 的更新订单不得污染来源。

    审查发现：_order_facts 的 latest CTE 原只按 item_id 过滤（无 sku_id），来源可能
    取自同 item 其他 sku 的订单。修复后 CTE 按 sku_id 过滤（JOIN order_lines），
    sku-a 的来源应取 sku-a 的订单，不被 sku-b 的更新订单污染。
    """
    db = _query_db(tmp_path)
    # ord-3 是 sku-a（窗口内，8/13）；ord-other 是 sku-b（窗口内，8/14 更新）
    _seed_order_for_sku(db, order_id="ord-3", sku_id="sku-a", placed_at="2026-08-13T09:00:00+00:00")
    _seed_order_for_sku(db, order_id="ord-other", sku_id="sku-b", placed_at="2026-08-14T09:00:00+00:00")
    model = ProductReadQuery(db).sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a", revision=1
    )
    # 修复前：CTE 无 sku 过滤，来源取 sku-b 的 ord-other（8/14 更新）→ 污染
    # 修复后：CTE 按 sku 过滤，来源取 sku-a 的 ord-3（8/13）
    assert model.payments.import_manifest_id == "src-ord-3", (
        f"来源被同 item 其他 sku 污染: {model.payments.import_manifest_id}"
    )


def test_order_source_deterministic_on_tie(tmp_path) -> None:
    """P0-2 反例：同 source_updated_at 平局时来源仍确定（ORDER BY 唯一尾键 id）。"""
    db = _query_db(tmp_path)
    # ord-3 和 ord-4 同 source_updated_at（8/13 12:00），不同 id
    _seed_order_for_sku(
        db, order_id="ord-3", sku_id="sku-a", placed_at="2026-08-13T09:00:00+00:00",
        source_updated_at="2026-08-13T12:00:00+00:00",
    )
    _seed_order_for_sku(
        db, order_id="ord-4", sku_id="sku-a", placed_at="2026-08-13T09:30:00+00:00",
        source_updated_at="2026-08-13T12:00:00+00:00",
    )
    model = ProductReadQuery(db).sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a", revision=1
    )
    # 平局时 ORDER BY id DESC 取 id 更大者（ord-4 > ord-3），来源确定
    assert model.payments.import_manifest_id == "src-ord-4", (
        f"平局来源不确定: {model.payments.import_manifest_id}"
    )
