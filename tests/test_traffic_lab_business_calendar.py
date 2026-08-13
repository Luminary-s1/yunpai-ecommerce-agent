from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ecommerce_agent.database import Database
from ecommerce_agent.service import AgentService
from ecommerce_agent.simulation import VirtualStoreSimulation
from ecommerce_agent.traffic_lab import (
    CreativeAssetCreate,
    ListingRevisionCreate,
    TrafficAnalysisEngine,
    TrafficExperimentCreate,
    TrafficLabService,
)

from conftest import make_settings


BASE_TIME = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def _window(window_id: str, assignment: str, start: datetime) -> dict[str, object]:
    return {
        "window_id": window_id,
        "assignment": assignment,
        "window_start": start.isoformat(),
        "window_end": (start + timedelta(hours=1)).isoformat(),
        "washout": False,
    }


def _issue_codes(
    engine: TrafficAnalysisEngine,
    timezone: str,
    windows: list[dict[str, object]],
) -> set[str]:
    issues: list[dict[str, object]] = []
    engine._check_switchback_design(
        {
            "washout_window": 0,
            "business_timezone": timezone,
            "business_calendar_id": "calendar-test",
            "business_calendar_version": 1,
            "business_calendar_policy_version": "store-business-calendar-v1",
        },
        windows,
        issues,
    )
    return {str(item["code"]) for item in issues}


def _seed_revisions(
    db: Database,
    *,
    tenant_id: str = "tenant-a",
    store_id: str = "store-a",
) -> tuple[TrafficLabService, dict[str, object], dict[str, object]]:
    service = TrafficLabService(db)
    asset = service.register_asset(
        tenant_id,
        CreativeAssetCreate(
            sha256="b" * 64,
            mime_type="image/png",
            width=1200,
            height=1200,
            storage_ref="objects/traffic-lab/business-calendar.png",
            source_ref="fixture://business-calendar",
            feature_schema_version="image-v1",
        ),
    )
    common = {
        "connector_id": "connector-a",
        "store_id": store_id,
        "item_id": "item-a",
        "sku_id": "sku-a",
        "main_image_asset_id": str(asset["asset_id"]),
        "sale_price": "109.00",
        "attributes": {"stock_status": "in_stock"},
        "active_from": BASE_TIME - timedelta(days=10),
        "active_to": BASE_TIME + timedelta(days=30),
    }
    control = service.create_revision(
        tenant_id,
        ListingRevisionCreate(
            **common,
            revision_no=1,
            title="日历实验基准",
            source_updated_at=BASE_TIME - timedelta(days=10),
        ),
    )
    treatment = service.create_revision(
        tenant_id,
        ListingRevisionCreate(
            **common,
            revision_no=2,
            title="日历实验处理",
            source_updated_at=BASE_TIME - timedelta(days=9),
        ),
    )
    return service, control, treatment


def _experiment(
    control: dict[str, object],
    treatment: dict[str, object],
    *,
    started_at: datetime,
) -> TrafficExperimentCreate:
    return TrafficExperimentCreate(
        store_id="store-a",
        sku_id="sku-a",
        experiment_type="switchback",
        primary_metric="ctr",
        started_at=started_at,
        ended_at=started_at + timedelta(days=2),
        control_revision_id=str(control["id"]),
        treatment_revision_id=str(treatment["id"]),
        minimum_exposure=100,
        washout_window=0,
        analysis_policy_version="traffic-analysis-v2",
    )


def _calendar_types():
    from ecommerce_agent.business_calendar import (
        StoreBusinessCalendarService,
        StoreBusinessCalendarUpsert,
    )

    return StoreBusinessCalendarService, StoreBusinessCalendarUpsert


def _unique_index_shapes(conn, table: str) -> set[tuple[str, ...]]:
    return {
        tuple(
            str(column[2])
            for column in conn.execute(f"PRAGMA index_info({index[1]})").fetchall()
        )
        for index in conn.execute(f"PRAGMA index_list({table})").fetchall()
        if bool(index[2])
    }


def test_v32_calendar_schema_is_versioned_and_legacy_experiment_fields_are_nullable(
    tmp_path,
) -> None:
    db = Database(tmp_path / "calendar-schema.sqlite3")
    db.initialize()

    with db.connect() as conn:
        migrations = {
            int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")
        }
        calendar_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(store_business_calendars)")
        }
        experiment_columns = {
            str(row[1]): row
            for row in conn.execute("PRAGMA table_info(traffic_experiments)")
        }
        unique_shapes = _unique_index_shapes(conn, "store_business_calendars")

    assert 32 in migrations
    assert {
        "tenant_id",
        "store_id",
        "timezone",
        "record_version",
        "effective_from",
        "changed_by",
        "policy_version",
    } <= calendar_columns
    assert ("tenant_id", "store_id", "record_version") in unique_shapes
    assert ("tenant_id", "store_id", "effective_from") in unique_shapes
    for field in (
        "business_calendar_id",
        "business_calendar_version",
        "business_timezone",
        "business_calendar_policy_version",
    ):
        assert field in experiment_columns
        assert int(experiment_columns[field][3]) == 0


def test_experiment_request_cannot_override_authoritative_business_calendar() -> None:
    payload = _experiment(
        {"id": "control"},
        {"id": "treatment"},
        started_at=BASE_TIME,
    ).model_dump()
    payload["business_timezone"] = "UTC"

    with pytest.raises(ValueError):
        TrafficExperimentCreate.model_validate(payload)


def test_switchback_uses_frozen_store_calendar_for_local_balance(tmp_path) -> None:
    db = Database(tmp_path / "local-balance.sqlite3")
    db.initialize()
    engine = TrafficAnalysisEngine(db)
    windows = [
        _window("c-1", "control", datetime(2026, 8, 9, 16, tzinfo=UTC)),
        _window("t-1", "treatment", datetime(2026, 8, 10, 15, tzinfo=UTC)),
        _window("t-2", "treatment", datetime(2026, 8, 10, 16, tzinfo=UTC)),
        _window("c-2", "control", datetime(2026, 8, 11, 15, tzinfo=UTC)),
    ]

    issues = _issue_codes(engine, "Asia/Shanghai", windows)

    assert "switchback_hour_distribution_imbalanced" not in issues
    assert "switchback_date_distribution_imbalanced" not in issues
    assert "switchback_weekday_distribution_imbalanced" not in issues


def test_switchback_rejects_utc_balance_that_is_locally_imbalanced(tmp_path) -> None:
    db = Database(tmp_path / "utc-false-green.sqlite3")
    db.initialize()
    engine = TrafficAnalysisEngine(db)
    windows = [
        _window("c-1", "control", datetime(2026, 8, 10, 0, tzinfo=UTC)),
        _window("t-1", "treatment", datetime(2026, 8, 10, 23, tzinfo=UTC)),
        _window("t-2", "treatment", datetime(2026, 8, 11, 0, tzinfo=UTC)),
        _window("c-2", "control", datetime(2026, 8, 11, 23, tzinfo=UTC)),
    ]

    issues = _issue_codes(engine, "Asia/Shanghai", windows)

    assert "switchback_date_distribution_imbalanced" in issues
    assert "switchback_weekday_distribution_imbalanced" in issues


def test_experiment_creation_requires_authoritative_store_calendar(tmp_path) -> None:
    db = Database(tmp_path / "calendar-required.sqlite3")
    db.initialize()
    service, control, treatment = _seed_revisions(db)

    with pytest.raises(ValueError, match="store_business_timezone_required"):
        service.create_experiment(
            "tenant-a",
            _experiment(
                control,
                treatment,
                started_at=BASE_TIME + timedelta(days=1),
            ),
        )


def test_calendar_is_tenant_store_scoped_and_rejects_unknown_iana_zone(tmp_path) -> None:
    db = Database(tmp_path / "calendar-scope.sqlite3")
    db.initialize()
    CalendarService, CalendarUpsert = _calendar_types()
    calendars = CalendarService(db)
    effective_from = BASE_TIME
    created = calendars.upsert_calendar(
        "tenant-a",
        CalendarUpsert(
            store_id="store-a",
            timezone="Asia/Shanghai",
            effective_from=effective_from,
            changed_by="admin-a",
        ),
    )

    assert created["record_version"] == 1
    assert created["changed_by"] == "admin-a"
    assert calendars.get_effective(
        "tenant-a", "store-a", at=effective_from
    )["timezone"] == "Asia/Shanghai"
    with pytest.raises(ValueError, match="store_business_calendar_not_found"):
        calendars.get_effective("tenant-b", "store-a", at=effective_from)
    with pytest.raises(ValueError, match="store_business_calendar_not_found"):
        calendars.get_effective("tenant-a", "store-b", at=effective_from)
    with pytest.raises(ValueError, match="business_timezone_invalid"):
        CalendarUpsert(
            store_id="store-a",
            timezone="Mars/Olympus",
            effective_from=effective_from,
            changed_by="admin-a",
        )


def test_experiments_freeze_calendar_version_and_old_version_replays(tmp_path) -> None:
    db = Database(tmp_path / "calendar-version.sqlite3")
    db.initialize()
    CalendarService, CalendarUpsert = _calendar_types()
    calendars = CalendarService(db)
    service, control, treatment = _seed_revisions(db)
    calendars.upsert_calendar(
        "tenant-a",
        CalendarUpsert(
            store_id="store-a",
            timezone="Asia/Shanghai",
            effective_from=BASE_TIME,
            changed_by="admin-a",
        ),
    )
    first = service.create_experiment(
        "tenant-a",
        _experiment(
            control,
            treatment,
            started_at=BASE_TIME + timedelta(days=1),
        ),
    )
    calendars.upsert_calendar(
        "tenant-a",
        CalendarUpsert(
            store_id="store-a",
            timezone="America/New_York",
            effective_from=BASE_TIME + timedelta(days=2),
            changed_by="admin-b",
        ),
    )
    second = service.create_experiment(
        "tenant-a",
        _experiment(
            control,
            treatment,
            started_at=BASE_TIME + timedelta(days=3),
        ),
    )

    reloaded_first = service.get_experiment(
        "tenant-a", str(first["experiment_id"])
    )
    assert reloaded_first["business_timezone"] == "Asia/Shanghai"
    assert reloaded_first["business_calendar_version"] == 1
    assert second["business_timezone"] == "America/New_York"
    assert second["business_calendar_version"] == 2
    first_analysis = TrafficAnalysisEngine(db).analyze_experiment(
        "tenant-a", str(first["experiment_id"])
    )
    assert first_analysis["evidence"]["business_calendar"] == {
        "calendar_id": first["business_calendar_id"],
        "record_version": 1,
        "timezone": "Asia/Shanghai",
        "policy_version": "store-business-calendar-v1",
    }


def test_legacy_experiment_without_frozen_calendar_is_readable_but_blocked(
    tmp_path,
) -> None:
    db = Database(tmp_path / "legacy-calendar.sqlite3")
    db.initialize()
    CalendarService, CalendarUpsert = _calendar_types()
    CalendarService(db).upsert_calendar(
        "tenant-a",
        CalendarUpsert(
            store_id="store-a",
            timezone="Asia/Shanghai",
            effective_from=BASE_TIME,
            changed_by="admin-a",
        ),
    )
    service, control, treatment = _seed_revisions(db)
    experiment = service.create_experiment(
        "tenant-a",
        _experiment(
            control,
            treatment,
            started_at=BASE_TIME + timedelta(days=1),
        ),
    )
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE traffic_experiments
            SET business_calendar_id=NULL,
                business_calendar_version=NULL,
                business_timezone=NULL,
                business_calendar_policy_version=NULL
            WHERE tenant_id=? AND experiment_id=?
            """,
            ("tenant-a", experiment["experiment_id"]),
        )

    readable = service.get_experiment("tenant-a", str(experiment["experiment_id"]))
    analysis = TrafficAnalysisEngine(db).analyze_experiment(
        "tenant-a", str(experiment["experiment_id"])
    )
    issue_codes = {
        item["code"] for item in analysis["evidence"]["quality_gate"]["issues"]
    }

    assert readable["business_timezone"] is None
    assert analysis["evidence"]["quality_gate"]["status"] == "blocked"
    assert "business_timezone_evidence_missing" in issue_codes


def test_switchback_calendar_uses_zoneinfo_across_dst_fold(tmp_path) -> None:
    db = Database(tmp_path / "calendar-dst.sqlite3")
    db.initialize()
    engine = TrafficAnalysisEngine(db)
    windows = [
        _window("c-1", "control", datetime(2026, 11, 1, 5, tzinfo=UTC)),
        _window("t-1", "treatment", datetime(2026, 11, 1, 6, tzinfo=UTC)),
        _window("c-2", "control", datetime(2026, 11, 2, 6, tzinfo=UTC)),
        _window("t-2", "treatment", datetime(2026, 11, 2, 6, tzinfo=UTC)),
    ]

    issues = _issue_codes(engine, "America/New_York", windows)

    assert "switchback_hour_distribution_imbalanced" not in issues


def test_simulate_store_persists_fixture_business_calendar(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        fixture = VirtualStoreSimulation._load_fixture()
        VirtualStoreSimulation(service).run(
            tenant_id="tenant-test",
            actor="admin-test",
            include_customer_service=False,
        )

        calendar = service.operations.business_calendars.get_latest(
            "tenant-test", fixture["store"]["store_id"]
        )

        assert calendar["timezone"] == fixture["store"]["timezone"]
        assert calendar["changed_by"] == "admin-test"
    finally:
        service.close()
