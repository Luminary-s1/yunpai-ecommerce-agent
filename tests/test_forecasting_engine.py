from __future__ import annotations

from datetime import date, timedelta

import pytest

from ecommerce_agent.forecasting import ForecastEngine, ForecastPolicy


def _series(values: list[float | int | None]) -> list[tuple[date, float | int | None]]:
    start = date(2026, 1, 1)
    return [(start + timedelta(days=offset), value) for offset, value in enumerate(values)]


def test_rolling_backtest_uses_only_each_origins_past() -> None:
    policy = ForecastPolicy(minimum_history_days=14, backtest_windows=4)
    engine = ForecastEngine(policy=policy)
    prefix = [8 + (index % 3) for index in range(49)]
    ordinary = engine.backtest(_series(prefix + [9] * 7))
    future_spike = engine.backtest(_series(prefix + [900] * 7))

    ordinary_by_key = {
        (row["model_name"], row["origin_date"]): row for row in ordinary
    }
    spike_by_key = {
        (row["model_name"], row["origin_date"]): row for row in future_spike
    }

    assert ordinary_by_key.keys() == spike_by_key.keys()
    origins = {name: tuple(row["origin_date"] for row in ordinary if row["model_name"] == name) for name in policy.candidate_models}
    assert len(set(origins.values())) == 1
    for key, row in ordinary_by_key.items():
        assert row["training_end"] < row["forecast_start"]
        assert row["forecast"] == spike_by_key[key]["forecast"]


def test_baseline_is_retained_when_new_model_has_no_required_improvement() -> None:
    result = ForecastEngine().evaluate(_series([10] * 56))

    assert result["champion_model"] in {
        "last_value",
        "seasonal_naive_7",
        "rolling_mean",
    }
    assert result["champion_reason"]["code"] == "baseline_retained"
    assert result["champion_reason"]["required_relative_improvement"] > 0
    assert {item["model_name"] for item in result["ranking"]} >= {
        "last_value",
        "seasonal_naive_7",
        "rolling_mean",
        "weighted_moving_average",
        "ewma",
        "croston",
        "tsb",
    }


@pytest.mark.parametrize(
    ("values", "expected_type"),
    [
        ([10] * 56, "stable"),
        (list(range(1, 57)), "rising_trend"),
        ([2, 4, 6, 8, 10, 12, 14] * 8, "weekly_seasonal"),
        ([{15: 13, 19: 13, 27: 13, 35: 5, 41: 3, 53: 3}.get(index, 0) for index in range(56)], "intermittent"),
        ([0] * 56, "intermittent"),
        ([3, 4, 3, 4], "cold_start"),
    ],
)
def test_sequence_types_and_intervals_are_deterministic(
    values: list[int], expected_type: str
) -> None:
    engine = ForecastEngine()

    first = engine.evaluate(_series(values))
    replay = engine.evaluate(_series(values))

    assert first == replay
    assert first["demand_type"] == expected_type
    if expected_type == "intermittent" and max(values) > 0:
        assert first["champion_reason"]["code"] == "challenger_improved"
    assert len(first["points"]) == 30
    assert set(first["horizon_totals"]) == {"7", "14", "30"}
    assert all(
        point["p50"] <= point["p80"] <= point["p95"]
        for point in first["points"]
    )


def test_failed_candidate_does_not_block_available_models() -> None:
    def fail_ewma(_values: list[float | None], _horizon: int) -> list[float]:
        raise RuntimeError("injected_model_failure")

    engine = ForecastEngine(forecaster_overrides={"ewma": fail_ewma})
    result = engine.evaluate(_series([7 + (index % 2) for index in range(56)]))
    ewma = next(item for item in result["ranking"] if item["model_name"] == "ewma")

    assert ewma["windows_successful"] == 0
    assert ewma["windows_failed"] > 0
    assert result["champion_model"] != "ewma"
    assert len(result["points"]) == 30


def test_zero_actual_windows_make_wape_incomparable_without_division_by_zero() -> None:
    result = ForecastEngine().evaluate(_series([0] * 56))

    assert result["metrics"]["wape"] is None
    assert result["metrics"]["bias"] is None
    assert result["metrics"]["rmse"] == 0
    assert result["champion_reason"]["comparison_metric"] == "rmse"
