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


def _facts(values: list[int]) -> list[dict]:
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


def test_get_run_is_tenant_isolated(tmp_path) -> None:
    _db, service = _service(tmp_path, _facts([4] * 56))
    run = service.run(TENANT, store_id=STORE, sku_id=SKU)

    with pytest.raises(ForecastRunError, match="forecast_run_not_found"):
        service.get_run("other-tenant", run["run_id"])


def test_all_failed_candidates_return_an_explicit_engine_failure(tmp_path) -> None:
    def fail(_values: list[float | None], _horizon: int) -> list[float]:
        raise RuntimeError("injected_model_failure")

    engine = ForecastEngine(
        forecaster_overrides={name: fail for name in SUPPORTED_FORECAST_MODELS}
    )
    _db, service = _service(tmp_path, _facts([5] * 56), engine=engine)

    with pytest.raises(ForecastRunError, match="forecast_engine_failed:forecast_baseline_failed"):
        service.run(TENANT, store_id=STORE, sku_id=SKU)
