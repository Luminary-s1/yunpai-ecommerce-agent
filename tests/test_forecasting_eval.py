from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

from scripts.forecast_eval_runtime import _interval_coverage
from scripts.run_forecast_eval import run_evaluation


FIXTURE = Path(__file__).parents[1] / "evals/forecasting/forecast_eval_v1.json"
REQUIRED_CATEGORIES = {
    "stable", "rising", "falling", "weekly", "intermittent", "many_zeros",
    "promotion_spike", "stockout", "missing", "cold_start",
}


def test_forecast_eval_passes_numeric_structural_and_oracle_boundary_gates(tmp_path) -> None:
    report = run_evaluation(FIXTURE, tmp_path / "forecast-eval.sqlite3")

    assert report["passed"] is True
    assert report["ground_truth_boundary"]["status"] == "passed"
    assert report["ground_truth_boundary"]["oracle_field_overlap"] == []
    assert REQUIRED_CATEGORIES <= {result["category"] for result in report["results"]}
    assert all(result["checks"]["rolling_origin"] for result in report["results"])
    assert all(result["interval_coverage"]["p95"] >= result["interval_coverage"]["p80"] for result in report["results"])
    assert {result["wape_comparable"] for result in report["results"]} == {True, False}
    assert {result["bias_effect"] for result in report["results"]} >= {
        "negative", "none", "positive",
    }


def test_oracle_pollution_is_reported_even_when_production_ignores_extra_input(tmp_path) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["scenarios"] = [deepcopy(fixture["scenarios"][0])]
    scenario = fixture["scenarios"][0]
    scenario["input"]["expected_type_code"] = scenario["oracle"]["expected_type_code"]
    polluted = tmp_path / "polluted.json"
    polluted.write_text(json.dumps(fixture), encoding="utf-8")

    report = run_evaluation(polluted, tmp_path / "polluted.sqlite3")

    assert report["passed"] is False
    assert report["ground_truth_boundary"]["status"] == "failed"
    assert "expected_type_code" in report["ground_truth_boundary"]["oracle_field_overlap"]


def test_independent_oracle_can_reject_a_numerically_wrong_expectation(tmp_path) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["scenarios"] = [deepcopy(fixture["scenarios"][1])]
    fixture["scenarios"][0]["oracle"]["expected_type_code"] = "stable"
    wrong = tmp_path / "wrong-oracle.json"
    wrong.write_text(json.dumps(fixture), encoding="utf-8")

    report = run_evaluation(wrong, tmp_path / "wrong-oracle.sqlite3")

    assert report["passed"] is False
    assert report["results"][0]["checks"]["demand_type"] is False
    assert report["results"][0]["observed"]["demand_type"] == "rising_trend"


def test_interval_coverage_rejects_zero_width_overforecast() -> None:
    run = {
        "champion_model": "last_value",
        "backtests": [
            {
                "model_name": "last_value",
                "failure_reason": None,
                "actual": [1.0, 2.0],
                "forecast": [5.0, 5.0],
            }
        ],
        "points": [{"p50": 5.0, "p80": 5.0, "p95": 5.0}],
    }

    coverage = _interval_coverage(run)

    assert coverage == {"p80": 0.0, "p95": 0.0}


def test_forecast_eval_direct_cli_emits_a_passing_report(tmp_path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts/run_forecast_eval.py"),
            str(FIXTURE),
            str(tmp_path / "cli.sqlite3"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["passed"] is True
