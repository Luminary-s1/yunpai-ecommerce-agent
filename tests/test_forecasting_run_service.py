from __future__ import annotations

from datetime import date, timedelta

from ecommerce_agent.database import Database
from ecommerce_agent.forecasting import (
    ForecastEngine,
    ForecastRunService,
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
