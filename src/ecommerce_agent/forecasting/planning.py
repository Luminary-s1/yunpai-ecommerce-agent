from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..database import Database, utc_now


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _number(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise InventoryPlanningError("planning_number_invalid")
    return result


def _text(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


class InventoryPlanningError(ValueError):
    """Raised when a deterministic inventory plan cannot be built or read safely."""


class InventoryPlanningPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    warehouse_id: str | None = Field(default=None, min_length=1, max_length=128)
    supplier_lead_days: int = Field(default=7, ge=0, le=30)
    review_period_days: int = Field(default=7, ge=1, le=30)
    service_level: Decimal = Field(default=Decimal("0.80"), ge=Decimal("0.50"), le=Decimal("0.95"))
    minimum_order_qty: Decimal = Field(default=Decimal("0"), ge=0)
    order_multiple: Decimal = Field(default=Decimal("1"), gt=0)
    minimum_safety_stock: Decimal = Field(default=Decimal("0"), ge=0)
    maximum_stock_days: int = Field(default=30, ge=1, le=30)
    policy_version: str = Field(default="inventory-plan-v1", min_length=1, max_length=128)

    @field_validator(
        "service_level", "minimum_order_qty", "order_multiple", "minimum_safety_stock"
    )
    @classmethod
    def finite_decimals(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("planning_policy_number_invalid")
        return value


class ForecastRunReader(Protocol):
    def get_run(self, tenant_id: str, run_id: str) -> dict[str, Any]: ...


class InventoryBalanceReader(Protocol):
    def list_balances(
        self, tenant_id: str, *, store_id: str | None = None, sku_id: str | None = None
    ) -> list[dict[str, Any]]: ...


class InventoryPlanningService:
    """Persist advisory plans derived from immutable forecasts and inventory snapshots."""

    def __init__(
        self,
        db: Database,
        *,
        forecasts: ForecastRunReader,
        inventory: InventoryBalanceReader,
    ) -> None:
        self.db = db
        self.forecasts = forecasts
        self.inventory = inventory

    def create_plan(
        self,
        tenant_id: str,
        forecast_run_id: str,
        policy: InventoryPlanningPolicy,
    ) -> dict[str, Any]:
        try:
            forecast = self.forecasts.get_run(tenant_id, forecast_run_id)
        except ValueError as exc:
            raise InventoryPlanningError(f"planning_forecast_unavailable:{exc}") from exc
        if forecast.get("status") not in {"completed", "degraded"}:
            raise InventoryPlanningError("planning_forecast_not_usable")
        if (
            forecast.get("tenant_id") != tenant_id
            or forecast.get("run_id") != forecast_run_id
        ):
            raise InventoryPlanningError("planning_forecast_scope_mismatch")
        store_id, sku_id = str(forecast["store_id"]), str(forecast["sku_id"])
        balances = self.inventory.list_balances(
            tenant_id, store_id=store_id, sku_id=sku_id
        )
        if policy.warehouse_id is not None:
            balances = [
                item for item in balances if item["warehouse_id"] == policy.warehouse_id
            ]
        if not balances:
            raise InventoryPlanningError("planning_inventory_snapshot_not_found")
        if any(
            item["store_id"] != store_id or item["sku_id"] != sku_id
            for item in balances
        ):
            raise InventoryPlanningError("planning_inventory_scope_mismatch")
        warehouses = [str(item["warehouse_id"]) for item in balances]
        if len(warehouses) != len(set(warehouses)):
            raise InventoryPlanningError("planning_inventory_snapshot_ambiguous")
        snapshot = [
            {
                key: item[key]
                for key in (
                    "id", "connector_id", "store_id", "warehouse_id", "sku_id",
                    "on_hand", "reserved", "inbound", "source_id",
                    "source_updated_at", "version",
                )
            }
            for item in sorted(balances, key=lambda row: (row["warehouse_id"], row["id"]))
        ]
        inventory_as_of = min(str(item["source_updated_at"]) for item in snapshot)
        calculation = self._calculate(forecast["points"], snapshot, policy)
        policy_evidence = self._policy_evidence(policy)
        forecast_evidence = {
            key: forecast.get(key)
            for key in (
                "run_id", "data_hash", "training_start", "training_end",
                "demand_policy_version", "forecast_policy_version", "status",
                "champion_model", "champion_reason", "model_version", "wape",
                "bias", "smape", "rmse",
            )
        }
        forecast_evidence["anomalies"] = forecast.get("anomalies", [])
        inventory_hash = hashlib.sha256(_json(snapshot).encode()).hexdigest()
        input_hash = hashlib.sha256(
            _json(
                {
                    "forecast": {**forecast_evidence, "points": forecast["points"]},
                    "inventory_snapshot_hash": inventory_hash,
                    "policy": policy_evidence,
                }
            ).encode()
        ).hexdigest()
        plan_id = "inventory-plan-" + uuid.uuid5(
            uuid.NAMESPACE_URL, f"{tenant_id}/{input_hash}"
        ).hex
        created_at = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            policy_id = self._ensure_policy(
                conn, tenant_id, store_id, sku_id, policy, policy_evidence, created_at
            )
            existing = conn.execute(
                "SELECT plan_id FROM inventory_plans WHERE tenant_id=? AND input_hash=?",
                (tenant_id, input_hash),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO inventory_plans(
                        plan_id, tenant_id, store_id, sku_id, warehouse_id,
                        forecast_run_id, planning_policy_id, planning_policy_version,
                        inventory_snapshot_json, inventory_snapshot_hash,
                        inventory_as_of, forecast_evidence_json, selected_quantile,
                        on_hand, reserved, inbound, available, future_supply,
                        lead_time_demand, lead_review_demand, reorder_point,
                        target_stock, maximum_stock, recommended_order_qty,
                        stockout_dates_json, risk_level, overstock_risk,
                        allocation_boundary_json, calculation_steps_json,
                        action_mode, input_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'advisory_only', ?, ?)
                    """,
                    (
                        plan_id, tenant_id, store_id, sku_id, policy.warehouse_id,
                        forecast_run_id, policy_id, policy.policy_version, _json(snapshot),
                        inventory_hash, inventory_as_of, _json(forecast_evidence),
                        calculation["selected_quantile"], calculation["on_hand"],
                        calculation["reserved"], calculation["inbound"],
                        calculation["available"], calculation["future_supply"],
                        calculation["lead_time_demand"], calculation["lead_review_demand"],
                        calculation["reorder_point"], calculation["target_stock"],
                        calculation["maximum_stock"],
                        calculation["recommended_order_qty"],
                        _json(calculation["stockout_dates"]), calculation["risk_level"],
                        calculation["overstock_risk"],
                        _json(calculation["allocation_boundary"]),
                        _json(calculation["calculation_steps"]), input_hash, created_at,
                    ),
                )
            else:
                plan_id = str(existing["plan_id"])
        return self.get_plan(tenant_id, plan_id)

    def get_plan(self, tenant_id: str, plan_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM inventory_plans WHERE tenant_id=? AND plan_id=?",
                (tenant_id, plan_id),
            ).fetchone()
            policy = None if row is None else conn.execute(
                """SELECT * FROM inventory_planning_policies
                WHERE tenant_id=? AND policy_id=?""",
                (tenant_id, row["planning_policy_id"]),
            ).fetchone()
        if row is None:
            raise InventoryPlanningError("inventory_plan_not_found")
        if policy is None:
            raise InventoryPlanningError("inventory_plan_policy_not_found")
        result = dict(row)
        result["overstock_risk"] = bool(result["overstock_risk"])
        result["planning_policy"] = {
            key: policy[key]
            for key in (
                "policy_id", "store_id", "sku_id", "warehouse_id",
                "supplier_lead_days", "review_period_days", "service_level",
                "minimum_order_qty", "order_multiple", "minimum_safety_stock",
                "maximum_stock_days", "policy_version", "active_from",
            )
        }
        for stored, exposed in (
            ("inventory_snapshot_json", "inventory_snapshot"),
            ("forecast_evidence_json", "forecast_evidence"),
            ("stockout_dates_json", "stockout_dates"),
            ("allocation_boundary_json", "allocation_boundary"),
            ("calculation_steps_json", "calculation_steps"),
        ):
            result[exposed] = json.loads(result.pop(stored))
        return result

    @staticmethod
    def _policy_evidence(policy: InventoryPlanningPolicy) -> dict[str, Any]:
        return {
            "warehouse_id": policy.warehouse_id,
            "supplier_lead_days": policy.supplier_lead_days,
            "review_period_days": policy.review_period_days,
            "service_level": _text(policy.service_level),
            "minimum_order_qty": _text(policy.minimum_order_qty),
            "order_multiple": _text(policy.order_multiple),
            "minimum_safety_stock": _text(policy.minimum_safety_stock),
            "maximum_stock_days": policy.maximum_stock_days,
            "policy_version": policy.policy_version,
        }

    @staticmethod
    def _ensure_policy(
        conn: Any,
        tenant_id: str,
        store_id: str,
        sku_id: str,
        policy: InventoryPlanningPolicy,
        evidence: dict[str, Any],
        created_at: str,
    ) -> str:
        existing = conn.execute(
            """SELECT * FROM inventory_planning_policies
            WHERE tenant_id=? AND store_id=? AND sku_id=?
              AND COALESCE(warehouse_id, '')=COALESCE(?, '') AND policy_version=?""",
            (tenant_id, store_id, sku_id, policy.warehouse_id, policy.policy_version),
        ).fetchone()
        fields = (
            "supplier_lead_days", "review_period_days", "service_level",
            "minimum_order_qty", "order_multiple", "minimum_safety_stock",
            "maximum_stock_days",
        )
        if existing is not None:
            if tuple(existing[key] for key in fields) != tuple(evidence[key] for key in fields):
                raise InventoryPlanningError("planning_policy_version_conflict")
            return str(existing["policy_id"])
        policy_id = "inventory-policy-" + uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{tenant_id}/{store_id}/{sku_id}/{policy.warehouse_id or '*'}/{policy.policy_version}",
        ).hex
        conn.execute(
            """
            INSERT INTO inventory_planning_policies(
                policy_id, tenant_id, store_id, sku_id, warehouse_id,
                supplier_lead_days, review_period_days, service_level,
                minimum_order_qty, order_multiple, minimum_safety_stock,
                maximum_stock_days, policy_version, active_from, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy_id, tenant_id, store_id, sku_id, policy.warehouse_id,
                *(evidence[key] for key in fields), policy.policy_version,
                created_at, created_at,
            ),
        )
        return policy_id

    @staticmethod
    def _calculate(
        raw_points: list[dict[str, Any]],
        snapshot: list[dict[str, Any]],
        policy: InventoryPlanningPolicy,
    ) -> dict[str, Any]:
        points = sorted(raw_points, key=lambda item: str(item["forecast_date"]))
        required_days = max(
            policy.supplier_lead_days + policy.review_period_days,
            policy.maximum_stock_days,
        )
        if len(points) < required_days:
            raise InventoryPlanningError("planning_forecast_horizon_insufficient")
        dates = [date.fromisoformat(str(item["forecast_date"])) for item in points]
        if len(dates) != len(set(dates)) or any(
            current != previous + timedelta(days=1)
            for previous, current in zip(dates, dates[1:])
        ):
            raise InventoryPlanningError("planning_forecast_dates_invalid")
        for point in points:
            p50, p80, p95 = (_number(point[key]) for key in ("p50", "p80", "p95"))
            if p50 < 0 or not p50 <= p80 <= p95:
                raise InventoryPlanningError("planning_forecast_quantiles_invalid")
        quantile = "p50" if policy.service_level <= Decimal("0.50") else (
            "p80" if policy.service_level <= Decimal("0.80") else "p95"
        )
        demand = [_number(point[quantile]) for point in points]
        on_hand = sum((_number(item["on_hand"]) for item in snapshot), Decimal("0"))
        reserved = sum((_number(item["reserved"]) for item in snapshot), Decimal("0"))
        inbound = sum((_number(item["inbound"]) for item in snapshot), Decimal("0"))
        available, future_supply = on_hand - reserved, on_hand - reserved + inbound
        lead = sum(demand[: policy.supplier_lead_days], Decimal("0"))
        lead_review = sum(
            demand[: policy.supplier_lead_days + policy.review_period_days], Decimal("0")
        )
        reorder = lead + policy.minimum_safety_stock
        target = lead_review + policy.minimum_safety_stock
        raw_order = max(Decimal("0"), target - future_supply)
        after_moq = (
            max(raw_order, policy.minimum_order_qty) if raw_order > 0 else Decimal("0")
        )
        after_multiple = (
            (after_moq / policy.order_multiple).to_integral_value(rounding=ROUND_CEILING)
            * policy.order_multiple
            if after_moq > 0 else Decimal("0")
        )
        maximum_stock = (
            sum(demand[: policy.maximum_stock_days], Decimal("0"))
            + policy.minimum_safety_stock
        )
        capacity = max(Decimal("0"), maximum_stock - future_supply)
        recommended = after_multiple
        if recommended > capacity:
            recommended = (
                (capacity / policy.order_multiple).to_integral_value(rounding=ROUND_FLOOR)
                * policy.order_multiple
            )
            if Decimal("0") < recommended < policy.minimum_order_qty:
                recommended = Decimal("0")
        stockout_dates = InventoryPlanningService._stockout_dates(points, future_supply)
        overstock_risk = future_supply > maximum_stock
        risk_level = (
            "critical" if future_supply <= 0 or stockout_dates["p50"] else
            "high" if stockout_dates["p80"] else
            "medium" if stockout_dates["p95"] or overstock_risk else "low"
        )
        values = {
            "selected_quantile": quantile,
            "on_hand": _text(on_hand), "reserved": _text(reserved),
            "inbound": _text(inbound), "available": _text(available),
            "future_supply": _text(future_supply), "lead_time_demand": _text(lead),
            "lead_review_demand": _text(lead_review), "reorder_point": _text(reorder),
            "target_stock": _text(target), "maximum_stock": _text(maximum_stock),
            "recommended_order_qty": _text(recommended), "stockout_dates": stockout_dates,
            "risk_level": risk_level, "overstock_risk": overstock_risk,
        }
        values["allocation_boundary"] = {
            "demand_scope": "store_sku",
            "supply_scope": (
                "warehouse_supply_location" if policy.warehouse_id else "store_aggregate"
            ),
            "warehouse_ids": [str(item["warehouse_id"]) for item in snapshot],
            "demand_copy_count": 1,
            "warehouse_allocation": "not_computed",
        }
        values["calculation_steps"] = [
            {
                "step": "inventory_aggregation",
                "on_hand": _text(on_hand), "reserved": _text(reserved),
                "inbound": _text(inbound), "output": _text(future_supply),
            },
            {
                "step": "quantile_demand", "quantile": quantile,
                "lead_days": policy.supplier_lead_days,
                "review_days": policy.review_period_days,
                "lead_output": _text(lead), "output": _text(lead_review),
            },
            {
                "step": "minimum_safety_stock", "lead_input": _text(lead),
                "input": _text(lead_review),
                "minimum": _text(policy.minimum_safety_stock),
                "reorder_output": _text(reorder), "output": _text(target),
            },
            {
                "step": "minimum_order_quantity", "input": _text(raw_order),
                "minimum": _text(policy.minimum_order_qty), "output": _text(after_moq),
            },
            {
                "step": "order_multiple", "input": _text(after_moq),
                "multiple": _text(policy.order_multiple), "rounding": "ceiling",
                "output": _text(after_multiple),
            },
            {
                "step": "maximum_stock_days", "input": _text(after_multiple),
                "maximum_stock": _text(maximum_stock), "capacity": _text(capacity),
                "rounding": "floor_to_order_multiple", "output": _text(recommended),
            },
        ]
        return values

    @staticmethod
    def _stockout_dates(
        points: list[dict[str, Any]], future_supply: Decimal
    ) -> dict[str, str | None]:
        results: dict[str, str | None] = {}
        for quantile in ("p50", "p80", "p95"):
            cumulative = Decimal("0")
            depletion = None
            for point in points:
                cumulative += _number(point[quantile])
                if future_supply <= 0 or cumulative >= future_supply:
                    depletion = str(point["forecast_date"])
                    break
            results[quantile] = depletion
        return results
