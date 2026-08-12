from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ecommerce_agent.database import Database
from ecommerce_agent.forecasting import ForecastPolicy

from scripts.forecast_eval_runtime import (
    ALLOWED_CALL_FIELDS,
    bias_effect,
    field_names,
    run_scenario,
)


def _score(
    scenario_id: str,
    category: str,
    observed: dict[str, Any],
    oracle: dict[str, Any],
    gates: dict[str, Any],
) -> dict[str, Any]:
    comparable = observed["wape"] is not None and observed["bias"] is not None
    effect = bias_effect(observed["bias"], gates)
    fallback = (
        observed["champion_reason"].get("baseline_model")
        == observed["champion_model"]
        and observed["champion_reason"]["code"]
        in {"baseline_retained", "cold_start_baseline"}
    )
    expected_effect = str(oracle["expected_bias_effect"])
    inventory_checks: dict[str, bool] = {}
    if "expected_inventory" in oracle:
        plan = observed["inventory_plan"] or {}
        for expected_key, expected_value in oracle["expected_inventory"].items():
            actual_key = expected_key.removeprefix("expected_")
            if actual_key == "demand_copy_count":
                actual = plan.get("allocation_boundary", {}).get(actual_key)
            else:
                actual = plan.get(actual_key)
            inventory_checks[actual_key] = actual == expected_value
    coverage = observed["interval_coverage"]
    required_anomaly = oracle.get("required_anomaly_code")
    checks = {
        "demand_type": observed["demand_type"] == oracle["expected_type_code"],
        "candidate_selection": observed["champion_model"] in observed["candidate_models"],
        "baseline_fallback": fallback is oracle["expected_baseline_fallback"],
        "rolling_origin": observed["rolling_structure_valid"]
        and observed["rolling_origins"] >= int(gates["minimum_rolling_origins"]),
        "future_invariance": observed["future_invariant"],
        "wape_comparability": comparable is oracle["expected_wape_comparable"],
        "wape_bound": (not comparable)
        or observed["wape"] <= float(oracle["maximum_wape"]),
        "bias_direction": expected_effect == "any" or effect == expected_effect,
        "interval_coverage": coverage["p80"] >= float(gates["minimum_p80_coverage"])
        and coverage["p95"] >= float(gates["minimum_p95_coverage"])
        and coverage["p95"] >= coverage["p80"],
        "required_anomaly": required_anomaly is None
        or required_anomaly in observed["anomaly_codes"],
        "inventory": all(inventory_checks.values()),
    }
    return {
        "scenario_id": scenario_id,
        "category": category,
        "passed": all(checks.values()),
        "checks": checks,
        "observed": {
            key: observed[key]
            for key in (
                "demand_type", "champion_model", "champion_reason", "wape", "bias",
                "rolling_origins", "future_invariant", "anomaly_codes",
                "production_input_digest",
            )
        },
        "wape_comparable": comparable,
        "bias_effect": effect,
        "interval_coverage": coverage,
        "inventory_checks": inventory_checks,
    }


def _audit_boundary(
    inputs: list[dict[str, Any]], traces: list[dict[str, Any]], oracles: list[dict[str, Any]]
) -> dict[str, Any]:
    scenario_fields = field_names(inputs)
    oracle_fields = {"oracle"} | field_names(oracles)
    production_fields: set[str] = set()
    unexpected: list[str] = []
    for call in traces:
        component = str(call.get("component"))
        arguments = {str(value) for value in call.get("argument_fields", [])}
        production_fields.update(arguments)
        production_fields.update(str(value) for value in call.get("evidence_fields", []))
        production_fields.update(str(value) for value in call.get("policy_fields", []))
        if component not in ALLOWED_CALL_FIELDS:
            unexpected.append(f"component:{component}")
        else:
            unexpected.extend(
                f"{component}.{field}"
                for field in sorted(arguments - ALLOWED_CALL_FIELDS[component])
            )
    overlap = sorted((scenario_fields | production_fields) & oracle_fields)
    imported = bool(overlap or unexpected)
    return {
        "status": "failed" if imported else "passed",
        "analysis_imported_ground_truth": imported,
        "scenario_input_fields": sorted(scenario_fields),
        "production_call_fields": sorted(production_fields),
        "oracle_fields": sorted(oracle_fields),
        "oracle_field_overlap": overlap,
        "unexpected_production_fields": sorted(unexpected),
        "production_call_count": len(traces),
        "production_trace_digests": [
            call["evidence_digest"] for call in traces if "evidence_digest" in call
        ],
    }


def run_evaluation(fixture_path: Path, db_path: Path) -> dict[str, Any]:
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if fixture.get("schema_version") != "forecast-eval-v1":
        raise ValueError("unsupported_forecast_eval_fixture_version")
    if db_path.exists():
        raise FileExistsError(f"evaluation database already exists: {db_path}")
    db = Database(db_path)
    db.initialize()
    raw_policy = fixture["forecast_policy"]
    policy = ForecastPolicy(
        policy_version=str(raw_policy["policy_version"]),
        minimum_history_days=int(raw_policy["minimum_history_days"]),
        backtest_windows=int(raw_policy["backtest_windows"]),
        required_relative_improvement=float(raw_policy["required_relative_improvement"]),
    )
    inputs: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    observations: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for scenario in fixture["scenarios"]:
        scenario_input = scenario["input"]
        inputs.append(scenario_input)
        observed, scenario_trace = run_scenario(
            db, policy, str(scenario["scenario_id"]), scenario_input
        )
        observations.append((scenario, observed))
        traces.extend(scenario_trace)
    oracles: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for scenario, observed in observations:
        oracle = scenario["oracle"]
        oracles.append(oracle)
        results.append(
            _score(
                str(scenario["scenario_id"]),
                str(scenario["category"]),
                observed,
                oracle,
                fixture["numeric_gates"],
            )
        )
    boundary = _audit_boundary(inputs, traces, oracles)
    return {
        "schema_version": fixture["schema_version"],
        "passed": all(result["passed"] for result in results)
        and boundary["status"] == "passed",
        "fixture": str(fixture_path),
        "numeric_gates": fixture["numeric_gates"],
        "ground_truth_boundary": boundary,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the M6-R WP5 forecast evaluation")
    parser.add_argument("fixture", type=Path)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    report = run_evaluation(args.fixture, args.database)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
