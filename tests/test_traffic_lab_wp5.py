from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app

from conftest import make_settings


class _TrafficConsoleStructure(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.nav_views: set[str] = set()
        self.fields: set[str] = set()

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "button" and values.get("data-view"):
            self.nav_views.add(values["data-view"])
        if values.get("data-traffic-field"):
            self.fields.add(values["data-traffic-field"])


def test_traffic_lab_console_has_structured_evidence_and_manual_analysis(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        before = app.state.agent.operations.traffic_lab.domain.list_analysis_runs(
            "tenant-test", "not-created"
        )
        page = client.get("/admin")
        after = app.state.agent.operations.traffic_lab.domain.list_analysis_runs(
            "tenant-test", "not-created"
        )
    structure = _TrafficConsoleStructure()
    structure.feed(page.text)
    assert page.status_code == 200
    assert before == after == []
    assert "traffic-lab" in structure.nav_views
    assert {
        "trafficSku",
        "trafficStore",
        "trafficExperiment",
        "trafficTransitionStatus",
        "trafficEndedAt",
        "transitionTrafficExperiment",
        "analyzeTrafficExperiment",
    } <= structure.ids
    assert {
        "control",
        "treatment",
        "window",
        "sample_size",
        "uplift",
        "confidence_interval",
        "lag",
        "contamination",
        "counter_evidence",
    } <= structure.fields


def test_wp5_mechanism_eval_is_numeric_structured_and_bidirectional(tmp_path) -> None:
    from scripts.run_traffic_analysis_eval import run_evaluation

    fixture = Path(__file__).parents[1] / "evals" / "traffic_lab" / "wp5_mechanism_v1.json"
    fixture_document = json.loads(fixture.read_text(encoding="utf-8"))
    assert all(
        "effect_direction" in scenario["expected"]
        for scenario in fixture_document["scenarios"]
    )
    report = run_evaluation(fixture, tmp_path / "traffic-wp5.sqlite3")

    assert report["passed"] is True
    assert report["analysis_imported_ground_truth"] is False
    results = {item["scenario_id"]: item for item in report["results"]}
    assert {item["category"] for item in results.values()} == {
        "no_effect",
        "ctr_cvr_feedback",
        "inventory_penalty",
        "title_image_weight",
        "interaction",
        "time_noise",
    }
    assert all(
        item["passed"] and all(value is True for value in item["checks"].values())
        for item in results.values()
    )
    assert all("effect_direction" in item["checks"] for item in results.values())
    no_effect = results["no_effect_balanced"]
    assert no_effect["effect"] == 0
    assert no_effect["effect_direction"] == "none"
    assert no_effect["confidence_interval"]["includes_zero"] is True
    for scenario_id in ("ctr_feedback", "cvr_feedback"):
        assert results[scenario_id]["effect"] > 0
        assert results[scenario_id]["lag_analysis"]["status"] == "supported"
        assert results[scenario_id]["lag_analysis"]["best_supported_lag_minutes"] == 120
    inventory = results["inventory_penalty"]
    assert inventory["effect"] < 0
    assert inventory["observed"] == "blocked"
    assert "stock_not_available" in inventory["issue_codes"]
    assert results["title_weight"]["effect_direction"] == "positive"
    assert results["image_weight"]["effect_direction"] == "positive"
    assert "multiple_treatment_variables_changed" in results["interaction"]["issue_codes"]
    assert "switchback_hour_distribution_imbalanced" in results["time_noise"]["issue_codes"]
