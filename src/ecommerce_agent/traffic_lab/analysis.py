from __future__ import annotations

import json
import math
import queue
import statistics
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from ..business_calendar import (
    STORE_BUSINESS_CALENDAR_POLICY_VERSION,
    StoreBusinessCalendarService,
)
from ..connectors import SourceProvenanceResolver, unknown_source_provenance
from ..database import Database
from ..llm import ModelGateway
from ..traffic_source_identity import LEGACY_UNSCOPED_CONNECTOR_ID
from .freshness import analysis_input_freshness
from .models import TrafficAnalysisInterpretation, _TrafficAnalysisRunRecord
from .service import TrafficLabError, TrafficLabService


ANALYSIS_CODE_VERSION = "traffic-analysis-code-v3"
TRAFFIC_ANALYSIS_PROMPT_VERSION = "traffic-analysis-explain-v1"

_TRAFFIC_ANALYSIS_SYSTEM_PROMPT = """\
你是 Traffic Lab 的统计结果解释器。确定性代码已经固化统计事实，你没有执行权，且不得修改、替代或重新计算 effect、confidence interval、sample size、quality gate、statistical conclusion 或任何证据引用。

只做三件事：
1. 用谨慎语言解释已给出的证据与反证；
2. 把机制描述为待验证假设，不宣称掌握平台内部权重或因果机制；
3. 提出下一轮单变量实验，每条建议只能改变一个变量。

严格按用户消息中的 output_schema 返回一个 JSON object，不要添加统计字段或模型元数据。\
"""


class TrafficAnalysisInterpreter(Protocol):
    """Optional AI boundary: consume facts and return explanation-only fields."""

    def interpret(self, facts: dict[str, Any]) -> Any: ...


class TrafficAnalysisModelInterpreter:
    """Adapt the shared model gateway to the explanation-only Traffic schema."""

    def __init__(self, gateway: ModelGateway) -> None:
        self.gateway = gateway

    def interpret(self, facts: dict[str, Any]) -> dict[str, Any]:
        output_schema = TrafficAnalysisInterpretation.model_json_schema()
        properties = output_schema.get("properties", {})
        required = output_schema.get("required", [])
        for field in ("model_provider", "model_name", "prompt_version"):
            properties.pop(field, None)
            if field in required:
                required.remove(field)
        request = {
            "statistics_authority": "deterministic_code",
            "facts": facts,
            "output_schema": output_schema,
        }
        raw = self.gateway.generate_json(
            [
                {"role": "system", "content": _TRAFFIC_ANALYSIS_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(request, ensure_ascii=False, sort_keys=True),
                },
            ],
            thinking_enabled=False,
        )
        interpretation = dict(raw)
        interpretation.update(
            {
                "model_provider": self.gateway.settings.model_provider,
                "model_name": self.gateway.settings.model_name,
                "prompt_version": TRAFFIC_ANALYSIS_PROMPT_VERSION,
            }
        )
        return interpretation


@dataclass(frozen=True)
class _AnalysisPolicy:
    version: str
    confidence_level: float
    z_value: float
    minimum_buckets_per_assignment: int
    minimum_lag_pairs: int
    lag_steps: tuple[int, ...]
    required_control_attributes: tuple[str, ...]


@dataclass(frozen=True)
class _MetricSpec:
    kind: str
    unit: str
    numerator: str | None = None
    denominator: str | None = None
    value_field: str | None = None


_POLICIES = {
    "traffic-analysis-v2": _AnalysisPolicy(
        version="traffic-analysis-v2",
        confidence_level=0.95,
        z_value=1.959963984540054,
        minimum_buckets_per_assignment=4,
        minimum_lag_pairs=4,
        lag_steps=(1, 2, 4, 8, 12),
        required_control_attributes=(
            "category",
            "stock_status",
            "campaign",
            "ad_plan",
            "holiday_calendar_version",
            "store_traffic_baseline_version",
            "historical_ctr",
            "historical_cvr",
        ),
    )
}

_METRIC_SPECS = {
    "ctr": _MetricSpec(
        kind="proportion",
        unit="rate",
        numerator="clicks",
        denominator="impressions",
    ),
    "cvr": _MetricSpec(
        kind="proportion",
        unit="rate",
        numerator="orders",
        denominator="clicks",
    ),
    "impressions": _MetricSpec(kind="mean", unit="count_per_bucket", value_field="impressions"),
    "recommend_impressions": _MetricSpec(
        kind="mean", unit="count_per_bucket", value_field="recommend_impressions"
    ),
    "search_impressions": _MetricSpec(
        kind="mean", unit="count_per_bucket", value_field="search_impressions"
    ),
    "clicks": _MetricSpec(kind="mean", unit="count_per_bucket", value_field="clicks"),
    "orders": _MetricSpec(kind="mean", unit="count_per_bucket", value_field="orders"),
    "sales_amount": _MetricSpec(
        kind="mean", unit="currency_per_bucket", value_field="sales_amount"
    ),
}

_METHODS = {
    "aa": "aa_v1",
    "switchback": "switchback_uplift_v1",
}


class TrafficAnalysisEngine:
    """Compute and persist WP4 statistical facts without delegating authority to AI."""

    def __init__(
        self,
        db: Database,
        *,
        interpreter: TrafficAnalysisInterpreter | None = None,
        interpretation_timeout_seconds: float = 10.0,
        source_provenance_resolver: SourceProvenanceResolver | None = None,
        business_calendars: StoreBusinessCalendarService | None = None,
    ) -> None:
        if (
            not math.isfinite(interpretation_timeout_seconds)
            or interpretation_timeout_seconds <= 0
        ):
            raise ValueError("interpretation_timeout_must_be_positive")
        self.db = db
        self.service = TrafficLabService(
            db,
            business_calendars=business_calendars,
        )
        self.interpreter = interpreter
        self.interpretation_timeout_seconds = interpretation_timeout_seconds
        self.source_provenance_resolver = source_provenance_resolver

    def analyze_experiment(
        self,
        tenant_id: str,
        experiment_id: str,
    ) -> dict[str, Any]:
        experiment = self.service.get_experiment(tenant_id, experiment_id)
        policy = _POLICIES.get(str(experiment["analysis_policy_version"]))
        if policy is None:
            raise TrafficLabError("unsupported_analysis_policy_version")

        control = self.service.get_revision(
            tenant_id, str(experiment["control_revision_id"])
        )
        treatment = self.service.get_revision(
            tenant_id, str(experiment["treatment_revision_id"])
        )
        windows = self.service.list_experiment_windows(tenant_id, experiment_id)
        issues: list[dict[str, Any]] = []
        business_calendar = self._business_calendar_evidence(experiment, issues)

        if experiment["status"] != "completed" or experiment["ended_at"] is None:
            self._add_issue(issues, "experiment_not_completed")
        method = _METHODS.get(str(experiment["experiment_type"]))
        if method is None:
            method = "unsupported_v1"
            self._add_issue(
                issues,
                "analysis_method_not_implemented",
                experiment_type=experiment["experiment_type"],
            )

        metric_spec = _METRIC_SPECS.get(str(experiment["primary_metric"]))
        if metric_spec is None:
            self._add_issue(
                issues,
                "primary_metric_not_supported",
                primary_metric=experiment["primary_metric"],
            )

        window_quality = self.service.experiment_window_quality(tenant_id, experiment_id)
        for issue in window_quality["issues"]:
            self._add_issue(
                issues,
                str(issue["code"]),
                **{key: value for key, value in issue.items() if key != "code"},
            )

        control_snapshot = self._check_control_variables(
            control,
            treatment,
            policy,
            issues,
            require_treatment_variable=experiment["experiment_type"] == "switchback",
        )
        if experiment["experiment_type"] == "switchback":
            self._check_switchback_design(experiment, windows, issues)

        included, excluded = self._select_metrics(
            tenant_id,
            experiment,
            windows,
            issues,
        )
        self._check_metric_quality(included, experiment, policy, issues)

        sample_size = self._sample_size(
            included,
            str(experiment["primary_metric"]),
            metric_spec,
        )
        self._check_sample_size(sample_size, experiment, policy, issues)
        effect_estimate, confidence_interval = self._estimate_effect(
            included,
            str(experiment["primary_metric"]),
            metric_spec,
            policy,
        )
        effect_estimate["lag_analysis"] = self._lag_analysis(
            included,
            str(experiment["primary_metric"]),
            metric_spec,
            policy,
        )

        aa_gate = {"status": "not_required", "analysis_run_id": None}
        if experiment["experiment_type"] == "switchback":
            prior_aa = self._find_prior_clean_aa(tenant_id, experiment, policy)
            if prior_aa is None:
                aa_gate = {"status": "missing", "analysis_run_id": None}
                self._add_issue(issues, "aa_gate_missing")
            elif prior_aa["status"] != "passed":
                aa_gate = prior_aa
                self._add_issue(
                    issues,
                    "aa_gate_stale"
                    if prior_aa["status"] == "stale"
                    else "aa_gate_failed",
                )
            else:
                aa_gate = prior_aa

        if (
            experiment["experiment_type"] == "aa"
            and confidence_interval.get("includes_zero") is False
        ):
            self._add_issue(issues, "aa_false_positive_detected")

        conclusion = self._statistical_conclusion(
            str(experiment["experiment_type"]),
            effect_estimate,
            confidence_interval,
            issues,
        )
        strong_conclusion_allowed = (
            not issues
            and experiment["experiment_type"] == "switchback"
            and conclusion in {"positive_effect", "negative_effect"}
        )
        quality_gate = {
            "status": "blocked" if issues else "passed",
            "quality": "invalid" if issues else "valid",
            "strong_conclusion_allowed": strong_conclusion_allowed,
            "issues": issues,
        }
        data_window = self._data_window(experiment, windows, included, excluded)
        input_snapshot = self._input_snapshot(
            experiment,
            control,
            treatment,
            windows,
            included,
            excluded,
        )
        source_provenance = (
            self.source_provenance_resolver.freeze(
                (
                    revision["values"].get("connector_id")
                    for revision in input_snapshot["revisions"]
                ),
                basis="traffic_analysis_input_revisions",
            )
            if self.source_provenance_resolver is not None
            else unknown_source_provenance(
                basis="traffic_analysis_resolver_not_configured"
            )
        )
        evidence = {
            "quality_gate": quality_gate,
            "statistical_conclusion": conclusion,
            "aa_gate": aa_gate,
            "business_calendar": business_calendar,
            "window_quality": window_quality,
            "control_variables": control_snapshot,
            "input_snapshot": input_snapshot,
            "source_provenance": source_provenance,
            "statistics_authority": "deterministic_code",
        }
        counter_evidence = {
            "issues": issues,
            "analysis_limitations": [
                {
                    "code": "normal_approximation_v1",
                    "detail": (
                        "区间采用固化的 95% 正态近似，"
                        "不把单次区间视为平台机制真相。"
                    ),
                },
                {
                    "code": "lag_is_association_not_causation",
                    "detail": (
                        "lag 只报告当前指标与未来推荐曝光的相关性，"
                        "不证明平台因果权重。"
                    ),
                },
            ],
        }

        facts = {
            "method": method,
            "data_window": data_window,
            "sample_size": sample_size,
            "effect_estimate": effect_estimate,
            "confidence_interval": confidence_interval,
            "evidence": evidence,
            "counter_evidence": counter_evidence,
        }
        hypotheses = (
            {"status": "not_generated", "reason": "model_not_configured"}
            if self.interpreter is None
            else {"status": "pending", "reason": "interpretation_pending"}
        )
        persisted = self.service._create_analysis_run(
            tenant_id,
            experiment_id,
            _TrafficAnalysisRunRecord(
                method=method,
                data_window=data_window,
                sample_size=sample_size,
                effect_estimate=effect_estimate,
                confidence_interval=confidence_interval,
                evidence=evidence,
                counter_evidence=counter_evidence,
                hypotheses=hypotheses,
                model_provider=None,
                model_name=None,
                prompt_version=None,
                analysis_code_version=ANALYSIS_CODE_VERSION,
            ),
        )
        if self.interpreter is None:
            return persisted
        hypotheses, model_metadata = self._interpret(facts)
        return self.service._update_analysis_interpretation(
            tenant_id,
            str(persisted["analysis_run_id"]),
            hypotheses=hypotheses,
            model_provider=model_metadata["model_provider"],
            model_name=model_metadata["model_name"],
            prompt_version=model_metadata["prompt_version"],
        )

    @staticmethod
    def _add_issue(
        issues: list[dict[str, Any]],
        code: str,
        **details: Any,
    ) -> None:
        candidate = {"code": code, **details}
        if candidate not in issues:
            issues.append(candidate)

    @classmethod
    def _business_calendar_evidence(
        cls,
        experiment: dict[str, Any],
        issues: list[dict[str, Any]],
    ) -> dict[str, Any]:
        calendar_id = experiment.get("business_calendar_id")
        record_version = experiment.get("business_calendar_version")
        timezone_name = experiment.get("business_timezone")
        policy_version = experiment.get("business_calendar_policy_version")
        if any(
            value is None
            for value in (
                calendar_id,
                record_version,
                timezone_name,
                policy_version,
            )
        ):
            cls._add_issue(issues, "business_timezone_evidence_missing")
            return {
                "calendar_id": calendar_id,
                "record_version": record_version,
                "timezone": timezone_name,
                "policy_version": policy_version,
            }
        if policy_version != STORE_BUSINESS_CALENDAR_POLICY_VERSION:
            cls._add_issue(
                issues,
                "business_calendar_policy_unsupported",
                policy_version=policy_version,
            )
        try:
            if int(record_version) < 1:
                raise ValueError("record version must be positive")
            ZoneInfo(str(timezone_name))
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            cls._add_issue(issues, "business_timezone_evidence_invalid")
        return {
            "calendar_id": calendar_id,
            "record_version": record_version,
            "timezone": timezone_name,
            "policy_version": policy_version,
        }

    def _check_control_variables(
        self,
        control: dict[str, Any],
        treatment: dict[str, Any],
        policy: _AnalysisPolicy,
        issues: list[dict[str, Any]],
        *,
        require_treatment_variable: bool,
    ) -> dict[str, Any]:
        control_attributes = control["attributes"]
        treatment_attributes = treatment["attributes"]
        for field in policy.required_control_attributes:
            missing_from = [
                assignment
                for assignment, attributes in (
                    ("control", control_attributes),
                    ("treatment", treatment_attributes),
                )
                if field not in attributes
            ]
            if missing_from:
                self._add_issue(
                    issues,
                    "control_variable_missing",
                    field=field,
                    assignments=missing_from,
                )
                continue
            if control_attributes[field] != treatment_attributes[field]:
                self._add_issue(
                    issues,
                    "control_variable_changed",
                    field=field,
                    control=control_attributes[field],
                    treatment=treatment_attributes[field],
                )
        if (
            control_attributes.get("stock_status") != "in_stock"
            or treatment_attributes.get("stock_status") != "in_stock"
        ):
            self._add_issue(issues, "stock_not_available")
        if Decimal(str(control["sale_price"])) != Decimal(str(treatment["sale_price"])):
            self._add_issue(
                issues,
                "sale_price_changed",
                control=control["sale_price"],
                treatment=treatment["sale_price"],
            )

        changed_treatment_variables = []
        if control["title"] != treatment["title"]:
            changed_treatment_variables.append("title")
        if control["main_image_asset_id"] != treatment["main_image_asset_id"]:
            changed_treatment_variables.append("main_image")
        if require_treatment_variable and not changed_treatment_variables:
            self._add_issue(issues, "treatment_variable_missing")
        elif len(changed_treatment_variables) > 1:
            self._add_issue(
                issues,
                "multiple_treatment_variables_changed",
                variables=changed_treatment_variables,
            )
        uncontrolled_attributes = sorted(
            field
            for field in control_attributes.keys() | treatment_attributes.keys()
            if field not in policy.required_control_attributes
            and control_attributes.get(field) != treatment_attributes.get(field)
        )
        if uncontrolled_attributes:
            self._add_issue(
                issues,
                "unplanned_revision_attributes_changed",
                fields=uncontrolled_attributes,
            )
        return {
            "required_attributes": list(policy.required_control_attributes),
            "control": {
                "store_id": control["store_id"],
                "sku_id": control["sku_id"],
                "sale_price": control["sale_price"],
                "attributes": {
                    field: control_attributes.get(field)
                    for field in policy.required_control_attributes
                },
            },
            "treatment": {
                "store_id": treatment["store_id"],
                "sku_id": treatment["sku_id"],
                "sale_price": treatment["sale_price"],
                "attributes": {
                    field: treatment_attributes.get(field)
                    for field in policy.required_control_attributes
                },
            },
            "changed_treatment_variables": changed_treatment_variables,
            "unplanned_changed_attributes": uncontrolled_attributes,
        }

    def _check_switchback_design(
        self,
        experiment: dict[str, Any],
        windows: list[dict[str, Any]],
        issues: list[dict[str, Any]],
    ) -> None:
        timezone_name = experiment.get("business_timezone")
        if any(
            experiment.get(field) is None
            for field in (
                "business_calendar_id",
                "business_calendar_version",
                "business_timezone",
                "business_calendar_policy_version",
            )
        ):
            self._add_issue(issues, "business_timezone_evidence_missing")
            return
        if (
            experiment.get("business_calendar_policy_version")
            != STORE_BUSINESS_CALENDAR_POLICY_VERSION
        ):
            self._add_issue(
                issues,
                "business_calendar_policy_unsupported",
                policy_version=experiment.get("business_calendar_policy_version"),
            )
            return
        try:
            business_zone = ZoneInfo(str(timezone_name))
        except (ZoneInfoNotFoundError, ValueError):
            self._add_issue(issues, "business_timezone_evidence_invalid")
            return
        ordered = sorted(
            windows,
            key=lambda item: (
                item["window_start"],
                item["window_end"],
                item["window_id"],
            ),
        )
        active = [window for window in ordered if not window["washout"]]
        grouped = {
            assignment: [
                window for window in active if window["assignment"] == assignment
            ]
            for assignment in ("control", "treatment")
        }
        if any(len(grouped[assignment]) < 2 for assignment in grouped):
            self._add_issue(issues, "switchback_assignments_insufficient")
            return
        duration = {
            assignment: sum(
                (
                    datetime.fromisoformat(window["window_end"])
                    - datetime.fromisoformat(window["window_start"])
                ).total_seconds()
                for window in assignment_windows
            )
            for assignment, assignment_windows in grouped.items()
        }
        if duration["control"] != duration["treatment"]:
            self._add_issue(
                issues,
                "switchback_duration_imbalanced",
                control_seconds=duration["control"],
                treatment_seconds=duration["treatment"],
            )
        local_starts = {
            assignment: [
                datetime.fromisoformat(window["window_start"]).astimezone(
                    business_zone
                )
                for window in assignment_windows
            ]
            for assignment, assignment_windows in grouped.items()
        }
        hour_distributions = {
            assignment: Counter(item.hour for item in starts)
            for assignment, starts in local_starts.items()
        }
        if hour_distributions["control"] != hour_distributions["treatment"]:
            self._add_issue(
                issues,
                "switchback_hour_distribution_imbalanced",
                control=dict(sorted(hour_distributions["control"].items())),
                treatment=dict(sorted(hour_distributions["treatment"].items())),
            )
        date_distributions = {
            assignment: Counter(item.date().isoformat() for item in starts)
            for assignment, starts in local_starts.items()
        }
        if date_distributions["control"] != date_distributions["treatment"]:
            self._add_issue(
                issues,
                "switchback_date_distribution_imbalanced",
                control=dict(sorted(date_distributions["control"].items())),
                treatment=dict(sorted(date_distributions["treatment"].items())),
            )
        weekday_distributions = {
            assignment: Counter(item.weekday() for item in starts)
            for assignment, starts in local_starts.items()
        }
        if weekday_distributions["control"] != weekday_distributions["treatment"]:
            self._add_issue(
                issues,
                "switchback_weekday_distribution_imbalanced",
                control=dict(sorted(weekday_distributions["control"].items())),
                treatment=dict(sorted(weekday_distributions["treatment"].items())),
            )

        required_washout_seconds = int(experiment["washout_window"]) * 60
        for previous_active, next_active in zip(active, active[1:]):
            if previous_active["assignment"] == next_active["assignment"]:
                continue
            previous_end = datetime.fromisoformat(previous_active["window_end"])
            next_start = datetime.fromisoformat(next_active["window_start"])
            washout_intervals = sorted(
                (
                    max(datetime.fromisoformat(window["window_start"]), previous_end),
                    min(datetime.fromisoformat(window["window_end"]), next_start),
                )
                for window in ordered
                if window["washout"]
                and datetime.fromisoformat(window["window_start"]) < next_start
                and datetime.fromisoformat(window["window_end"]) > previous_end
            )
            merged: list[tuple[datetime, datetime]] = []
            for interval_start, interval_end in washout_intervals:
                if interval_end <= interval_start:
                    continue
                if not merged or interval_start > merged[-1][1]:
                    merged.append((interval_start, interval_end))
                else:
                    merged[-1] = (
                        merged[-1][0],
                        max(merged[-1][1], interval_end),
                    )
            observed_washout_seconds = int(
                sum((end - start).total_seconds() for start, end in merged)
            )
            if observed_washout_seconds < required_washout_seconds:
                self._add_issue(
                    issues,
                    "switchback_washout_insufficient",
                    previous_window_id=previous_active["window_id"],
                    next_window_id=next_active["window_id"],
                    observed_seconds=observed_washout_seconds,
                    required_seconds=required_washout_seconds,
                )

        sequence = [window["assignment"] for window in active]
        longest_run = 0
        current_run = 0
        previous: str | None = None
        for assignment in sequence:
            current_run = current_run + 1 if assignment == previous else 1
            longest_run = max(longest_run, current_run)
            previous = assignment
        if longest_run > 2:
            self._add_issue(
                issues,
                "switchback_order_imbalanced",
                longest_assignment_run=longest_run,
            )

    def _select_metrics(
        self,
        tenant_id: str,
        experiment: dict[str, Any],
        windows: list[dict[str, Any]],
        issues: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        revision_ids = sorted(
            {
                str(experiment["control_revision_id"]),
                str(experiment["treatment_revision_id"]),
            }
        )
        end = experiment["ended_at"]
        if end is None and windows:
            end = max(window["window_end"] for window in windows)
        if end is None:
            end = experiment["started_at"]
        placeholders = ",".join("?" for _ in revision_ids)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM traffic_metric_buckets
                WHERE tenant_id=?
                  AND connector_id<>?
                  AND listing_revision_id IN ({placeholders})
                  AND metric_end>? AND metric_start<?
                ORDER BY metric_start ASC, id ASC
                """,
                (
                    tenant_id,
                    LEGACY_UNSCOPED_CONNECTOR_ID,
                    *revision_ids,
                    experiment["started_at"],
                    end,
                ),
            ).fetchall()

        parsed_windows = [
            (
                window,
                datetime.fromisoformat(window["window_start"]),
                datetime.fromisoformat(window["window_end"]),
            )
            for window in windows
        ]
        included: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for sqlite_row in rows:
            row = dict(sqlite_row)
            metric_start = datetime.fromisoformat(row["metric_start"])
            metric_end = datetime.fromisoformat(row["metric_end"])
            matching = [
                window
                for window, window_start, window_end in parsed_windows
                if window["listing_revision_id"] == row["listing_revision_id"]
                and window_start <= metric_start
                and metric_end <= window_end
            ]
            if len(matching) > 1:
                self._add_issue(
                    issues,
                    "metric_matches_multiple_windows",
                    bucket_id=row["id"],
                    window_ids=[window["window_id"] for window in matching],
                )
                excluded.append(
                    self._excluded_bucket(row, "multiple_actual_windows")
                )
                continue
            if not matching:
                crossing = any(
                    window["listing_revision_id"] == row["listing_revision_id"]
                    and metric_start < window_end
                    and metric_end > window_start
                    for window, window_start, window_end in parsed_windows
                )
                if crossing:
                    self._add_issue(
                        issues,
                        "metric_crosses_window_boundary",
                        bucket_id=row["id"],
                    )
                excluded.append(
                    self._excluded_bucket(
                        row,
                        "crosses_window_boundary" if crossing else "outside_actual_window",
                    )
                )
                continue
            window = matching[0]
            if window["washout"]:
                row["assignment"] = window["assignment"]
                row["window_id"] = window["window_id"]
                excluded.append(self._excluded_bucket(row, "washout"))
                continue
            row["assignment"] = window["assignment"]
            row["window_id"] = window["window_id"]
            row["quality_flags"] = json.loads(row["quality_flags_json"])
            included.append(row)
        return included, excluded

    @classmethod
    def _excluded_bucket(
        cls,
        row: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        return {
            "bucket_id": row["id"],
            "reason": reason,
            "payload_hash": row["payload_hash"],
            "version": row["version"],
            "values": cls._bucket_values(row),
        }

    @staticmethod
    def _bucket_values(row: dict[str, Any]) -> dict[str, Any]:
        quality_flags = row.get("quality_flags")
        if quality_flags is None:
            quality_flags = json.loads(row.get("quality_flags_json", "[]"))
        fields = (
            "listing_revision_id",
            "metric_start",
            "metric_end",
            "bucket_granularity",
            "traffic_source",
            "impressions",
            "clicks",
            "visitors",
            "favorites",
            "cart_adds",
            "orders",
            "sales_amount",
            "ad_spend",
            "search_impressions",
            "recommend_impressions",
            "data_as_of",
            "source_id",
            "assignment",
            "window_id",
        )
        return {
            **{field: row.get(field) for field in fields},
            "quality_flags": quality_flags,
        }

    def _check_metric_quality(
        self,
        rows: list[dict[str, Any]],
        experiment: dict[str, Any],
        policy: _AnalysisPolicy,
        issues: list[dict[str, Any]],
    ) -> None:
        del policy
        if not rows:
            self._add_issue(issues, "analysis_samples_missing")
            return
        granularities = {row["bucket_granularity"] for row in rows}
        if len(granularities) > 1:
            self._add_issue(
                issues,
                "mixed_bucket_granularity",
                granularities=sorted(granularities),
            )
        traffic_sources = {row["traffic_source"] for row in rows}
        if len(traffic_sources) > 1:
            self._add_issue(
                issues,
                "multiple_traffic_sources",
                traffic_sources=sorted(traffic_sources),
            )
        flagged = [row["id"] for row in rows if row["quality_flags"]]
        if flagged:
            self._add_issue(
                issues,
                "metric_quality_flags_present",
                bucket_ids=flagged,
            )
        ad_spend_rates = {
            (
                Decimal(str(row["ad_spend"])) / Decimal(int(row["impressions"]))
                if int(row["impressions"]) > 0
                else Decimal("0")
            )
            for row in rows
        }
        if len(ad_spend_rates) > 1:
            self._add_issue(issues, "ad_spend_not_controlled")
        if any(row["assignment"] not in {"control", "treatment"} for row in rows):
            self._add_issue(issues, "experiment_assignment_invalid")
        if experiment["primary_metric"] == "cvr" and all(
            int(row["clicks"]) == 0 for row in rows
        ):
            self._add_issue(issues, "metric_denominator_zero", metric="cvr")
        if experiment["primary_metric"] == "cvr":
            invalid_cvr_buckets = [
                row["id"] for row in rows if int(row["orders"]) > int(row["clicks"])
            ]
            if invalid_cvr_buckets:
                self._add_issue(
                    issues,
                    "cvr_numerator_exceeds_denominator",
                    bucket_ids=invalid_cvr_buckets,
                )

    @staticmethod
    def _sample_size(
        rows: list[dict[str, Any]],
        metric: str,
        spec: _MetricSpec | None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"metric": metric}
        granularities = sorted({str(row["bucket_granularity"]) for row in rows})
        result["bucket_granularity"] = granularities[0] if len(granularities) == 1 else None
        for assignment in ("control", "treatment"):
            assigned = [row for row in rows if row["assignment"] == assignment]
            summary: dict[str, Any] = {
                "bucket_count": len(assigned),
                "impressions": sum(int(row["impressions"]) for row in assigned),
                "metric_denominator": 0,
                "metric_numerator": 0,
            }
            if spec is not None and spec.kind == "proportion":
                summary["metric_denominator"] = sum(
                    int(row[str(spec.denominator)]) for row in assigned
                )
                summary["metric_numerator"] = sum(
                    int(row[str(spec.numerator)]) for row in assigned
                )
            elif spec is not None and spec.kind == "mean":
                values = [float(row[str(spec.value_field)]) for row in assigned]
                summary["metric_denominator"] = len(values)
                summary["metric_numerator"] = sum(values)
            result[assignment] = summary
        return result

    def _check_sample_size(
        self,
        sample_size: dict[str, Any],
        experiment: dict[str, Any],
        policy: _AnalysisPolicy,
        issues: list[dict[str, Any]],
    ) -> None:
        for assignment in ("control", "treatment"):
            sample = sample_size[assignment]
            if sample["bucket_count"] < policy.minimum_buckets_per_assignment:
                self._add_issue(
                    issues,
                    "assignment_buckets_insufficient",
                    assignment=assignment,
                    observed=sample["bucket_count"],
                    required=policy.minimum_buckets_per_assignment,
                )
            if sample["impressions"] < int(experiment["minimum_exposure"]):
                self._add_issue(
                    issues,
                    "minimum_exposure_not_met",
                    assignment=assignment,
                    observed=sample["impressions"],
                    required=experiment["minimum_exposure"],
                )
            if sample["metric_denominator"] <= 0:
                self._add_issue(
                    issues,
                    "metric_denominator_zero",
                    assignment=assignment,
                    metric=sample_size["metric"],
                )
            elif sample_size["metric"] in {"ctr", "cvr"}:
                successes = sample["metric_numerator"]
                failures = sample["metric_denominator"] - successes
                if min(successes, failures) < 5:
                    self._add_issue(
                        issues,
                        "normal_approximation_unreliable",
                        assignment=assignment,
                        successes=successes,
                        failures=failures,
                    )

    def _estimate_effect(
        self,
        rows: list[dict[str, Any]],
        metric: str,
        spec: _MetricSpec | None,
        policy: _AnalysisPolicy,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        unavailable_effect = {
            "metric": metric,
            "estimator": None,
            "unit": None,
            "control": None,
            "treatment": None,
            "absolute": None,
            "relative": None,
            "direction": "unavailable",
        }
        unavailable_interval = {
            "confidence_level": policy.confidence_level,
            "method": None,
            "low": None,
            "high": None,
            "standard_error": None,
            "includes_zero": None,
        }
        if spec is None:
            return unavailable_effect, unavailable_interval
        grouped = {
            assignment: [row for row in rows if row["assignment"] == assignment]
            for assignment in ("control", "treatment")
        }
        if spec.kind == "proportion":
            totals: dict[str, tuple[int, int]] = {}
            for assignment, assigned in grouped.items():
                if any(
                    int(row[str(spec.numerator)])
                    > int(row[str(spec.denominator)])
                    for row in assigned
                ):
                    return unavailable_effect, unavailable_interval
                numerator = sum(int(row[str(spec.numerator)]) for row in assigned)
                denominator = sum(int(row[str(spec.denominator)]) for row in assigned)
                if denominator <= 0 or numerator < 0 or numerator > denominator:
                    return unavailable_effect, unavailable_interval
                totals[assignment] = (numerator, denominator)
            control_rate = totals["control"][0] / totals["control"][1]
            treatment_rate = totals["treatment"][0] / totals["treatment"][1]
            effect = treatment_rate - control_rate
            standard_error = math.sqrt(
                control_rate
                * (1 - control_rate)
                / totals["control"][1]
                + treatment_rate
                * (1 - treatment_rate)
                / totals["treatment"][1]
            )
            interval_method = "normal_difference_in_proportions"
            estimator = "difference_in_aggregate_rates"
        else:
            values = {
                assignment: [
                    float(row[str(spec.value_field)]) for row in assigned
                ]
                for assignment, assigned in grouped.items()
            }
            if any(len(assigned) < 2 for assigned in values.values()):
                return unavailable_effect, unavailable_interval
            control_rate = statistics.mean(values["control"])
            treatment_rate = statistics.mean(values["treatment"])
            effect = treatment_rate - control_rate
            standard_error = math.sqrt(
                statistics.variance(values["control"]) / len(values["control"])
                + statistics.variance(values["treatment"]) / len(values["treatment"])
            )
            interval_method = "normal_welch_difference_in_means"
            estimator = "difference_in_bucket_means"
        low = effect - policy.z_value * standard_error
        high = effect + policy.z_value * standard_error
        direction = "positive" if effect > 0 else "negative" if effect < 0 else "none"
        return (
            {
                "metric": metric,
                "estimator": estimator,
                "unit": spec.unit,
                "control": control_rate,
                "treatment": treatment_rate,
                "absolute": effect,
                "relative": effect / control_rate if control_rate != 0 else None,
                "direction": direction,
            },
            {
                "confidence_level": policy.confidence_level,
                "method": interval_method,
                "low": low,
                "high": high,
                "standard_error": standard_error,
                "includes_zero": low <= 0 <= high,
            },
        )

    def _lag_analysis(
        self,
        rows: list[dict[str, Any]],
        metric: str,
        spec: _MetricSpec | None,
        policy: _AnalysisPolicy,
    ) -> dict[str, Any]:
        if spec is None or spec.kind != "proportion" or not rows:
            return {
                "status": "not_applicable",
                "outcome_metric": "recommend_impressions",
                "results": [],
                "best_supported_lag_minutes": None,
            }
        granularities = {row["bucket_granularity"] for row in rows}
        if len(granularities) != 1:
            return {
                "status": "unavailable",
                "outcome_metric": "recommend_impressions",
                "results": [],
                "best_supported_lag_minutes": None,
            }
        step = timedelta(hours=1) if next(iter(granularities)) == "hour" else timedelta(days=1)
        by_start = {datetime.fromisoformat(row["metric_start"]): row for row in rows}
        results: list[dict[str, Any]] = []
        supported: list[dict[str, Any]] = []
        for lag_steps in policy.lag_steps:
            predictors: list[float] = []
            outcomes: list[float] = []
            for current_start, current in by_start.items():
                future = by_start.get(current_start + step * lag_steps)
                if future is None:
                    continue
                denominator = int(current[str(spec.denominator)])
                numerator = int(current[str(spec.numerator)])
                if denominator <= 0 or numerator < 0 or numerator > denominator:
                    continue
                predictors.append(numerator / denominator)
                outcomes.append(float(future["recommend_impressions"]))
            lag_minutes = int(step.total_seconds() // 60) * lag_steps
            correlation = self._pearson(predictors, outcomes)
            interval = self._correlation_interval(
                correlation,
                len(predictors),
                policy,
            )
            item = {
                "lag_minutes": lag_minutes,
                "pair_count": len(predictors),
                "pearson_r": correlation,
                "confidence_interval": interval,
            }
            results.append(item)
            if (
                correlation is not None
                and len(predictors) >= policy.minimum_lag_pairs
                and interval["includes_zero"] is False
            ):
                supported.append(item)
        supported.sort(
            key=lambda item: (-abs(float(item["pearson_r"])), int(item["lag_minutes"]))
        )
        return {
            "status": "supported" if supported else "inconclusive",
            "predictor_metric": metric,
            "outcome_metric": "recommend_impressions",
            "results": results,
            "best_supported_lag_minutes": (
                supported[0]["lag_minutes"] if supported else None
            ),
        }

    @staticmethod
    def _pearson(left: list[float], right: list[float]) -> float | None:
        if len(left) < 2 or len(left) != len(right):
            return None
        left_mean = statistics.mean(left)
        right_mean = statistics.mean(right)
        left_delta = [value - left_mean for value in left]
        right_delta = [value - right_mean for value in right]
        left_sum = sum(value * value for value in left_delta)
        right_sum = sum(value * value for value in right_delta)
        if left_sum == 0 or right_sum == 0:
            return None
        return sum(a * b for a, b in zip(left_delta, right_delta, strict=True)) / math.sqrt(
            left_sum * right_sum
        )

    @staticmethod
    def _correlation_interval(
        correlation: float | None,
        pair_count: int,
        policy: _AnalysisPolicy,
    ) -> dict[str, Any]:
        if correlation is None or pair_count <= 3:
            return {
                "confidence_level": policy.confidence_level,
                "method": "fisher_z",
                "low": None,
                "high": None,
                "includes_zero": None,
            }
        bounded = max(-0.999999, min(0.999999, correlation))
        transformed = math.atanh(bounded)
        margin = policy.z_value / math.sqrt(pair_count - 3)
        low = math.tanh(transformed - margin)
        high = math.tanh(transformed + margin)
        return {
            "confidence_level": policy.confidence_level,
            "method": "fisher_z",
            "low": low,
            "high": high,
            "includes_zero": low <= 0 <= high,
        }

    def _find_prior_clean_aa(
        self,
        tenant_id: str,
        experiment: dict[str, Any],
        policy: _AnalysisPolicy,
    ) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT analysis.*
                FROM traffic_analysis_runs AS analysis
                JOIN traffic_experiments AS candidate
                  ON candidate.tenant_id=analysis.tenant_id
                 AND candidate.experiment_id=analysis.experiment_id
                WHERE analysis.tenant_id=?
                  AND candidate.store_id=? AND candidate.sku_id=?
                  AND candidate.experiment_type='aa'
                  AND candidate.primary_metric=?
                  AND candidate.analysis_policy_version=?
                  AND candidate.ended_at IS NOT NULL
                  AND candidate.ended_at<=?
                  AND analysis.analysis_code_version=?
                ORDER BY candidate.ended_at DESC, analysis.created_at DESC,
                         analysis.analysis_run_id DESC
                LIMIT 1
                """,
                (
                    tenant_id,
                    experiment["store_id"],
                    experiment["sku_id"],
                    experiment["primary_metric"],
                    policy.version,
                    experiment["started_at"],
                    ANALYSIS_CODE_VERSION,
                ),
            ).fetchall()
        if not rows:
            return None
        row = rows[0]
        try:
            evidence = json.loads(row["evidence_json"])
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        passed = (
            row["method"] == "aa_v1"
            and evidence.get("quality_gate", {}).get("status") == "passed"
            and evidence.get("statistical_conclusion") == "no_detectable_effect"
        )
        status = "passed" if passed else "failed"
        if passed and not analysis_input_freshness(
            self.db,
            tenant_id,
            str(row["experiment_id"]),
            evidence,
            analysis_run_id=str(row["analysis_run_id"]),
        )["usable_as_current"]:
            status = "stale"
        return {
            "status": status,
            "analysis_run_id": row["analysis_run_id"],
        }

    @staticmethod
    def _statistical_conclusion(
        experiment_type: str,
        effect_estimate: dict[str, Any],
        confidence_interval: dict[str, Any],
        issues: list[dict[str, Any]],
    ) -> str:
        if issues:
            return "blocked"
        if effect_estimate["absolute"] is None:
            return "inconclusive"
        if experiment_type == "aa":
            return (
                "no_detectable_effect"
                if confidence_interval["includes_zero"] is True
                else "blocked"
            )
        if confidence_interval["includes_zero"] is True:
            return "inconclusive"
        return "positive_effect" if effect_estimate["absolute"] > 0 else "negative_effect"

    @staticmethod
    def _data_window(
        experiment: dict[str, Any],
        windows: list[dict[str, Any]],
        included: list[dict[str, Any]],
        excluded: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "experiment_start": experiment["started_at"],
            "experiment_end": experiment["ended_at"],
            "actual_window_ids": [window["window_id"] for window in windows],
            "included_bucket_ids": [row["id"] for row in included],
            "excluded_buckets": [
                {key: value for key, value in item.items() if key != "values"}
                for item in excluded
            ],
            "future_data_used_for_uplift": False,
        }

    @classmethod
    def _input_snapshot(
        cls,
        experiment: dict[str, Any],
        control: dict[str, Any],
        treatment: dict[str, Any],
        windows: list[dict[str, Any]],
        included: list[dict[str, Any]],
        excluded: list[dict[str, Any]],
    ) -> dict[str, Any]:
        revisions = {
            str(revision["id"]): {
                "revision_id": revision["id"],
                "payload_hash": revision["payload_hash"],
                "values": {
                    key: revision[key]
                    for key in (
                        "connector_id",
                        "store_id",
                        "item_id",
                        "sku_id",
                        "revision_no",
                        "title",
                        "main_image_asset_id",
                        "sale_price",
                        "attributes",
                        "active_from",
                        "active_to",
                        "source_updated_at",
                    )
                },
            }
            for revision in (control, treatment)
        }
        buckets = [
            {
                "bucket_id": row["id"],
                "payload_hash": row["payload_hash"],
                "version": row["version"],
                "disposition": "included",
                "reason": None,
                "values": cls._bucket_values(row),
            }
            for row in included
        ]
        buckets.extend(
            {
                "bucket_id": item["bucket_id"],
                "payload_hash": item["payload_hash"],
                "version": item["version"],
                "disposition": "excluded",
                "reason": item["reason"],
                "values": item["values"],
            }
            for item in excluded
        )
        return {
            "experiment_payload_hash": experiment["payload_hash"],
            "experiment_record_version": experiment["record_version"],
            "experiment": {
                "payload_hash": experiment["payload_hash"],
                "record_version": experiment["record_version"],
                "values": {
                    key: experiment[key]
                    for key in (
                        "experiment_id",
                        "store_id",
                        "sku_id",
                        "experiment_type",
                        "primary_metric",
                        "status",
                        "started_at",
                        "ended_at",
                        "control_revision_id",
                        "treatment_revision_id",
                        "minimum_exposure",
                        "washout_window",
                        "analysis_policy_version",
                        "business_calendar_id",
                        "business_calendar_version",
                        "business_timezone",
                        "business_calendar_policy_version",
                    )
                },
            },
            "revisions": list(revisions.values()),
            "windows": [
                {
                    "window_id": window["window_id"],
                    "payload_hash": window["payload_hash"],
                    "values": {
                        key: window[key]
                        for key in (
                            "experiment_id",
                            "listing_revision_id",
                            "window_start",
                            "window_end",
                            "assignment",
                            "washout",
                            "source_receipt_id",
                        )
                    },
                }
                for window in windows
            ],
            "buckets": buckets,
        }

    def _interpret(
        self,
        facts: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, str | None]]:
        empty_metadata = {
            "model_provider": None,
            "model_name": None,
            "prompt_version": None,
        }
        interpreter = self.interpreter
        if interpreter is None:
            return (
                {"status": "not_generated", "reason": "model_not_configured"},
                empty_metadata,
            )
        isolated_facts = json.loads(json.dumps(facts, ensure_ascii=False))
        responses: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def invoke_interpreter() -> None:
            try:
                responses.put(("ok", interpreter.interpret(isolated_facts)))
            except Exception:
                responses.put(("error", None))

        worker = threading.Thread(
            target=invoke_interpreter,
            name="traffic-analysis-interpreter",
            daemon=True,
        )
        worker.start()
        try:
            status, raw_interpretation = responses.get(
                timeout=self.interpretation_timeout_seconds
            )
        except queue.Empty:
            return (
                {"status": "unavailable", "reason": "interpreter_timeout"},
                empty_metadata,
            )
        if status == "error":
            return (
                {"status": "unavailable", "reason": "interpreter_error"},
                empty_metadata,
            )
        try:
            interpretation = TrafficAnalysisInterpretation.model_validate(
                raw_interpretation
            )
        except ValidationError:
            return (
                {"status": "rejected", "reason": "invalid_interpretation_schema"},
                empty_metadata,
            )
        except Exception:
            return (
                {"status": "rejected", "reason": "invalid_interpretation_schema"},
                empty_metadata,
            )
        metadata = {
            "model_provider": interpretation.model_provider,
            "model_name": interpretation.model_name,
            "prompt_version": interpretation.prompt_version,
        }
        explanation = interpretation.model_dump(
            mode="json",
            exclude={"model_provider", "model_name", "prompt_version"},
        )
        return ({"status": "generated", **explanation}, metadata)
