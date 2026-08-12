from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..database import Database, utc_now


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _number(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InventoryPlanningError("planning_number_invalid") from exc
    if not result.is_finite():
        raise InventoryPlanningError("planning_number_invalid")
    return result


def _text(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


class InventoryPlanningError(ValueError):
    """Raised when a deterministic inventory plan cannot be built or read safely."""


SERVICE_LEVEL_QUANTILES = {
    Decimal("0.50"): "p50",
    Decimal("0.80"): "p80",
    Decimal("0.95"): "p95",
}
INVENTORY_SNAPSHOT_SPREAD_LIMIT_HOURS = 24
PLANNING_EVIDENCE_MAX_AGE_HOURS = 48
INVENTORY_SNAPSHOT_FIELDS = (
    "id", "connector_id", "store_id", "warehouse_id", "sku_id",
    "on_hand", "reserved", "inbound", "source_id", "source_updated_at", "version",
)


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

    @field_validator("service_level")
    @classmethod
    def supported_service_level(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("planning_policy_number_invalid")
        if value not in SERVICE_LEVEL_QUANTILES:
            raise ValueError("planning_service_level_unsupported")
        return value

    @field_validator("minimum_order_qty", "order_multiple", "minimum_safety_stock")
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
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.db = db
        self.forecasts = forecasts
        self.inventory = inventory
        self._clock = clock

    def resolve_policy(
        self,
        tenant_id: str,
        *,
        store_id: str,
        sku_id: str,
        warehouse_id: str | None = None,
    ) -> InventoryPlanningPolicy | None:
        with self.db.connect() as conn:
            if warehouse_id is None:
                row = conn.execute(
                    """SELECT * FROM inventory_planning_policies
                    WHERE tenant_id=? AND store_id=? AND sku_id=?
                      AND warehouse_id IS NULL
                    ORDER BY active_from DESC, created_at DESC,
                             rowid DESC LIMIT 1""",
                    (tenant_id, store_id, sku_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT * FROM inventory_planning_policies
                    WHERE tenant_id=? AND store_id=? AND sku_id=?
                      AND (warehouse_id=? OR warehouse_id IS NULL)
                    ORDER BY CASE WHEN warehouse_id=? THEN 0 ELSE 1 END,
                             active_from DESC, created_at DESC,
                             rowid DESC LIMIT 1""",
                    (tenant_id, store_id, sku_id, warehouse_id, warehouse_id),
                ).fetchone()
        if row is None:
            return None
        try:
            return InventoryPlanningPolicy(
                warehouse_id=warehouse_id,
                supplier_lead_days=row["supplier_lead_days"],
                review_period_days=row["review_period_days"],
                service_level=row["service_level"],
                minimum_order_qty=row["minimum_order_qty"],
                order_multiple=row["order_multiple"],
                minimum_safety_stock=row["minimum_safety_stock"],
                maximum_stock_days=row["maximum_stock_days"],
                policy_version=row["policy_version"],
            )
        except (TypeError, ValueError) as exc:
            raise InventoryPlanningError("planning_policy_evidence_invalid") from exc

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
        try:
            store_id, sku_id = str(forecast["store_id"]), str(forecast["sku_id"])
            raw_points = forecast["points"]
        except (KeyError, TypeError) as exc:
            raise InventoryPlanningError("planning_forecast_evidence_invalid") from exc
        balances = self.inventory.list_balances(
            tenant_id, store_id=store_id, sku_id=sku_id
        )
        snapshot, snapshot_times = self._validated_snapshot(
            balances, store_id=store_id, sku_id=sku_id
        )
        if policy.warehouse_id is not None:
            snapshot = [
                item for item in snapshot
                if item["warehouse_id"] == policy.warehouse_id
            ]
            snapshot_times = [
                self._snapshot_time(item["source_updated_at"]) for item in snapshot
            ]
        if not snapshot:
            raise InventoryPlanningError("planning_inventory_snapshot_not_found")
        warehouses = [str(item["warehouse_id"]) for item in snapshot]
        if len(warehouses) != len(set(warehouses)):
            raise InventoryPlanningError("planning_inventory_snapshot_ambiguous")
        inventory_as_of = min(snapshot_times).isoformat()
        created_at = self._clock()
        now = self._evidence_time(created_at, "planning_clock_invalid")
        calculation = self._calculate(raw_points, snapshot, policy)
        quality_issues, assumptions = self._quality_evidence(
            forecast, snapshot_times, calculation, now=now
        )
        calculation["plan_quality"] = "degraded" if quality_issues else "standard"
        calculation["quality_issues"] = quality_issues
        calculation["assumptions"] = assumptions
        policy_evidence = self._policy_evidence(policy)
        forecast_evidence = {
            key: forecast.get(key)
            for key in (
                "run_id", "data_hash", "training_start", "training_end",
                "created_at",
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
                    "forecast": {**forecast_evidence, "points": raw_points},
                    "inventory_snapshot_hash": inventory_hash,
                    "policy": policy_evidence,
                    "quality_evidence": {
                        "plan_quality": calculation["plan_quality"],
                        "quality_issues": quality_issues,
                        "assumptions": assumptions,
                    },
                }
            ).encode()
        ).hexdigest()
        plan_id = "inventory-plan-" + uuid.uuid5(
            uuid.NAMESPACE_URL, f"{tenant_id}/{input_hash}"
        ).hex
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            policy_id, _write_status = self._ensure_policy(
                conn,
                tenant_id,
                store_id,
                sku_id,
                policy,
                policy_evidence,
                created_at,
                allow_inheritance=True,
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
                        on_hand, reserved, inbound, available, reservation_shortfall,
                        future_supply,
                        lead_time_demand, lead_review_demand, reorder_point,
                        target_stock, maximum_stock, recommended_order_qty,
                        quantity_status, quantity_reason, stockout_dates_json,
                        risk_level, risk_evidence_json, overstock_risk,
                        plan_quality, quality_issues_json, assumptions_json,
                        allocation_boundary_json, calculation_steps_json,
                        action_mode, input_hash, created_at
                    ) VALUES (
                        :plan_id, :tenant_id, :store_id, :sku_id, :warehouse_id,
                        :forecast_run_id, :planning_policy_id, :planning_policy_version,
                        :inventory_snapshot_json, :inventory_snapshot_hash,
                        :inventory_as_of, :forecast_evidence_json, :selected_quantile,
                        :on_hand, :reserved, :inbound, :available, :reservation_shortfall,
                        :future_supply, :lead_time_demand, :lead_review_demand,
                        :reorder_point, :target_stock, :maximum_stock,
                        :recommended_order_qty, :quantity_status, :quantity_reason,
                        :stockout_dates_json, :risk_level, :risk_evidence_json,
                        :overstock_risk, :plan_quality, :quality_issues_json,
                        :assumptions_json, :allocation_boundary_json,
                        :calculation_steps_json, 'advisory_only', :input_hash, :created_at
                    )
                    """,
                    {
                        "plan_id": plan_id,
                        "tenant_id": tenant_id,
                        "store_id": store_id,
                        "sku_id": sku_id,
                        "warehouse_id": policy.warehouse_id,
                        "forecast_run_id": forecast_run_id,
                        "planning_policy_id": policy_id,
                        "planning_policy_version": policy.policy_version,
                        "inventory_snapshot_json": _json(snapshot),
                        "inventory_snapshot_hash": inventory_hash,
                        "inventory_as_of": inventory_as_of,
                        "forecast_evidence_json": _json(forecast_evidence),
                        "selected_quantile": calculation["selected_quantile"],
                        "on_hand": calculation["on_hand"],
                        "reserved": calculation["reserved"],
                        "inbound": calculation["inbound"],
                        "available": calculation["available"],
                        "reservation_shortfall": calculation["reservation_shortfall"],
                        "future_supply": calculation["future_supply"],
                        "lead_time_demand": calculation["lead_time_demand"],
                        "lead_review_demand": calculation["lead_review_demand"],
                        "reorder_point": calculation["reorder_point"],
                        "target_stock": calculation["target_stock"],
                        "maximum_stock": calculation["maximum_stock"],
                        "recommended_order_qty": calculation["recommended_order_qty"],
                        "quantity_status": calculation["quantity_status"],
                        "quantity_reason": calculation["quantity_reason"],
                        "stockout_dates_json": _json(calculation["stockout_dates"]),
                        "risk_level": calculation["risk_level"],
                        "risk_evidence_json": _json(calculation["risk_evidence"]),
                        "overstock_risk": calculation["overstock_risk"],
                        "plan_quality": calculation["plan_quality"],
                        "quality_issues_json": _json(calculation["quality_issues"]),
                        "assumptions_json": _json(calculation["assumptions"]),
                        "allocation_boundary_json": _json(
                            calculation["allocation_boundary"]
                        ),
                        "calculation_steps_json": _json(
                            calculation["calculation_steps"]
                        ),
                        "input_hash": input_hash,
                        "created_at": created_at,
                    },
                )
            else:
                plan_id = str(existing["plan_id"])
        return self.get_plan(tenant_id, plan_id)

    def latest_plan(
        self,
        tenant_id: str,
        *,
        sku_id: str,
        store_id: str,
        warehouse_id: str | None = None,
    ) -> dict[str, Any]:
        conditions = ["tenant_id=?", "store_id=?", "sku_id=?"]
        params: list[Any] = [tenant_id, store_id, sku_id]
        if warehouse_id is None:
            conditions.append("warehouse_id IS NULL")
        else:
            conditions.append("warehouse_id=?")
            params.append(warehouse_id)
        with self.db.connect() as conn:
            row = conn.execute(
                f"""SELECT plan_id FROM inventory_plans
                WHERE {' AND '.join(conditions)}
                ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                tuple(params),
            ).fetchone()
        if row is None:
            raise InventoryPlanningError("inventory_plan_not_found")
        return self.get_plan(tenant_id, str(row["plan_id"]))

    def list_risks(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        sku_id: str | None = None,
        risk_level: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if store_id is not None:
            conditions.append("store_id=?")
            params.append(store_id)
        if sku_id is not None:
            conditions.append("sku_id=?")
            params.append(sku_id)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT plan_id, store_id, sku_id, COALESCE(warehouse_id, '') scope,
                risk_level
                FROM inventory_plans WHERE {' AND '.join(conditions)}
                ORDER BY created_at DESC, rowid DESC""",
                tuple(params),
            ).fetchall()
        latest: list[str] = []
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            key = (str(row["store_id"]), str(row["sku_id"]), str(row["scope"]))
            if key in seen:
                continue
            seen.add(key)
            if risk_level is not None and row["risk_level"] != risk_level:
                continue
            latest.append(str(row["plan_id"]))
            if len(latest) >= limit:
                break
        return [self.get_plan(tenant_id, plan_id) for plan_id in latest]

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
            ("risk_evidence_json", "risk_evidence"),
            ("quality_issues_json", "quality_issues"),
            ("assumptions_json", "assumptions"),
            ("allocation_boundary_json", "allocation_boundary"),
            ("calculation_steps_json", "calculation_steps"),
        ):
            result[exposed] = json.loads(result.pop(stored))
        return result

    @staticmethod
    def _snapshot_time(value: Any) -> datetime:
        return InventoryPlanningService._evidence_time(
            value, "planning_inventory_snapshot_invalid"
        )

    @staticmethod
    def _evidence_time(value: Any, error_code: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("timezone_required")
        except (TypeError, ValueError) as exc:
            raise InventoryPlanningError(error_code) from exc
        return parsed

    @classmethod
    def _validated_snapshot(
        cls,
        balances: list[dict[str, Any]],
        *,
        store_id: str,
        sku_id: str,
    ) -> tuple[list[dict[str, Any]], list[datetime]]:
        snapshot: list[dict[str, Any]] = []
        snapshot_times: list[datetime] = []
        try:
            for item in balances:
                row = {key: item[key] for key in INVENTORY_SNAPSHOT_FIELDS}
                if str(row["store_id"]) != store_id or str(row["sku_id"]) != sku_id:
                    raise InventoryPlanningError("planning_inventory_scope_mismatch")
                for key in ("on_hand", "reserved", "inbound"):
                    if _number(row[key]) < 0:
                        raise InventoryPlanningError(
                            "planning_inventory_snapshot_invalid"
                        )
                parsed = cls._snapshot_time(row["source_updated_at"])
                row["source_updated_at"] = parsed.isoformat()
                snapshot.append(row)
                snapshot_times.append(parsed)
        except InventoryPlanningError:
            raise
        except (KeyError, TypeError) as exc:
            raise InventoryPlanningError("planning_inventory_snapshot_invalid") from exc
        order = sorted(
            range(len(snapshot)),
            key=lambda index: (
                str(snapshot[index]["warehouse_id"]), str(snapshot[index]["id"])
            ),
        )
        return [snapshot[index] for index in order], [snapshot_times[index] for index in order]

    @classmethod
    def _quality_evidence(
        cls,
        forecast: dict[str, Any],
        snapshot_times: list[datetime],
        calculation: dict[str, Any],
        *,
        now: datetime,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        if forecast.get("status") == "degraded":
            issues.append({"code": "forecast_status_degraded", "status": "degraded"})
        anomalies = forecast.get("anomalies", [])
        if not isinstance(anomalies, list):
            raise InventoryPlanningError("planning_forecast_evidence_invalid")
        if anomalies:
            issues.append(
                {
                    "code": "forecast_anomalies_present",
                    "count": len(anomalies),
                    "anomaly_types": sorted(
                        {
                            str(item.get("anomaly_type", "unknown"))
                            for item in anomalies
                            if isinstance(item, dict)
                        }
                    ),
                }
            )
        if _number(calculation["reservation_shortfall"]) > 0:
            issues.append(
                {
                    "code": "reserved_exceeds_on_hand",
                    "reservation_shortfall": calculation["reservation_shortfall"],
                }
            )
        inbound = _number(calculation["inbound"])
        if inbound > 0:
            issues.append(
                {
                    "code": "inbound_eta_unavailable",
                    "inbound": calculation["inbound"],
                }
            )
        spread = max(snapshot_times) - min(snapshot_times)
        if spread > timedelta(hours=INVENTORY_SNAPSHOT_SPREAD_LIMIT_HOURS):
            issues.append(
                {
                    "code": "inventory_snapshot_time_spread",
                    "spread_seconds": int(spread.total_seconds()),
                    "limit_hours": INVENTORY_SNAPSHOT_SPREAD_LIMIT_HOURS,
                }
            )
        try:
            training_end = date.fromisoformat(str(forecast["training_end"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise InventoryPlanningError("planning_forecast_evidence_invalid") from exc
        oldest_snapshot_date = min(snapshot_times).date()
        if oldest_snapshot_date < training_end:
            issues.append(
                {
                    "code": "inventory_snapshot_precedes_forecast_training_end",
                    "oldest_snapshot_date": oldest_snapshot_date.isoformat(),
                    "forecast_training_end": training_end.isoformat(),
                }
            )
        stale_before = now - timedelta(hours=PLANNING_EVIDENCE_MAX_AGE_HOURS)
        oldest_snapshot = min(snapshot_times)
        if oldest_snapshot < stale_before:
            issues.append(
                {
                    "code": "inventory_snapshot_stale",
                    "inventory_as_of": oldest_snapshot.isoformat(),
                    "limit_hours": PLANNING_EVIDENCE_MAX_AGE_HOURS,
                }
            )
        try:
            forecast_created_at = cls._evidence_time(
                forecast["created_at"], "planning_forecast_evidence_invalid"
            )
        except KeyError as exc:
            raise InventoryPlanningError("planning_forecast_evidence_invalid") from exc
        if forecast_created_at < stale_before:
            issues.append(
                {
                    "code": "forecast_run_stale",
                    "forecast_created_at": forecast_created_at.isoformat(),
                    "limit_hours": PLANNING_EVIDENCE_MAX_AGE_HOURS,
                }
            )
        if training_end < stale_before.date():
            issues.append(
                {
                    "code": "forecast_training_data_stale",
                    "forecast_training_end": training_end.isoformat(),
                    "limit_hours": PLANNING_EVIDENCE_MAX_AGE_HOURS,
                }
            )
        assumptions = {
            "available_inventory": {
                "formula": "max(0,on_hand-reserved)",
                "reservation_shortfall_formula": "max(0,reserved-on_hand)",
            },
            "inbound_availability": (
                {
                    "mode": "assumed_available_day_0",
                    "eta_available": False,
                    "effect": "plan_quality_degraded",
                }
                if inbound > 0
                else {
                    "mode": "no_inbound_quantity",
                    "eta_available": False,
                    "effect": "none",
                }
            ),
            "inventory_snapshot_spread_limit_hours": (
                INVENTORY_SNAPSHOT_SPREAD_LIMIT_HOURS
            ),
            "evidence_max_age_hours": PLANNING_EVIDENCE_MAX_AGE_HOURS,
            "service_level_tiers": {
                _text(level): quantile
                for level, quantile in SERVICE_LEVEL_QUANTILES.items()
            },
            "minimum_safety_stock": "additive_before_order_constraints",
        }
        return issues, assumptions

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
        *,
        allow_inheritance: bool = False,
    ) -> tuple[str, str]:
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
        expected = tuple(evidence[key] for key in fields)
        if existing is not None:
            if tuple(existing[key] for key in fields) != expected:
                raise InventoryPlanningError("planning_policy_version_conflict")
            return str(existing["policy_id"]), "idempotent"
        if allow_inheritance and policy.warehouse_id is not None:
            inherited = conn.execute(
                """SELECT * FROM inventory_planning_policies
                WHERE tenant_id=? AND store_id=? AND sku_id=?
                  AND warehouse_id IS NULL AND policy_version=?""",
                (tenant_id, store_id, sku_id, policy.policy_version),
            ).fetchone()
            if (
                inherited is not None
                and tuple(inherited[key] for key in fields) == expected
            ):
                return str(inherited["policy_id"]), "inherited"
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
        return policy_id, "created"

    @staticmethod
    def _calculate(
        raw_points: list[dict[str, Any]],
        snapshot: list[dict[str, Any]],
        policy: InventoryPlanningPolicy,
    ) -> dict[str, Any]:
        try:
            points = sorted(raw_points, key=lambda item: str(item["forecast_date"]))
        except (KeyError, TypeError) as exc:
            raise InventoryPlanningError("planning_forecast_points_invalid") from exc
        required_days = max(
            policy.supplier_lead_days + policy.review_period_days,
            policy.maximum_stock_days,
        )
        if len(points) < required_days:
            raise InventoryPlanningError("planning_forecast_horizon_insufficient")
        try:
            dates = [date.fromisoformat(str(item["forecast_date"])) for item in points]
        except (KeyError, TypeError, ValueError) as exc:
            raise InventoryPlanningError("planning_forecast_dates_invalid") from exc
        if len(dates) != len(set(dates)) or any(
            current != previous + timedelta(days=1)
            for previous, current in zip(dates, dates[1:])
        ):
            raise InventoryPlanningError("planning_forecast_dates_invalid")
        try:
            for point in points:
                p50, p80, p95 = (
                    _number(point[key]) for key in ("p50", "p80", "p95")
                )
                if p50 < 0 or not p50 <= p80 <= p95:
                    raise InventoryPlanningError("planning_forecast_quantiles_invalid")
        except (KeyError, TypeError) as exc:
            raise InventoryPlanningError("planning_forecast_points_invalid") from exc
        quantile = SERVICE_LEVEL_QUANTILES[policy.service_level]
        demand = [_number(point[quantile]) for point in points]
        on_hand = sum((_number(item["on_hand"]) for item in snapshot), Decimal("0"))
        reserved = sum((_number(item["reserved"]) for item in snapshot), Decimal("0"))
        inbound = sum((_number(item["inbound"]) for item in snapshot), Decimal("0"))
        net_available = on_hand - reserved
        reservation_shortfall = max(Decimal("0"), -net_available)
        available = max(Decimal("0"), net_available)
        future_supply = available + inbound
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
        recommended: Decimal | None = after_multiple
        if recommended > capacity:
            recommended = (
                (capacity / policy.order_multiple).to_integral_value(rounding=ROUND_FLOOR)
                * policy.order_multiple
            )
            if Decimal("0") < recommended < policy.minimum_order_qty:
                recommended = Decimal("0")
        stockout_dates, stockout_days = InventoryPlanningService._stockout_evidence(
            points, future_supply
        )
        cumulative_demand = Decimal("0")
        inventory_projection = []
        for point, value in zip(points, demand):
            cumulative_demand += value
            inventory_projection.append(
                {
                    "forecast_date": str(point["forecast_date"]),
                    "selected_quantile": quantile,
                    "demand": _text(value),
                    "cumulative_demand": _text(cumulative_demand),
                    "projected_inventory": _text(future_supply - cumulative_demand),
                }
            )
        overstock_risk = future_supply > maximum_stock
        selected_stockout_day = stockout_days[quantile]
        if future_supply <= 0:
            risk_level, risk_reason = "critical", "no_future_supply"
        elif (
            selected_stockout_day is not None
            and selected_stockout_day <= policy.supplier_lead_days
        ):
            risk_level = "critical"
            risk_reason = "selected_quantile_depletion_within_lead_time"
        elif (
            selected_stockout_day is not None
            and selected_stockout_day
            <= policy.supplier_lead_days + policy.review_period_days
        ):
            risk_level = "high"
            risk_reason = "selected_quantile_depletion_within_review_period"
        elif selected_stockout_day is not None:
            risk_level = "medium"
            risk_reason = "selected_quantile_depletion_after_review_within_horizon"
        elif overstock_risk:
            risk_level, risk_reason = "medium", "future_supply_above_maximum_stock"
        else:
            risk_level = "low"
            risk_reason = "no_selected_quantile_depletion_or_overstock"
        quantity_withheld = policy.warehouse_id is not None
        if quantity_withheld:
            recommended = None
            quantity_status = "withheld"
            quantity_reason = "warehouse_allocation_not_computed"
        else:
            quantity_status = "advisory"
            quantity_reason = None
        values = {
            "selected_quantile": quantile,
            "on_hand": _text(on_hand), "reserved": _text(reserved),
            "inbound": _text(inbound), "available": _text(available),
            "reservation_shortfall": _text(reservation_shortfall),
            "future_supply": _text(future_supply), "lead_time_demand": _text(lead),
            "lead_review_demand": _text(lead_review), "reorder_point": _text(reorder),
            "target_stock": _text(target), "maximum_stock": _text(maximum_stock),
            "recommended_order_qty": None if recommended is None else _text(recommended),
            "quantity_status": quantity_status, "quantity_reason": quantity_reason,
            "stockout_dates": stockout_dates,
            "risk_level": risk_level, "overstock_risk": overstock_risk,
        }
        values["risk_evidence"] = {
            "basis": "selected_service_quantile_time_to_stockout",
            "selected_quantile": quantile,
            "selected_quantile_stockout_day": selected_stockout_day,
            "stockout_day_by_quantile": stockout_days,
            "supplier_lead_days": policy.supplier_lead_days,
            "review_period_days": policy.review_period_days,
            "classification_reason": risk_reason,
            "inventory_projection": inventory_projection,
            "scope": (
                "warehouse_supply_diagnostic_only"
                if quantity_withheld else "store_aggregate"
            ),
        }
        values["allocation_boundary"] = {
            "demand_scope": "store_sku",
            "supply_scope": (
                "warehouse_supply_location" if policy.warehouse_id else "store_aggregate"
            ),
            "warehouse_ids": [str(item["warehouse_id"]) for item in snapshot],
            "demand_copy_count": 1,
            "warehouse_allocation": "not_computed",
            "quantity_recommendation": (
                "withheld" if quantity_withheld else "store_aggregate_only"
            ),
        }
        calculation_steps = [
            {
                "step": "inventory_aggregation",
                "on_hand": _text(on_hand), "reserved": _text(reserved),
                "inbound": _text(inbound), "available": _text(available),
                "reservation_shortfall": _text(reservation_shortfall),
                "output": _text(future_supply),
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
        ]
        if quantity_withheld:
            calculation_steps.extend(
                {
                    "step": step,
                    "status": "not_applied",
                    "reason": "warehouse_allocation_not_computed",
                    "output": None,
                }
                for step in (
                    "minimum_order_quantity", "order_multiple", "maximum_stock_days"
                )
            )
        else:
            calculation_steps.extend(
                [
                    {
                        "step": "minimum_order_quantity", "input": _text(raw_order),
                        "minimum": _text(policy.minimum_order_qty),
                        "output": _text(after_moq),
                    },
                    {
                        "step": "order_multiple", "input": _text(after_moq),
                        "multiple": _text(policy.order_multiple), "rounding": "ceiling",
                        "output": _text(after_multiple),
                    },
                    {
                        "step": "maximum_stock_days", "input": _text(after_multiple),
                        "maximum_stock": _text(maximum_stock),
                        "capacity": _text(capacity),
                        "rounding": "floor_to_order_multiple",
                        "output": _text(recommended),
                    },
                ]
            )
        values["calculation_steps"] = calculation_steps
        return values

    @staticmethod
    def _stockout_evidence(
        points: list[dict[str, Any]], future_supply: Decimal
    ) -> tuple[dict[str, str | None], dict[str, int | None]]:
        dates: dict[str, str | None] = {}
        days: dict[str, int | None] = {}
        for quantile in ("p50", "p80", "p95"):
            cumulative = Decimal("0")
            depletion_date = None
            depletion_day = None
            for day_number, point in enumerate(points, start=1):
                cumulative += _number(point[quantile])
                if future_supply <= 0 or cumulative >= future_supply:
                    depletion_date = str(point["forecast_date"])
                    depletion_day = day_number
                    break
            dates[quantile] = depletion_date
            days[quantile] = depletion_day
        return dates, days
