from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Mapping, Sequence


Forecaster = Callable[[list[float | None], int], list[float]]
FORECAST_ENGINE_VERSION = "forecast-engine-v2"
FINAL_SELECTION_POLICY_VERSION = "forecast-final-selection-v1"
_BASELINE_ORDER = ("rolling_mean", "last_value", "seasonal_naive_7")
_BASELINES = frozenset(_BASELINE_ORDER)


def _known(values: Sequence[float | None]) -> list[float]:
    return [value for value in values if value is not None]


def _last_value(values: list[float | None], horizon: int) -> list[float]:
    observed = _known(values)
    if not observed:
        raise ValueError("no_observed_demand")
    return [observed[-1]] * horizon


def _seasonal_naive(values: list[float | None], horizon: int) -> list[float]:
    season = values[-7:]
    if len(season) < 7 or any(value is None for value in season):
        raise ValueError("complete_seven_day_season_required")
    return [float(season[offset % 7]) for offset in range(horizon)]


def _rolling_mean(values: list[float | None], horizon: int) -> list[float]:
    observed = _known(values)[-7:]
    if not observed:
        raise ValueError("no_observed_demand")
    return [statistics.fmean(observed)] * horizon


def _weighted_mean(values: list[float | None], horizon: int) -> list[float]:
    observed = _known(values)[-7:]
    if not observed:
        raise ValueError("no_observed_demand")
    weights = list(range(1, len(observed) + 1))
    level = sum(value * weight for value, weight in zip(observed, weights, strict=True)) / sum(
        weights
    )
    return [level] * horizon


def _ewma(values: list[float | None], horizon: int) -> list[float]:
    observed = _known(values)
    if not observed:
        raise ValueError("no_observed_demand")
    level = observed[0]
    for value in observed[1:]:
        level = 0.3 * value + 0.7 * level
    return [level] * horizon


def _croston(values: list[float | None], horizon: int) -> list[float]:
    size = interval = None
    gap = 1
    for value in values:
        if value is None:
            continue
        if value > 0:
            if size is None:
                size, interval = value, float(gap)
            else:
                size += 0.1 * (value - size)
                interval += 0.1 * (gap - interval)
            gap = 1
        else:
            gap += 1
    level = 0.0 if size is None or interval is None else size / interval
    return [level] * horizon


def _tsb(values: list[float | None], horizon: int) -> list[float]:
    observed = _known(values)
    if not observed:
        raise ValueError("no_observed_demand")
    probability = 1.0 if observed[0] > 0 else 0.0
    size = observed[0] if observed[0] > 0 else None
    for value in observed[1:]:
        occurred = 1.0 if value > 0 else 0.0
        probability += 0.1 * (occurred - probability)
        if value > 0:
            size = value if size is None else size + 0.1 * (value - size)
    return [probability * (size or 0.0)] * horizon


_FORECASTERS: dict[str, Forecaster] = {
    "last_value": _last_value,
    "seasonal_naive_7": _seasonal_naive,
    "rolling_mean": _rolling_mean,
    "weighted_moving_average": _weighted_mean,
    "ewma": _ewma,
    "croston": _croston,
    "tsb": _tsb,
}
SUPPORTED_FORECAST_MODELS = tuple(_FORECASTERS)
PRODUCT_FORECAST_HORIZONS = (7, 14, 30)


@dataclass(frozen=True)
class ForecastPolicy:
    policy_version: str = "forecast-v1"
    horizons: tuple[int, ...] = PRODUCT_FORECAST_HORIZONS
    minimum_history_days: int = 14
    backtest_windows: int = 4
    required_relative_improvement: float = 0.02
    interval_levels: tuple[float, float, float] = (0.5, 0.8, 0.95)
    candidate_models: tuple[str, ...] = SUPPORTED_FORECAST_MODELS

    def __post_init__(self) -> None:
        if self.minimum_history_days < 1 or self.backtest_windows < 1:
            raise ValueError("forecast_policy_positive_bounds_required")
        if not self.horizons or any(value < 1 for value in self.horizons):
            raise ValueError("forecast_policy_horizons_invalid")
        if tuple(sorted(set(self.horizons))) != self.horizons:
            raise ValueError("forecast_policy_horizons_invalid")
        if not 0 <= self.required_relative_improvement < 1:
            raise ValueError("forecast_policy_improvement_invalid")
        if self.interval_levels != (0.5, 0.8, 0.95):
            raise ValueError("forecast_policy_interval_levels_invalid")
        if not set(self.candidate_models) <= set(SUPPORTED_FORECAST_MODELS):
            raise ValueError("forecast_policy_model_unsupported")
        if len(set(self.candidate_models)) != len(self.candidate_models):
            raise ValueError("forecast_policy_model_duplicate")
        if not set(self.candidate_models) & _BASELINES:
            raise ValueError("forecast_policy_baseline_required")

    @property
    def backtest_horizon(self) -> int:
        return min(self.horizons)


class ForecastEngine:
    """Deterministic pure-Python forecasting and rolling-origin model selection."""

    def __init__(
        self,
        *,
        policy: ForecastPolicy | None = None,
        forecaster_overrides: Mapping[str, Forecaster] | None = None,
    ) -> None:
        self.policy = policy or ForecastPolicy()
        self.forecasters = dict(_FORECASTERS)
        for name, forecaster in (forecaster_overrides or {}).items():
            if name not in self.forecasters:
                raise ValueError("forecast_override_model_unsupported")
            self.forecasters[name] = forecaster

    def backtest(
        self, series: Sequence[tuple[date, float | int | None]]
    ) -> list[dict[str, object]]:
        dates, values = self._validate_series(series)
        horizon = self.policy.backtest_horizon
        origins = list(
            range(self.policy.minimum_history_days, len(values) - horizon + 1, horizon)
        )[-self.policy.backtest_windows :]
        origins = [
            origin for origin in origins if all(value is not None for value in values[origin : origin + horizon])
        ]
        rows: list[dict[str, object]] = []
        for origin in origins:
            actual = [float(value) for value in values[origin : origin + horizon] if value is not None]
            for model_name in self.policy.candidate_models:
                failure_reason = None
                forecast: list[float] = []
                metrics = self._metrics([], [])
                try:
                    forecast = self._validated_forecast(
                        self.forecasters[model_name](values[:origin], horizon), horizon
                    )
                    metrics = self._metrics(actual, forecast)
                except (ArithmeticError, ValueError, RuntimeError) as exc:
                    failure_reason = f"{type(exc).__name__}:{exc}"
                rows.append(
                    {
                        "model_name": model_name,
                        "origin_date": dates[origin].isoformat(),
                        "training_start": dates[0].isoformat(),
                        "training_end": dates[origin - 1].isoformat(),
                        "forecast_start": dates[origin].isoformat(),
                        "forecast_end": dates[origin + horizon - 1].isoformat(),
                        "actual": actual,
                        "forecast": forecast,
                        "metrics": metrics,
                        "failure_reason": failure_reason,
                    }
                )
        return rows

    def evaluate(
        self, series: Sequence[tuple[date, float | int | None]]
    ) -> dict[str, object]:
        dates, values = self._validate_series(series)
        demand_type = self._demand_type(values)
        backtests = self.backtest(list(zip(dates, values, strict=True)))
        ranking = self._rank(backtests)
        horizon = max(self.policy.horizons)
        for item in ranking:
            item["eligible_for_final_forecast"] = bool(
                item["eligible_for_champion"]
            )
            item["final_forecast_status"] = "not_attempted"
            item["final_forecast_failure_reason"] = None
        if not backtests:
            cold_start_candidates = [
                name
                for name in _BASELINE_ORDER
                if name in self.policy.candidate_models
            ]
            for item in ranking:
                item["eligible_for_final_forecast"] = (
                    item["model_name"] in cold_start_candidates
                )
            champion, forecast, attempts = self._forecast_cold_start(
                values,
                horizon,
                cold_start_candidates,
                ranking,
            )
            reason = {
                "code": "cold_start_baseline",
                "comparison_metric": None,
                "baseline_model": champion,
                "required_relative_improvement": self.policy.required_relative_improvement,
            }
            metrics = self._metrics([], [])
        else:
            champion, reason, forecast, attempts = self._forecast_ranked_candidate(
                values,
                horizon,
                ranking,
            )
            metrics = next(item["metrics"] for item in ranking if item["model_name"] == champion)
        reason = {
            **reason,
            "selection_policy_version": FINAL_SELECTION_POLICY_VERSION,
            "initial_champion_model": attempts[0]["model_name"],
            "fallback_applied": len(attempts) > 1,
            "final_forecast_attempts": attempts,
        }
        residuals = [
            abs(actual - predicted)
            for row in backtests
            if row["model_name"] == champion and row["failure_reason"] is None
            for actual, predicted in zip(row["actual"], row["forecast"], strict=True)
        ]
        if residuals:
            upper80, upper95 = (
                self._quantile(residuals, level) for level in self.policy.interval_levels[1:]
            )
        else:
            scale = max(1.0, statistics.fmean(_known(values)) * 0.5)
            upper80, upper95 = scale, scale * 2
        points = [
            {
                "forecast_date": (dates[-1] + timedelta(days=offset + 1)).isoformat(),
                "p50": self._rounded(value),
                "p80": self._rounded(value + upper80),
                "p95": self._rounded(value + upper95),
            }
            for offset, value in enumerate(forecast)
        ]
        return {
            "model_version": FORECAST_ENGINE_VERSION,
            "demand_type": demand_type,
            "quality_status": "degraded" if not backtests else "ready",
            "training_start": dates[0].isoformat(),
            "training_end": dates[-1].isoformat(),
            "candidate_models": list(self.policy.candidate_models),
            "ranking": ranking,
            "champion_model": champion,
            "champion_reason": reason,
            "metrics": metrics,
            "backtests": backtests,
            "points": points,
            "horizon_totals": {
                str(horizon): {
                    quantile: self._rounded(sum(float(point[quantile]) for point in points[:horizon]))
                    for quantile in ("p50", "p80", "p95")
                }
                for horizon in self.policy.horizons
            },
        }

    def _rank(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        ranking: list[dict[str, object]] = []
        for name in self.policy.candidate_models:
            model_rows = [row for row in rows if row["model_name"] == name]
            successful = [row for row in model_rows if row["failure_reason"] is None]
            actual = [float(value) for row in successful for value in row["actual"]]
            forecast = [float(value) for row in successful for value in row["forecast"]]
            metrics = self._metrics(actual, forecast)
            comparison_metric = "wape" if metrics["wape"] is not None else "rmse"
            eligible = bool(successful) and len(successful) == len(model_rows)
            ranking.append(
                {
                    "model_name": name,
                    "is_baseline": name in _BASELINES,
                    "windows_successful": len(successful),
                    "windows_failed": len(model_rows) - len(successful),
                    "eligible_for_champion": eligible,
                    "comparison_metric": comparison_metric,
                    "score": metrics[comparison_metric],
                    "metrics": metrics,
                }
            )
        return sorted(
            ranking,
            key=lambda item: (
                not item["eligible_for_champion"],
                float(item["score"]) if item["score"] is not None else math.inf,
                str(item["model_name"]),
            ),
        )

    def _select_champion(
        self,
        ranking: list[dict[str, object]],
        *,
        eligibility_field: str = "eligible_for_champion",
    ) -> tuple[str, dict[str, object]]:
        usable = [item for item in ranking if item[eligibility_field]]
        baseline = next((item for item in usable if item["is_baseline"]), None)
        challenger = next((item for item in usable if not item["is_baseline"]), None)
        if baseline is None:
            raise ValueError("forecast_baseline_failed")
        baseline_score = float(baseline["score"])
        improved = (
            challenger is not None
            and baseline_score > 0
            and float(challenger["score"])
            <= baseline_score * (1 - self.policy.required_relative_improvement)
        )
        champion = challenger if improved else baseline
        return str(champion["model_name"]), {
            "code": "challenger_improved" if improved else "baseline_retained",
            "comparison_metric": champion["comparison_metric"],
            "baseline_model": baseline["model_name"],
            "challenger_model": challenger["model_name"] if challenger else None,
            "required_relative_improvement": self.policy.required_relative_improvement,
        }

    def _forecast_ranked_candidate(
        self,
        values: list[float | None],
        horizon: int,
        ranking: list[dict[str, object]],
    ) -> tuple[str, dict[str, object], list[float], list[dict[str, object]]]:
        attempts: list[dict[str, object]] = []
        while True:
            try:
                champion, reason = self._select_champion(
                    ranking,
                    eligibility_field="eligible_for_final_forecast",
                )
            except ValueError as exc:
                if str(exc) != "forecast_baseline_failed":
                    raise
                raise ValueError("forecast_final_candidates_failed") from exc
            item = next(
                candidate
                for candidate in ranking
                if candidate["model_name"] == champion
            )
            try:
                forecast = self._validated_forecast(
                    self.forecasters[champion](values, horizon),
                    horizon,
                )
            except (ArithmeticError, ValueError, RuntimeError) as exc:
                failure_reason = f"{type(exc).__name__}:{exc}"
                item["eligible_for_final_forecast"] = False
                item["final_forecast_status"] = "failed"
                item["final_forecast_failure_reason"] = failure_reason
                attempts.append(
                    {
                        "model_name": champion,
                        "status": "failed",
                        "failure_reason": failure_reason,
                    }
                )
                continue
            item["final_forecast_status"] = "selected"
            attempts.append(
                {
                    "model_name": champion,
                    "status": "selected",
                    "failure_reason": None,
                }
            )
            return champion, reason, forecast, attempts

    def _forecast_cold_start(
        self,
        values: list[float | None],
        horizon: int,
        candidates: list[str],
        ranking: list[dict[str, object]],
    ) -> tuple[str, list[float], list[dict[str, object]]]:
        attempts: list[dict[str, object]] = []
        by_name = {str(item["model_name"]): item for item in ranking}
        for champion in candidates:
            item = by_name[champion]
            try:
                forecast = self._validated_forecast(
                    self.forecasters[champion](values, horizon),
                    horizon,
                )
            except (ArithmeticError, ValueError, RuntimeError) as exc:
                failure_reason = f"{type(exc).__name__}:{exc}"
                item["eligible_for_final_forecast"] = False
                item["final_forecast_status"] = "failed"
                item["final_forecast_failure_reason"] = failure_reason
                attempts.append(
                    {
                        "model_name": champion,
                        "status": "failed",
                        "failure_reason": failure_reason,
                    }
                )
                continue
            item["final_forecast_status"] = "selected"
            attempts.append(
                {
                    "model_name": champion,
                    "status": "selected",
                    "failure_reason": None,
                }
            )
            return champion, forecast, attempts
        raise ValueError("forecast_final_candidates_failed")

    def _demand_type(self, values: list[float | None]) -> str:
        observed = _known(values)
        if len(observed) < self.policy.minimum_history_days:
            return "cold_start"
        if sum(value == 0 for value in observed) / len(observed) >= 0.5:
            return "intermittent"
        adjacent = [abs(right - left) for left, right in zip(observed, observed[1:])]
        seasonal = [
            abs(values[index] - values[index - 7])
            for index in range(7, len(values))
            if values[index] is not None and values[index - 7] is not None
        ]
        if (
            len(seasonal) >= 14
            and statistics.fmean(adjacent) > 0.05
            and statistics.fmean(seasonal) <= statistics.fmean(adjacent) * 0.25
        ):
            return "weekly_seasonal"
        third = max(1, len(observed) // 3)
        change = statistics.fmean(observed[-third:]) - statistics.fmean(observed[:third])
        threshold = max(1.0, abs(statistics.fmean(observed)) * 0.25)
        if abs(change) >= threshold:
            return "rising_trend" if change > 0 else "falling_trend"
        if max(observed) - min(observed) <= max(1.0, statistics.fmean(observed) * 0.15):
            return "stable"
        return "variable"

    @staticmethod
    def _validate_series(
        series: Sequence[tuple[date, float | int | None]]
    ) -> tuple[list[date], list[float | None]]:
        if not series:
            raise ValueError("forecast_series_required")
        dates = [item[0] for item in series]
        if any(right != left + timedelta(days=1) for left, right in zip(dates, dates[1:])):
            raise ValueError("forecast_series_must_be_daily_contiguous")
        values = [None if item[1] is None else float(item[1]) for item in series]
        if any(value is not None and (not math.isfinite(value) or value < 0) for value in values):
            raise ValueError("forecast_series_value_invalid")
        if not _known(values):
            raise ValueError("no_observed_demand")
        return dates, values

    @staticmethod
    def _validated_forecast(values: list[float], horizon: int) -> list[float]:
        if len(values) != horizon or any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("forecast_output_invalid")
        return values

    @classmethod
    def _metrics(cls, actual: list[float], forecast: list[float]) -> dict[str, float | None]:
        if not actual:
            return {"wape": None, "bias": None, "smape": None, "rmse": None}
        errors = [predicted - observed for observed, predicted in zip(actual, forecast, strict=True)]
        denominator = sum(actual)
        smape_terms = [
            0.0 if observed == predicted == 0 else 2 * abs(predicted - observed) / (observed + predicted)
            for observed, predicted in zip(actual, forecast, strict=True)
        ]
        return {
            "wape": None if denominator == 0 else cls._rounded(sum(abs(value) for value in errors) / denominator),
            "bias": None if denominator == 0 else cls._rounded(sum(errors) / denominator),
            "smape": cls._rounded(statistics.fmean(smape_terms)),
            "rmse": cls._rounded(math.sqrt(statistics.fmean(value * value for value in errors))),
        }

    @staticmethod
    def _quantile(values: list[float], level: float) -> float:
        ordered = sorted(values)
        position = (len(ordered) - 1) * level
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    @staticmethod
    def _rounded(value: float) -> float:
        return round(value, 9)
