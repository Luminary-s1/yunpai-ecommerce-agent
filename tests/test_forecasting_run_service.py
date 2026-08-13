from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

import pytest

from ecommerce_agent.database import Database
from ecommerce_agent.forecasting import (
    ForecastEngine,
    ForecastPolicy,
    ForecastRunError,
    ForecastRunService,
    SUPPORTED_FORECAST_MODELS,
)


TENANT = "tenant-forecast-run"
STORE = "store-forecast-run"
SKU = "sku-forecast-run"


class _FactSource:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def list_facts(
        self, tenant_id: str, *, store_id: str, sku_id: str, **_kwargs: object
    ) -> list[dict]:
        return [
            dict(row)
            for row in self.rows
            if row["tenant_id"] == tenant_id
            and row["store_id"] == store_id
            and row["sku_id"] == sku_id
        ]


class _RecordingEngine(ForecastEngine):
    def __init__(self) -> None:
        super().__init__()
        self.received_series: list[tuple[date, float | int | None]] = []

    def evaluate(
        self, series: Sequence[tuple[date, float | int | None]]
    ) -> dict[str, object]:
        self.received_series = list(series)
        return super().evaluate(self.received_series)


def _facts(values: list[int | None]) -> list[dict]:
    start = date(2026, 1, 1)
    return [
        {
            "id": f"fact-{offset}",
            "tenant_id": TENANT,
            "store_id": STORE,
            "sku_id": SKU,
            "business_date": (start + timedelta(days=offset)).isoformat(),
            "eligible_units": value,
            "stockout_flag": "false",
            "demand_policy_version": "demand-v1",
            "fact_version": 1,
            "payload_hash": f"hash-{offset}-{value}",
            "quality_flags": [],
        }
        for offset, value in enumerate(values)
    ]


def _service(tmp_path, rows: list[dict], *, engine: ForecastEngine | None = None):
    db = Database(tmp_path / "forecast-runs.sqlite3")
    db.initialize()
    return db, ForecastRunService(db, facts=_FactSource(rows), engine=engine)


def test_run_persists_replayable_policy_backtests_and_quantiles(tmp_path) -> None:
    db, service = _service(tmp_path, _facts([10] * 56))

    first = service.run(TENANT, store_id=STORE, sku_id=SKU)
    replay = service.run(TENANT, store_id=STORE, sku_id=SKU)

    assert first["status"] == "completed"
    assert first["champion_model"] in {
        "last_value",
        "seasonal_naive_7",
        "rolling_mean",
    }
    assert len(first["points"]) == 30
    assert first["backtests"]
    assert all(
        point["p50"] <= point["p80"] <= point["p95"]
        for point in first["points"]
    )
    assert all(
        row["training_end"] < row["forecast_start"] for row in first["backtests"]
    )
    assert first["data_hash"] == replay["data_hash"]
    assert first["points"] == replay["points"]
    with db.connect() as conn:
        policy_count = conn.execute(
            "SELECT COUNT(*) FROM forecast_policies WHERE tenant_id=? AND store_id=?",
            (TENANT, STORE),
        ).fetchone()[0]
    assert policy_count == 1
    conflict = ForecastRunService(
        db,
        facts=_FactSource(_facts([10] * 56)),
        engine=ForecastEngine(
            policy=ForecastPolicy(required_relative_improvement=0.5)
        ),
    )
    with pytest.raises(ForecastRunError, match="forecast_policy_version_conflict"):
        conflict.run(TENANT, store_id=STORE, sku_id=SKU)


def test_run_marks_gaps_stockouts_and_unknown_inventory_as_degraded(tmp_path) -> None:
    rows = _facts([8] * 56)
    rows.pop(20)
    rows[29]["stockout_flag"] = "true"
    rows[39]["stockout_flag"] = "unknown"
    engine = _RecordingEngine()
    _db, service = _service(tmp_path, rows, engine=engine)

    result = service.run(TENANT, store_id=STORE, sku_id=SKU)
    anomalies = {item["anomaly_type"]: item for item in result["anomalies"]}
    values_by_date = {
        business_date.isoformat(): value
        for business_date, value in engine.received_series
    }

    assert result["status"] == "degraded"
    assert {
        "missing_demand_day",
        "stockout_excluded",
        "stockout_unknown",
    } <= anomalies.keys()
    for anomaly_type in ("missing_demand_day", "stockout_excluded"):
        business_dates = anomalies[anomaly_type]["evidence"]["business_dates"]
        assert business_dates
        assert all(values_by_date[item] is None for item in business_dates)
    assert all(
        values_by_date[item] == 8
        for item in anomalies["stockout_unknown"]["evidence"]["business_dates"]
    )


def test_failed_candidate_is_persisted_without_blocking_the_run(tmp_path) -> None:
    def fail_ewma(_values: list[float | None], _horizon: int) -> list[float]:
        raise RuntimeError("injected_model_failure")

    engine = ForecastEngine(forecaster_overrides={"ewma": fail_ewma})
    _db, service = _service(tmp_path, _facts([6, 7] * 28), engine=engine)

    result = service.run(TENANT, store_id=STORE, sku_id=SKU)
    failed = [
        row
        for row in result["backtests"]
        if row["model_name"] == "ewma" and row["failure_reason"] is not None
    ]

    assert result["status"] == "completed"
    assert result["champion_model"] != "ewma"
    assert failed
    assert len(result["points"]) == 30


def test_final_forecast_fallback_selection_and_failure_are_persisted(tmp_path) -> None:
    values: list[int | None] = [1, 2, 3, 4, 5, 6, 7] * 9
    values[-1] = None
    _db, service = _service(tmp_path, _facts(values))

    result = service.run(TENANT, store_id=STORE, sku_id=SKU)

    assert result["champion_model"] == "rolling_mean"
    assert result["champion_reason"]["initial_champion_model"] == (
        "seasonal_naive_7"
    )
    assert result["champion_reason"]["fallback_applied"] is True
    failures = result["champion_reason"]["final_forecast_attempts"]
    assert failures[0]["model_name"] == "seasonal_naive_7"
    assert failures[0]["failure_reason"] == (
        "ValueError:complete_seven_day_season_required"
    )
    persisted_ranking = result["candidate_models"]["ranking"]
    seasonal = next(
        item for item in persisted_ranking
        if item["model_name"] == "seasonal_naive_7"
    )
    assert seasonal["eligible_for_final_forecast"] is False
    model_failure = next(
        item for item in result["anomalies"]
        if item["anomaly_type"] == "model_failure"
    )
    assert {
        "model_name": "seasonal_naive_7",
        "phase": "final_forecast",
        "failure_reason": "ValueError:complete_seven_day_season_required",
    }.items() <= model_failure["evidence"]["failures"][0].items()


def test_get_run_is_tenant_isolated(tmp_path) -> None:
    _db, service = _service(tmp_path, _facts([4] * 56))
    run = service.run(TENANT, store_id=STORE, sku_id=SKU)

    with pytest.raises(ForecastRunError, match="forecast_run_not_found"):
        service.get_run("other-tenant", run["run_id"])


def test_policy_resolution_prefers_sku_override_then_store_default(tmp_path) -> None:
    db, service = _service(tmp_path, _facts([4] * 56))
    store_default = ForecastPolicy(
        policy_version="forecast-store-default-v1",
        minimum_history_days=21,
    )
    sku_override = ForecastPolicy(
        policy_version="forecast-sku-override-v1",
        minimum_history_days=28,
    )
    with db.connect() as conn:
        service._ensure_policy(
            conn,
            TENANT,
            STORE,
            None,
            service._policy_evidence(store_default),
            "2026-08-12T00:00:00+00:00",
        )

    resolved_default = service.resolve_policy(
        TENANT, store_id=STORE, sku_id="sku-without-override"
    )
    assert resolved_default is not None
    assert resolved_default.policy_version == "forecast-store-default-v1"
    assert resolved_default.minimum_history_days == 21

    with db.connect() as conn:
        service._ensure_policy(
            conn,
            TENANT,
            STORE,
            SKU,
            service._policy_evidence(sku_override),
            "2026-08-12T01:00:00+00:00",
        )

    resolved_override = service.resolve_policy(TENANT, store_id=STORE, sku_id=SKU)
    assert resolved_override is not None
    assert resolved_override.policy_version == "forecast-sku-override-v1"
    assert resolved_override.minimum_history_days == 28


def test_policy_resolution_breaks_equal_timestamps_by_newest_rowid(tmp_path) -> None:
    db, service = _service(tmp_path, _facts([4] * 56))
    created_at = "2026-08-12T00:00:00+00:00"
    older = ForecastPolicy(
        policy_version="forecast-same-time-zzz",
        minimum_history_days=21,
    )
    newer = ForecastPolicy(
        policy_version="forecast-same-time-aaa",
        minimum_history_days=28,
    )
    with db.connect() as conn:
        for policy in (older, newer):
            service._ensure_policy(
                conn,
                TENANT,
                STORE,
                SKU,
                service._policy_evidence(policy),
                created_at,
            )
        conn.execute(
            """CREATE INDEX test_forecast_policy_scan_order
            ON forecast_policies(
                tenant_id, store_id, sku_id, policy_version DESC
            )"""
        )
        conn.execute("ANALYZE")

    resolved = service.resolve_policy(TENANT, store_id=STORE, sku_id=SKU)

    assert resolved is not None
    assert resolved.policy_version == "forecast-same-time-aaa"
    assert resolved.minimum_history_days == 28


def test_all_failed_candidates_return_an_explicit_engine_failure(tmp_path) -> None:
    def fail(_values: list[float | None], _horizon: int) -> list[float]:
        raise RuntimeError("injected_model_failure")

    engine = ForecastEngine(
        forecaster_overrides={name: fail for name in SUPPORTED_FORECAST_MODELS}
    )
    _db, service = _service(tmp_path, _facts([5] * 56), engine=engine)

    with pytest.raises(
        ForecastRunError,
        match="forecast_engine_failed:forecast_final_candidates_failed",
    ):
        service.run(TENANT, store_id=STORE, sku_id=SKU)
