from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ecommerce_agent.business.inventory import (
    InventoryBalanceUpsert,
    InventoryService,
)
from ecommerce_agent.business.orders import OrderLineInput, OrderService, OrderUpsert
from ecommerce_agent.business.source_versioning import payload_digest
from ecommerce_agent.database import Database


SOURCE_TIME = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)


def _database_at_v36(path) -> Database:
    db = Database(path)
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in [*range(1, 31), 32, 33, 34, 35, 36]:
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, SOURCE_TIME.isoformat()),
            )
        conn.execute("PRAGMA user_version = 36")
    return db


def _inventory_value(**changes) -> InventoryBalanceUpsert:
    payload = {
        "connector_id": "legacy-connector",
        "store_id": "store-a",
        "warehouse_id": "warehouse-a",
        "sku_id": "sku-a",
        "on_hand": Decimal("5"),
        "reserved": Decimal("1"),
        "inbound": Decimal("2"),
        "average_daily_sales": Decimal("1"),
        "source_updated_at": SOURCE_TIME,
        "source_id": "legacy-inventory-event",
    }
    payload.update(changes)
    return InventoryBalanceUpsert.model_validate(payload)


def _order_value(**changes) -> OrderUpsert:
    payload = {
        "connector_id": "legacy-connector",
        "store_id": "store-a",
        "order_id": "order-a",
        "order_status": "paid",
        "payment_status": "paid",
        "currency": "CNY",
        "total_amount": Decimal("109"),
        "placed_at": SOURCE_TIME,
        "lines": [
            OrderLineInput(
                line_id="line-a",
                sku_id="sku-a",
                title="legacy item",
                quantity=1,
                unit_price=Decimal("109"),
            )
        ],
        "source_updated_at": SOURCE_TIME,
        "source_id": "legacy-order-event",
    }
    payload.update(changes)
    return OrderUpsert.model_validate(payload)


def _legacy_payload(value) -> dict:
    payload = value.model_dump(mode="json")
    payload["source_updated_at"] = SOURCE_TIME.isoformat()
    payload.pop("item_id", None)
    for line in payload.get("lines", []):
        line.pop("item_id", None)
    return payload


def test_v36_inventory_replay_stays_idempotent_after_v39(tmp_path) -> None:
    db = _database_at_v36(tmp_path / "legacy-inventory.sqlite3")
    value = _inventory_value()
    legacy_payload = _legacy_payload(value)
    legacy_hash = payload_digest(legacy_payload)
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO inventory_balances(
                id, tenant_id, connector_id, store_id, warehouse_id, sku_id,
                on_hand, reserved, inbound, average_daily_sales, source_id,
                source_updated_at, payload_hash, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "inventory-legacy", "tenant-a", value.connector_id, value.store_id,
                value.warehouse_id, value.sku_id, str(value.on_hand),
                str(value.reserved), str(value.inbound), str(value.average_daily_sales),
                value.source_id, SOURCE_TIME.isoformat(), legacy_hash, 1,
                SOURCE_TIME.isoformat(), SOURCE_TIME.isoformat(),
            ),
        )

    db.initialize()
    replayed = InventoryService(db).upsert("tenant-a", value)

    assert replayed["write_status"] == "idempotent"
    assert replayed["version"] == 1
    with db.connect() as conn:
        stored_hash = conn.execute(
            "SELECT payload_hash FROM inventory_balances WHERE id='inventory-legacy'"
        ).fetchone()[0]
    assert stored_hash == legacy_hash

    with pytest.raises(ValueError, match="source_version_conflict"):
        InventoryService(db).upsert(
            "tenant-a", value.model_copy(update={"on_hand": Decimal("6")})
        )


def test_v36_order_replay_stays_idempotent_after_v39(tmp_path) -> None:
    db = _database_at_v36(tmp_path / "legacy-order.sqlite3")
    value = _order_value()
    legacy_payload = _legacy_payload(value)
    legacy_hash = payload_digest(legacy_payload)
    snapshot = json.dumps(legacy_payload, ensure_ascii=False, sort_keys=True)
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO commerce_orders(
                id, tenant_id, connector_id, store_id, external_order_id,
                order_status, payment_status, currency, total_amount, placed_at,
                buyer_ref_hash, source_id, source_updated_at, payload_hash, version,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "order-legacy", "tenant-a", value.connector_id, value.store_id,
                value.order_id, value.order_status, value.payment_status, value.currency,
                str(value.total_amount), SOURCE_TIME.isoformat(), None, value.source_id,
                SOURCE_TIME.isoformat(), legacy_hash, 1, SOURCE_TIME.isoformat(),
                SOURCE_TIME.isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO commerce_order_lines(
                id, order_id, external_line_id, sku_id, title, quantity, unit_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("line-legacy", "order-legacy", "line-a", "sku-a", "legacy item", 1, "109"),
        )
        conn.execute(
            """
            INSERT INTO commerce_order_events(
                id, order_id, version, source_updated_at, payload_hash,
                snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "event-legacy", "order-legacy", 1, SOURCE_TIME.isoformat(),
                legacy_hash, snapshot, SOURCE_TIME.isoformat(),
            ),
        )

    db.initialize()
    replayed = OrderService(db).upsert("tenant-a", value)

    assert replayed["write_status"] == "idempotent"
    assert replayed["version"] == 1
    with db.connect() as conn:
        stored = conn.execute(
            "SELECT payload_hash FROM commerce_orders WHERE id='order-legacy'"
        ).fetchone()[0]
        stored_event = conn.execute(
            "SELECT payload_hash, snapshot_json FROM commerce_order_events "
            "WHERE id='event-legacy'"
        ).fetchone()
    assert stored == legacy_hash
    assert tuple(stored_event) == (legacy_hash, snapshot)

    with pytest.raises(ValueError, match="source_version_conflict"):
        OrderService(db).upsert(
            "tenant-a", value.model_copy(update={"total_amount": Decimal("110")})
        )


def test_v36_order_with_header_item_replay_stays_idempotent_after_v39(
    tmp_path,
) -> None:
    """Replay the v36 shape exposed by 753ff15's public OrderService.

    That version stored ``OrderUpsert.item_id`` on the order header while its
    ``OrderLineInput`` schema had no item field.  V39 must accept the additive
    line field as the same event without weakening genuine payload conflicts.
    """
    db = _database_at_v36(tmp_path / "legacy-order-header-item.sqlite3")
    value = _order_value(item_id="item-a")
    legacy_payload = value.model_dump(mode="json")
    legacy_payload["source_updated_at"] = SOURCE_TIME.isoformat()
    for line in legacy_payload["lines"]:
        line.pop("item_id", None)
    legacy_hash = payload_digest(legacy_payload)
    snapshot = json.dumps(legacy_payload, ensure_ascii=False, sort_keys=True)

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
                "order-legacy-header-item", "tenant-a", value.connector_id,
                value.store_id, value.order_id, "item-a", value.order_status,
                value.payment_status, value.currency, str(value.total_amount),
                SOURCE_TIME.isoformat(), None, value.source_id,
                SOURCE_TIME.isoformat(), legacy_hash, 1, SOURCE_TIME.isoformat(),
                SOURCE_TIME.isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO commerce_order_lines(
                id, order_id, external_line_id, sku_id, title, quantity, unit_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "line-legacy-header-item", "order-legacy-header-item", "line-a",
                "sku-a", "legacy item", 1, "109",
            ),
        )
        conn.execute(
            """
            INSERT INTO commerce_order_events(
                id, order_id, version, source_updated_at, payload_hash,
                snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "event-legacy-header-item", "order-legacy-header-item", 1,
                SOURCE_TIME.isoformat(), legacy_hash, snapshot,
                SOURCE_TIME.isoformat(),
            ),
        )

    db.initialize()

    replayed = OrderService(db).upsert("tenant-a", value)
    assert replayed["write_status"] == "idempotent"
    assert replayed["version"] == 1

    with db.connect() as conn:
        stored = conn.execute(
            "SELECT item_id, payload_hash FROM commerce_orders "
            "WHERE id='order-legacy-header-item'"
        ).fetchone()
        stored_line_item = conn.execute(
            "SELECT item_id FROM commerce_order_lines "
            "WHERE id='line-legacy-header-item'"
        ).fetchone()[0]
    assert tuple(stored) == ("item-a", legacy_hash)
    assert stored_line_item == "item-a"

    with pytest.raises(ValueError, match="source_version_conflict"):
        OrderService(db).upsert(
            "tenant-a", value.model_copy(update={"total_amount": Decimal("110")})
        )
