from __future__ import annotations

import sqlite3
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ecommerce_agent.business.inventory import InventoryBalanceUpsert, InventoryService
from ecommerce_agent.business.service import OperationsService
from ecommerce_agent.database import Database
from ecommerce_agent.forecasting import (
    InventoryPlanningError,
    InventoryPlanningPolicy,
    InventoryPlanningService,
)


TENANT = "tenant-planning"
STORE = "store-planning"
SKU = "sku-planning"


class _ForecastReader:
    def __init__(self, run: dict):
        self.run = run

    def get_run(self, tenant_id: str, run_id: str) -> dict:
        if tenant_id != self.run["tenant_id"] or run_id != self.run["run_id"]:
            raise ValueError("forecast_run_not_found")
        return deepcopy(self.run)


def _seed_forecast(db: Database, *, days: int = 10) -> dict:
    run_id = "forecast-run-planning"
    created_at = "2026-08-12T00:00:00+00:00"
    points = [
        {
            "forecast_date": (date(2026, 9, 1) + timedelta(days=offset)).isoformat(),
            "p50": 4.0,
            "p80": 5.0,
            "p95": 6.0,
        }
        for offset in range(days)
    ]
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO forecast_runs(
                run_id, tenant_id, store_id, sku_id, training_start, training_end,
                data_hash, demand_policy_version, forecast_policy_version,
                candidate_models_json, champion_model, champion_reason, model_version,
                wape, bias, smape, rmse, forecast_horizon, status, created_at
            ) VALUES (?, ?, ?, ?, '2026-07-01', '2026-08-31', 'forecast-data-hash',
                      'demand-v1', 'forecast-v1', '{}', 'rolling_mean', '{}',
                      'forecast-engine-v1', 0.1, 0.0, 0.1, 1.0, ?, 'completed', ?)
            """,
            (run_id, TENANT, STORE, SKU, days, created_at),
        )
        for offset, point in enumerate(points):
            conn.execute(
                """
                INSERT INTO forecast_points(
                    point_id, tenant_id, run_id, forecast_date, p50, p80, p95, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"forecast-point-planning-{offset}", TENANT, run_id,
                    point["forecast_date"], point["p50"], point["p80"],
                    point["p95"], created_at,
                ),
            )
    return {
        "run_id": run_id,
        "tenant_id": TENANT,
        "store_id": STORE,
        "sku_id": SKU,
        "data_hash": "forecast-data-hash",
        "training_start": "2026-07-01",
        "training_end": "2026-08-31",
        "demand_policy_version": "demand-v1",
        "forecast_policy_version": "forecast-v1",
        "status": "completed",
        "champion_model": "rolling_mean",
        "champion_reason": {"selection": "baseline_retained"},
        "model_version": "forecast-engine-v1",
        "wape": 0.1,
        "bias": 0.0,
        "smape": 0.1,
        "rmse": 1.0,
        "points": points,
        "anomalies": [],
    }


def _balance(
    inventory: InventoryService,
    *,
    warehouse_id: str,
    on_hand: str,
    reserved: str,
    inbound: str,
    connector_id: str = "virtual-inventory",
    source_updated_at: datetime = datetime(2026, 8, 12, tzinfo=timezone.utc),
) -> None:
    inventory.upsert(
        TENANT,
        InventoryBalanceUpsert(
            connector_id=connector_id,
            store_id=STORE,
            warehouse_id=warehouse_id,
            sku_id=SKU,
            on_hand=Decimal(on_hand),
            reserved=Decimal(reserved),
            inbound=Decimal(inbound),
            source_updated_at=source_updated_at,
        ),
    )


def _service(tmp_path, *, days: int = 10):
    db = Database(tmp_path / "inventory-planning.sqlite3")
    db.initialize()
    run = _seed_forecast(db, days=days)
    inventory = InventoryService(db)
    return db, inventory, InventoryPlanningService(
        db, forecasts=_ForecastReader(run), inventory=inventory
    )


def _policy(**overrides: object) -> InventoryPlanningPolicy:
    values = {
        "supplier_lead_days": 2,
        "review_period_days": 2,
        "service_level": Decimal("0.80"),
        "minimum_order_qty": Decimal("10"),
        "order_multiple": Decimal("6"),
        "minimum_safety_stock": Decimal("3"),
        "maximum_stock_days": 8,
    }
    values.update(overrides)
    return InventoryPlanningPolicy(**values)


def test_v29_database_upgrades_to_v30_without_rebuilding_existing_tables(tmp_path) -> None:
    db = Database(tmp_path / "v29-inventory-planning.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 30):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?)",
                (version, "2026-08-12T00:00:00+00:00"),
            )
        conn.execute("CREATE TABLE legacy_probe(id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO legacy_probe VALUES ('probe-1', 'preserved')")

    db.initialize()
    db.initialize()

    with db.connect() as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        migrations = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        policy_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(inventory_planning_policies)"
        )}
        plan_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(inventory_plans)"
        )}
        probe = conn.execute("SELECT value FROM legacy_probe WHERE id='probe-1'").fetchone()[0]

    assert Database.SCHEMA_VERSION >= 30
    assert 30 in migrations
    assert {"inventory_planning_policies", "inventory_plans"} <= tables
    assert {"service_level", "order_multiple", "maximum_stock_days"} <= policy_columns
    assert {
        "forecast_run_id", "inventory_snapshot_json", "reorder_point",
        "inventory_as_of", "target_stock", "recommended_order_qty",
        "overstock_risk", "calculation_steps_json",
    } <= plan_columns
    assert probe == "preserved"


def test_plan_is_numeric_replayable_advisory_and_does_not_duplicate_multiwarehouse_demand(
    tmp_path,
) -> None:
    db, inventory, service = _service(tmp_path)
    _balance(inventory, warehouse_id="warehouse-a", on_hand="12", reserved="2", inbound="3")
    _balance(inventory, warehouse_id="warehouse-b", on_hand="8", reserved="1", inbound="2")

    first = service.create_plan(TENANT, "forecast-run-planning", _policy())
    replay = service.create_plan(TENANT, "forecast-run-planning", _policy())

    assert first == replay
    assert first["selected_quantile"] == "p80"
    assert first["available"] == "17"
    assert first["inbound"] == "5"
    assert first["future_supply"] == "22"
    assert first["inventory_as_of"] == "2026-08-12T00:00:00+00:00"
    assert first["forecast_evidence"]["anomalies"] == []
    assert first["forecast_evidence"]["demand_policy_version"] == "demand-v1"
    assert first["forecast_evidence"]["forecast_policy_version"] == "forecast-v1"
    assert first["planning_policy"]["policy_version"] == "inventory-plan-v1"
    assert first["planning_policy"]["minimum_safety_stock"] == "3"
    assert first["lead_time_demand"] == "10"
    assert first["lead_review_demand"] == "20"
    assert first["reorder_point"] == "13"
    assert first["target_stock"] == "23"
    assert first["recommended_order_qty"] == "12"
    assert first["overstock_risk"] is False
    assert [step["step"] for step in first["calculation_steps"]] == [
        "inventory_aggregation", "quantile_demand", "minimum_safety_stock",
        "minimum_order_quantity", "order_multiple", "maximum_stock_days",
    ]
    assert first["calculation_steps"][3] == {
        "step": "minimum_order_quantity", "input": "1", "minimum": "10", "output": "10",
    }
    assert first["calculation_steps"][4] == {
        "step": "order_multiple", "input": "10", "multiple": "6",
        "rounding": "ceiling", "output": "12",
    }
    assert first["stockout_dates"] == {
        "p50": "2026-09-06", "p80": "2026-09-05", "p95": "2026-09-04",
    }
    assert first["allocation_boundary"] == {
        "demand_scope": "store_sku",
        "supply_scope": "store_aggregate",
        "warehouse_ids": ["warehouse-a", "warehouse-b"],
        "demand_copy_count": 1,
        "warehouse_allocation": "not_computed",
    }
    assert first["action_mode"] == "advisory_only"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM inventory_plans").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="inventory_plan_immutable"):
            conn.execute(
                "UPDATE inventory_plans SET recommended_order_qty='999' WHERE plan_id=?",
                (first["plan_id"],),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="inventory_planning_policy_immutable"
        ):
            conn.execute(
                "UPDATE inventory_planning_policies SET service_level='0.95' WHERE policy_id=?",
                (first["planning_policy_id"],),
            )


def test_plan_creation_never_mutates_inventory_facts(tmp_path) -> None:
    _db, inventory, service = _service(tmp_path)
    _balance(inventory, warehouse_id="warehouse-a", on_hand="12", reserved="2", inbound="3")
    before = inventory.list_balances(TENANT, store_id=STORE, sku_id=SKU)

    service.create_plan(TENANT, "forecast-run-planning", _policy())

    assert inventory.list_balances(TENANT, store_id=STORE, sku_id=SKU) == before


def test_policy_scope_conflict_tenant_isolation_and_warehouse_boundary(tmp_path) -> None:
    _db, inventory, service = _service(tmp_path)
    _balance(inventory, warehouse_id="warehouse-a", on_hand="12", reserved="2", inbound="3")
    _balance(inventory, warehouse_id="warehouse-b", on_hand="8", reserved="1", inbound="2")

    plan = service.create_plan(
        TENANT,
        "forecast-run-planning",
        _policy(warehouse_id="warehouse-a", service_level=Decimal("0.95")),
    )

    assert plan["available"] == "10"
    assert plan["selected_quantile"] == "p95"
    assert plan["lead_time_demand"] == "12"
    assert plan["allocation_boundary"]["supply_scope"] == "warehouse_supply_location"
    assert plan["allocation_boundary"]["demand_copy_count"] == 1
    with pytest.raises(InventoryPlanningError, match="inventory_plan_not_found"):
        service.get_plan("other-tenant", plan["plan_id"])
    with pytest.raises(InventoryPlanningError, match="planning_forecast_unavailable"):
        service.create_plan("other-tenant", "forecast-run-planning", _policy())
    with pytest.raises(InventoryPlanningError, match="planning_policy_version_conflict"):
        service.create_plan(
            TENANT,
            "forecast-run-planning",
            _policy(
                warehouse_id="warehouse-a",
                service_level=Decimal("0.95"),
                supplier_lead_days=3,
            ),
        )


def test_maximum_stock_cap_and_invalid_inputs_fail_explicitly(tmp_path) -> None:
    _db, inventory, service = _service(tmp_path)
    _balance(inventory, warehouse_id="warehouse-a", on_hand="0", reserved="0", inbound="0")

    capped = service.create_plan(
        TENANT, "forecast-run-planning", _policy(maximum_stock_days=4)
    )
    assert capped["target_stock"] == "23"
    assert capped["calculation_steps"][-2]["output"] == "24"
    assert capped["maximum_stock"] == "23"
    assert capped["recommended_order_qty"] == "18"
    assert capped["overstock_risk"] is False
    assert capped["calculation_steps"][-1] == {
        "step": "maximum_stock_days", "input": "24", "maximum_stock": "23",
        "capacity": "23", "rounding": "floor_to_order_multiple", "output": "18",
    }

    _balance(
        inventory,
        warehouse_id="warehouse-a",
        on_hand="30",
        reserved="0",
        inbound="0",
        source_updated_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )
    overstock = service.create_plan(
        TENANT, "forecast-run-planning", _policy(maximum_stock_days=4)
    )
    assert overstock["plan_id"] != capped["plan_id"]
    assert overstock["overstock_risk"] is True
    assert service.get_plan(TENANT, capped["plan_id"]) == capped

    with pytest.raises(InventoryPlanningError, match="planning_forecast_horizon_insufficient"):
        service.create_plan(
            TENANT,
            "forecast-run-planning",
            _policy(policy_version="inventory-plan-long-v1", maximum_stock_days=11),
        )
    service.forecasts.run["points"][0]["p80"] = 3.0
    with pytest.raises(InventoryPlanningError, match="planning_forecast_quantiles_invalid"):
        service.create_plan(
            TENANT,
            "forecast-run-planning",
            _policy(policy_version="inventory-plan-invalid-quantile-v1"),
        )
    service.forecasts.run["points"][0]["p80"] = 5.0
    _balance(
        inventory,
        warehouse_id="warehouse-a",
        on_hand="1",
        reserved="0",
        inbound="0",
        connector_id="duplicate-feed",
    )
    with pytest.raises(InventoryPlanningError, match="planning_inventory_snapshot_ambiguous"):
        service.create_plan(
            TENANT,
            "forecast-run-planning",
            _policy(policy_version="inventory-plan-ambiguous-v1"),
        )


def test_operations_wires_planning_to_public_forecast_and_inventory_services(tmp_path) -> None:
    db = Database(tmp_path / "operations-planning.sqlite3")
    db.initialize()
    operations = OperationsService(db)

    assert isinstance(operations.inventory_plans, InventoryPlanningService)
    assert operations.inventory_plans.forecasts is operations.forecast_runs
    assert operations.inventory_plans.inventory is operations.inventory
