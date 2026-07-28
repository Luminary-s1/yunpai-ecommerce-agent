from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.business import CatalogItemUpsert, MetricQuery, OrderUpsert
from ecommerce_agent.business.catalog import CatalogService
from ecommerce_agent.business.orders import (
    AfterSaleCaseInput,
    LogisticsSnapshotInput,
    OrderLineInput,
)
from ecommerce_agent.database import Database
from ecommerce_agent.service import AgentService
from ecommerce_agent.tools import ToolExecutionContext

from conftest import make_settings


SOURCE_TIME = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)


def catalog_item(**changes) -> CatalogItemUpsert:
    payload = {
        "connector_id": "test-connector",
        "store_id": "store-001",
        "item_id": "item-001",
        "sku_id": "sku-001",
        "title": "测试商品",
        "status": "active",
        "sale_price": "109.00",
        "currency": "CNY",
        "attributes": {"edition": "standard"},
        "source_updated_at": SOURCE_TIME,
        "source_id": "source-catalog-001",
    }
    payload.update(changes)
    return CatalogItemUpsert.model_validate(payload)


def order_fact(**changes) -> OrderUpsert:
    payload = {
        "connector_id": "test-connector",
        "store_id": "store-001",
        "order_id": "ORDER-001",
        "order_status": "shipped",
        "payment_status": "paid",
        "currency": "CNY",
        "total_amount": "109.00",
        "placed_at": SOURCE_TIME - timedelta(days=1),
        "buyer_ref_hash": "a" * 32,
        "lines": [
            OrderLineInput(
                line_id="line-001",
                sku_id="sku-001",
                title="测试商品",
                quantity=1,
                unit_price="109.00",
            )
        ],
        "logistics": LogisticsSnapshotInput(
            carrier="测试快递",
            tracking_no_masked="TEST****0001",
            status="in_transit",
            last_event="运输中",
            last_event_at=SOURCE_TIME - timedelta(hours=1),
        ),
        "after_sales": [],
        "source_updated_at": SOURCE_TIME,
        "source_id": "source-order-001",
    }
    payload.update(changes)
    return OrderUpsert.model_validate(payload)


def test_catalog_source_version_contract_and_tenant_isolation(tmp_path) -> None:
    db = Database(tmp_path / "catalog.sqlite3")
    db.initialize()
    service = CatalogService(db)

    first = service.upsert("tenant-a", catalog_item())
    same = service.upsert("tenant-a", catalog_item())
    assert first["write_status"] == "applied"
    assert same["write_status"] == "idempotent"
    assert same["version"] == 1

    with pytest.raises(ValueError, match="source_version_conflict"):
        service.upsert("tenant-a", catalog_item(title="同版本冲突商品"))
    with pytest.raises(ValueError, match="stale_source_version"):
        service.upsert(
            "tenant-a",
            catalog_item(source_updated_at=SOURCE_TIME - timedelta(seconds=1)),
        )

    updated = service.upsert(
        "tenant-a",
        catalog_item(
            title="新版商品",
            sale_price="119.00",
            source_updated_at=SOURCE_TIME + timedelta(seconds=1),
        ),
    )
    assert updated["version"] == 2
    assert updated["title"] == "新版商品"
    assert service.list_items("tenant-b", sku_id="sku-001") == []


def test_catalog_concurrent_replay_creates_one_version(tmp_path) -> None:
    path = tmp_path / "concurrent.sqlite3"
    Database(path).initialize()

    def write_once(_: int) -> str:
        return CatalogService(Database(Path(path))).upsert("tenant-a", catalog_item())[
            "write_status"
        ]

    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(write_once, range(16)))
    assert statuses.count("applied") == 1
    assert statuses.count("idempotent") == 15
    rows = CatalogService(Database(path)).list_items("tenant-a")
    assert len(rows) == 1
    assert rows[0]["version"] == 1


def test_order_aggregate_history_idempotency_and_stale_protection(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        first = service.operations.orders.upsert("tenant-a", order_fact())
        replay = service.operations.orders.upsert("tenant-a", order_fact())
        assert first["write_status"] == "applied"
        assert replay["write_status"] == "idempotent"
        assert replay["version"] == 1
        assert replay["logistics"]["tracking_no_masked"] == "TEST****0001"

        after_sale = AfterSaleCaseInput(
            case_id="AS-001",
            case_type="refund",
            status="reviewing",
            requested_amount="20.00",
            approved_amount="0",
            reason_code="price_protection",
            opened_at=SOURCE_TIME,
            updated_at=SOURCE_TIME + timedelta(minutes=10),
        )
        updated = service.operations.orders.upsert(
            "tenant-a",
            order_fact(
                order_status="delivered",
                after_sales=[after_sale],
                source_updated_at=SOURCE_TIME + timedelta(hours=1),
            ),
        )
        assert updated["version"] == 2
        assert updated["order_status"] == "delivered"
        assert updated["after_sales"][0]["case_id"] == "AS-001"
        assert [event["version"] for event in service.operations.orders.history("tenant-a", "ORDER-001")] == [1, 2]

        with pytest.raises(ValueError, match="stale_source_version"):
            service.operations.orders.upsert(
                "tenant-a",
                order_fact(source_updated_at=SOURCE_TIME - timedelta(seconds=1)),
            )
        with pytest.raises(ValueError, match="source_version_conflict"):
            service.operations.orders.upsert(
                "tenant-a",
                order_fact(
                    order_status="closed",
                    source_updated_at=SOURCE_TIME + timedelta(hours=1),
                ),
            )
        assert service.operations.orders.list_orders("tenant-b", order_id="ORDER-001") == []
    finally:
        service.close()


def test_order_aggregate_update_rolls_back_as_one_transaction(tmp_path, monkeypatch) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        service.operations.orders.upsert("tenant-a", order_fact())

        def fail_children(*_args, **_kwargs):
            raise RuntimeError("injected_child_failure")

        monkeypatch.setattr(service.operations.orders, "_replace_children", fail_children)
        with pytest.raises(RuntimeError, match="injected_child_failure"):
            service.operations.orders.upsert(
                "tenant-a",
                order_fact(
                    order_status="delivered",
                    source_updated_at=SOURCE_TIME + timedelta(hours=1),
                ),
            )
        current = service.operations.orders.list_orders("tenant-a", order_id="ORDER-001")[0]
        assert current["version"] == 1
        assert current["order_status"] == "shipped"
        assert len(service.operations.orders.history("tenant-a", "ORDER-001")) == 1
    finally:
        service.close()


def test_virtual_sync_metrics_and_agent_order_scope(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        for resource in ("catalog", "orders", "inventory", "competitor_price"):
            run = service.operations.sync(
                tenant_id="tenant-test",
                connector_id="virtual_taobao",
                resource=resource,
                actor="test",
            )
            assert run["status"] == "succeeded"
        for resource in ("catalog", "orders", "inventory", "competitor_price"):
            assert service.operations.sync(
                tenant_id="tenant-test",
                connector_id="virtual_taobao",
                resource=resource,
            )["items_applied"] == 0

        active_metric = service.operations.metrics.query(
            "tenant-test", MetricQuery(metric="active_sku_count")
        )
        assert active_metric["value"] == 2
        assert active_metric["definition_version"] == "1.0"
        assert service.operations.metrics.query(
            "tenant-test", MetricQuery(metric="gross_revenue")
        )["value"] == "308.00"
        assert service.operations.metrics.query(
            "tenant-test", MetricQuery(metric="after_sale_order_rate")
        )["value"] == "0.5000"
        assert service.operations.metrics.query(
            "tenant-test", MetricQuery(metric="inventory_risk_count")
        )["value"] == 1
        assert service.operations.metrics.query(
            "tenant-test", MetricQuery(metric="competitor_lower_price_count")
        )["value"] == 1

        context = ToolExecutionContext(
            tenant_id="tenant-test",
            client_id="client-test",
            session_id="session-test",
            trace_id="trace-test",
            trusted_context={
                "authorized": True,
                "order_id": "VIRTUAL-ORDER-001",
                "shop_id": "virtual-shop-001",
            },
        )
        with pytest.raises(ValueError, match="order_scope_mismatch"):
            service.tools.validate_selection(
                name="get_order_facts",
                arguments={"order_id": "VIRTUAL-ORDER-002", "store_id": "virtual-shop-001"},
                requested_mode="observe",
                context=context,
            )
        with pytest.raises(ValueError, match="store_scope_mismatch"):
            service.tools.validate_selection(
                name="get_order_facts",
                arguments={
                    "order_id": "VIRTUAL-ORDER-001",
                    "store_id": "another-shop",
                },
                requested_mode="observe",
                context=context,
            )
        with pytest.raises(ValueError, match="trusted_context_missing"):
            service.tools.validate_selection(
                name="get_order_facts",
                arguments={"order_id": "VIRTUAL-ORDER-001", "store_id": "virtual-shop-001"},
                requested_mode="observe",
                context=context.__class__(
                    tenant_id=context.tenant_id,
                    client_id=context.client_id,
                    session_id=context.session_id,
                    trace_id=context.trace_id,
                    trusted_context={},
                ),
            )
        spec, arguments = service.tools.validate_selection(
            name="get_order_facts",
            arguments={"order_id": "VIRTUAL-ORDER-001", "store_id": "virtual-shop-001"},
            requested_mode="observe",
            context=context,
        )
        result = service.tools.execute(spec=spec, arguments=arguments, context=context)
        assert result.postcondition_met is True
        assert result.output["orders"][0]["order_id"] == "VIRTUAL-ORDER-001"
        assert result.output["orders"][0]["data_quality"] == "traceable"

        with pytest.raises(ValueError, match="tool_arguments_invalid"):
            service.tools.validate_selection(
                name="get_business_metric",
                arguments={"metric": "order_count", "sql": "DROP TABLE commerce_orders"},
                requested_mode="observe",
                context=context,
            )
    finally:
        service.close()


def test_operations_api_catalog_orders_metrics_and_conflict(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    headers = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}
    with TestClient(app) as client:
        for resource in ("catalog", "orders"):
            response = client.post(
                "/v1/connectors/virtual_taobao/sync",
                headers=headers,
                json={"resource": resource},
            )
            assert response.status_code == 200
            assert response.json()["items_applied"] == 2

        products = client.get("/v1/catalog/items?sku_id=YP-SKU-001", headers=headers)
        orders = client.get("/v1/orders?order_id=VIRTUAL-ORDER-002", headers=headers)
        history = client.get("/v1/orders/VIRTUAL-ORDER-002/history", headers=headers)
        metric = client.post(
            "/v1/metrics/query",
            headers=headers,
            json={"metric": "gross_revenue"},
        )
        assert products.status_code == 200 and len(products.json()) == 1
        assert products.json()[0]["data_quality"] == "traceable"
        assert orders.status_code == 200 and orders.json()[0]["after_sales"]
        assert history.status_code == 200 and history.json()[0]["version"] == 1
        assert metric.status_code == 200 and metric.json()["value"] == "308.00"

        invalid_metric = client.post(
            "/v1/metrics/query",
            headers=headers,
            json={"metric": "order_count", "sql": "SELECT * FROM api_clients"},
        )
        assert invalid_metric.status_code == 422

        current = products.json()[0]
        conflict_payload = {
            "connector_id": current["connector_id"],
            "store_id": current["store_id"],
            "item_id": current["item_id"],
            "sku_id": current["sku_id"],
            "title": "冲突标题",
            "status": current["status"],
            "sale_price": current["sale_price"],
            "currency": current["currency"],
            "attributes": current["attributes"],
            "source_updated_at": current["source_updated_at"],
            "source_id": current["source_id"],
        }
        conflict = client.post("/v1/catalog/items", headers=headers, json=conflict_payload)
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "source_version_conflict"


def seed_qingchuan_catalog(catalog_service: CatalogService, tenant_id: str) -> None:
    for changes in (
        {
            "item_id": "QC-SPU-AF5",
            "sku_id": "QC-AF5-WHITE",
            "title": "晴川空气炸锅 5L 云白款",
            "source_id": "src-af5-white",
            "attributes": {
                "brand": "晴川",
                "category": "空气炸锅",
                "model": "AF5",
                "capacity_l": 5,
                "color": "云白",
            },
        },
        {
            "item_id": "QC-SPU-AF5",
            "sku_id": "QC-AF5-GREEN",
            "title": "晴川空气炸锅 5L 松绿色",
            "source_id": "src-af5-green",
            "attributes": {
                "brand": "晴川",
                "category": "空气炸锅",
                "model": "AF5",
                "capacity_l": 5,
                "color": "松绿",
            },
        },
        {
            "item_id": "QC-SPU-VC1",
            "sku_id": "QC-VC-A1",
            "title": "晴川无线吸尘器 A1",
            "source_id": "src-vc-a1",
            "attributes": {"brand": "晴川", "category": "无线吸尘器", "model": "VC-A1"},
        },
        {
            "item_id": "QC-SPU-OLD",
            "sku_id": "QC-AF-RETIRED",
            "title": "晴川空气炸锅 3L 停售款",
            "status": "deleted",
            "source_id": "src-af-retired",
            "attributes": {"brand": "晴川", "category": "空气炸锅"},
        },
    ):
        catalog_service.upsert(tenant_id, catalog_item(**changes))


def test_catalog_search_resolves_customer_wording_without_sku(tmp_path) -> None:
    db = Database(tmp_path / "search.sqlite3")
    db.initialize()
    service = CatalogService(db)
    seed_qingchuan_catalog(service, "tenant-a")

    fuzzy = service.search_items("tenant-a", keyword="你们店铺空气炸锅什么参数？")
    assert [row["sku_id"] for row in fuzzy] == ["QC-AF5-GREEN", "QC-AF5-WHITE"]
    assert all(row["match_score"] > 0 for row in fuzzy)
    assert {"空气", "气炸", "炸锅"} <= set(fuzzy[0]["matched_terms"])

    colored = service.search_items("tenant-a", keyword="晴川空气炸锅 5L 松绿色")
    assert colored[0]["sku_id"] == "QC-AF5-GREEN"
    assert colored[0]["match_score"] > colored[1]["match_score"]

    attribute_only = service.search_items("tenant-a", keyword="松绿")
    assert [row["sku_id"] for row in attribute_only] == ["QC-AF5-GREEN"]

    assert service.search_items("tenant-a", keyword="我要退款") == []
    assert service.search_items("tenant-a", keyword="5") == []
    assert service.search_items("tenant-a", keyword="") == []
    assert service.search_items("tenant-b", keyword="空气炸锅") == []
    assert service.search_items("tenant-a", keyword="空气炸锅", store_id="store-999") == []


def test_search_products_tool_resolves_fuzzy_wording_within_store(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        seed_qingchuan_catalog(service.operations.catalog, "tenant-test")
        context = ToolExecutionContext(
            tenant_id="tenant-test",
            client_id="client-test",
            session_id="session-test",
            trace_id="trace-test",
            trusted_context={"store_id": "store-001"},
        )

        spec, arguments = service.tools.validate_selection(
            name="search_products",
            arguments={"keyword": "你们店铺空气炸锅什么参数？"},
            requested_mode="observe",
            context=context,
        )
        ambiguous = service.tools.execute(spec=spec, arguments=arguments, context=context)
        assert ambiguous.status == "success"
        assert ambiguous.postcondition_met is True
        assert ambiguous.output["resolution"] == "ambiguous"
        assert ambiguous.output["store_id"] == "store-001"
        assert [item["sku_id"] for item in ambiguous.output["items"]] == [
            "QC-AF5-GREEN",
            "QC-AF5-WHITE",
        ]

        _, resolved_args = service.tools.validate_selection(
            name="search_products",
            arguments={"keyword": "松绿"},
            requested_mode="observe",
            context=context,
        )
        resolved = service.tools.execute(spec=spec, arguments=resolved_args, context=context)
        assert resolved.output["resolution"] == "resolved"
        assert resolved.output["items"][0]["sku_id"] == "QC-AF5-GREEN"

        _, empty_args = service.tools.validate_selection(
            name="search_products",
            arguments={"keyword": "我要退款"},
            requested_mode="observe",
            context=context,
        )
        empty = service.tools.execute(spec=spec, arguments=empty_args, context=context)
        assert empty.output["resolution"] == "no_match"
        assert empty.output["items"] == []

        with pytest.raises(ValueError, match="tool_policy_denied:store_scope_mismatch"):
            service.tools.validate_selection(
                name="search_products",
                arguments={"keyword": "空气炸锅", "store_id": "other-store"},
                requested_mode="observe",
                context=context,
            )
    finally:
        service.close()
