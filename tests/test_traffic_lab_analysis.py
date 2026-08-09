from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Event
import time

import pytest

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


BASE_TIME = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
CONTROL_ATTRIBUTES = {
    "category": "air-circulator",
    "stock_status": "in_stock",
    "campaign": None,
    "ad_plan": "fixed-baseline",
    "holiday_calendar_version": "cn-2026-v1",
    "store_traffic_baseline_version": "store-001-hourly-v1",
    "historical_ctr": 0.052,
    "historical_cvr": 0.022,
}


def _seed_revisions(
    db: Database,
    tenant_id: str,
    *,
    attributes: dict[str, object] | None = None,
    treatment_attributes: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    service = TrafficLabService(db)
    control_asset = service.register_asset(
        tenant_id,
        CreativeAssetCreate(
            sha256="a" * 64,
            mime_type="image/png",
            width=1200,
            height=1200,
            storage_ref="objects/traffic-lab/control.png",
            source_ref="fixture://traffic-analysis/control",
            feature_schema_version="image-v1",
        ),
    )
    control_attributes = CONTROL_ATTRIBUTES if attributes is None else attributes
    common = {
        "connector_id": "virtual_taobao",
        "store_id": "store-001",
        "item_id": "item-001",
        "sku_id": "sku-001",
        "sale_price": "109.00",
        "active_from": BASE_TIME - timedelta(days=2),
        "active_to": BASE_TIME + timedelta(days=10),
    }
    control = service.create_revision(
        tenant_id,
        ListingRevisionCreate(
            **common,
            revision_no=1,
            title="云湃循环扇 标题 A",
            main_image_asset_id=str(control_asset["asset_id"]),
            attributes=control_attributes,
            source_updated_at=BASE_TIME - timedelta(days=2),
        ),
    )
    treatment = service.create_revision(
        tenant_id,
        ListingRevisionCreate(
            **common,
            revision_no=2,
            title="云湃循环扇 强劲静音 标题 B",
            main_image_asset_id=str(control_asset["asset_id"]),
            attributes=(
                control_attributes
                if treatment_attributes is None
                else treatment_attributes
            ),
            source_updated_at=BASE_TIME - timedelta(days=1),
        ),
    )
    return control, treatment


def _metric(
    revision_id: str,
    metric_start: datetime,
    *,
    ctr: float,
    cvr: float,
    recommend_impressions: int,
    source_id: str,
) -> TrafficMetricBucketUpsert:
    impressions = 1_000
    clicks = round(impressions * ctr)
    orders = max(1, round(clicks * cvr))
    return TrafficMetricBucketUpsert(
        listing_revision_id=revision_id,
        metric_start=metric_start,
        metric_end=metric_start + timedelta(hours=1),
        bucket_granularity="hour",
        traffic_source="recommend",
        impressions=impressions,
        clicks=clicks,
        visitors=clicks,
        favorites=max(0, round(clicks * 0.07)),
        cart_adds=max(0, round(clicks * 0.05)),
        orders=orders,
        sales_amount=str(orders * 109),
        ad_spend="0",
        search_impressions=impressions - recommend_impressions,
        recommend_impressions=recommend_impressions,
        data_as_of=metric_start + timedelta(hours=1, minutes=5),
        source_id=source_id,
    )


def _seed_experiment(
    db: Database,
    tenant_id: str,
    control: dict[str, object],
    treatment: dict[str, object],
    *,
    experiment_type: str,
    start: datetime,
    control_ctr: float,
    treatment_ctr: float,
    primary_metric: str = "ctr",
    control_cvr: float = 0.05,
    treatment_cvr: float = 0.05,
    minimum_exposure: int = 2_000,
    overlap: bool = False,
    analysis_policy_version: str = "traffic-analysis-v2",
) -> dict[str, object]:
    service = TrafficLabService(db)
    end = start + timedelta(days=1, hours=7)
    same_revision = experiment_type == "aa"
    experiment = service.create_experiment(
        tenant_id,
        TrafficExperimentCreate(
            store_id="store-001",
            sku_id="sku-001",
            experiment_type=experiment_type,
            primary_metric=primary_metric,
            started_at=start,
            ended_at=end,
            control_revision_id=str(control["id"]),
            treatment_revision_id=(
                str(control["id"]) if same_revision else str(treatment["id"])
            ),
            minimum_exposure=minimum_exposure,
            washout_window=60,
            analysis_policy_version=analysis_policy_version,
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

    assignments = [
        (start + timedelta(hours=0), "control", 700),
        (start + timedelta(hours=2), "treatment", 600),
        (start + timedelta(hours=4), "control", 900),
        (start + timedelta(hours=6), "treatment", 600),
        (start + timedelta(days=1, hours=0), "treatment", 700),
        (start + timedelta(days=1, hours=2), "control", 900),
        (start + timedelta(days=1, hours=4), "treatment", 600),
        (start + timedelta(days=1, hours=6), "control", 900),
    ]
    included_bucket_ids: list[str] = []
    first_washout: tuple[datetime, dict[str, object]] | None = None
    for index, (window_start, assignment, recommend_impressions) in enumerate(
        assignments, start=1
    ):
        revision = control if same_revision or assignment == "control" else treatment
        service.add_experiment_window(
            tenant_id,
            experiment_id,
            TrafficExperimentWindowCreate(
                listing_revision_id=str(revision["id"]),
                window_start=window_start,
                window_end=window_start + timedelta(hours=1),
                assignment=assignment,
                source_receipt_id=f"{experiment_id}-receipt-{index}",
            ),
        )
        ctr = control_ctr if assignment == "control" else treatment_ctr
        cvr = control_cvr if assignment == "control" else treatment_cvr
        bucket = service.upsert_metric_bucket(
            tenant_id,
            _metric(
                str(revision["id"]),
                window_start,
                ctr=ctr,
                cvr=cvr,
                recommend_impressions=recommend_impressions,
                source_id=f"{experiment_id}-metric-{index}",
            ),
        )
        included_bucket_ids.append(str(bucket["id"]))

        if index < len(assignments):
            next_start, next_assignment, _ = assignments[index]
            active_end = window_start + timedelta(hours=1)
            if active_end < next_start:
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
                        source_receipt_id=(
                            f"{experiment_id}-washout-receipt-{index}"
                        ),
                    ),
                )
                if first_washout is None:
                    first_washout = (active_end, next_revision)

    assert first_washout is not None
    washout_start, washout_revision = first_washout
    washout_bucket = service.upsert_metric_bucket(
        tenant_id,
        _metric(
            str(washout_revision["id"]),
            washout_start,
            ctr=0.9,
            cvr=0.05,
            recommend_impressions=999,
            source_id=f"{experiment_id}-washout-metric",
        ),
    )

    if overlap:
        service.add_experiment_window(
            tenant_id,
            experiment_id,
            TrafficExperimentWindowCreate(
                listing_revision_id=str(control["id"]),
                window_start=start + timedelta(minutes=30),
                window_end=start + timedelta(hours=1, minutes=30),
                assignment="control",
                source_receipt_id=f"{experiment_id}-overlap-receipt",
            ),
        )

    service.transition_experiment(
        tenant_id,
        experiment_id,
        TrafficExperimentTransition(status="completed", ended_at=end),
    )
    return {
        "experiment_id": experiment_id,
        "included_bucket_ids": included_bucket_ids,
        "washout_bucket_id": str(washout_bucket["id"]),
    }


def _seed_scheduled_switchback(
    db: Database,
    tenant_id: str,
    control: dict[str, object],
    treatment: dict[str, object],
    *,
    schedule: list[tuple[datetime, str, float, float]],
    washout_window: int = 60,
) -> dict[str, object]:
    service = TrafficLabService(db)
    start = schedule[0][0]
    end = schedule[-1][0] + timedelta(hours=1)
    experiment = service.create_experiment(
        tenant_id,
        TrafficExperimentCreate(
            store_id="store-001",
            sku_id="sku-001",
            experiment_type="switchback",
            primary_metric="ctr",
            started_at=start,
            ended_at=end,
            control_revision_id=str(control["id"]),
            treatment_revision_id=str(treatment["id"]),
            minimum_exposure=2_000,
            washout_window=washout_window,
            analysis_policy_version="traffic-analysis-v2",
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
    for index, (window_start, assignment, ctr, cvr) in enumerate(schedule, start=1):
        revision = control if assignment == "control" else treatment
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
            _metric(
                str(revision["id"]),
                window_start,
                ctr=ctr,
                cvr=cvr,
                recommend_impressions=500 + index,
                source_id=f"{experiment_id}-metric-{index}",
            ),
        )
        if index < len(schedule):
            next_start, next_assignment, _, _ = schedule[index]
            active_end = window_start + timedelta(hours=1)
            if active_end < next_start:
                next_revision = (
                    control if next_assignment == "control" else treatment
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
    return {"experiment_id": experiment_id}


def _issue_codes(run: dict[str, object]) -> set[str]:
    gate = run["evidence"]["quality_gate"]  # type: ignore[index]
    return {issue["code"] for issue in gate["issues"]}


def test_aa_then_switchback_produce_code_owned_effect_ci_lag_and_input_snapshot(
    tmp_path,
) -> None:
    db = Database(tmp_path / "traffic-analysis.sqlite3")
    db.initialize()
    control, treatment = _seed_revisions(db, "tenant-a")
    aa = _seed_experiment(
        db,
        "tenant-a",
        control,
        treatment,
        experiment_type="aa",
        start=BASE_TIME,
        control_ctr=0.05,
        treatment_ctr=0.05,
    )
    aa_run = TrafficAnalysisEngine(db).analyze_experiment(
        "tenant-a", str(aa["experiment_id"])
    )
    assert aa_run["method"] == "aa_v1"
    assert aa_run["effect_estimate"]["absolute"] == pytest.approx(0.0)
    assert aa_run["confidence_interval"]["includes_zero"] is True
    assert aa_run["evidence"]["quality_gate"]["status"] == "passed"
    assert aa_run["evidence"]["statistical_conclusion"] == "no_detectable_effect"
    assert aa_run["hypotheses"] == {
        "status": "not_generated",
        "reason": "model_not_configured",
    }

    switchback = _seed_experiment(
        db,
        "tenant-a",
        control,
        treatment,
        experiment_type="switchback",
        start=BASE_TIME + timedelta(days=3),
        control_ctr=0.05,
        treatment_ctr=0.08,
    )
    run = TrafficAnalysisEngine(db).analyze_experiment(
        "tenant-a", str(switchback["experiment_id"])
    )

    assert run["method"] == "switchback_uplift_v1"
    assert run["sample_size"]["control"] == {
        "bucket_count": 4,
        "impressions": 4_000,
        "metric_denominator": 4_000,
        "metric_numerator": 200,
    }
    assert run["sample_size"]["treatment"] == {
        "bucket_count": 4,
        "impressions": 4_000,
        "metric_denominator": 4_000,
        "metric_numerator": 320,
    }
    assert run["effect_estimate"]["absolute"] == pytest.approx(0.03)
    assert run["effect_estimate"]["direction"] == "positive"
    assert run["confidence_interval"]["low"] > 0
    assert run["confidence_interval"]["includes_zero"] is False
    assert run["effect_estimate"]["lag_analysis"]["best_supported_lag_minutes"] == 120
    assert run["evidence"]["quality_gate"] == {
        "status": "passed",
        "quality": "valid",
        "strong_conclusion_allowed": True,
        "issues": [],
    }
    assert run["evidence"]["statistical_conclusion"] == "positive_effect"
    assert set(run["data_window"]["included_bucket_ids"]) == set(
        switchback["included_bucket_ids"]
    )
    assert switchback["washout_bucket_id"] not in run["data_window"][
        "included_bucket_ids"
    ]
    assert {
        item["bucket_id"] for item in run["data_window"]["excluded_buckets"]
    } >= {switchback["washout_bucket_id"]}
    input_buckets = {
        item["bucket_id"]: item
        for item in run["evidence"]["input_snapshot"]["buckets"]
    }
    assert set(input_buckets) >= {
        *switchback["included_bucket_ids"],
        switchback["washout_bucket_id"],
    }
    assert input_buckets[switchback["washout_bucket_id"]]["disposition"] == "excluded"
    assert input_buckets[switchback["washout_bucket_id"]]["reason"] == "washout"
    assert input_buckets[switchback["washout_bucket_id"]]["values"]["clicks"] == 900
    excluded = {
        item["bucket_id"]: item for item in run["data_window"]["excluded_buckets"]
    }
    assert excluded[switchback["washout_bucket_id"]]["payload_hash"]
    assert excluded[switchback["washout_bucket_id"]]["version"] == 1
    assert run["analysis_code_version"] == "traffic-analysis-code-v2"


def test_switchback_requires_a_prior_clean_aa_gate(tmp_path) -> None:
    db = Database(tmp_path / "traffic-analysis-aa-gate.sqlite3")
    db.initialize()
    control, treatment = _seed_revisions(db, "tenant-no-aa")
    switchback = _seed_experiment(
        db,
        "tenant-no-aa",
        control,
        treatment,
        experiment_type="switchback",
        start=BASE_TIME + timedelta(days=3),
        control_ctr=0.05,
        treatment_ctr=0.08,
    )

    run = TrafficAnalysisEngine(db).analyze_experiment(
        "tenant-no-aa", str(switchback["experiment_id"])
    )

    assert run["effect_estimate"]["absolute"] == pytest.approx(0.03)
    assert run["confidence_interval"]["low"] > 0
    assert "aa_gate_missing" in _issue_codes(run)
    assert run["evidence"]["quality_gate"]["status"] == "blocked"
    assert run["evidence"]["quality_gate"]["strong_conclusion_allowed"] is False
    assert run["evidence"]["statistical_conclusion"] == "blocked"


def test_quality_gate_blocks_missing_controls_insufficient_samples_and_overlap(
    tmp_path,
) -> None:
    db = Database(tmp_path / "traffic-analysis-quality.sqlite3")
    db.initialize()

    missing_control, missing_treatment = _seed_revisions(
        db,
        "tenant-missing-control",
        attributes={"stock_status": "in_stock", "campaign": None},
    )
    missing = _seed_experiment(
        db,
        "tenant-missing-control",
        missing_control,
        missing_treatment,
        experiment_type="aa",
        start=BASE_TIME,
        control_ctr=0.05,
        treatment_ctr=0.05,
    )
    missing_run = TrafficAnalysisEngine(db).analyze_experiment(
        "tenant-missing-control", str(missing["experiment_id"])
    )
    assert "control_variable_missing" in _issue_codes(missing_run)
    missing_fields = {
        issue["field"]
        for issue in missing_run["evidence"]["quality_gate"]["issues"]
        if issue["code"] == "control_variable_missing"
    }
    assert {
        "category",
        "holiday_calendar_version",
        "store_traffic_baseline_version",
        "historical_ctr",
        "historical_cvr",
    } <= missing_fields
    assert missing_run["evidence"]["quality_gate"]["status"] == "blocked"

    sparse_control, sparse_treatment = _seed_revisions(db, "tenant-sparse")
    sparse = _seed_experiment(
        db,
        "tenant-sparse",
        sparse_control,
        sparse_treatment,
        experiment_type="aa",
        start=BASE_TIME,
        control_ctr=0.05,
        treatment_ctr=0.05,
        minimum_exposure=10_000,
    )
    sparse_run = TrafficAnalysisEngine(db).analyze_experiment(
        "tenant-sparse", str(sparse["experiment_id"])
    )
    assert "minimum_exposure_not_met" in _issue_codes(sparse_run)
    assert sparse_run["evidence"]["quality_gate"]["status"] == "blocked"

    overlap_control, overlap_treatment = _seed_revisions(db, "tenant-overlap")
    overlap = _seed_experiment(
        db,
        "tenant-overlap",
        overlap_control,
        overlap_treatment,
        experiment_type="aa",
        start=BASE_TIME,
        control_ctr=0.05,
        treatment_ctr=0.05,
        overlap=True,
    )
    overlap_run = TrafficAnalysisEngine(db).analyze_experiment(
        "tenant-overlap", str(overlap["experiment_id"])
    )
    assert "experiment_window_overlap" in _issue_codes(overlap_run)
    assert overlap_run["evidence"]["quality_gate"]["status"] == "blocked"


def test_ai_interpreter_can_add_explanations_but_cannot_override_statistics(
    tmp_path,
) -> None:
    db = Database(tmp_path / "traffic-analysis-boundary.sqlite3")
    db.initialize()
    control, treatment = _seed_revisions(db, "tenant-a")
    aa = _seed_experiment(
        db,
        "tenant-a",
        control,
        treatment,
        experiment_type="aa",
        start=BASE_TIME,
        control_ctr=0.05,
        treatment_ctr=0.05,
    )

    class AttemptedOverride:
        def interpret(self, facts: dict[str, object]) -> dict[str, object]:
            facts["effect_estimate"]["absolute"] = 99  # type: ignore[index]
            facts["sample_size"]["control"]["impressions"] = 1  # type: ignore[index]
            facts["confidence_interval"]["low"] = 99  # type: ignore[index]
            facts["evidence"]["quality_gate"]["status"] = "blocked"  # type: ignore[index]
            return {
                "summary": "尝试改写统计结果",
                "evidence_explanation": [],
                "counter_evidence_explanation": [],
                "mechanism_hypotheses": [],
                "next_experiments": [],
                "model_provider": "fixed-test-double",
                "model_name": "override-attempt",
                "prompt_version": "traffic-analysis-explain-v1",
                "effect_estimate": {"absolute": 99},
                "sample_size": {"control": {"impressions": 1}},
                "confidence_interval": {"low": 99},
                "quality_gate": {"status": "blocked"},
            }

    rejected = TrafficAnalysisEngine(db, interpreter=AttemptedOverride()).analyze_experiment(
        "tenant-a", str(aa["experiment_id"])
    )
    assert rejected["effect_estimate"]["absolute"] == pytest.approx(0.0)
    assert rejected["sample_size"]["control"]["impressions"] == 4_000
    assert rejected["confidence_interval"]["includes_zero"] is True
    assert rejected["evidence"]["quality_gate"]["status"] == "passed"
    assert rejected["hypotheses"] == {
        "status": "rejected",
        "reason": "invalid_interpretation_schema",
    }
    assert rejected["model_provider"] is None
    assert rejected["model_name"] is None

    class FixedInterpretation:
        def interpret(self, facts: dict[str, object]) -> dict[str, object]:
            effect = facts["effect_estimate"]
            assert isinstance(effect, dict)
            assert effect["absolute"] == pytest.approx(0.0)
            return {
                "summary": (
                    "A/A 未显示可检测差异，采集链路暂未制造稳定假阳性。"
                ),
                "evidence_explanation": ["effect 与区间均来自确定性分析结果。"],
                "counter_evidence_explanation": ["样本仍只覆盖两个自然日。"],
                "mechanism_hypotheses": [
                    {
                        "claim": "当前采集和窗口分配可用于下一轮 switchback。",
                        "evidence_refs": ["quality_gate", "confidence_interval"],
                        "counter_evidence_refs": ["analysis_limitations"],
                    }
                ],
                "next_experiments": [
                    {
                        "variable": "title",
                        "change": "只替换标题，保持图片、价格、库存与广告不变",
                        "expected_observation": "CTR 变化及后续推荐曝光 lag",
                    }
                ],
                "model_provider": "fixed-test-double",
                "model_name": "table-driven-v1",
                "prompt_version": "traffic-analysis-explain-v1",
            }

    explained = TrafficAnalysisEngine(db, interpreter=FixedInterpretation()).analyze_experiment(
        "tenant-a", str(aa["experiment_id"])
    )
    assert explained["effect_estimate"]["absolute"] == pytest.approx(0.0)
    assert explained["hypotheses"]["status"] == "generated"
    assert explained["hypotheses"]["mechanism_hypotheses"][0]["claim"].startswith(
        "当前采集"
    )
    assert explained["model_provider"] == "fixed-test-double"
    assert explained["model_name"] == "table-driven-v1"
    assert explained["prompt_version"] == "traffic-analysis-explain-v1"

    class UnavailableInterpreter:
        def interpret(self, facts: dict[str, object]) -> dict[str, object]:
            del facts
            raise RuntimeError("provider unavailable")

    unavailable = TrafficAnalysisEngine(
        db, interpreter=UnavailableInterpreter()
    ).analyze_experiment("tenant-a", str(aa["experiment_id"]))
    assert unavailable["effect_estimate"]["absolute"] == pytest.approx(0.0)
    assert unavailable["hypotheses"] == {
        "status": "unavailable",
        "reason": "interpreter_error",
    }


def test_cvr_direction_is_recovered_after_a_metric_specific_aa_gate(tmp_path) -> None:
    db = Database(tmp_path / "traffic-analysis-cvr.sqlite3")
    db.initialize()
    control, treatment = _seed_revisions(db, "tenant-cvr")
    aa = _seed_experiment(
        db,
        "tenant-cvr",
        control,
        treatment,
        experiment_type="aa",
        start=BASE_TIME,
        primary_metric="cvr",
        control_ctr=0.20,
        treatment_ctr=0.20,
        control_cvr=0.05,
        treatment_cvr=0.05,
    )
    aa_run = TrafficAnalysisEngine(db).analyze_experiment(
        "tenant-cvr", str(aa["experiment_id"])
    )
    assert aa_run["evidence"]["quality_gate"]["status"] == "passed"

    switchback = _seed_experiment(
        db,
        "tenant-cvr",
        control,
        treatment,
        experiment_type="switchback",
        start=BASE_TIME + timedelta(days=3),
        primary_metric="cvr",
        control_ctr=0.20,
        treatment_ctr=0.20,
        control_cvr=0.05,
        treatment_cvr=0.15,
    )
    run = TrafficAnalysisEngine(db).analyze_experiment(
        "tenant-cvr", str(switchback["experiment_id"])
    )

    assert run["effect_estimate"]["metric"] == "cvr"
    assert run["effect_estimate"]["absolute"] == pytest.approx(0.10)
    assert run["confidence_interval"]["low"] > 0
    assert run["evidence"]["quality_gate"]["strong_conclusion_allowed"] is True
    assert run["evidence"]["statistical_conclusion"] == "positive_effect"


def test_inventory_change_and_aa_false_positive_are_counter_evidence_gates(
    tmp_path,
) -> None:
    db = Database(tmp_path / "traffic-analysis-counter-evidence.sqlite3")
    db.initialize()
    control, treatment = _seed_revisions(
        db,
        "tenant-stock",
        treatment_attributes={
            "stock_status": "out_of_stock",
            "campaign": None,
            "ad_plan": "fixed-baseline",
        },
    )
    clean_aa = _seed_experiment(
        db,
        "tenant-stock",
        control,
        treatment,
        experiment_type="aa",
        start=BASE_TIME,
        control_ctr=0.05,
        treatment_ctr=0.05,
    )
    assert TrafficAnalysisEngine(db).analyze_experiment(
        "tenant-stock", str(clean_aa["experiment_id"])
    )["evidence"]["quality_gate"]["status"] == "passed"

    contaminated = _seed_experiment(
        db,
        "tenant-stock",
        control,
        treatment,
        experiment_type="switchback",
        start=BASE_TIME + timedelta(days=3),
        control_ctr=0.05,
        treatment_ctr=0.08,
    )
    contaminated_run = TrafficAnalysisEngine(db).analyze_experiment(
        "tenant-stock", str(contaminated["experiment_id"])
    )
    assert {"control_variable_changed", "stock_not_available"} <= _issue_codes(
        contaminated_run
    )
    assert contaminated_run["evidence"]["quality_gate"]["status"] == "blocked"
    assert contaminated_run["evidence"]["statistical_conclusion"] == "blocked"

    false_positive_control, false_positive_treatment = _seed_revisions(
        db, "tenant-aa-false-positive"
    )
    false_positive = _seed_experiment(
        db,
        "tenant-aa-false-positive",
        false_positive_control,
        false_positive_treatment,
        experiment_type="aa",
        start=BASE_TIME,
        control_ctr=0.05,
        treatment_ctr=0.08,
    )
    false_positive_run = TrafficAnalysisEngine(db).analyze_experiment(
        "tenant-aa-false-positive", str(false_positive["experiment_id"])
    )
    assert false_positive_run["confidence_interval"]["includes_zero"] is False
    assert "aa_false_positive_detected" in _issue_codes(false_positive_run)
    assert false_positive_run["evidence"]["quality_gate"]["status"] == "blocked"


def test_switchback_blocks_hour_distribution_confounding_instead_of_claiming_effect(
    tmp_path,
) -> None:
    db = Database(tmp_path / "traffic-analysis-hour-confound.sqlite3")
    db.initialize()
    tenant_id = "tenant-hour-confound"
    control, treatment = _seed_revisions(db, tenant_id)
    aa = _seed_experiment(
        db,
        tenant_id,
        control,
        treatment,
        experiment_type="aa",
        start=BASE_TIME,
        control_ctr=0.05,
        treatment_ctr=0.05,
    )
    assert TrafficAnalysisEngine(db).analyze_experiment(
        tenant_id, str(aa["experiment_id"])
    )["evidence"]["quality_gate"]["status"] == "passed"

    start = BASE_TIME + timedelta(days=3)
    schedule: list[tuple[datetime, str, float, float]] = []
    for day in range(4):
        if day < 3:
            assignments = ((0, "control"), (2, "treatment"))
        else:
            assignments = ((0, "treatment"), (2, "control"))
        schedule.extend(
            (
                start + timedelta(days=day, hours=hour),
                assignment,
                0.10 if hour == 0 else 0.01,
                0.05,
            )
            for hour, assignment in assignments
        )
    experiment = _seed_scheduled_switchback(
        db,
        tenant_id,
        control,
        treatment,
        schedule=schedule,
    )

    run = TrafficAnalysisEngine(db).analyze_experiment(
        tenant_id, str(experiment["experiment_id"])
    )

    assert "switchback_hour_distribution_imbalanced" in _issue_codes(run)
    assert run["evidence"]["quality_gate"]["strong_conclusion_allowed"] is False
    assert run["evidence"]["statistical_conclusion"] == "blocked"


def test_latest_failed_aa_invalidates_an_older_clean_gate(tmp_path) -> None:
    db = Database(tmp_path / "traffic-analysis-latest-aa.sqlite3")
    db.initialize()
    tenant_id = "tenant-latest-aa"
    control, treatment = _seed_revisions(db, tenant_id)
    clean = _seed_experiment(
        db,
        tenant_id,
        control,
        treatment,
        experiment_type="aa",
        start=BASE_TIME,
        control_ctr=0.05,
        treatment_ctr=0.05,
    )
    clean_run = TrafficAnalysisEngine(db).analyze_experiment(
        tenant_id, str(clean["experiment_id"])
    )
    assert clean_run["evidence"]["quality_gate"]["status"] == "passed"

    failed = _seed_experiment(
        db,
        tenant_id,
        control,
        treatment,
        experiment_type="aa",
        start=BASE_TIME + timedelta(days=2),
        control_ctr=0.05,
        treatment_ctr=0.08,
    )
    failed_run = TrafficAnalysisEngine(db).analyze_experiment(
        tenant_id, str(failed["experiment_id"])
    )
    assert failed_run["evidence"]["quality_gate"]["status"] == "blocked"

    switchback = _seed_experiment(
        db,
        tenant_id,
        control,
        treatment,
        experiment_type="switchback",
        start=BASE_TIME + timedelta(days=5),
        control_ctr=0.05,
        treatment_ctr=0.08,
    )
    run = TrafficAnalysisEngine(db).analyze_experiment(
        tenant_id, str(switchback["experiment_id"])
    )

    assert run["evidence"]["aa_gate"] == {
        "status": "failed",
        "analysis_run_id": failed_run["analysis_run_id"],
    }
    assert "aa_gate_failed" in _issue_codes(run)
    assert run["evidence"]["quality_gate"]["strong_conclusion_allowed"] is False


def test_mutated_aa_inputs_make_the_prior_gate_stale(tmp_path) -> None:
    db = Database(tmp_path / "traffic-analysis-stale-aa.sqlite3")
    db.initialize()
    tenant_id = "tenant-stale-aa"
    control, treatment = _seed_revisions(db, tenant_id)
    aa = _seed_experiment(
        db,
        tenant_id,
        control,
        treatment,
        experiment_type="aa",
        start=BASE_TIME,
        control_ctr=0.05,
        treatment_ctr=0.05,
    )
    aa_run = TrafficAnalysisEngine(db).analyze_experiment(
        tenant_id, str(aa["experiment_id"])
    )
    assert aa_run["evidence"]["quality_gate"]["status"] == "passed"

    service = TrafficLabService(db)
    original = service.get_metric_bucket(tenant_id, str(aa["included_bucket_ids"][0]))
    replacement = _metric(
        str(control["id"]),
        datetime.fromisoformat(original["metric_start"]),
        ctr=0.06,
        cvr=0.05,
        recommend_impressions=int(original["recommend_impressions"]),
        source_id=str(original["source_id"]),
    ).model_copy(
        update={
            "data_as_of": datetime.fromisoformat(original["data_as_of"])
            + timedelta(minutes=1)
        }
    )
    assert service.upsert_metric_bucket(tenant_id, replacement)["version"] == 2

    switchback = _seed_experiment(
        db,
        tenant_id,
        control,
        treatment,
        experiment_type="switchback",
        start=BASE_TIME + timedelta(days=3),
        control_ctr=0.05,
        treatment_ctr=0.08,
    )
    run = TrafficAnalysisEngine(db).analyze_experiment(
        tenant_id, str(switchback["experiment_id"])
    )

    assert run["evidence"]["aa_gate"] == {
        "status": "stale",
        "analysis_run_id": aa_run["analysis_run_id"],
    }
    assert "aa_gate_stale" in _issue_codes(run)
    assert run["evidence"]["quality_gate"]["strong_conclusion_allowed"] is False


def test_invalid_cvr_is_persisted_as_a_blocked_quality_result(tmp_path) -> None:
    db = Database(tmp_path / "traffic-analysis-invalid-cvr.sqlite3")
    db.initialize()
    tenant_id = "tenant-invalid-cvr"
    control, treatment = _seed_revisions(db, tenant_id)
    experiment = _seed_experiment(
        db,
        tenant_id,
        control,
        treatment,
        experiment_type="aa",
        start=BASE_TIME,
        primary_metric="cvr",
        control_ctr=0.05,
        treatment_ctr=0.05,
        control_cvr=30.0,
        treatment_cvr=30.0,
        minimum_exposure=0,
    )

    run = TrafficAnalysisEngine(db).analyze_experiment(
        tenant_id, str(experiment["experiment_id"])
    )

    assert "cvr_numerator_exceeds_denominator" in _issue_codes(run)
    assert run["effect_estimate"]["absolute"] is None
    assert run["evidence"]["quality_gate"]["status"] == "blocked"
    assert TrafficLabService(db).get_analysis_run(
        tenant_id, run["analysis_run_id"]
    )["analysis_run_id"] == run["analysis_run_id"]


def test_switchback_requires_per_change_washout_and_one_treatment_variable(
    tmp_path,
) -> None:
    db = Database(tmp_path / "traffic-analysis-design-gates.sqlite3")
    db.initialize()
    tenant_id = "tenant-design-gates"
    control, treatment = _seed_revisions(db, tenant_id)
    aa = _seed_experiment(
        db,
        tenant_id,
        control,
        treatment,
        experiment_type="aa",
        start=BASE_TIME,
        control_ctr=0.05,
        treatment_ctr=0.05,
    )
    assert TrafficAnalysisEngine(db).analyze_experiment(
        tenant_id, str(aa["experiment_id"])
    )["evidence"]["quality_gate"]["status"] == "passed"

    start = BASE_TIME + timedelta(days=3)
    schedule = [
        (start, "control", 0.05, 0.05),
        (start + timedelta(hours=1), "treatment", 0.05, 0.05),
        (start + timedelta(days=1), "treatment", 0.05, 0.05),
        (start + timedelta(days=1, hours=1), "control", 0.05, 0.05),
    ]
    experiment = _seed_scheduled_switchback(
        db,
        tenant_id,
        control,
        control,
        schedule=schedule,
        washout_window=60,
    )

    run = TrafficAnalysisEngine(db).analyze_experiment(
        tenant_id, str(experiment["experiment_id"])
    )

    assert {
        "switchback_washout_insufficient",
        "treatment_variable_missing",
    } <= _issue_codes(run)
    assert run["evidence"]["quality_gate"]["strong_conclusion_allowed"] is False


def test_interpreter_timeout_keeps_the_deterministic_run_available(tmp_path) -> None:
    db = Database(tmp_path / "traffic-analysis-interpreter-timeout.sqlite3")
    db.initialize()
    tenant_id = "tenant-interpreter-timeout"
    control, treatment = _seed_revisions(db, tenant_id)
    aa = _seed_experiment(
        db,
        tenant_id,
        control,
        treatment,
        experiment_type="aa",
        start=BASE_TIME,
        control_ctr=0.05,
        treatment_ctr=0.05,
    )
    release = Event()

    class BlockingInterpreter:
        def interpret(self, facts: dict[str, object]) -> dict[str, object]:
            del facts
            release.wait(timeout=1)
            return {
                "summary": "不应在超时后覆盖已持久化统计。",
                "model_provider": "fixed-test-double",
                "model_name": "blocking-v1",
                "prompt_version": "traffic-analysis-explain-v1",
            }

    started = time.monotonic()
    run = TrafficAnalysisEngine(
        db,
        interpreter=BlockingInterpreter(),
        interpretation_timeout_seconds=0.02,
    ).analyze_experiment(tenant_id, str(aa["experiment_id"]))
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 0.5
    assert run["effect_estimate"]["absolute"] == pytest.approx(0.0)
    assert run["hypotheses"] == {
        "status": "unavailable",
        "reason": "interpreter_timeout",
    }
    stored = TrafficLabService(db).get_analysis_run(
        tenant_id, run["analysis_run_id"]
    )
    assert stored["effect_estimate"] == run["effect_estimate"]
    assert stored["hypotheses"] == run["hypotheses"]


def test_deterministic_run_is_persisted_before_interpretation_starts(tmp_path) -> None:
    db = Database(tmp_path / "traffic-analysis-persist-first.sqlite3")
    db.initialize()
    tenant_id = "tenant-persist-first"
    control, treatment = _seed_revisions(db, tenant_id)
    aa = _seed_experiment(
        db,
        tenant_id,
        control,
        treatment,
        experiment_type="aa",
        start=BASE_TIME,
        control_ctr=0.05,
        treatment_ctr=0.05,
    )
    observed: dict[str, object] = {}

    class PersistenceInspectingInterpreter:
        def interpret(self, facts: dict[str, object]) -> dict[str, object]:
            del facts
            runs = TrafficLabService(db).list_analysis_runs(
                tenant_id, str(aa["experiment_id"])
            )
            observed["run_count"] = len(runs)
            observed["hypotheses"] = runs[0]["hypotheses"]
            return {
                "summary": "确定性记录已先于解释持久化。",
                "model_provider": "fixed-test-double",
                "model_name": "persistence-inspector-v1",
                "prompt_version": "traffic-analysis-explain-v1",
            }

    run = TrafficAnalysisEngine(
        db,
        interpreter=PersistenceInspectingInterpreter(),
    ).analyze_experiment(tenant_id, str(aa["experiment_id"]))

    assert observed == {
        "run_count": 1,
        "hypotheses": {
            "status": "pending",
            "reason": "interpretation_pending",
        },
    }
    assert run["hypotheses"]["status"] == "generated"


def test_legacy_and_unknown_policies_are_rejected_before_a_run_is_written(
    tmp_path,
) -> None:
    for policy_version in ("traffic-analysis-v1", "traffic-analysis-unknown"):
        db = Database(tmp_path / f"{policy_version}.sqlite3")
        db.initialize()
        control, treatment = _seed_revisions(db, "tenant-a")
        experiment = _seed_experiment(
            db,
            "tenant-a",
            control,
            treatment,
            experiment_type="aa",
            start=BASE_TIME,
            control_ctr=0.05,
            treatment_ctr=0.05,
            analysis_policy_version=policy_version,
        )

        with pytest.raises(ValueError, match="unsupported_analysis_policy_version"):
            TrafficAnalysisEngine(db).analyze_experiment(
                "tenant-a", str(experiment["experiment_id"])
            )

        assert TrafficLabService(db).list_analysis_runs(
            "tenant-a", str(experiment["experiment_id"])
        ) == []
