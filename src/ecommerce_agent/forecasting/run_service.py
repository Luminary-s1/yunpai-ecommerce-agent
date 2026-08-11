from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Protocol

from ..database import Database, utc_now
from .engine import ForecastEngine
from .models import DEMAND_V1


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ForecastRunError(ValueError):
    """Raised when a persisted forecast run cannot be built or read safely."""


class DemandFactReader(Protocol):
    def list_facts(
        self,
        tenant_id: str,
        *,
        store_id: str,
        sku_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        include_history: bool = False,
    ) -> list[dict[str, Any]]: ...


class ForecastRunService:
    """Read demand-v1 facts and atomically persist deterministic forecast evidence."""

    def __init__(
        self,
        db: Database,
        *,
        facts: DemandFactReader,
        engine: ForecastEngine | None = None,
    ) -> None:
        self.db = db
        self.facts = facts
        self.engine = engine or ForecastEngine()

    def run(self, tenant_id: str, *, store_id: str, sku_id: str) -> dict[str, Any]:
        facts = sorted(
            self.facts.list_facts(tenant_id, store_id=store_id, sku_id=sku_id),
            key=lambda item: str(item["business_date"]),
        )
        if not facts:
            raise ForecastRunError("forecast_history_not_found")
        if any(
            item["tenant_id"] != tenant_id
            or item["store_id"] != store_id
            or item["sku_id"] != sku_id
            for item in facts
        ):
            raise ForecastRunError("forecast_fact_scope_mismatch")
        if {item["demand_policy_version"] for item in facts} != {DEMAND_V1.policy_version}:
            raise ForecastRunError("forecast_demand_policy_unsupported")

        series, input_issues = self._series(facts)
        evaluation = self.engine.evaluate(series)
        model_failures = [
            item
            for item in evaluation["ranking"]
            if item["windows_failed"]
        ]
        anomalies = self._anomalies(input_issues, evaluation, model_failures)
        status = (
            "degraded"
            if input_issues or evaluation["quality_status"] == "degraded"
            else "completed"
        )
        policy = self._policy_evidence()
        data_hash = hashlib.sha256(
            _json(
                {
                    "facts": [
                        {
                            key: item[key]
                            for key in ("id", "business_date", "fact_version", "payload_hash")
                        }
                        for item in facts
                    ],
                    "forecast_policy": policy,
                }
            ).encode("utf-8")
        ).hexdigest()
        run_id = f"forecast-run-{uuid.uuid4().hex}"
        created_at = utc_now()
        candidate_evidence = {
            "models": evaluation["candidate_models"],
            "ranking": evaluation["ranking"],
            "demand_type": evaluation["demand_type"],
            "horizon_totals": evaluation["horizon_totals"],
            "policy": policy,
        }
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_policy(conn, tenant_id, store_id, policy, created_at)
            metrics = evaluation["metrics"]
            conn.execute(
                """
                INSERT INTO forecast_runs(
                    run_id, tenant_id, store_id, sku_id, training_start, training_end,
                    data_hash, demand_policy_version, forecast_policy_version,
                    candidate_models_json, champion_model, champion_reason, model_version,
                    wape, bias, smape, rmse, forecast_horizon, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    tenant_id,
                    store_id,
                    sku_id,
                    evaluation["training_start"],
                    evaluation["training_end"],
                    data_hash,
                    DEMAND_V1.policy_version,
                    policy["policy_version"],
                    _json(candidate_evidence),
                    evaluation["champion_model"],
                    _json(evaluation["champion_reason"]),
                    evaluation["model_version"],
                    metrics["wape"],
                    metrics["bias"],
                    metrics["smape"],
                    metrics["rmse"],
                    len(evaluation["points"]),
                    status,
                    created_at,
                ),
            )
            for row in evaluation["backtests"]:
                conn.execute(
                    """
                    INSERT INTO forecast_backtests(
                        backtest_id, tenant_id, run_id, model_name, origin_date,
                        training_start, training_end, forecast_start, forecast_end,
                        actual_json, forecast_json, metrics_json, failure_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"forecast-backtest-{uuid.uuid4().hex}",
                        tenant_id,
                        run_id,
                        row["model_name"],
                        row["origin_date"],
                        row["training_start"],
                        row["training_end"],
                        row["forecast_start"],
                        row["forecast_end"],
                        _json(row["actual"]),
                        _json(row["forecast"]),
                        _json(row["metrics"]),
                        row["failure_reason"],
                        created_at,
                    ),
                )
            for point in evaluation["points"]:
                conn.execute(
                    """
                    INSERT INTO forecast_points(
                        point_id, tenant_id, run_id, forecast_date, p50, p80, p95, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"forecast-point-{uuid.uuid4().hex}",
                        tenant_id,
                        run_id,
                        point["forecast_date"],
                        point["p50"],
                        point["p80"],
                        point["p95"],
                        created_at,
                    ),
                )
            for anomaly in anomalies:
                conn.execute(
                    """
                    INSERT INTO forecast_anomalies(
                        anomaly_id, tenant_id, store_id, sku_id, run_id, anomaly_type,
                        severity, evidence_json, resolution_status, created_at, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, NULL)
                    """,
                    (
                        f"forecast-anomaly-{uuid.uuid4().hex}",
                        tenant_id,
                        store_id,
                        sku_id,
                        run_id,
                        anomaly["anomaly_type"],
                        anomaly["severity"],
                        _json(anomaly["evidence"]),
                        created_at,
                    ),
                )
        return self.get_run(tenant_id, run_id)

    def get_run(self, tenant_id: str, run_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            run = conn.execute(
                "SELECT * FROM forecast_runs WHERE tenant_id=? AND run_id=?",
                (tenant_id, run_id),
            ).fetchone()
            if run is None:
                raise ForecastRunError("forecast_run_not_found")
            backtests = conn.execute(
                """SELECT * FROM forecast_backtests WHERE tenant_id=? AND run_id=?
                ORDER BY model_name, origin_date""",
                (tenant_id, run_id),
            ).fetchall()
            points = conn.execute(
                """SELECT forecast_date, p50, p80, p95 FROM forecast_points
                WHERE tenant_id=? AND run_id=? ORDER BY forecast_date""",
                (tenant_id, run_id),
            ).fetchall()
            anomalies = conn.execute(
                """SELECT anomaly_type, severity, evidence_json, resolution_status
                FROM forecast_anomalies WHERE tenant_id=? AND run_id=?
                ORDER BY anomaly_type""",
                (tenant_id, run_id),
            ).fetchall()
        view = dict(run)
        candidate_evidence = json.loads(view.pop("candidate_models_json"))
        view["candidate_models"] = candidate_evidence
        view["champion_reason"] = json.loads(view["champion_reason"])
        view["horizon_totals"] = candidate_evidence["horizon_totals"]
        view["backtests"] = [
            {
                **{
                    key: row[key]
                    for key in (
                        "model_name",
                        "origin_date",
                        "training_start",
                        "training_end",
                        "forecast_start",
                        "forecast_end",
                        "failure_reason",
                    )
                },
                "actual": json.loads(row["actual_json"]),
                "forecast": json.loads(row["forecast_json"]),
                "metrics": json.loads(row["metrics_json"]),
            }
            for row in backtests
        ]
        view["points"] = [dict(row) for row in points]
        view["anomalies"] = [
            {
                **{key: row[key] for key in ("anomaly_type", "severity", "resolution_status")},
                "evidence": json.loads(row["evidence_json"]),
            }
            for row in anomalies
        ]
        return view

    def _series(
        self, facts: list[dict[str, Any]]
    ) -> tuple[list[tuple[date, float | int | None]], dict[str, list[str]]]:
        by_date = {date.fromisoformat(str(item["business_date"])): item for item in facts}
        if len(by_date) != len(facts):
            raise ForecastRunError("forecast_duplicate_business_date")
        start, end = min(by_date), max(by_date)
        issues: dict[str, list[str]] = defaultdict(list)
        series: list[tuple[date, float | int | None]] = []
        current = start
        while current <= end:
            fact = by_date.get(current)
            value: float | int | None = None
            if fact is None or fact["eligible_units"] is None:
                issues["missing_demand_day"].append(current.isoformat())
            elif fact["stockout_flag"] == "true":
                issues["stockout_excluded"].append(current.isoformat())
            else:
                value = fact["eligible_units"]
                if fact["stockout_flag"] == "unknown":
                    issues["stockout_unknown"].append(current.isoformat())
            series.append((current, value))
            current += timedelta(days=1)
        return series, dict(issues)

    def _policy_evidence(self) -> dict[str, Any]:
        policy = self.engine.policy
        return {
            "policy_version": policy.policy_version,
            "horizons": list(policy.horizons),
            "minimum_history_days": policy.minimum_history_days,
            "candidate_models": list(policy.candidate_models),
            "backtest_windows": policy.backtest_windows,
            "interval_levels": [0.5, 0.8, 0.95],
            "required_relative_improvement": policy.required_relative_improvement,
            "demand_policy_version": DEMAND_V1.policy_version,
        }

    @staticmethod
    def _ensure_policy(
        conn: Any,
        tenant_id: str,
        store_id: str,
        policy: dict[str, Any],
        created_at: str,
    ) -> None:
        existing = conn.execute(
            """SELECT * FROM forecast_policies
            WHERE tenant_id=? AND store_id=? AND sku_id IS NULL AND policy_version=?""",
            (tenant_id, store_id, policy["policy_version"]),
        ).fetchone()
        expected = (
            _json(policy["horizons"]),
            policy["minimum_history_days"],
            _json(policy["candidate_models"]),
            policy["backtest_windows"],
            _json(policy["interval_levels"]),
            policy["demand_policy_version"],
        )
        if existing is not None:
            actual = tuple(
                existing[key]
                for key in (
                    "horizons_json",
                    "minimum_history_days",
                    "candidate_models_json",
                    "backtest_windows",
                    "interval_levels_json",
                    "demand_policy_version",
                )
            )
            if actual != expected:
                raise ForecastRunError("forecast_policy_version_conflict")
            return
        policy_id = "forecast-policy-" + uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{tenant_id}/{store_id}/{policy['policy_version']}",
        ).hex
        conn.execute(
            """
            INSERT INTO forecast_policies(
                policy_id, tenant_id, store_id, sku_id, horizons_json,
                minimum_history_days, candidate_models_json, backtest_windows,
                interval_levels_json, demand_policy_version, policy_version,
                active_from, created_at
            ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy_id,
                tenant_id,
                store_id,
                *expected,
                policy["policy_version"],
                created_at,
                created_at,
            ),
        )

    @staticmethod
    def _anomalies(
        input_issues: dict[str, list[str]],
        evaluation: dict[str, object],
        model_failures: list[dict[str, object]],
    ) -> list[dict[str, Any]]:
        severity = {
            "missing_demand_day": "high",
            "stockout_excluded": "medium",
            "stockout_unknown": "medium",
        }
        anomalies = [
            {
                "anomaly_type": kind,
                "severity": severity[kind],
                "evidence": {"count": len(days), "business_dates": days},
            }
            for kind, days in sorted(input_issues.items())
        ]
        if evaluation["quality_status"] == "degraded":
            anomalies.append(
                {
                    "anomaly_type": "cold_start",
                    "severity": "medium",
                    "evidence": {"demand_type": evaluation["demand_type"]},
                }
            )
        if model_failures:
            anomalies.append(
                {
                    "anomaly_type": "model_failure",
                    "severity": "low",
                    "evidence": {
                        "models": [item["model_name"] for item in model_failures],
                        "fallback_available": True,
                    },
                }
            )
        return anomalies
