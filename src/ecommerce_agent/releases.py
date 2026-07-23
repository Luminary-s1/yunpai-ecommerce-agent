from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .database import Database, utc_now


ReleaseMode = Literal["shadow", "assist", "collaborative", "automatic"]
RiskLevel = Literal["low", "medium"]


class ReleaseError(ValueError):
    pass


class ReleasePolicyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_key: str = Field(
        min_length=3, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]+$"
    )
    name: str = Field(min_length=2, max_length=160)
    platform: str = Field(default="taobao", min_length=2, max_length=32)
    store_id: str = Field(min_length=1, max_length=128)
    mode: ReleaseMode
    traffic_percentage: int = Field(default=0, ge=0, le=100)
    intent_allowlist: list[str] = Field(min_length=1, max_length=64)
    max_risk_level: RiskLevel = "low"
    require_sources: bool = True
    allow_model_fallback: bool = False
    min_replay_cases: int = Field(default=20, ge=1, le=10_000)
    max_replay_failure_rate: float = Field(default=0.02, ge=0, le=1)
    max_replay_severe_errors: int = Field(default=0, ge=0, le=100)
    runtime_min_samples: int = Field(default=100, ge=1, le=1_000_000)
    max_runtime_failure_rate: float = Field(default=0.02, ge=0, le=1)
    max_runtime_severe_errors: int = Field(default=0, ge=0, le=100)

    @field_validator("intent_allowlist")
    @classmethod
    def validate_intents(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip().lower() for value in values]
        if any(not value or len(value) > 64 for value in cleaned):
            raise ValueError("intent allowlist values must be 1 to 64 characters")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("intent allowlist contains duplicates")
        return cleaned

    @model_validator(mode="after")
    def validate_automation_policy(self) -> "ReleasePolicyCreateRequest":
        if self.mode in {"collaborative", "automatic"} and not self.require_sources:
            raise ValueError("collaborative and automatic releases must require sources")
        if self.mode == "automatic" and self.allow_model_fallback:
            raise ValueError("automatic releases cannot allow model fallback")
        return self


class ReleaseTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_record_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=500)


class ReplayExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_intent: str | None = Field(default=None, min_length=1, max_length=64)
    expected_requires_human: bool | None = None
    require_sources: bool = False
    forbidden_answer_terms: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("forbidden_answer_terms")
    @classmethod
    def validate_forbidden_terms(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value or len(value) > 120 for value in cleaned):
            raise ValueError("forbidden answer terms must be 1 to 120 characters")
        return cleaned


class ReleaseReplayCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(
        min_length=1, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
    )
    message: str = Field(min_length=1, max_length=4000)
    context: dict[str, Any] = Field(default_factory=dict, max_length=16)
    expectation: ReplayExpectation

    @model_validator(mode="after")
    def require_assertion(self) -> "ReleaseReplayCase":
        expected = self.expectation
        if not any(
            (
                expected.expected_intent is not None,
                expected.expected_requires_human is not None,
                expected.require_sources,
                bool(expected.forbidden_answer_terms),
            )
        ):
            raise ValueError("each replay case must contain at least one assertion")
        return self


class ReleaseReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: list[ReleaseReplayCase] = Field(min_length=1, max_length=500)

    @field_validator("cases")
    @classmethod
    def unique_case_ids(cls, values: list[ReleaseReplayCase]) -> list[ReleaseReplayCase]:
        case_ids = [item.case_id for item in values]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("replay case ids must be unique")
        return values


class ReleaseService:
    _RISK_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

    def __init__(self, db: Database):
        self.db = db

    def create(
        self,
        tenant_id: str,
        request: ReleasePolicyCreateRequest,
        actor: str,
    ) -> dict[str, Any]:
        now = utc_now()
        release_id = f"release-{uuid.uuid4().hex}"
        with self.db._write_lock, self.db.connect() as conn:
            previous = conn.execute(
                """
                SELECT * FROM release_policies
                WHERE tenant_id=? AND release_key=?
                ORDER BY version DESC LIMIT 1
                """,
                (tenant_id, request.release_key),
            ).fetchone()
            if previous is not None:
                if (
                    previous["platform"] != request.platform
                    or previous["store_id"] != request.store_id
                ):
                    raise ReleaseError("release key scope cannot change between versions")
                version = int(previous["version"]) + 1
            else:
                version = 1
            conn.execute(
                """
                INSERT INTO release_policies(
                    id, tenant_id, release_key, version, name, platform, store_id,
                    mode, traffic_percentage, intent_allowlist_json, max_risk_level,
                    require_sources, allow_model_fallback, min_replay_cases,
                    max_replay_failure_rate, max_replay_severe_errors,
                    runtime_min_samples, max_runtime_failure_rate,
                    max_runtime_severe_errors, rollout_salt, status,
                    latest_replay_run_id, evaluation_passed, evaluation_json,
                    pause_reason, record_version, created_by, approved_by,
                    created_at, updated_at, evaluated_at, approved_at,
                    activated_at, paused_at, retired_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'draft', NULL, NULL, NULL, NULL, 1, ?, NULL, ?, ?, NULL, NULL,
                    NULL, NULL, NULL
                )
                """,
                (
                    release_id,
                    tenant_id,
                    request.release_key,
                    version,
                    request.name,
                    request.platform,
                    request.store_id,
                    request.mode,
                    request.traffic_percentage,
                    json.dumps(request.intent_allowlist, ensure_ascii=False),
                    request.max_risk_level,
                    int(request.require_sources),
                    int(request.allow_model_fallback),
                    request.min_replay_cases,
                    request.max_replay_failure_rate,
                    request.max_replay_severe_errors,
                    request.runtime_min_samples,
                    request.max_runtime_failure_rate,
                    request.max_runtime_severe_errors,
                    secrets.token_hex(16),
                    actor,
                    now,
                    now,
                ),
            )
            saved = conn.execute(
                "SELECT * FROM release_policies WHERE id=?", (release_id,)
            ).fetchone()
        self.db.audit(
            "release.created",
            actor,
            release_id,
            {
                "release_key": request.release_key,
                "version": version,
                "mode": request.mode,
                "traffic_percentage": request.traffic_percentage,
            },
            tenant_id,
        )
        return self._policy_view(saved)

    def list_policies(
        self, tenant_id: str, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM release_policies WHERE tenant_id=?"
        params: list[Any] = [tenant_id]
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY updated_at DESC, version DESC"
        with self.db.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._policy_view(row) for row in rows]

    def get_policy(self, tenant_id: str, release_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM release_policies WHERE id=? AND tenant_id=?",
                (release_id, tenant_id),
            ).fetchone()
        if row is None:
            raise ReleaseError("release policy not found")
        return self._policy_view(row)

    def run_replay(
        self,
        tenant_id: str,
        release_id: str,
        request: ReleaseReplayRequest,
        actor: str,
        runner: Callable[[ReleaseReplayCase], Any],
    ) -> dict[str, Any]:
        policy = self.get_policy(tenant_id, release_id)
        if policy["status"] not in {"draft", "evaluated"}:
            raise ReleaseError("release replay requires draft or evaluated status")
        dataset_payload = [case.model_dump(mode="json") for case in request.cases]
        dataset_hash = hashlib.sha256(
            json.dumps(
                dataset_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        run_id = f"replay-{uuid.uuid4().hex}"
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO release_replay_runs(
                    id, tenant_id, release_id, dataset_hash, status,
                    started_by, created_at
                ) VALUES (?, ?, ?, ?, 'running', ?, ?)
                """,
                (run_id, tenant_id, release_id, dataset_hash, actor, now),
            )

        results: list[dict[str, Any]] = []
        for case in request.cases:
            try:
                response = runner(case)
                result = self._evaluate_case(policy, case, response)
            except Exception as exc:
                result = {
                    "case_id": case.case_id,
                    "passed": False,
                    "severe": True,
                    "violations": ["execution_error"],
                    "actual": {"error_type": type(exc).__name__},
                }
            results.append(result)

        total = len(results)
        passed_cases = sum(1 for item in results if item["passed"])
        failed_cases = total - passed_cases
        severe_errors = sum(1 for item in results if item["severe"])
        failure_rate = failed_cases / total if total else 1.0
        gate_passed = (
            total >= int(policy["min_replay_cases"])
            and failure_rate <= float(policy["max_replay_failure_rate"])
            and severe_errors <= int(policy["max_replay_severe_errors"])
        )
        report = {
            "run_id": run_id,
            "release_id": release_id,
            "dataset_hash": dataset_hash,
            "passed": gate_passed,
            "summary": {
                "total": total,
                "passed": passed_cases,
                "failed": failed_cases,
                "failure_rate": failure_rate,
                "severe_errors": severe_errors,
                "required_cases": policy["min_replay_cases"],
                "max_failure_rate": policy["max_replay_failure_rate"],
                "max_severe_errors": policy["max_replay_severe_errors"],
            },
            "results": results,
        }
        completed_at = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                UPDATE release_replay_runs
                SET status=?, total_cases=?, passed_cases=?, failed_cases=?,
                    severe_errors=?, failure_rate=?, results_json=?, completed_at=?
                WHERE id=? AND tenant_id=? AND status='running'
                """,
                (
                    "passed" if gate_passed else "failed",
                    total,
                    passed_cases,
                    failed_cases,
                    severe_errors,
                    failure_rate,
                    json.dumps(results, ensure_ascii=False),
                    completed_at,
                    run_id,
                    tenant_id,
                ),
            )
            cursor = conn.execute(
                """
                UPDATE release_policies
                SET status='evaluated', latest_replay_run_id=?, evaluation_passed=?,
                    evaluation_json=?, evaluated_at=?, updated_at=?,
                    record_version=record_version+1
                WHERE id=? AND tenant_id=? AND status IN ('draft','evaluated')
                  AND record_version=?
                """,
                (
                    run_id,
                    int(gate_passed),
                    json.dumps(report["summary"], ensure_ascii=False),
                    completed_at,
                    completed_at,
                    release_id,
                    tenant_id,
                    policy["record_version"],
                ),
            )
            if cursor.rowcount != 1:
                raise ReleaseError("release policy changed while replay was running")
        self.db.audit(
            "release.replay_completed",
            actor,
            release_id,
            {
                "run_id": run_id,
                "passed": gate_passed,
                "total": total,
                "failed": failed_cases,
                "severe_errors": severe_errors,
                "dataset_hash": dataset_hash,
            },
            tenant_id,
        )
        return report

    def approve(
        self,
        tenant_id: str,
        release_id: str,
        request: ReleaseTransitionRequest,
        actor: str,
    ) -> dict[str, Any]:
        policy = self.get_policy(tenant_id, release_id)
        if policy["status"] != "evaluated" or not policy["evaluation_passed"]:
            raise ReleaseError("release must pass replay or versioned evaluation before approval")
        if policy["mode"] in {"collaborative", "automatic"} and actor == policy["created_by"]:
            raise ReleaseError("automated release approval requires a second operator")
        saved = self._transition(
            tenant_id,
            release_id,
            request,
            actor,
            from_status="evaluated",
            to_status="approved",
            extra={"approved_by": actor, "approved_at": utc_now()},
            event="release.approved",
        )
        return saved

    def apply_evaluation(
        self,
        tenant_id: str,
        release_id: str,
        *,
        run_id: str,
        passed: bool,
        summary: Mapping[str, Any],
        expected_record_version: int,
        actor: str,
    ) -> dict[str, Any]:
        """Attach a completed, versioned evaluation run to a draft release."""

        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            run = conn.execute(
                """
                SELECT id, status, release_id FROM evaluation_runs
                WHERE id=? AND tenant_id=?
                """,
                (run_id, tenant_id),
            ).fetchone()
            if run is None or run["release_id"] != release_id:
                raise ReleaseError("evaluation run does not belong to the release")
            expected_status = "passed" if passed else "failed"
            if run["status"] != expected_status:
                raise ReleaseError("evaluation run status does not match its gate result")
            cursor = conn.execute(
                """
                UPDATE release_policies
                SET status='evaluated', latest_evaluation_run_id=?,
                    evaluation_passed=?, evaluation_json=?, evaluated_at=?,
                    updated_at=?, record_version=record_version+1
                WHERE id=? AND tenant_id=? AND status IN ('draft','evaluated')
                  AND record_version=?
                """,
                (
                    run_id,
                    int(passed),
                    json.dumps(summary, ensure_ascii=False),
                    now,
                    now,
                    release_id,
                    tenant_id,
                    expected_record_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ReleaseError("release policy changed while evaluation was running")
            saved = conn.execute(
                "SELECT * FROM release_policies WHERE id=?", (release_id,)
            ).fetchone()
        metrics = summary.get("metrics", summary)
        self.db.audit(
            "release.evaluation_applied",
            actor,
            release_id,
            {
                "run_id": run_id,
                "passed": passed,
                "total_cases": metrics.get("total_cases")
                if isinstance(metrics, Mapping)
                else None,
            },
            tenant_id,
        )
        return self._policy_view(saved)

    def activate(
        self,
        tenant_id: str,
        release_id: str,
        request: ReleaseTransitionRequest,
        actor: str,
    ) -> dict[str, Any]:
        policy = self.get_policy(tenant_id, release_id)
        if policy["status"] != "approved" or not policy["evaluation_passed"]:
            raise ReleaseError("only an approved passing release can be activated")
        if int(policy["traffic_percentage"]) <= 0:
            raise ReleaseError("release traffic percentage must be greater than zero")
        if int(policy["record_version"]) != request.expected_record_version:
            raise ReleaseError("release transition or version conflict")
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                UPDATE release_policies
                SET status='retired', retired_at=?, updated_at=?,
                    record_version=record_version+1
                WHERE tenant_id=? AND platform=? AND store_id=? AND status='active'
                  AND id<>?
                """,
                (
                    now,
                    now,
                    tenant_id,
                    policy["platform"],
                    policy["store_id"],
                    release_id,
                ),
            )
            cursor = conn.execute(
                """
                UPDATE release_policies
                SET status='active', activated_at=?, pause_reason=NULL,
                    paused_at=NULL, updated_at=?, record_version=record_version+1
                WHERE id=? AND tenant_id=? AND status='approved' AND record_version=?
                """,
                (now, now, release_id, tenant_id, request.expected_record_version),
            )
            if cursor.rowcount != 1:
                raise ReleaseError("release transition or version conflict")
            saved = conn.execute(
                "SELECT * FROM release_policies WHERE id=?", (release_id,)
            ).fetchone()
        self.db.audit(
            "release.activated",
            actor,
            release_id,
            {"note": request.note, "traffic_percentage": policy["traffic_percentage"]},
            tenant_id,
        )
        return self._policy_view(saved)

    def pause(
        self,
        tenant_id: str,
        release_id: str,
        request: ReleaseTransitionRequest,
        actor: str,
    ) -> dict[str, Any]:
        return self._transition(
            tenant_id,
            release_id,
            request,
            actor,
            from_status="active",
            to_status="paused",
            extra={"paused_at": utc_now(), "pause_reason": request.note or "manual pause"},
            event="release.paused",
        )

    def retire(
        self,
        tenant_id: str,
        release_id: str,
        request: ReleaseTransitionRequest,
        actor: str,
    ) -> dict[str, Any]:
        policy = self.get_policy(tenant_id, release_id)
        if policy["status"] not in {"draft", "evaluated", "approved", "active", "paused"}:
            raise ReleaseError("release cannot be retired from its current status")
        return self._transition(
            tenant_id,
            release_id,
            request,
            actor,
            from_status=str(policy["status"]),
            to_status="retired",
            extra={"retired_at": utc_now()},
            event="release.retired",
        )

    def assignment(
        self,
        tenant_id: str,
        platform: str,
        store_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM release_policies
                WHERE tenant_id=? AND platform=? AND status='active'
                  AND store_id IN (?, '*')
                ORDER BY CASE WHEN store_id=? THEN 0 ELSE 1 END, activated_at DESC
                LIMIT 1
                """,
                (tenant_id, platform, store_id, store_id),
            ).fetchone()
        if row is None:
            return {
                "policy": None,
                "selected": False,
                "bucket": None,
                "reason": "no_active_release",
            }
        policy = self._policy_view(row)
        digest = hashlib.sha256(
            f"{policy['rollout_salt']}:{tenant_id}:{platform}:{store_id}:{conversation_id}".encode(
                "utf-8"
            )
        ).digest()
        bucket = int.from_bytes(digest[:8], "big") % 10_000
        selected = bucket < int(policy["traffic_percentage"]) * 100
        return {
            "policy": policy,
            "selected": selected,
            "bucket": bucket,
            "reason": "selected" if selected else "control_bucket",
        }

    def record_response(
        self,
        tenant_id: str,
        assignment: Mapping[str, Any],
        *,
        conversation_id: str,
        event_id: str,
        response: Any,
    ) -> dict[str, Any]:
        policy = assignment.get("policy")
        if not isinstance(policy, Mapping) or policy.get("tenant_id") != tenant_id:
            raise ReleaseError("release assignment does not belong to the tenant")
        selected = bool(assignment.get("selected"))
        assessment = self._assess_runtime(policy, selected, response)
        observation_id = f"release-observation-{uuid.uuid4().hex}"
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            existing = conn.execute(
                """
                SELECT * FROM release_observations
                WHERE tenant_id=? AND release_id=? AND event_id=?
                """,
                (tenant_id, policy["id"], event_id),
            ).fetchone()
            if existing is not None:
                return self._observation_view(existing)
            conn.execute(
                """
                INSERT INTO release_observations(
                    id, tenant_id, release_id, conversation_id, event_id,
                    assignment_bucket, selected, intent, risk_level,
                    requires_human, source_count, model_fallback, action,
                    violations_json, severe, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    tenant_id,
                    policy["id"],
                    conversation_id,
                    event_id,
                    int(assignment["bucket"]),
                    int(selected),
                    assessment["intent"],
                    assessment["risk_level"],
                    int(assessment["requires_human"]),
                    assessment["source_count"],
                    int(assessment["model_fallback"]),
                    assessment["action"],
                    json.dumps(assessment["violations"], ensure_ascii=False),
                    int(assessment["severe"]),
                    now,
                ),
            )
            saved = conn.execute(
                "SELECT * FROM release_observations WHERE id=?", (observation_id,)
            ).fetchone()
        paused = self._maybe_auto_pause(tenant_id, str(policy["id"]))
        view = self._observation_view(saved)
        view["release_paused"] = paused
        return view

    def mark_delivery_failure(
        self,
        tenant_id: str,
        release_id: str,
        event_id: str,
        reason: str,
    ) -> dict[str, Any]:
        with self.db._write_lock, self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM release_observations
                WHERE tenant_id=? AND release_id=? AND event_id=?
                """,
                (tenant_id, release_id, event_id),
            ).fetchone()
            if row is None:
                raise ReleaseError("release observation not found")
            violations = json.loads(row["violations_json"] or "[]")
            marker = f"delivery_{reason}"[:96]
            if marker not in violations:
                violations.append(marker)
            conn.execute(
                """
                UPDATE release_observations
                SET action='blocked', violations_json=?, severe=1
                WHERE id=?
                """,
                (json.dumps(violations, ensure_ascii=False), row["id"]),
            )
            saved = conn.execute(
                "SELECT * FROM release_observations WHERE id=?", (row["id"],)
            ).fetchone()
        paused = self._maybe_auto_pause(tenant_id, release_id)
        view = self._observation_view(saved)
        view["release_paused"] = paused
        return view

    def list_observations(
        self, tenant_id: str, release_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.get_policy(tenant_id, release_id)
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM release_observations
                WHERE tenant_id=? AND release_id=?
                ORDER BY created_at DESC LIMIT ?
                """,
                (tenant_id, release_id, limit),
            ).fetchall()
        return [self._observation_view(row) for row in rows]

    def runtime_summary(self, tenant_id: str, release_id: str) -> dict[str, Any]:
        policy = self.get_policy(tenant_id, release_id)
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS samples,
                       COALESCE(SUM(CASE WHEN severe=1 THEN 1 ELSE 0 END), 0) AS severe,
                       COALESCE(SUM(CASE WHEN violations_json<>'[]' THEN 1 ELSE 0 END), 0)
                           AS failures
                FROM release_observations
                WHERE tenant_id=? AND release_id=? AND selected=1
                """,
                (tenant_id, release_id),
            ).fetchone()
        samples = int(row["samples"])
        failures = int(row["failures"])
        return {
            "release_id": release_id,
            "status": policy["status"],
            "samples": samples,
            "failures": failures,
            "failure_rate": failures / samples if samples else 0.0,
            "severe_errors": int(row["severe"]),
            "limits": {
                "runtime_min_samples": policy["runtime_min_samples"],
                "max_runtime_failure_rate": policy["max_runtime_failure_rate"],
                "max_runtime_severe_errors": policy["max_runtime_severe_errors"],
            },
        }

    def _evaluate_case(
        self, policy: Mapping[str, Any], case: ReleaseReplayCase, response: Any
    ) -> dict[str, Any]:
        actual = self._response_values(response)
        violations, severe = self._policy_violations(policy, actual)
        expected = case.expectation
        if expected.expected_intent and actual["intent"] != expected.expected_intent.lower():
            violations.append("intent_mismatch")
        if (
            expected.expected_requires_human is not None
            and actual["requires_human"] != expected.expected_requires_human
        ):
            marker = (
                "missed_handoff"
                if expected.expected_requires_human and not actual["requires_human"]
                else "unexpected_handoff"
            )
            violations.append(marker)
            if marker == "missed_handoff":
                severe.append(marker)
        if expected.require_sources and not actual["sources"] and not actual["requires_human"]:
            violations.append("case_evidence_missing")
            severe.append("case_evidence_missing")
        answer_folded = actual["answer"].casefold()
        for term in expected.forbidden_answer_terms:
            if term.casefold() in answer_folded:
                violations.append("forbidden_answer_term")
                severe.append("forbidden_answer_term")
                break
        violations = list(dict.fromkeys(violations))
        severe = list(dict.fromkeys(severe))
        return {
            "case_id": case.case_id,
            "passed": not violations,
            "severe": bool(severe),
            "violations": violations,
            "actual": {
                "intent": actual["intent"],
                "risk_level": actual["risk_level"],
                "requires_human": actual["requires_human"],
                "source_count": len(actual["sources"]),
                "model_fallback": actual["model_fallback"],
            },
        }

    def _assess_runtime(
        self, policy: Mapping[str, Any], selected: bool, response: Any
    ) -> dict[str, Any]:
        actual = self._response_values(response)
        violations, severe = self._policy_violations(policy, actual)
        if not selected:
            action = "control"
        elif policy["mode"] == "shadow":
            action = "shadow"
        elif actual["requires_human"]:
            action = "handoff"
        elif policy["mode"] == "assist":
            action = "draft"
        elif violations:
            action = "handoff" if severe else "draft"
        else:
            action = "send"
        return {
            "intent": actual["intent"],
            "risk_level": actual["risk_level"],
            "requires_human": actual["requires_human"],
            "source_count": len(actual["sources"]),
            "model_fallback": actual["model_fallback"],
            "action": action,
            "violations": list(dict.fromkeys(violations)),
            "severe": bool(severe),
        }

    def _policy_violations(
        self, policy: Mapping[str, Any], actual: Mapping[str, Any]
    ) -> tuple[list[str], list[str]]:
        violations: list[str] = []
        severe: list[str] = []
        if actual["intent"] not in policy["intent_allowlist"]:
            violations.append("intent_not_allowlisted")
            if policy["mode"] in {"collaborative", "automatic"}:
                severe.append("intent_not_allowlisted")
        if (
            self._RISK_RANK.get(actual["risk_level"], 99)
            > self._RISK_RANK[str(policy["max_risk_level"])]
            and not actual["requires_human"]
        ):
            violations.append("risk_above_release_limit")
            severe.append("risk_above_release_limit")
        if policy["require_sources"] and not actual["sources"] and not actual["requires_human"]:
            violations.append("evidence_missing")
            severe.append("evidence_missing")
        if actual["model_fallback"] and not policy["allow_model_fallback"]:
            violations.append("model_fallback_disallowed")
            if policy["mode"] in {"collaborative", "automatic"}:
                severe.append("model_fallback_disallowed")
        return violations, severe

    def _maybe_auto_pause(self, tenant_id: str, release_id: str) -> bool:
        summary = self.runtime_summary(tenant_id, release_id)
        if summary["status"] != "active":
            return summary["status"] == "paused"
        limits = summary["limits"]
        reason: str | None = None
        if summary["severe_errors"] > int(limits["max_runtime_severe_errors"]):
            reason = "severe_error_budget_exceeded"
        elif (
            summary["samples"] >= int(limits["runtime_min_samples"])
            and summary["failure_rate"] > float(limits["max_runtime_failure_rate"])
        ):
            reason = "runtime_failure_rate_exceeded"
        if reason is None:
            return False
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE release_policies
                SET status='paused', pause_reason=?, paused_at=?, updated_at=?,
                    record_version=record_version+1
                WHERE id=? AND tenant_id=? AND status='active'
                """,
                (reason, now, now, release_id, tenant_id),
            )
        if cursor.rowcount:
            self.db.audit(
                "release.auto_paused",
                "release-gate",
                release_id,
                {
                    "reason": reason,
                    "samples": summary["samples"],
                    "failure_rate": summary["failure_rate"],
                    "severe_errors": summary["severe_errors"],
                },
                tenant_id,
            )
        return bool(cursor.rowcount)

    def _transition(
        self,
        tenant_id: str,
        release_id: str,
        request: ReleaseTransitionRequest,
        actor: str,
        *,
        from_status: str,
        to_status: str,
        extra: Mapping[str, Any],
        event: str,
    ) -> dict[str, Any]:
        allowed_columns = {
            "approved_by",
            "approved_at",
            "paused_at",
            "pause_reason",
            "retired_at",
        }
        if set(extra) - allowed_columns:
            raise ReleaseError("invalid release transition metadata")
        assignments = ["status=?", "updated_at=?", "record_version=record_version+1"]
        values: list[Any] = [to_status, utc_now()]
        for column, value in extra.items():
            assignments.append(f"{column}=?")
            values.append(value)
        values.extend(
            [release_id, tenant_id, from_status, request.expected_record_version]
        )
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE release_policies SET {', '.join(assignments)}
                WHERE id=? AND tenant_id=? AND status=? AND record_version=?
                """,
                values,
            )
            if cursor.rowcount != 1:
                raise ReleaseError("release transition or version conflict")
            saved = conn.execute(
                "SELECT * FROM release_policies WHERE id=?", (release_id,)
            ).fetchone()
        self.db.audit(
            event,
            actor,
            release_id,
            {"from": from_status, "to": to_status, "note": request.note},
            tenant_id,
        )
        return self._policy_view(saved)

    @staticmethod
    def _response_values(response: Any) -> dict[str, Any]:
        def value(name: str, default: Any) -> Any:
            if isinstance(response, Mapping):
                return response.get(name, default)
            return getattr(response, name, default)

        sources = value("sources", []) or []
        return {
            "answer": str(value("answer", "")),
            "intent": str(value("intent", "unknown")).lower(),
            "risk_level": str(value("risk_level", "critical")).lower(),
            "requires_human": bool(value("requires_human", True)),
            "sources": list(sources),
            "model_fallback": bool(value("model_fallback", True)),
        }

    @staticmethod
    def _policy_view(row: Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["intent_allowlist"] = json.loads(item.pop("intent_allowlist_json"))
        item["require_sources"] = bool(item["require_sources"])
        item["allow_model_fallback"] = bool(item["allow_model_fallback"])
        item["evaluation_passed"] = (
            None if item["evaluation_passed"] is None else bool(item["evaluation_passed"])
        )
        item["evaluation"] = json.loads(item.pop("evaluation_json") or "null")
        return item

    @staticmethod
    def _observation_view(row: Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["selected"] = bool(item["selected"])
        item["requires_human"] = (
            None if item["requires_human"] is None else bool(item["requires_human"])
        )
        item["model_fallback"] = (
            None if item["model_fallback"] is None else bool(item["model_fallback"])
        )
        item["severe"] = bool(item["severe"])
        item["violations"] = json.loads(item.pop("violations_json") or "[]")
        return item
