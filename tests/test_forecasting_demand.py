from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ecommerce_agent.business.inventory import InventoryBalanceUpsert, InventoryService
from ecommerce_agent.business.orders import OrderLineInput, OrderService, OrderUpsert
from ecommerce_agent.business.service import OperationsService
from ecommerce_agent.database import Database
from ecommerce_agent.forecasting import DemandFactRebuild, DemandFactService, ForecastRunService


TENANT_A = "tenant-forecast-a"
TENANT_B = "tenant-forecast-b"
STORE = "store-forecast"


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _order(
    *,
    order_id: str,
    sku_id: str,
    quantity: int,
    placed_at: datetime,
    source_updated_at: datetime,
    order_status: str = "delivered",
    payment_status: str = "paid",
) -> OrderUpsert:
    return OrderUpsert(
        connector_id="forecast-fixture",
        store_id=STORE,
        order_id=order_id,
        order_status=order_status,
        payment_status=payment_status,
        total_amount=Decimal(quantity * 10),
        placed_at=placed_at,
        source_updated_at=source_updated_at,
        source_id=f"source-{order_id}",
        lines=[
            OrderLineInput(
                line_id=f"line-{order_id}",
                sku_id=sku_id,
                title=f"Fixture {sku_id}",
                quantity=quantity,
                unit_price=Decimal("10.00"),
            )
        ],
    )


def _services(tmp_path) -> tuple[OrderService, InventoryService, DemandFactService]:
    db = Database(tmp_path / "forecasting.sqlite3")
    db.initialize()
    orders = OrderService(db)
    inventory = InventoryService(db)
    return orders, inventory, DemandFactService(db, orders=orders, inventory=inventory)


def _rebuild(
    service: DemandFactService,
    *,
    tenant_id: str = TENANT_A,
    sku_id: str | None,
    start_date: date,
    end_date: date,
    coverage_complete: bool,
    mode: str = "full",
) -> dict:
    return service.rebuild(
        tenant_id,
        DemandFactRebuild(
            store_id=STORE,
            sku_id=sku_id,
            start_date=start_date,
            end_date=end_date,
            coverage_complete=coverage_complete,
            mode=mode,
        ),
    )


def test_v28_database_upgrades_to_v29_with_forecasting_contract(tmp_path) -> None:
    db = Database(tmp_path / "v28-forecasting.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 29):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-08-11T00:00:00+00:00"),
            )
        conn.execute("CREATE TABLE legacy_probe(id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO legacy_probe VALUES ('probe-1', 'preserved')")

    db.initialize()
    db.initialize()

    with db.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        migrations = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations")
        }
        migration_count = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=29"
        ).fetchone()[0]
        fact_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(demand_daily_facts)")
        }
        probe = conn.execute(
            "SELECT value FROM legacy_probe WHERE id='probe-1'"
        ).fetchone()[0]

    assert Database.SCHEMA_VERSION >= 29
    assert 29 in migrations
    assert migration_count == 1
    assert {
        "demand_daily_facts",
        "forecast_policies",
        "forecast_runs",
        "forecast_backtests",
        "forecast_points",
        "forecast_anomalies",
    } <= tables
    assert {
        "tenant_id",
        "store_id",
        "sku_id",
        "business_date",
        "gross_units",
        "eligible_units",
        "price",
        "promotion_flag",
        "source_watermark",
        "fact_version",
        "demand_policy_version",
        "quality_flags_json",
        "payload_hash",
    } <= fact_columns
    assert probe == "preserved"


def test_demand_v1_uses_shanghai_business_date_and_traces_source_facts(tmp_path) -> None:
    orders, _inventory, facts = _services(tmp_path)
    orders.upsert(
        TENANT_A,
        _order(
            order_id="near-midnight",
            sku_id="sku-clock",
            quantity=2,
            placed_at=_at("2026-08-10T15:59:00+00:00"),
            source_updated_at=_at("2026-08-10T16:05:00+00:00"),
        ),
    )
    orders.upsert(
        TENANT_A,
        _order(
            order_id="at-midnight",
            sku_id="sku-clock",
            quantity=3,
            placed_at=_at("2026-08-10T16:00:00+00:00"),
            source_updated_at=_at("2026-08-10T16:06:00+00:00"),
        ),
    )

    result = _rebuild(
        facts,
        sku_id="sku-clock",
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 11),
        coverage_complete=True,
    )
    rows = {
        item["business_date"]: item
        for item in facts.list_facts(TENANT_A, store_id=STORE, sku_id="sku-clock")
    }

    assert result["demand_policy"] == {
        "policy_version": "demand-v1",
        "timezone": "Asia/Shanghai",
        "included_payment_statuses": ["paid", "partially_refunded", "refunded"],
        "excluded_order_statuses": ["canceled"],
        "late_arrival_policy": "rebuild_fixed_14_day_window",
        "rebuild_lookback_days": 14,
    }
    assert rows["2026-08-10"]["eligible_units"] == 2
    assert rows["2026-08-11"]["eligible_units"] == 3
    order_lineage = rows["2026-08-10"]["lineage"]["orders"]
    assert len(order_lineage) == 1
    assert {
        "connector_id": "forecast-fixture",
        "order_id": "near-midnight",
        "source_id": "source-near-midnight",
        "source_updated_at": "2026-08-10T16:05:00+00:00",
        "version": 1,
    }.items() <= order_lineage[0].items()
    assert rows["2026-08-11"]["source_watermark"]["orders"]["count"] == 1


def test_rebuild_is_idempotent_and_cancellation_creates_traceable_backfill(tmp_path) -> None:
    orders, _inventory, facts = _services(tmp_path)
    placed_at = _at("2026-08-10T04:00:00+00:00")
    orders.upsert(
        TENANT_A,
        _order(
            order_id="correctable-order",
            sku_id="sku-correction",
            quantity=3,
            placed_at=placed_at,
            source_updated_at=_at("2026-08-10T05:00:00+00:00"),
        ),
    )
    kwargs = {
        "sku_id": "sku-correction",
        "start_date": date(2026, 8, 10),
        "end_date": date(2026, 8, 10),
        "coverage_complete": True,
    }

    first = _rebuild(facts, **kwargs)
    replay = _rebuild(facts, **kwargs)
    orders.upsert(
        TENANT_A,
        _order(
            order_id="correctable-order",
            sku_id="sku-correction",
            quantity=3,
            placed_at=placed_at,
            source_updated_at=_at("2026-08-11T05:00:00+00:00"),
            order_status="canceled",
        ),
    )
    backfill = _rebuild(facts, mode="incremental", **kwargs)
    history = facts.list_facts(
        TENANT_A,
        store_id=STORE,
        sku_id="sku-correction",
        include_history=True,
    )

    assert first["facts_written"] == 1
    assert replay["facts_idempotent"] == 1
    assert backfill["mode"] == "incremental"
    assert [item["fact_version"] for item in history] == [1, 2]
    assert history[-1]["gross_units"] == 3
    assert history[-1]["eligible_units"] == 0
    assert history[-1]["lineage"]["orders"][0]["version"] == 2
    assert history[-1]["source_watermark"]["orders"]["max_source_updated_at"] == (
        "2026-08-11T05:00:00+00:00"
    )


def test_demand_facts_distinguish_true_zero_missing_data_and_stockout_states(tmp_path) -> None:
    orders, inventory, facts = _services(tmp_path)
    for business_day, sku_id in (
        ("2026-08-10", "sku-stockout"),
        ("2026-08-11", "sku-in-stock"),
        ("2026-08-12", "sku-unknown-stock"),
    ):
        orders.upsert(
            TENANT_A,
            _order(
                order_id=f"order-{sku_id}",
                sku_id=sku_id,
                quantity=1,
                placed_at=_at(f"{business_day}T04:00:00+00:00"),
                source_updated_at=_at(f"{business_day}T05:00:00+00:00"),
            ),
        )
    inventory.upsert(
        TENANT_A,
        InventoryBalanceUpsert(
            connector_id="forecast-fixture",
            store_id=STORE,
            warehouse_id="warehouse-1",
            sku_id="sku-stockout",
            on_hand=Decimal("0"),
            reserved=Decimal("0"),
            source_updated_at=_at("2026-08-10T02:00:00+00:00"),
            source_id="stockout-snapshot",
        ),
    )
    inventory.upsert(
        TENANT_A,
        InventoryBalanceUpsert(
            connector_id="forecast-fixture",
            store_id=STORE,
            warehouse_id="warehouse-1",
            sku_id="sku-in-stock",
            on_hand=Decimal("9"),
            reserved=Decimal("2"),
            source_updated_at=_at("2026-08-11T02:00:00+00:00"),
            source_id="in-stock-snapshot",
        ),
    )

    for business_day, sku_id in (
        (date(2026, 8, 10), "sku-stockout"),
        (date(2026, 8, 11), "sku-in-stock"),
        (date(2026, 8, 12), "sku-unknown-stock"),
    ):
        _rebuild(
            facts,
            sku_id=sku_id,
            start_date=business_day,
            end_date=business_day,
            coverage_complete=True,
        )
    _rebuild(
        facts,
        sku_id="sku-zero",
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 10),
        coverage_complete=True,
    )
    _rebuild(
        facts,
        sku_id="sku-missing",
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 10),
        coverage_complete=False,
    )
    rows = {
        item["sku_id"]: item
        for item in facts.list_facts(TENANT_A, store_id=STORE)
    }

    assert rows["sku-stockout"]["stockout_flag"] == "true"
    assert rows["sku-stockout"]["available_stock"] == "0.00"
    assert rows["sku-in-stock"]["stockout_flag"] == "false"
    assert rows["sku-in-stock"]["available_stock"] == "7.00"
    assert rows["sku-unknown-stock"]["stockout_flag"] == "unknown"
    assert rows["sku-unknown-stock"]["available_stock"] is None
    assert rows["sku-zero"]["eligible_units"] == 0
    assert "zero_demand" in rows["sku-zero"]["quality_flags"]
    assert rows["sku-missing"]["eligible_units"] is None
    assert "data_coverage_missing" in rows["sku-missing"]["quality_flags"]


def test_store_wide_rebuild_uses_inventory_skus_without_crossing_scope(tmp_path) -> None:
    orders, inventory, facts = _services(tmp_path)
    orders.upsert(
        TENANT_A,
        _order(
            order_id="store-wide-order-only",
            sku_id="sku-order-only",
            quantity=2,
            placed_at=_at("2026-08-11T04:00:00+00:00"),
            source_updated_at=_at("2026-08-11T05:00:00+00:00"),
        ),
    )
    inventory.upsert(
        TENANT_A,
        InventoryBalanceUpsert(
            connector_id="forecast-fixture",
            store_id=STORE,
            warehouse_id="warehouse-1",
            sku_id="sku-inventory-only",
            on_hand=Decimal("8"),
            reserved=Decimal("1"),
            source_updated_at=_at("2026-08-10T02:00:00+00:00"),
            source_id="inventory-only-snapshot",
        ),
    )
    inventory.upsert(
        TENANT_A,
        InventoryBalanceUpsert(
            connector_id="forecast-fixture",
            store_id="other-store",
            warehouse_id="warehouse-1",
            sku_id="sku-other-store-only",
            on_hand=Decimal("3"),
            source_updated_at=_at("2026-08-10T02:00:00+00:00"),
            source_id="other-store-snapshot",
        ),
    )
    inventory.upsert(
        TENANT_B,
        InventoryBalanceUpsert(
            connector_id="forecast-fixture",
            store_id=STORE,
            warehouse_id="warehouse-1",
            sku_id="sku-other-tenant-only",
            on_hand=Decimal("4"),
            source_updated_at=_at("2026-08-10T02:00:00+00:00"),
            source_id="other-tenant-snapshot",
        ),
    )

    rebuilt = _rebuild(
        facts,
        sku_id=None,
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 16),
        coverage_complete=True,
    )
    rows = facts.list_facts(TENANT_A, store_id=STORE)

    assert rebuilt["facts_written"] == 14
    universe = rebuilt["sku_universe"]
    assert universe["policy_version"] == "demand-sku-universe-v1"
    assert universe["scope"] == "store_wide"
    assert universe["sku_count"] == 2
    assert universe["digest"]
    assert {
        "sku_id": "sku-inventory-only",
        "sources": ["current_inventory_balances"],
    } in universe["members"]
    assert {
        "sku_id": "sku-order-only",
        "sources": ["window_order_lines"],
    } in universe["members"]
    assert {item["sku_id"] for item in rows} == {
        "sku-inventory-only",
        "sku-order-only",
    }
    inventory_only = [
        item for item in rows if item["sku_id"] == "sku-inventory-only"
    ]
    assert all(item["eligible_units"] == 0 for item in inventory_only)
    assert all("zero_demand" in item["quality_flags"] for item in inventory_only)
    assert facts.list_facts(TENANT_A, store_id="other-store") == []
    assert facts.list_facts(TENANT_B, store_id=STORE) == []

    forecast = ForecastRunService(facts.db, facts=facts).run(
        TENANT_A,
        store_id=STORE,
        sku_id="sku-inventory-only",
    )
    assert forecast["candidate_models"]["demand_type"] == "cold_start"
    assert forecast["points"]


def test_inventory_sku_without_confirmed_coverage_remains_missing_not_zero(
    tmp_path,
) -> None:
    _orders, inventory, facts = _services(tmp_path)
    inventory.upsert(
        TENANT_A,
        InventoryBalanceUpsert(
            connector_id="forecast-fixture",
            store_id=STORE,
            warehouse_id="warehouse-1",
            sku_id="sku-coverage-unknown",
            on_hand=Decimal("5"),
            source_updated_at=_at("2026-08-10T02:00:00+00:00"),
            source_id="coverage-unknown-snapshot",
        ),
    )

    rebuilt = _rebuild(
        facts,
        sku_id=None,
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 16),
        coverage_complete=False,
    )
    rows = facts.list_facts(
        TENANT_A,
        store_id=STORE,
        sku_id="sku-coverage-unknown",
    )

    assert rebuilt["facts_written"] == 7
    assert all(item["eligible_units"] is None for item in rows)
    assert all("data_coverage_missing" in item["quality_flags"] for item in rows)
    with pytest.raises(
        ValueError,
        match="forecast_engine_failed:no_observed_demand",
    ):
        ForecastRunService(facts.db, facts=facts).run(
            TENANT_A,
            store_id=STORE,
            sku_id="sku-coverage-unknown",
        )


def test_demand_fact_reads_and_writes_are_tenant_isolated(tmp_path) -> None:
    orders, _inventory, facts = _services(tmp_path)
    orders.upsert(
        TENANT_A,
        _order(
            order_id="tenant-a-order",
            sku_id="sku-shared-id",
            quantity=2,
            placed_at=_at("2026-08-10T04:00:00+00:00"),
            source_updated_at=_at("2026-08-10T05:00:00+00:00"),
        ),
    )
    orders.upsert(
        TENANT_B,
        _order(
            order_id="tenant-b-order",
            sku_id="sku-shared-id",
            quantity=7,
            placed_at=_at("2026-08-10T04:00:00+00:00"),
            source_updated_at=_at("2026-08-10T05:00:00+00:00"),
        ),
    )
    _rebuild(
        facts,
        tenant_id=TENANT_A,
        sku_id="sku-shared-id",
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 10),
        coverage_complete=True,
    )

    tenant_a_rows = facts.list_facts(TENANT_A, store_id=STORE, sku_id="sku-shared-id")
    tenant_b_rows = facts.list_facts(TENANT_B, store_id=STORE, sku_id="sku-shared-id")

    assert tenant_a_rows[0]["eligible_units"] == 2
    assert tenant_b_rows == []


def test_demand_daily_fact_versions_cannot_be_mutated_in_place(tmp_path) -> None:
    orders, _inventory, facts = _services(tmp_path)
    orders.upsert(
        TENANT_A,
        _order(
            order_id="immutable-order",
            sku_id="sku-immutable",
            quantity=1,
            placed_at=_at("2026-08-10T04:00:00+00:00"),
            source_updated_at=_at("2026-08-10T05:00:00+00:00"),
        ),
    )
    _rebuild(
        facts,
        sku_id="sku-immutable",
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 10),
        coverage_complete=True,
    )
    fact_id = facts.list_facts(TENANT_A, store_id=STORE, sku_id="sku-immutable")[0]["id"]

    with facts.db.connect() as conn, pytest.raises(
        sqlite3.IntegrityError, match="demand_daily_fact_immutable"
    ):
        conn.execute(
            "UPDATE demand_daily_facts SET eligible_units=99 WHERE id=?", (fact_id,)
        )


def test_operations_wires_forecasting_to_public_order_and_inventory_services(tmp_path) -> None:
    db = Database(tmp_path / "operations.sqlite3")
    db.initialize()

    operations = OperationsService(db)

    assert isinstance(operations.forecasting, DemandFactService)
    assert operations.forecasting.orders is operations.orders
    assert operations.forecasting.inventory is operations.inventory
    assert isinstance(operations.forecast_runs, ForecastRunService)
    assert operations.forecast_runs.facts is operations.forecasting
