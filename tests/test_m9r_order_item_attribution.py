from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ecommerce_agent.business.inventory import InventoryBalanceUpsert
from ecommerce_agent.business.orders import (
    AfterSaleCaseInput,
    LogisticsSnapshotInput,
    OrderLineInput,
    OrderService,
    OrderUpsert,
)
from ecommerce_agent.database import Database


NOW = datetime(2026, 8, 24, 8, tzinfo=UTC)


def _v36_database(db: Database) -> None:
    db.path.parent.mkdir(parents=True, exist_ok=True)
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in (*range(1, 31), 32, 33, 34, 35, 36):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, NOW.isoformat()),
            )


def _order(*, item_id: str, source_time: datetime) -> OrderUpsert:
    return OrderUpsert(
        connector_id="connector-a",
        store_id="store-a",
        order_id="external-order-1",
        item_id=item_id,
        order_status="shipped",
        payment_status="paid",
        total_amount="109",
        placed_at=NOW - timedelta(days=1),
        lines=[
            OrderLineInput(
                line_id=f"line-{item_id}",
                sku_id="sku-shared",
                title=f"product-{item_id}",
                quantity=1,
                unit_price="109",
            )
        ],
        logistics=LogisticsSnapshotInput(
            carrier="carrier-a",
            tracking_no_masked="TRACK****001",
            status="in_transit",
            last_event="moving",
            last_event_at=source_time,
        ),
        after_sales=[
            AfterSaleCaseInput(
                case_id=f"case-{item_id}",
                case_type="refund",
                status="reviewing",
                requested_amount="10",
                opened_at=source_time,
                updated_at=source_time,
            )
        ],
        source_updated_at=source_time,
        source_id=f"source-{item_id}",
    )


def test_v36_to_v39_preserves_order_aggregate_and_backfills_line_item(tmp_path) -> None:
    db = Database(tmp_path / "v36-orders.sqlite3")
    _v36_database(db)
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO commerce_orders(
                id, tenant_id, connector_id, store_id, external_order_id, item_id,
                order_status, payment_status, currency, total_amount, placed_at,
                buyer_ref_hash, source_id, source_updated_at, payload_hash, version,
                created_at, updated_at
            ) VALUES ('order-1','tenant-a','connector-a','store-a','external-1','item-a',
                      'shipped','paid','CNY','109',?,NULL,'source-1',?,'hash-1',1,?,?)
            """,
            (NOW.isoformat(), NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
        conn.execute(
            "INSERT INTO commerce_order_lines VALUES "
            "('line-1','order-1','external-line-1','sku-a','title-a',1,'109')"
        )
        conn.execute(
            "INSERT INTO commerce_order_logistics VALUES "
            "('order-1','carrier','TRACK****001','in_transit','moving',?)",
            (NOW.isoformat(),),
        )
        conn.execute(
            """
            INSERT INTO commerce_after_sale_cases VALUES(
                'case-1','order-1','external-case-1','refund','reviewing',
                '10','0','reason-a',?,?
            )
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
        conn.execute(
            "INSERT INTO commerce_order_events VALUES "
            "('event-1','order-1',1,?,'hash-1','{}',?)",
            (NOW.isoformat(), NOW.isoformat()),
        )

    db.initialize()
    db.initialize()

    with db.connect() as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "commerce_orders",
                "commerce_order_lines",
                "commerce_order_logistics",
                "commerce_after_sale_cases",
                "commerce_order_events",
            )
        }
        line_item = conn.execute(
            "SELECT item_id FROM commerce_order_lines WHERE id='line-1'"
        ).fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        migration_count = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=39"
        ).fetchone()[0]

    assert counts == {
        "commerce_orders": 1,
        "commerce_order_lines": 1,
        "commerce_order_logistics": 1,
        "commerce_after_sale_cases": 1,
        "commerce_order_events": 1,
    }
    assert line_item == "item-a"
    assert foreign_keys == []
    assert migration_count == 1


def test_external_order_remains_one_aggregate_across_item_updates(tmp_path) -> None:
    db = Database(tmp_path / "order-natural-key.sqlite3")
    db.initialize()
    orders = OrderService(db)

    orders.upsert("tenant-a", _order(item_id="item-a", source_time=NOW))
    current = orders.upsert(
        "tenant-a", _order(item_id="item-b", source_time=NOW + timedelta(minutes=1))
    )

    with db.connect() as conn:
        header_count = conn.execute(
            "SELECT COUNT(*) FROM commerce_orders WHERE tenant_id='tenant-a' "
            "AND external_order_id='external-order-1'"
        ).fetchone()[0]
    assert header_count == 1
    assert current["lines"][0]["item_id"] == "item-b"
    assert current["logistics"]["tracking_no_masked"] == "TRACK****001"
    assert current["after_sales"][0]["case_id"] == "case-item-b"
    assert [event["version"] for event in orders.history(
        "tenant-a", "external-order-1", store_id="store-a"
    )] == [1, 2]


def test_mixed_item_order_persists_attribution_on_each_line(tmp_path) -> None:
    db = Database(tmp_path / "mixed-item-order.sqlite3")
    db.initialize()
    orders = OrderService(db)
    value = _order(item_id="item-a", source_time=NOW).model_copy(
        update={
            "item_id": None,
            "total_amount": Decimal("208"),
            "lines": [
                OrderLineInput(
                    line_id="line-a", sku_id="sku-shared", item_id="item-a",
                    title="product-a", quantity=1, unit_price="109",
                ),
                OrderLineInput(
                    line_id="line-b", sku_id="sku-shared", item_id="item-b",
                    title="product-b", quantity=1, unit_price="99",
                ),
            ],
        }
    )

    result = orders.upsert("tenant-a", value)

    assert result["item_id"] is None
    assert {line["line_id"]: line["item_id"] for line in result["lines"]} == {
        "line-a": "item-a",
        "line-b": "item-b",
    }


def test_item_identity_rejects_blank_values() -> None:
    with pytest.raises(ValidationError):
        _order(item_id="", source_time=NOW)
    with pytest.raises(ValidationError):
        OrderLineInput(
            line_id="line-a", sku_id="sku-a", item_id="",
            title="product-a", quantity=1, unit_price="1",
        )
    with pytest.raises(ValidationError):
        InventoryBalanceUpsert(
            connector_id="connector-a", store_id="store-a", warehouse_id="warehouse-a",
            sku_id="sku-a", item_id="", on_hand="1", source_updated_at=NOW,
        )
