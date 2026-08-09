from __future__ import annotations

import json
from pathlib import Path


def test_wp4_blackbox_eval_recovers_effects_and_rejects_counterexamples(
    tmp_path,
) -> None:
    from scripts.run_traffic_analysis_eval import run_evaluation

    fixture = (
        Path(__file__).parents[1]
        / "evals"
        / "traffic_lab"
        / "wp4_blackbox_v1.json"
    )
    report = run_evaluation(fixture, tmp_path / "traffic-blackbox.sqlite3")

    assert report["schema_version"] == "traffic-analysis-blackbox-v1"
    assert report["passed"] is True
    results = {item["scenario_id"]: item for item in report["results"]}
    assert results["ctr_positive_balanced"]["observed"] == "positive_effect"
    assert results["cvr_positive_balanced"]["observed"] == "positive_effect"
    assert results["no_effect_hour_confound"]["strong_conclusion_allowed"] is False
    assert "switchback_hour_distribution_imbalanced" in results[
        "no_effect_hour_confound"
    ]["issue_codes"]
    assert results["inventory_polluted"]["observed"] == "blocked"
    assert "stock_not_available" in results["inventory_polluted"]["issue_codes"]
    boundary = report["ground_truth_boundary"]
    assert boundary["status"] == "passed"
    assert boundary["unexpected_analysis_fields"] == []
    assert boundary["oracle_field_overlap"] == []
    assert "expected" in boundary["oracle_fields"]
    assert boundary["analysis_engine_call_count"] >= len(results)
    assert report["analysis_imported_ground_truth"] is boundary[
        "analysis_imported_ground_truth"
    ]
    assert report["analysis_imported_ground_truth"] is False


def test_wp4_blackbox_eval_fails_when_oracle_field_enters_analysis_input(
    tmp_path,
) -> None:
    from scripts.run_traffic_analysis_eval import run_evaluation

    source_fixture = (
        Path(__file__).parents[1]
        / "evals"
        / "traffic_lab"
        / "wp4_blackbox_v1.json"
    )
    fixture = json.loads(source_fixture.read_text(encoding="utf-8"))
    fixture["scenarios"] = [fixture["scenarios"][0]]
    scenario = fixture["scenarios"][0]
    scenario["input"]["conclusion"] = scenario["expected"]["conclusion"]
    leaked_fixture = tmp_path / "leaked-oracle.json"
    leaked_fixture.write_text(
        json.dumps(fixture, ensure_ascii=False),
        encoding="utf-8",
    )

    report = run_evaluation(
        leaked_fixture,
        tmp_path / "leaked-oracle.sqlite3",
    )

    assert report["passed"] is False
    assert report["analysis_imported_ground_truth"] is True
    boundary = report["ground_truth_boundary"]
    assert boundary["status"] == "failed"
    assert "conclusion" in boundary["oracle_field_overlap"]
