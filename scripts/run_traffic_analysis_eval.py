from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecommerce_agent.database import Database
from ecommerce_agent.traffic_lab import (
    CreativeAssetCreate,
    ListingRevisionCreate,
    TrafficAnalysisEngine,
    TrafficExperimentCreate,
    TrafficExperimentTransition,
    TrafficExperimentWindowCreate,
    TrafficLabService,
    TrafficMetricBucketUpsert,
)


_BASE_TIME = datetime(2026, 8, 1, tzinfo=UTC)
_BALANCED_ASSIGNMENTS = (
    (0, 0, "control"),
    (0, 2, "treatment"),
    (0, 4, "control"),
    (0, 6, "treatment"),
    (1, 0, "treatment"),
    (1, 2, "control"),
    (1, 4, "treatment"),
    (1, 6, "control"),
)
_ANALYSIS_SCENARIO_REQUEST_FIELDS = frozenset(
    {"scenario_id", "scenario_input"}
)
_ANALYSIS_ENGINE_CALL_FIELDS = frozenset({"tenant_id", "experiment_id"})


def _field_names(value: Any) -> set[str]:
    if isinstance(value, dict):
        names = {str(key) for key in value}
        for item in value.values():
            names.update(_field_names(item))
        return names
    if isinstance(value, list):
        names: set[str] = set()
        for item in value:
            names.update(_field_names(item))
        return names
    return set()


def _audit_ground_truth_boundary(
    *,
    analysis_scenario_requests: list[dict[str, Any]],
    analysis_engine_calls: list[dict[str, str]],
    oracles: list[dict[str, Any]],
) -> dict[str, Any]:
    scenario_fields = {
        str(field)
        for request in analysis_scenario_requests
        for field in request
    }
    engine_call_fields = {
        str(field) for call in analysis_engine_calls for field in call
    }
    analysis_fields = _field_names(analysis_scenario_requests)
    analysis_fields.update(_field_names(analysis_engine_calls))
    oracle_fields = {"expected"}
    oracle_fields.update(_field_names(oracles))
    unexpected_analysis_fields = sorted(
        [
            f"scenario.{field}"
            for field in scenario_fields - _ANALYSIS_SCENARIO_REQUEST_FIELDS
        ]
        + [
            f"engine.{field}"
            for field in engine_call_fields - _ANALYSIS_ENGINE_CALL_FIELDS
        ]
    )
    oracle_field_overlap = sorted(analysis_fields & oracle_fields)
    imported_ground_truth = bool(
        unexpected_analysis_fields or oracle_field_overlap
    )
    return {
        "status": "failed" if imported_ground_truth else "passed",
        "analysis_imported_ground_truth": imported_ground_truth,
        "analysis_scenario_count": len(analysis_scenario_requests),
        "analysis_engine_call_count": len(analysis_engine_calls),
        "analysis_scenario_fields": sorted(scenario_fields),
        "analysis_engine_call_fields": sorted(engine_call_fields),
        "analysis_fields": sorted(analysis_fields),
        "oracle_fields": sorted(oracle_fields),
        "unexpected_analysis_fields": unexpected_analysis_fields,
        "oracle_field_overlap": oracle_field_overlap,
    }


def _seed_revisions(
    service: TrafficLabService,
    tenant_id: str,
    controls: dict[str, Any],
    treatment_overrides: dict[str, Any],
    *,
    title_changed: bool = True,
    image_changed: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    asset = service.register_asset(
        tenant_id,
        CreativeAssetCreate(
            sha256="b" * 64,
            mime_type="image/png",
            width=1200,
            height=1200,
            storage_ref=f"objects/traffic-eval/{tenant_id}.png",
            source_ref=f"fixture://traffic-analysis/{tenant_id}",
            feature_schema_version="image-v1",
        ),
    )
    treatment_asset = asset
    if image_changed:
        treatment_asset = service.register_asset(
            tenant_id,
            CreativeAssetCreate(
                sha256="c" * 64,
                mime_type="image/png",
                width=1200,
                height=1200,
                storage_ref=f"objects/traffic-eval/{tenant_id}-treatment.png",
                source_ref=f"fixture://traffic-analysis/{tenant_id}/treatment",
                feature_schema_version="image-v1",
            ),
        )
    common = {
        "connector_id": "blackbox_fixture",
        "store_id": "eval-store",
        "item_id": "eval-item",
        "sku_id": "eval-sku",
        "sale_price": "109.00",
        "active_from": _BASE_TIME - timedelta(days=2),
        "active_to": _BASE_TIME + timedelta(days=30),
    }
    control = service.create_revision(
        tenant_id,
        ListingRevisionCreate(
            **common,
            revision_no=1,
            title="黑盒评测标题 A",
            main_image_asset_id=str(asset["asset_id"]),
            attributes=controls,
            source_updated_at=_BASE_TIME - timedelta(days=2),
        ),
    )
    treatment_attributes = {**controls, **treatment_overrides}
    treatment = service.create_revision(
        tenant_id,
        ListingRevisionCreate(
            **common,
            revision_no=2,
            title="黑盒评测标题 B" if title_changed else "黑盒评测标题 A",
            main_image_asset_id=str(treatment_asset["asset_id"]),
            attributes=treatment_attributes,
            source_updated_at=_BASE_TIME - timedelta(days=1),
        ),
    )
    return control, treatment


def _metric_value(
    revision_id: str,
    start: datetime,
    observation: dict[str, Any],
    source_id: str,
) -> TrafficMetricBucketUpsert:
    impressions = int(observation["impressions"])
    recommend_impressions = int(observation.get("recommend_impressions", 600))
    orders = int(observation["orders"])
    return TrafficMetricBucketUpsert(
        listing_revision_id=revision_id,
        metric_start=start,
        metric_end=start + timedelta(hours=1),
        bucket_granularity="hour",
        traffic_source="recommend",
        impressions=impressions,
        clicks=int(observation["clicks"]),
        visitors=int(observation["clicks"]),
        favorites=0,
        cart_adds=0,
        orders=orders,
        sales_amount=str(orders * 109),
        ad_spend="0",
        search_impressions=impressions - recommend_impressions,
        recommend_impressions=recommend_impressions,
        data_as_of=start + timedelta(hours=1, minutes=5),
        source_id=source_id,
    )


def _seed_completed_experiment(
    service: TrafficLabService,
    tenant_id: str,
    control: dict[str, Any],
    treatment: dict[str, Any],
    *,
    experiment_type: str,
    primary_metric: str,
    start: datetime,
    buckets: list[dict[str, Any]],
    policy_version: str,
) -> str:
    end = max(
        start + timedelta(days=int(item["day"]), hours=int(item["hour"]) + 1)
        for item in buckets
    )
    same_revision = experiment_type == "aa"
    experiment = service.create_experiment(
        tenant_id,
        TrafficExperimentCreate(
            store_id="eval-store",
            sku_id="eval-sku",
            experiment_type=experiment_type,
            primary_metric=primary_metric,
            started_at=start,
            ended_at=end,
            control_revision_id=str(control["id"]),
            treatment_revision_id=(
                str(control["id"]) if same_revision else str(treatment["id"])
            ),
            minimum_exposure=2_000,
            washout_window=60,
            analysis_policy_version=policy_version,
        ),
    )
    experiment_id = str(experiment["experiment_id"])
    service.transition_experiment(
        tenant_id,
        experiment_id,
        TrafficExperimentTransition(status="ready"),
    )
    service.transition_experiment(
        tenant_id,
        experiment_id,
        TrafficExperimentTransition(status="running"),
    )
    ordered = sorted(buckets, key=lambda item: (int(item["day"]), int(item["hour"])))
    for index, item in enumerate(ordered):
        window_start = start + timedelta(
            days=int(item["day"]), hours=int(item["hour"])
        )
        assignment = str(item["assignment"])
        revision = control if same_revision or assignment == "control" else treatment
        service.add_experiment_window(
            tenant_id,
            experiment_id,
            TrafficExperimentWindowCreate(
                listing_revision_id=str(revision["id"]),
                window_start=window_start,
                window_end=window_start + timedelta(hours=1),
                assignment=assignment,
                source_receipt_id=f"{experiment_id}-active-{index}",
            ),
        )
        service.upsert_metric_bucket(
            tenant_id,
            _metric_value(
                str(revision["id"]),
                window_start,
                item,
                f"{experiment_id}-metric-{index}",
            ),
        )
        if index + 1 >= len(ordered):
            continue
        next_item = ordered[index + 1]
        active_end = window_start + timedelta(hours=1)
        next_start = start + timedelta(
            days=int(next_item["day"]), hours=int(next_item["hour"])
        )
        if active_end >= next_start:
            continue
        next_assignment = str(next_item["assignment"])
        next_revision = (
            control
            if same_revision or next_assignment == "control"
            else treatment
        )
        service.add_experiment_window(
            tenant_id,
            experiment_id,
            TrafficExperimentWindowCreate(
                listing_revision_id=str(next_revision["id"]),
                window_start=active_end,
                window_end=next_start,
                assignment=next_assignment,
                washout=True,
                source_receipt_id=f"{experiment_id}-washout-{index}",
            ),
        )
    service.transition_experiment(
        tenant_id,
        experiment_id,
        TrafficExperimentTransition(status="completed", ended_at=end),
    )
    return experiment_id


def _aa_buckets(observation: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "day": day,
            "hour": hour,
            "assignment": assignment,
            **observation,
            "recommend_impressions": 500 + index * 10,
        }
        for index, (day, hour, assignment) in enumerate(_BALANCED_ASSIGNMENTS)
    ]


def _analyze_scenario(
    db: Database,
    controls: dict[str, Any],
    policy_version: str,
    scenario_id: str,
    scenario_input: dict[str, Any],
    analysis_engine_calls: list[dict[str, str]],
) -> dict[str, Any]:
    tenant_id = f"eval-{scenario_id}"
    service = TrafficLabService(db)
    control, treatment = _seed_revisions(
        service,
        tenant_id,
        controls,
        scenario_input.get("treatment_attribute_overrides", {}),
        title_changed=bool(scenario_input.get("title_changed", True)),
        image_changed=bool(scenario_input.get("image_changed", False)),
    )
    aa_id = _seed_completed_experiment(
        service,
        tenant_id,
        control,
        treatment,
        experiment_type="aa",
        primary_metric=str(scenario_input["primary_metric"]),
        start=_BASE_TIME,
        buckets=_aa_buckets(scenario_input["aa_observation"]),
        policy_version=policy_version,
    )
    aa_call = {"tenant_id": tenant_id, "experiment_id": aa_id}
    analysis_engine_calls.append(dict(aa_call))
    aa_run = TrafficAnalysisEngine(db).analyze_experiment(**aa_call)
    experiment_id = _seed_completed_experiment(
        service,
        tenant_id,
        control,
        treatment,
        experiment_type="switchback",
        primary_metric=str(scenario_input["primary_metric"]),
        start=_BASE_TIME + timedelta(days=4),
        buckets=scenario_input["switchback_buckets"],
        policy_version=policy_version,
    )
    analysis_call = {
        "tenant_id": tenant_id,
        "experiment_id": experiment_id,
    }
    analysis_engine_calls.append(dict(analysis_call))
    run = TrafficAnalysisEngine(db).analyze_experiment(**analysis_call)
    gate = run["evidence"]["quality_gate"]
    issue_codes = sorted(issue["code"] for issue in gate["issues"])
    return {
        "scenario_id": scenario_id,
        "aa_gate_passed": aa_run["evidence"]["quality_gate"]["status"]
        == "passed",
        "observed": run["evidence"]["statistical_conclusion"],
        "strong_conclusion_allowed": gate["strong_conclusion_allowed"],
        "issue_codes": issue_codes,
        "effect": run["effect_estimate"]["absolute"],
        "effect_direction": run["effect_estimate"]["direction"],
        "lag_analysis": run["effect_estimate"]["lag_analysis"],
        "sample_size": run["sample_size"],
        "confidence_interval": {
            "low": run["confidence_interval"]["low"],
            "high": run["confidence_interval"]["high"],
            "includes_zero": run["confidence_interval"]["includes_zero"],
        },
    }


def _score_scenario(
    observation: dict[str, Any],
    expected: dict[str, Any],
) -> dict[str, Any]:
    issue_codes = observation["issue_codes"]
    checks = {
        "aa_gate_passed": observation["aa_gate_passed"],
        "conclusion": observation["observed"] == expected["conclusion"],
        "strong_conclusion_allowed": observation["strong_conclusion_allowed"]
        is expected["strong_conclusion_allowed"],
        "required_issue_codes": set(expected.get("required_issue_codes", []))
        <= set(issue_codes),
        "forbidden_issue_codes": not (
            set(expected.get("forbidden_issue_codes", [])) & set(issue_codes)
        ),
        "effect_direction": expected.get("effect_direction", observation["effect_direction"])
        == observation["effect_direction"],
        "confidence_includes_zero": expected.get(
            "confidence_includes_zero",
            observation["confidence_interval"]["includes_zero"],
        )
        is observation["confidence_interval"]["includes_zero"],
        "lag_status": expected.get("lag_status", observation["lag_analysis"]["status"])
        == observation["lag_analysis"]["status"],
        "best_supported_lag_minutes": expected.get(
            "best_supported_lag_minutes",
            observation["lag_analysis"]["best_supported_lag_minutes"],
        )
        == observation["lag_analysis"]["best_supported_lag_minutes"],
    }
    return {
        "scenario_id": observation["scenario_id"],
        "passed": all(checks.values()),
        "checks": checks,
        "observed": observation["observed"],
        "strong_conclusion_allowed": observation[
            "strong_conclusion_allowed"
        ],
        "issue_codes": issue_codes,
        "effect": observation["effect"],
        "effect_direction": observation["effect_direction"],
        "lag_analysis": observation["lag_analysis"],
        "sample_size": observation["sample_size"],
        "confidence_interval": observation["confidence_interval"],
    }


def run_evaluation(fixture_path: Path, db_path: Path) -> dict[str, Any]:
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if fixture["schema_version"] != "traffic-analysis-blackbox-v1":
        raise ValueError("unsupported_blackbox_fixture_version")
    if db_path.exists():
        raise FileExistsError(f"evaluation database already exists: {db_path}")
    db = Database(db_path)
    db.initialize()
    analysis_scenario_requests: list[dict[str, Any]] = []
    analysis_engine_calls: list[dict[str, str]] = []
    oracles: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for scenario in fixture["scenarios"]:
        analysis_request = {
            "scenario_id": str(scenario["scenario_id"]),
            "scenario_input": scenario["input"],
        }
        analysis_scenario_requests.append(analysis_request)
        observation = _analyze_scenario(
            db,
            fixture["control_attributes"],
            str(fixture["analysis_policy_version"]),
            analysis_engine_calls=analysis_engine_calls,
            **analysis_request,
        )
        expected = scenario["expected"]
        oracles.append(expected)
        scored = _score_scenario(observation, expected)
        scored["category"] = str(scenario.get("category") or "legacy")
        results.append(scored)
    ground_truth_boundary = _audit_ground_truth_boundary(
        analysis_scenario_requests=analysis_scenario_requests,
        analysis_engine_calls=analysis_engine_calls,
        oracles=oracles,
    )
    analysis_imported_ground_truth = ground_truth_boundary[
        "analysis_imported_ground_truth"
    ]
    return {
        "schema_version": fixture["schema_version"],
        "passed": all(result["passed"] for result in results)
        and not analysis_imported_ground_truth,
        "analysis_imported_ground_truth": analysis_imported_ground_truth,
        "ground_truth_boundary": ground_truth_boundary,
        "fixture": str(fixture_path),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the WP4 black-box analysis eval")
    parser.add_argument("fixture", type=Path)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    report = run_evaluation(args.fixture, args.database)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
