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
                id, tenant_id, connector_id, store_id, warehouse_id, sku_id, item_id,
                on_hand, reserved, inbound, average_daily_sales, source_id,
                source_updated_at, payload_hash, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "inv-1", "tenant-a", "virtual_taobao", "store-a", "wh-1", "sku-a",
                "item-a",
                "50", "0", "10", "2", "src-inv",
                "2026-08-10T00:00:00+00:00", "c" * 64, 1,
                "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00",
            ),
        )
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
                "ord-1", "tenant-a", "virtual_taobao", "store-a", "ext-1", "item-a",
                "paid",
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


def test_query_item_isolation_no_cross_item(tmp_path) -> None:
    """G3 反假绿：同 SKU 在 item-a 下，请求 item-b 必须 MISSING（不跨 item 串数）。

    种 item-a 的 sku-a 库存/订单，请求 item-b 的 sku-a → _revision_window 用
    item_id 过滤找不到 revision → 库存/订单返回 MISSING，不返回 item-a 的数据。
    """
    db = Database(tmp_path / "query-item-iso.sqlite3")
    db.initialize()
    _seed(db)  # 种 item-a 的 sku-a
    query = ProductReadQuery(db)
    # 请求错误的 item-b（sku-a 实际属于 item-a）
    model = query.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-b", sku_id="sku-a"
    )
    # 库存/订单必须 MISSING（不能把 item-a 的 sku-a 库存串到 item-b）
    assert model.sellable_stock.evidence_state is EvidenceState.MISSING, (
        f"item-b 串到 item-a 的库存: {model.sellable_stock.value}"
    )
    assert model.in_transit_stock.evidence_state is EvidenceState.MISSING
    assert model.impressions.evidence_state is EvidenceState.MISSING


def test_query_product_and_competitor_domains(tmp_path) -> None:
    """G2 反假绿：商品/竞品域真实查询，广告/实验域显式 MISSING。

    种 mapping + canonical + competitor 数据，断言商品域返回真实值、
    竞品域返回真实价；广告/实验域无 SKU 级来源必须 MISSING（非 zero/None 静默）。
    """
    db = Database(tmp_path / "query-domains.sqlite3")
    db.initialize()
    _seed(db)
    # 种商品映射 + canonical
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO readonly_canonical_products(canonical_product_id, tenant_id, store_id, internal_part_number, merchant_code, title, normalized_title, source_kind, source_reference, policy_version, payload_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("cp-1", "tenant-a", "store-a", "mpn-1", "mc-1", "测试商品标题", "测试商品标题", "actual", "ref-1", "v1", "a"*64, "2026-08-10T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO readonly_product_mapping_events(event_id, tenant_id, store_id, connector_id, sku_id, mapping_version, expected_version, event_type, canonical_product_id, item_id, merchant_code, decision_key, reason, actor_ref, policy_version, payload_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ev-1", "tenant-a", "store-a", "virtual_taobao", "sku-a", 1, 0, "confirmed", "cp-1", "item-a", "mc-1", "dk-1", "match", "actor", "v1", "a"*64, "2026-08-10T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO competitor_observations(id, tenant_id, connector_id, store_id, subject_sku, competitor_name, competitor_sku, subject_price, competitor_price, currency, source_type, source_ref, is_estimate, observed_at, source_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("comp-1", "tenant-a", "taobao", "store-a", "sku-a", "竞品A", "comp-sku-1", "109.00", "99.00", "CNY", "authorized_api", "ref-comp", 0, "2026-08-10T00:00:00+00:00", "src-comp", "2026-08-10T00:00:00+00:00"),
        )
    query = ProductReadQuery(db)
    model = query.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a"
    )
    # 商品域真实查询
    assert model.title == "测试商品标题"
    assert model.merchant_code == "mc-1"
    assert model.material_code == "mpn-1"  # internal_part_number（料号），非 item_id
    # 竞品域真实查询
    assert model.competitor_price.value == 99.0
    assert model.competitor_price.evidence_state is EvidenceState.ACTUAL
    # 广告/实验域显式 MISSING（非 zero/None）
    assert model.ad_spend.evidence_state is EvidenceState.MISSING
    assert model.ad_spend.reason == "ad_metric_store_level_only"
    assert model.experiment_state.evidence_state is EvidenceState.MISSING
    assert model.experiment_state.reason == "experiment_state_provided_by_wp2_bridge"


def test_query_refund_closed_loop(tmp_path) -> None:
    """G4 反假绿：退款有 approved 金额才投影真值，rejected 不计入，reason 不污染。

    种单行订单 + approved 退款，断言 refunds 有值、payments/net_sales 的 reason 为 None；
    再种 rejected 退款，断言 refunds 不被计入。
    """
    db = Database(tmp_path / "query-refund.sqlite3")
    db.initialize()
    _seed(db)
    with db.connect() as conn:
        # 单行订单 ord-1（sku-a 单 SKU）+ approved 退款 20
        conn.execute(
            "INSERT INTO commerce_after_sale_cases(id, order_id, external_case_id, case_type, status, requested_amount, approved_amount, reason_code, opened_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("case-1", "ord-1", "ext-case-1", "refund", "approved", "20.00", "20.00", None, "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00"),
        )
    query = ProductReadQuery(db)
    model = query.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a"
    )
    assert model.refunds.value == 20.0, f"退款应投影 approved 金额: {model.refunds.value}"
    # 关键：payments/net_sales 有值，reason 不能是退款缺失的 reason
    assert model.payments.value is not None
    assert model.payments.reason is None, f"payments 被退款 reason 污染: {model.payments.reason}"
    # 再种 rejected 退款，应被 status 过滤掉（不改变 refunds）
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO commerce_after_sale_cases(id, order_id, external_case_id, case_type, status, requested_amount, approved_amount, reason_code, opened_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("case-2", "ord-1", "ext-case-2", "refund", "rejected", "30.00", "0.00", None, "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00"),
        )
    model2 = query.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a"
    )
    assert model2.refunds.value == 20.0, (
        f"rejected 退款不应计入: {model2.refunds.value}"
    )


def test_query_revoked_mapping_returns_none(tmp_path) -> None:
    """R2（证据诚实）：revoked 映射不得复活——最新事件是 revoked → 无映射。

    复验阻断项 2b：商品映射只按 confirmed 取最新，revoked 事件被忽略，回落到旧
    confirmed → 映射复活。修复后最新事件是 revoked → material_code/title/merchant_code
    全 None（对齐 M7-R get_latest_mapping 语义）。
    """
    db = Database(tmp_path / "query-revoked.sqlite3")
    db.initialize()
    with db.connect() as conn:
        # 权威 connector 来源：该 SKU 的 listing_revisions（_product_mapping 按它过滤）
        # 先种 creative_assets（listing_revisions.main_image_asset_id 外键引用）
        conn.execute(
            "INSERT INTO creative_assets(asset_id, tenant_id, sha256, mime_type, width, height, storage_ref, source_ref, feature_schema_version, payload_hash, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("asset-1", "tenant-a", "e" * 64, "image/png", 1200, 1200, "objects/a.png", "fixture://a", "image-v1", "f" * 64, "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO listing_revisions(id, tenant_id, connector_id, store_id, item_id, sku_id, revision_no, title, main_image_asset_id, sale_price, attributes_json, active_from, active_to, source_updated_at, payload_hash, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("rev-1", "tenant-a", "virtual_taobao", "store-a", "item-a", "sku-a", 1, "测试商品", "asset-1", "109.00", '{}', "2026-08-01T00:00:00+00:00", "2026-08-30T00:00:00+00:00", "2026-08-10T00:00:00+00:00", "a"*64, "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO readonly_canonical_products(canonical_product_id, tenant_id, store_id, internal_part_number, merchant_code, title, normalized_title, source_kind, source_reference, policy_version, payload_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("cp-1", "tenant-a", "store-a", "mpn-1", "mc-1", "测试商品标题", "测试商品标题", "actual", "ref-1", "v1", "a"*64, "2026-08-10T00:00:00+00:00"),
        )
        # confirmed v1（supersedes_event_id 必须 NULL，v1 无前驱）
        conn.execute(
            "INSERT INTO readonly_product_mapping_events(event_id, tenant_id, store_id, connector_id, sku_id, mapping_version, expected_version, event_type, canonical_product_id, item_id, merchant_code, decision_key, reason, actor_ref, policy_version, supersedes_event_id, payload_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ev-1", "tenant-a", "store-a", "virtual_taobao", "sku-a", 1, 0, "confirmed", "cp-1", "item-a", "mc-1", "dk-1", "match", "actor", "v1", None, "a"*64, "2026-08-10T00:00:00+00:00"),
        )
        # revoked v2（最新，supersedes_event_id 引用 v1）
        conn.execute(
            "INSERT INTO readonly_product_mapping_events(event_id, tenant_id, store_id, connector_id, sku_id, mapping_version, expected_version, event_type, canonical_product_id, item_id, merchant_code, decision_key, reason, actor_ref, policy_version, supersedes_event_id, payload_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ev-2", "tenant-a", "store-a", "virtual_taobao", "sku-a", 2, 1, "revoked", "cp-1", "item-a", None, "dk-2", "unmatch", "actor", "v1", "ev-1", "b"*64, "2026-08-11T00:00:00+00:00"),
        )
    query = ProductReadQuery(db)
    model = query.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a"
    )
    # 最新事件是 revoked → 映射失效，三个字段全 None（不回落旧 confirmed）
    assert model.material_code is None, f"revoked 后料号不应复活: {model.material_code}"
    assert model.merchant_code is None, f"revoked 后商家编码不应复活: {model.merchant_code}"
    assert model.title is None, f"revoked 后标题不应复活: {model.title}"


def test_query_mapping_confirmed_after_revoke_restores(tmp_path) -> None:
    """R2（证据诚实）：revoked 后再 confirmed → 映射恢复（最新事件才是权威）。"""
    db = Database(tmp_path / "query-reconfirm.sqlite3")
    db.initialize()
    with db.connect() as conn:
        # 权威 connector 来源：该 SKU 的 listing_revisions（_product_mapping 按它过滤）
        # 先种 creative_assets（listing_revisions.main_image_asset_id 外键引用）
        conn.execute(
            "INSERT INTO creative_assets(asset_id, tenant_id, sha256, mime_type, width, height, storage_ref, source_ref, feature_schema_version, payload_hash, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("asset-1", "tenant-a", "e" * 64, "image/png", 1200, 1200, "objects/a.png", "fixture://a", "image-v1", "f" * 64, "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO listing_revisions(id, tenant_id, connector_id, store_id, item_id, sku_id, revision_no, title, main_image_asset_id, sale_price, attributes_json, active_from, active_to, source_updated_at, payload_hash, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("rev-1", "tenant-a", "virtual_taobao", "store-a", "item-a", "sku-a", 1, "测试商品", "asset-1", "109.00", '{}', "2026-08-01T00:00:00+00:00", "2026-08-30T00:00:00+00:00", "2026-08-10T00:00:00+00:00", "a"*64, "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO readonly_canonical_products(canonical_product_id, tenant_id, store_id, internal_part_number, merchant_code, title, normalized_title, source_kind, source_reference, policy_version, payload_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("cp-1", "tenant-a", "store-a", "mpn-1", "mc-1", "测试商品标题", "测试商品标题", "actual", "ref-1", "v1", "a"*64, "2026-08-10T00:00:00+00:00"),
        )
        for i, (ev, ver, etype) in enumerate(
            (("ev-1", 1, "confirmed"), ("ev-2", 2, "revoked"), ("ev-3", 3, "confirmed"))
        ):
            supersedes = None if ver == 1 else f"ev-{ver - 1}"
            conn.execute(
                "INSERT INTO readonly_product_mapping_events(event_id, tenant_id, store_id, connector_id, sku_id, mapping_version, expected_version, event_type, canonical_product_id, item_id, merchant_code, decision_key, reason, actor_ref, policy_version, supersedes_event_id, payload_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ev, "tenant-a", "store-a", "virtual_taobao", "sku-a", ver, ver - 1, etype, "cp-1", "item-a", "mc-1", f"dk-{ver}", "match", "actor", "v1", supersedes, "a"*64, "2026-08-10T00:00:00+00:00"),
            )
    query = ProductReadQuery(db)
    model = query.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a"
    )
    # 最新事件是 confirmed v3 → 映射恢复
    assert model.material_code == "mpn-1"
    assert model.merchant_code == "mc-1"
    assert model.title == "测试商品标题"


def test_query_mapping_not_hidden_by_other_connector_version(tmp_path) -> None:
    """P0-3 反例：跨 connector 高 version 不得掩盖权威 connector 的映射。

    审查发现：_product_mapping 原不过滤 connector_id，跨连接器按 mapping_version
    DESC 取最大——demo 连接器高 version 可能掩盖 operational 连接器的 revoked。
    修复后按权威 connector（listing_revisions 最新行）过滤，只查该 connector 的映射。
    """
    db = Database(tmp_path / "query-multi-conn.sqlite3")
    db.initialize()
    with db.connect() as conn:
        # 权威 connector 来源 + asset（listing_revisions 外键）
        conn.execute(
            "INSERT INTO creative_assets(asset_id, tenant_id, sha256, mime_type, width, height, storage_ref, source_ref, feature_schema_version, payload_hash, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("asset-1", "tenant-a", "e" * 64, "image/png", 1200, 1200, "objects/a.png", "fixture://a", "image-v1", "f" * 64, "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO listing_revisions(id, tenant_id, connector_id, store_id, item_id, sku_id, revision_no, title, main_image_asset_id, sale_price, attributes_json, active_from, active_to, source_updated_at, payload_hash, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("rev-1", "tenant-a", "virtual_taobao", "store-a", "item-a", "sku-a", 1, "测试商品", "asset-1", "109.00", '{}', "2026-08-01T00:00:00+00:00", "2026-08-30T00:00:00+00:00", "2026-08-10T00:00:00+00:00", "a"*64, "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00"),
        )

        # taobao：高 revision_no(5) 但旧 source_updated_at(08-09)——若权威 connector 按
        # revision_no DESC 选会错选 taobao（掩盖 operational）；应按 source_updated_at 选
        conn.execute(
            "INSERT INTO listing_revisions(id, tenant_id, connector_id, store_id, item_id, sku_id, revision_no, title, main_image_asset_id, sale_price, attributes_json, active_from, active_to, source_updated_at, payload_hash, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("rev-ta", "tenant-a", "taobao", "store-a", "item-a", "sku-a", 5, "测试商品", "asset-1", "109.00", '{}', "2026-08-01T00:00:00+00:00", "2026-08-30T00:00:00+00:00", "2026-08-09T00:00:00+00:00", "b"*64, "2026-08-09T00:00:00+00:00", "2026-08-09T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO readonly_canonical_products(canonical_product_id, tenant_id, store_id, internal_part_number, merchant_code, title, normalized_title, source_kind, source_reference, policy_version, payload_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("cp-1", "tenant-a", "store-a", "mpn-1", "mc-1", "测试商品标题", "测试商品标题", "actual", "ref-1", "v1", "a"*64, "2026-08-10T00:00:00+00:00"),
        )
        # 权威 connector（virtual_taobao）：v1 confirmed
        conn.execute(
            "INSERT INTO readonly_product_mapping_events(event_id, tenant_id, store_id, connector_id, sku_id, mapping_version, expected_version, event_type, canonical_product_id, item_id, merchant_code, decision_key, reason, actor_ref, policy_version, supersedes_event_id, payload_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ev-vt1", "tenant-a", "store-a", "virtual_taobao", "sku-a", 1, 0, "confirmed", "cp-1", "item-a", "mc-1", "dk-1", "match", "actor", "v1", None, "a"*64, "2026-08-10T00:00:00+00:00"),
        )
        # 非权威 connector（taobao）：v2 revoked（高 version，若跨 connector 取最大会被选到）
        conn.execute(
            "INSERT INTO readonly_product_mapping_events(event_id, tenant_id, store_id, connector_id, sku_id, mapping_version, expected_version, event_type, canonical_product_id, item_id, merchant_code, decision_key, reason, actor_ref, policy_version, supersedes_event_id, payload_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ev-ta2", "tenant-a", "store-a", "taobao", "sku-a", 2, 1, "revoked", "cp-1", "item-a", None, "dk-2", "unmatch", "actor", "v1", "ev-vt1", "b"*64, "2026-08-11T00:00:00+00:00"),
        )
    query = ProductReadQuery(db)
    model = query.sku_read_model(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a"
    )
    # 修复前：跨 connector 取 mapping_version 最大 → taobao v2 revoked → 映射为 None（掩盖）
    # 修复后：按权威 connector virtual_taobao 过滤 → v1 confirmed → 映射保留
    assert model.material_code == "mpn-1", (
        f"权威 connector 的映射被跨 connector 高 version 掩盖: {model.material_code}"
    )
