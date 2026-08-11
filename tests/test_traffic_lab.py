from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

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


def asset(**changes) -> CreativeAssetCreate:
    payload = {
        "sha256": "a" * 64,
        "mime_type": "image/png",
        "width": 1200,
        "height": 1200,
        "storage_ref": "objects/traffic-lab/a.png",
        "source_ref": "fixture://asset-a",
        "feature_schema_version": "image-v1",
    }
    payload.update(changes)
    return CreativeAssetCreate.model_validate(payload)


def revision(asset_id: str, **changes) -> ListingRevisionCreate:
    payload = {
        "connector_id": "virtual_taobao",
        "store_id": "store-001",
        "item_id": "item-001",
        "sku_id": "sku-001",
        "revision_no": 1,
        "title": "测试商品 标题 A",
        "main_image_asset_id": asset_id,
        "sale_price": "109.00",
        "attributes": {"stock_status": "in_stock", "campaign": None},
        "active_from": BASE_TIME,
        "active_to": BASE_TIME + timedelta(hours=4),
        "source_updated_at": BASE_TIME,
    }
    payload.update(changes)
    return ListingRevisionCreate.model_validate(payload)


def metric_bucket(revision_id: str | None, **changes) -> TrafficMetricBucketUpsert:
    payload = {
        "listing_revision_id": revision_id,
        "metric_start": BASE_TIME + timedelta(hours=1),
        "metric_end": BASE_TIME + timedelta(hours=2),
        "bucket_granularity": "hour",
        "traffic_source": "recommend",
        "impressions": 1000,
        "clicks": 80,
        "visitors": 75,
        "favorites": 8,
        "cart_adds": 5,
        "orders": 2,
        "sales_amount": "218.00",
        "ad_spend": "0",
        "search_impressions": 100,
        "recommend_impressions": 900,
        "data_as_of": BASE_TIME + timedelta(hours=3),
        "source_id": "metric-source-001",
    }
    payload.update(changes)
    return TrafficMetricBucketUpsert.model_validate(payload)


def experiment(control_id: str, treatment_id: str) -> TrafficExperimentCreate:
    return TrafficExperimentCreate(
        store_id="store-001",
        sku_id="sku-001",
        experiment_type="switchback",
        primary_metric="ctr",
        started_at=BASE_TIME,
        ended_at=BASE_TIME + timedelta(hours=4),
        control_revision_id=control_id,
        treatment_revision_id=treatment_id,
        minimum_exposure=1000,
        washout_window=15,
        analysis_policy_version="traffic-analysis-v2",
    )


def test_v27_database_upgrades_to_v28_without_rebuilding_existing_data(tmp_path) -> None:
    db = Database(tmp_path / "v27-traffic-lab.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 28):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-08-01T00:00:00+00:00"),
            )
        conn.execute("CREATE TABLE legacy_probe(id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO legacy_probe VALUES ('probe-1', 'preserved')")

    db.initialize()
    db.initialize()

    with db.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        migration_count = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=28"
        ).fetchone()[0]
        probe = conn.execute("SELECT value FROM legacy_probe WHERE id='probe-1'").fetchone()[0]
    assert Database.SCHEMA_VERSION >= 28
    assert {
        "creative_assets",
        "listing_revisions",
        "traffic_metric_buckets",
        "traffic_metric_quarantine",
        "traffic_experiments",
        "traffic_experiment_windows",
        "traffic_analysis_runs",
    } <= tables
    assert migration_count == 1
    assert probe == "preserved"


def test_asset_and_revision_contract_is_idempotent_immutable_and_reports_timeline_issues(
    tmp_path,
) -> None:
    db = Database(tmp_path / "traffic-lab.sqlite3")
    db.initialize()
    service = TrafficLabService(db)

    created_asset = service.register_asset("tenant-a", asset())
    repeated_asset = service.register_asset("tenant-a", asset())
    assert created_asset["write_status"] == "applied"
    assert repeated_asset["write_status"] == "idempotent"
    assert repeated_asset["asset_id"] == created_asset["asset_id"]
    with pytest.raises(ValueError, match="source_version_conflict"):
        service.register_asset("tenant-a", asset(width=800))
    with pytest.raises(ValueError, match="storage_ref_credentials_forbidden"):
        asset(storage_ref="https://user:secret@example.test/private/a.png")
    with pytest.raises(ValueError, match="storage_ref_not_approved"):
        asset(storage_ref="https://cdn.example.test/private/a.png")
    assert (
        asset(storage_ref="s3://approved-bucket/traffic-lab/a.png").storage_ref
        == "s3://approved-bucket/traffic-lab/a.png"
    )

    first = service.create_revision(
        "tenant-a",
        revision(
            created_asset["asset_id"],
            revision_no=1,
            active_to=BASE_TIME + timedelta(hours=1),
        ),
    )
    same = service.create_revision(
        "tenant-a",
        revision(
            created_asset["asset_id"],
            revision_no=1,
            active_to=BASE_TIME + timedelta(hours=1),
        ),
    )
    assert first["write_status"] == "applied"
    assert same["write_status"] == "idempotent"
    with pytest.raises(ValueError, match="source_version_conflict"):
        service.create_revision(
            "tenant-a",
            revision(
                created_asset["asset_id"],
                revision_no=1,
                title="同版本冲突标题",
                active_to=BASE_TIME + timedelta(hours=1),
            ),
        )

    service.create_revision(
        "tenant-a",
        revision(
            created_asset["asset_id"],
            revision_no=2,
            title="测试商品 标题 B",
            active_from=BASE_TIME + timedelta(hours=2),
            active_to=BASE_TIME + timedelta(hours=3),
            source_updated_at=BASE_TIME + timedelta(hours=2),
        ),
    )
    service.create_revision(
        "tenant-a",
        revision(
            created_asset["asset_id"],
            revision_no=3,
            title="测试商品 标题 C",
            active_from=BASE_TIME + timedelta(hours=2, minutes=30),
            active_to=BASE_TIME + timedelta(hours=4),
            source_updated_at=BASE_TIME + timedelta(hours=3),
        ),
    )
    quality = service.revision_timeline_quality(
        "tenant-a",
        connector_id="virtual_taobao",
        store_id="store-001",
        item_id="item-001",
        sku_id="sku-001",
    )
    assert quality["quality"] == "invalid"
    assert {issue["code"] for issue in quality["issues"]} == {
        "revision_window_gap",
        "revision_window_overlap",
    }

    with db.connect() as conn, pytest.raises(
        sqlite3.IntegrityError, match="listing_revision_immutable"
    ):
        conn.execute(
            "UPDATE listing_revisions SET title='mutated' WHERE id=?",
            (first["id"],),
        )


def test_metric_bucket_source_versions_trace_to_one_revision_and_asset(tmp_path) -> None:
    db = Database(tmp_path / "traffic-metrics.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    created_asset = service.register_asset("tenant-a", asset())
    created_revision = service.create_revision(
        "tenant-a", revision(created_asset["asset_id"])
    )

    first = service.ingest_metric_bucket(
        "tenant-a", metric_bucket(created_revision["id"])
    )
    same = service.ingest_metric_bucket(
        "tenant-a", metric_bucket(created_revision["id"])
    )
    assert first["disposition"] == "accepted"
    assert same["disposition"] == "accepted"
    assert first["write_status"] == "applied"
    assert same["write_status"] == "idempotent"
    assert same["version"] == 1

    with pytest.raises(ValueError, match="source_version_conflict"):
        service.upsert_metric_bucket(
            "tenant-a", metric_bucket(created_revision["id"], clicks=81)
        )

    corrected = service.upsert_metric_bucket(
        "tenant-a",
        metric_bucket(
            created_revision["id"],
            clicks=82,
            data_as_of=BASE_TIME + timedelta(hours=4),
        ),
    )
    assert corrected["write_status"] == "applied"
    assert corrected["version"] == 2
    with pytest.raises(ValueError, match="stale_source_version"):
        service.upsert_metric_bucket(
            "tenant-a", metric_bucket(created_revision["id"], clicks=79)
        )

    trace = service.trace_metric_bucket("tenant-a", corrected["id"])
    assert trace["metric"]["listing_revision_id"] == created_revision["id"]
    assert trace["revision"] == {
        "id": created_revision["id"],
        "revision_no": 1,
        "title": "测试商品 标题 A",
        "main_image_asset_id": created_asset["asset_id"],
        "sale_price": "109.00",
        "active_from": BASE_TIME.isoformat(),
        "active_to": (BASE_TIME + timedelta(hours=4)).isoformat(),
    }
    assert trace["asset"]["asset_id"] == created_asset["asset_id"]
    assert trace["asset"]["sha256"] == "a" * 64

    outside = metric_bucket(
        created_revision["id"],
        source_id="metric-outside",
        metric_start=BASE_TIME + timedelta(hours=4),
        metric_end=BASE_TIME + timedelta(hours=5),
    )
    with pytest.raises(ValueError, match="metric_outside_revision_window"):
        service.upsert_metric_bucket("tenant-a", outside)
    quarantined = service.ingest_metric_bucket("tenant-a", outside)
    assert quarantined["disposition"] == "quarantined"
    assert quarantined["reason_code"] == "metric_outside_revision_window"
    assert all(
        row["source_id"] != "metric-outside"
        for row in service.list_metric_buckets("tenant-a")
    )
    with pytest.raises(ValueError, match="clicks_cannot_exceed_impressions"):
        metric_bucket(created_revision["id"], clicks=1001)
    anomalous = service.upsert_metric_bucket(
        "tenant-a",
        metric_bucket(
            created_revision["id"],
            source_id="metric-anomalous",
            orders=1,
            sales_amount="0",
            data_as_of=BASE_TIME + timedelta(hours=5),
        ),
    )
    assert anomalous["orders"] == 1
    assert anomalous["sales_amount"] == "0"
    assert anomalous["quality_flags"] == ["orders_without_sales_amount"]


def test_unattributed_metric_is_versioned_in_quarantine_and_excluded_from_metrics(
    tmp_path,
) -> None:
    db = Database(tmp_path / "traffic-metric-quarantine.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    value = metric_bucket(
        None,
        source_id="metric-unattributed",
        data_as_of=BASE_TIME + timedelta(hours=3),
    )

    first = service.ingest_metric_bucket("tenant-a", value)
    same = service.ingest_metric_bucket("tenant-a", value)

    assert first["disposition"] == "quarantined"
    assert first["reason_code"] == "listing_revision_missing"
    assert first["write_status"] == "applied"
    assert same["write_status"] == "idempotent"
    assert same["quarantine_id"] == first["quarantine_id"]
    assert same["version"] == 1
    assert same["payload"]["listing_revision_id"] is None
    assert service.list_metric_buckets("tenant-a") == []
    assert [row["quarantine_id"] for row in service.list_metric_quarantine("tenant-a")] == [
        first["quarantine_id"]
    ]

    with pytest.raises(ValueError, match="source_version_conflict"):
        service.ingest_metric_bucket("tenant-a", value.model_copy(update={"clicks": 81}))

    corrected = service.ingest_metric_bucket(
        "tenant-a",
        value.model_copy(
            update={
                "clicks": 82,
                "data_as_of": BASE_TIME + timedelta(hours=4),
            }
        ),
    )
    assert corrected["quarantine_id"] == first["quarantine_id"]
    assert corrected["version"] == 2
    assert corrected["payload"]["clicks"] == 82
    with pytest.raises(ValueError, match="stale_source_version"):
        service.ingest_metric_bucket("tenant-a", value)

    assert service.list_metric_quarantine("tenant-b") == []
    with pytest.raises(ValueError, match="traffic_metric_quarantine_not_found"):
        service.get_metric_quarantine("tenant-b", first["quarantine_id"])


def test_metric_source_moves_between_accepted_and_quarantine_without_dual_state(
    tmp_path,
) -> None:
    db = Database(tmp_path / "traffic-metric-state.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    created_asset = service.register_asset("tenant-a", asset())
    created_revision = service.create_revision(
        "tenant-a", revision(created_asset["asset_id"])
    )
    accepted_value = metric_bucket(created_revision["id"])

    accepted = service.ingest_metric_bucket("tenant-a", accepted_value)
    quarantined = service.ingest_metric_bucket(
        "tenant-a",
        accepted_value.model_copy(
            update={
                "listing_revision_id": None,
                "data_as_of": BASE_TIME + timedelta(hours=4),
            }
        ),
    )

    assert accepted["version"] == 1
    assert quarantined["version"] == 2
    assert service.list_metric_buckets("tenant-a") == []
    assert len(service.list_metric_quarantine("tenant-a")) == 1
    with pytest.raises(ValueError, match="stale_source_version"):
        service.ingest_metric_bucket("tenant-a", accepted_value)

    resolved = service.ingest_metric_bucket(
        "tenant-a",
        accepted_value.model_copy(
            update={"data_as_of": BASE_TIME + timedelta(hours=5)}
        ),
    )
    assert resolved["disposition"] == "accepted"
    assert resolved["version"] == 3
    assert len(service.list_metric_buckets("tenant-a")) == 1
    assert service.list_metric_quarantine("tenant-a") == []


def test_experiment_windows_analysis_and_all_id_queries_are_tenant_scoped(tmp_path) -> None:
    db = Database(tmp_path / "traffic-experiments.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    created_asset = service.register_asset("tenant-a", asset())
    control = service.create_revision(
        "tenant-a",
        revision(
            created_asset["asset_id"],
            active_to=BASE_TIME + timedelta(hours=2),
        ),
    )
    treatment = service.create_revision(
        "tenant-a",
        revision(
            created_asset["asset_id"],
            revision_no=2,
            title="测试商品 标题 B",
            active_from=BASE_TIME + timedelta(hours=2),
            active_to=BASE_TIME + timedelta(hours=4),
            source_updated_at=BASE_TIME + timedelta(hours=2),
        ),
    )
    created_experiment = service.create_experiment(
        "tenant-a", experiment(control["id"], treatment["id"])
    )

    with pytest.raises(ValueError, match="invalid_experiment_transition"):
        service.transition_experiment(
            "tenant-a",
            created_experiment["experiment_id"],
            TrafficExperimentTransition(status="running"),
        )
    service.transition_experiment(
        "tenant-a",
        created_experiment["experiment_id"],
        TrafficExperimentTransition(status="ready"),
    )
    running = service.transition_experiment(
        "tenant-a",
        created_experiment["experiment_id"],
        TrafficExperimentTransition(status="running"),
    )
    assert running["status"] == "running"

    first_window = TrafficExperimentWindowCreate(
        listing_revision_id=control["id"],
        window_start=BASE_TIME,
        window_end=BASE_TIME + timedelta(hours=1),
        assignment="control",
        washout=False,
        source_receipt_id="receipt-001",
    )
    window = service.add_experiment_window(
        "tenant-a", created_experiment["experiment_id"], first_window
    )
    repeated = service.add_experiment_window(
        "tenant-a", created_experiment["experiment_id"], first_window
    )
    assert window["write_status"] == "applied"
    assert repeated["write_status"] == "idempotent"
    with pytest.raises(ValueError, match="source_version_conflict"):
        service.add_experiment_window(
            "tenant-a",
            created_experiment["experiment_id"],
            first_window.model_copy(update={"window_end": BASE_TIME + timedelta(hours=2)}),
        )

    service.add_experiment_window(
        "tenant-a",
        created_experiment["experiment_id"],
        TrafficExperimentWindowCreate(
            listing_revision_id=treatment["id"],
            window_start=BASE_TIME + timedelta(hours=2),
            window_end=BASE_TIME + timedelta(hours=3),
            assignment="treatment",
            washout=False,
            source_receipt_id="receipt-002",
        ),
    )
    service.add_experiment_window(
        "tenant-a",
        created_experiment["experiment_id"],
        TrafficExperimentWindowCreate(
            listing_revision_id=control["id"],
            window_start=BASE_TIME + timedelta(hours=2, minutes=30),
            window_end=BASE_TIME + timedelta(hours=4),
            assignment="control",
            washout=True,
            source_receipt_id=None,
        ),
    )
    quality = service.experiment_window_quality(
        "tenant-a", created_experiment["experiment_id"]
    )
    assert quality["quality"] == "invalid"
    assert {
        "experiment_window_gap",
        "experiment_window_overlap",
        "source_receipt_missing",
    } <= {issue["code"] for issue in quality["issues"]}

    analysis = TrafficAnalysisEngine(db).analyze_experiment(
        "tenant-a", created_experiment["experiment_id"]
    )
    assert service.get_analysis_run("tenant-a", analysis["analysis_run_id"])[
        "method"
    ] == "switchback_uplift_v1"
    assert "analysis_samples_missing" in {
        issue["code"] for issue in analysis["evidence"]["quality_gate"]["issues"]
    }

    for getter, row_id, error in (
        (service.get_asset, created_asset["asset_id"], "creative_asset_not_found"),
        (service.get_revision, control["id"], "listing_revision_not_found"),
        (service.get_experiment, created_experiment["experiment_id"], "traffic_experiment_not_found"),
        (service.get_analysis_run, analysis["analysis_run_id"], "traffic_analysis_run_not_found"),
    ):
        with pytest.raises(ValueError, match=error):
            getter("tenant-b", row_id)
    assert service.list_revisions("tenant-b") == []
    assert service.list_assets("tenant-b") == []
    assert service.list_metric_buckets("tenant-b") == []
    assert service.list_experiments("tenant-b") == []
    assert service.list_experiment_windows(
        "tenant-b", created_experiment["experiment_id"]
    ) == []
    assert service.list_analysis_runs(
        "tenant-b", created_experiment["experiment_id"]
    ) == []
    with pytest.raises(ValueError, match="listing_revision_not_found"):
        service.upsert_metric_bucket(
            "tenant-b",
            metric_bucket(control["id"], source_id="tenant-b-probe"),
        )
    with pytest.raises(ValueError, match="creative_asset_not_found"):
        service.create_revision("tenant-b", revision(created_asset["asset_id"]))
    with pytest.raises(ValueError, match="listing_revision_not_found"):
        service.create_experiment(
            "tenant-b", experiment(control["id"], treatment["id"])
        )
    with pytest.raises(ValueError, match="traffic_experiment_not_found"):
        service.add_experiment_window(
            "tenant-b", created_experiment["experiment_id"], first_window
        )
    with pytest.raises(ValueError, match="traffic_experiment_not_found"):
        TrafficAnalysisEngine(db).analyze_experiment(
            "tenant-b", created_experiment["experiment_id"]
        )
