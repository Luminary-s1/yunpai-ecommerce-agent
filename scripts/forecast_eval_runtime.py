from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Sequence

from ecommerce_agent.database import Database
from ecommerce_agent.forecasting import (
    ForecastEngine,
    ForecastPolicy,
    ForecastRunService,
    InventoryPlanningPolicy,
    InventoryPlanningService,
)


START_DATE = date(2026, 6, 1)
EVAL_NOW = datetime(2026, 8, 12, 12, tzinfo=UTC).isoformat()
ALLOWED_CALL_FIELDS = {
    "ForecastRunService.run": frozenset({"tenant_id", "store_id", "sku_id"}),
    "DemandFactReader.list_facts": frozenset({"tenant_id", "store_id", "sku_id"}),
    "ForecastEngine.evaluate": frozenset({"series"}),
    "InventoryPlanningService.create_plan": frozenset(
        {"tenant_id", "forecast_run_id", "planning_policy"}
    ),
    "InventoryBalanceReader.list_balances": frozenset(
        {"tenant_id", "store_id", "sku_id"}
    ),
}


def field_names(value: Any) -> set[str]:
    if isinstance(value, dict):
        names = {str(key) for key in value}
        for item in value.values():
            names.update(field_names(item))
        return names
    if isinstance(value, list):
        return {name for item in value for name in field_names(item)}
    return set()


def digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class FactSource:
    def __init__(self, rows: list[dict[str, Any]], trace: list[dict[str, Any]]):
        self.rows = rows
        self.trace = trace

    def list_facts(
        self, tenant_id: str, *, store_id: str, sku_id: str, **_kwargs: object
    ) -> list[dict[str, Any]]:
        self.trace.append(
            {
                "component": "DemandFactReader.list_facts",
                "argument_fields": ["tenant_id", "store_id", "sku_id"],
                "evidence_fields": sorted(field_names(self.rows)),
                "evidence_digest": digest(self.rows),
            }
        )
        return [
            dict(row)
            for row in self.rows
            if row["tenant_id"] == tenant_id
            and row["store_id"] == store_id
            and row["sku_id"] == sku_id
        ]


class RecordingEngine(ForecastEngine):
    def __init__(self, policy: ForecastPolicy, trace: list[dict[str, Any]]):
        super().__init__(policy=policy)
        self.trace = trace
        self.received_series: list[tuple[date, float | int | None]] = []
        self.last_evaluation: dict[str, object] | None = None

    def evaluate(
        self, series: Sequence[tuple[date, float | int | None]]
    ) -> dict[str, object]:
        self.received_series = list(series)
        serializable = [
            {"business_date": day.isoformat(), "demand_value": value}
            for day, value in self.received_series
        ]
        self.trace.append(
            {
                "component": "ForecastEngine.evaluate",
                "argument_fields": ["series"],
                "evidence_fields": sorted(field_names(serializable)),
                "policy_fields": sorted(asdict(self.policy)),
                "evidence_digest": digest(serializable),
            }
        )
        self.last_evaluation = super().evaluate(self.received_series)
        return self.last_evaluation


class InventorySource:
    def __init__(self, balances: list[dict[str, Any]], trace: list[dict[str, Any]]):
        self.balances = balances
        self.trace = trace

    def list_balances(
        self, tenant_id: str, *, store_id: str | None = None, sku_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.trace.append(
            {
                "component": "InventoryBalanceReader.list_balances",
                "argument_fields": ["tenant_id", "store_id", "sku_id"],
                "evidence_fields": sorted(field_names(self.balances)),
                "evidence_digest": digest(self.balances),
            }
        )
        return [dict(item) for item in self.balances]


def _series_values(spec: dict[str, Any]) -> tuple[list[float], set[int], set[int]]:
    days = int(spec["days"])
    kind = str(spec["kind"])
    if days < 1:
        raise ValueError("forecast_eval_days_invalid")
    if kind == "constant":
        values = [float(spec["level"])] * days
    elif kind == "linear":
        values = [
            float(spec["start"]) + float(spec["step"]) * offset
            for offset in range(days)
        ]
    elif kind == "weekly":
        pattern = [float(value) for value in spec["pattern"]]
        if len(pattern) != 7:
            raise ValueError("forecast_eval_weekly_pattern_invalid")
        values = [pattern[offset % 7] for offset in range(days)]
    elif kind == "sparse":
        period, active = int(spec["period"]), int(spec["offset"])
        values = [
            float(spec["level"]) if offset % period == active else 0.0
            for offset in range(days)
        ]
    else:
        raise ValueError("forecast_eval_series_kind_unsupported")
    for override in spec.get("overrides", []):
        values[int(override["offset"])] = float(override["value"])
    if any(value < 0 for value in values):
        raise ValueError("forecast_eval_series_value_invalid")
    missing = {int(value) for value in spec.get("missing_offsets", [])}
    if "observed_offsets" in spec:
        observed = {int(value) for value in spec["observed_offsets"]}
        missing.update(set(range(days)) - observed)
    stockouts = {int(value) for value in spec.get("stockout_offsets", [])}
    if any(offset < 0 or offset >= days for offset in missing | stockouts):
        raise ValueError("forecast_eval_series_offset_invalid")
    return values, missing, stockouts


def _facts(
    tenant_id: str, store_id: str, sku_id: str, spec: dict[str, Any]
) -> list[dict[str, Any]]:
    values, missing, stockouts = _series_values(spec)
    return [
        {
            "id": f"fact-{tenant_id}-{offset}",
            "tenant_id": tenant_id,
            "store_id": store_id,
            "sku_id": sku_id,
            "business_date": (START_DATE + timedelta(days=offset)).isoformat(),
            "eligible_units": value,
            "stockout_flag": "true" if offset in stockouts else "false",
            "demand_policy_version": "demand-v1",
            "fact_version": 1,
            "payload_hash": digest({"offset": offset, "value": value}),
            "quality_flags": [],
        }
        for offset, value in enumerate(values)
        if offset not in missing
    ]


def _future_invariant(
    engine: ForecastEngine, series: list[tuple[date, float | int | None]]
) -> bool:
    ordinary = engine.backtest(series)
    starts = [
        date.fromisoformat(str(row["forecast_start"]))
        for row in ordinary
        if row["failure_reason"] is None
    ]
    if not starts:
        return False
    cutoff = max(starts)
    mutated = [
        (day, value if day < cutoff or value is None else float(value) + 997.0)
        for day, value in series
    ]
    changed = engine.backtest(mutated)
    original_rows = {
        (row["model_name"], row["origin_date"]): row["forecast"]
        for row in ordinary
        if row["failure_reason"] is None
        and row["forecast_start"] == cutoff.isoformat()
    }
    changed_rows = {
        (row["model_name"], row["origin_date"]): row["forecast"]
        for row in changed
        if row["failure_reason"] is None
        and row["forecast_start"] == cutoff.isoformat()
    }
    common = original_rows.keys() & changed_rows.keys()
    return bool(common) and all(
        original_rows[key] == changed_rows[key] for key in common
    )


def _interval_coverage(run: dict[str, Any]) -> dict[str, float]:
    champion = str(run["champion_model"])
    rows = [
        row
        for row in run["backtests"]
        if row["model_name"] == champion and row["failure_reason"] is None
    ]
    point = run["points"][0]
    widths = {
        level: float(point[level]) - float(point["p50"])
        for level in ("p80", "p95")
    }
    pairs = [
        (float(actual), float(predicted))
        for row in rows
        for actual, predicted in zip(row["actual"], row["forecast"], strict=True)
    ]
    if not pairs:
        return {"p80": 0.0, "p95": 0.0}
    has_nonzero_error = any(actual != predicted for actual, predicted in pairs)
    return {
        level: (
            0.0
            if has_nonzero_error and width <= 0.0
            else sum(actual <= predicted + width for actual, predicted in pairs)
            / len(pairs)
        )
        for level, width in widths.items()
    }


def bias_effect(bias: float | None, gates: dict[str, Any]) -> str:
    if bias is None:
        return "incomparable"
    if abs(bias) <= float(gates["maximum_absolute_no_effect_bias"]):
        return "none"
    threshold = float(gates["minimum_directional_bias_magnitude"])
    if bias <= -threshold:
        return "negative"
    if bias >= threshold:
        return "positive"
    return "indeterminate"


def _inventory_plan(
    db: Database,
    service: ForecastRunService,
    trace: list[dict[str, Any]],
    run: dict[str, Any],
    inventory_input: dict[str, Any],
) -> dict[str, Any]:
    balances = [
        {
            "id": f"balance-{index}",
            "connector_id": "forecast-eval",
            "store_id": run["store_id"],
            "warehouse_id": item["warehouse_id"],
            "sku_id": run["sku_id"],
            "on_hand": str(item["on_hand"]),
            "reserved": str(item["reserved"]),
            "inbound": str(item["inbound"]),
            "source_id": f"forecast-eval-{index}",
            "source_updated_at": EVAL_NOW,
            "version": 1,
        }
        for index, item in enumerate(inventory_input["balances"])
    ]
    raw_policy = inventory_input["policy"]
    policy = InventoryPlanningPolicy(
        supplier_lead_days=int(raw_policy["supplier_lead_days"]),
        review_period_days=int(raw_policy["review_period_days"]),
        service_level=Decimal(str(raw_policy["service_level"])),
        minimum_order_qty=Decimal(str(raw_policy["minimum_order_qty"])),
        order_multiple=Decimal(str(raw_policy["order_multiple"])),
        minimum_safety_stock=Decimal(str(raw_policy["minimum_safety_stock"])),
        maximum_stock_days=int(raw_policy["maximum_stock_days"]),
    )
    trace.append(
        {
            "component": "InventoryPlanningService.create_plan",
            "argument_fields": ["tenant_id", "forecast_run_id", "planning_policy"],
            "policy_fields": sorted(policy.model_dump()),
        }
    )
    planner = InventoryPlanningService(
        db,
        forecasts=service,
        inventory=InventorySource(balances, trace),
        clock=lambda: EVAL_NOW,
    )
    return planner.create_plan(str(run["tenant_id"]), str(run["run_id"]), policy)


def run_scenario(
    db: Database,
    policy: ForecastPolicy,
    scenario_id: str,
    scenario_input: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tenant_id, store_id, sku_id = f"eval-{scenario_id}", "eval-store", "eval-sku"
    trace: list[dict[str, Any]] = []
    rows = _facts(tenant_id, store_id, sku_id, scenario_input["series_spec"])
    engine = RecordingEngine(policy, trace)
    service = ForecastRunService(db, facts=FactSource(rows, trace), engine=engine)
    trace.append(
        {
            "component": "ForecastRunService.run",
            "argument_fields": ["tenant_id", "store_id", "sku_id"],
        }
    )
    run = service.run(tenant_id, store_id=store_id, sku_id=sku_id)
    if engine.last_evaluation is None:
        raise RuntimeError("forecast_eval_engine_result_missing")
    coverage = _interval_coverage(run)
    origins = {
        row["origin_date"]
        for row in run["backtests"]
        if row["model_name"] == run["champion_model"]
        and row["failure_reason"] is None
    }
    plan = None
    if "inventory" in scenario_input:
        plan = _inventory_plan(db, service, trace, run, scenario_input["inventory"])
    return (
        {
            "demand_type": engine.last_evaluation["demand_type"],
            "champion_model": run["champion_model"],
            "champion_reason": run["champion_reason"],
            "candidate_models": run["candidate_models"]["models"],
            "wape": run["wape"],
            "bias": run["bias"],
            "rolling_origins": len(origins),
            "rolling_structure_valid": bool(run["backtests"])
            and all(
                row["training_end"] < row["forecast_start"]
                for row in run["backtests"]
            ),
            "future_invariant": _future_invariant(engine, engine.received_series),
            "interval_coverage": coverage,
            "anomaly_codes": sorted(
                item["anomaly_type"] for item in run["anomalies"]
            ),
            "inventory_plan": plan,
            "production_input_digest": digest(scenario_input),
        },
        trace,
    )
