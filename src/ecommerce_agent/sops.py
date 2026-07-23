from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .database import Database, utc_now
from .text_utils import checksum, redact_sensitive
from .tools import ToolExecutionContext, ToolRegistry, ToolResult


class SopError(ValueError):
    pass


class SopTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intents: list[str] = Field(min_length=1, max_length=20)


class SopStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(
        default=None, min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    observe: str | None = Field(default=None, min_length=1, max_length=128)
    clarify_if_missing: str | None = Field(default=None, min_length=1, max_length=80)
    evaluate: str | None = Field(default=None, min_length=1, max_length=128)
    propose: str | None = Field(default=None, min_length=1, max_length=128)
    act: str | None = Field(default=None, min_length=1, max_length=128)
    max_attempts: int = Field(default=1, ge=1, le=10)
    requires_approval: bool = False
    compensate_with: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_operation(self) -> "SopStep":
        operations = [
            name
            for name in ("observe", "clarify_if_missing", "evaluate", "propose", "act")
            if getattr(self, name) is not None
        ]
        if len(operations) != 1:
            raise ValueError("each SOP step must contain one supported operation")
        if self.requires_approval and operations[0] != "act":
            raise ValueError("requires_approval is only valid for act steps")
        if self.compensate_with and operations[0] != "act":
            raise ValueError("compensate_with is only valid for act steps")
        if operations[0] == "act" and self.max_attempts != 1:
            raise ValueError("act steps cannot be retried automatically")
        if operations[0] not in {"observe", "act"} and self.max_attempts != 1:
            raise ValueError("only observe steps support multiple attempts")
        return self

    @property
    def operation(self) -> str:
        for name in ("observe", "clarify_if_missing", "evaluate", "propose", "act"):
            if getattr(self, name) is not None:
                return name
        raise SopError("SOP step has no operation")

    @property
    def capability(self) -> str:
        return str(getattr(self, self.operation))


class SopDsl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger: SopTrigger
    required_context: list[str] = Field(default_factory=list, max_length=30)
    steps: list[SopStep] = Field(min_length=1, max_length=30)
    guards: dict[str, Any] = Field(default_factory=dict)
    handoff: dict[str, list[str]] = Field(default_factory=dict)
    success: dict[str, str]

    @field_validator("required_context")
    @classmethod
    def validate_context_fields(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("required_context contains duplicates")
        if any(not value or len(value) > 80 for value in values):
            raise ValueError("invalid required_context field")
        return values

    @model_validator(mode="after")
    def validate_handoff_and_success(self) -> "SopDsl":
        if set(self.handoff) - {"when"}:
            raise ValueError("handoff only supports the 'when' condition list")
        if "postcondition" not in self.success or not self.success["postcondition"].strip():
            raise ValueError("success.postcondition is required")
        step_ids: set[str] = set()
        for index, step in enumerate(self.steps):
            if step.id is None:
                step.id = f"step_{index + 1:02d}"
            if step.id in step_ids:
                raise ValueError("SOP step ids must be unique")
            step_ids.add(step.id)
        return self


class SopCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sop_key: str = Field(min_length=2, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    name: str = Field(min_length=2, max_length=120)
    intent: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    risk_level: Literal["low", "medium", "high", "critical"]
    dsl: SopDsl

    @model_validator(mode="after")
    def intent_must_be_declared(self) -> "SopCreateRequest":
        if self.intent not in self.dsl.trigger.intents:
            raise ValueError("definition intent must be listed in dsl.trigger.intents")
        return self


class SopReviseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_record_version: int = Field(ge=1)
    dsl: SopDsl


class SopTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_record_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=1000)


class SopStepResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_record_version: int = Field(ge=1)
    resolution: Literal["approve", "confirm_succeeded", "confirm_failed", "retry"]
    note: str = Field(min_length=2, max_length=1000)


class SopCompensationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_record_version: int = Field(ge=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    trusted_context: dict[str, Any] = Field(default_factory=dict)
    note: str = Field(min_length=2, max_length=1000)


class SopService:
    def __init__(self, db: Database, tools: ToolRegistry | None = None):
        self.db = db
        self.tools = tools

    def attach_tools(self, tools: ToolRegistry) -> None:
        self.tools = tools

    def seed_defaults(self, tenant_id: str) -> int:
        templates = (
            (
                "builtin.refund_control",
                "退款申请受控处理",
                "refund",
                "critical",
                {
                    "trigger": {"intents": ["refund"]},
                    "required_context": [],
                    "steps": [
                        {"observe": "get_order_facts", "max_attempts": 2},
                        {"evaluate": "refund_policy"},
                        {"propose": "refund_order"},
                    ],
                    "guards": {
                        "allow_external_write": False,
                        "external_write_requires_verified_postcondition": True,
                    },
                    "handoff": {"when": ["identity_missing", "policy_conflict", "tool_unavailable"]},
                    "success": {"postcondition": "refund_status_verified"},
                },
            ),
            (
                "builtin.order_change_control",
                "订单变更受控处理",
                "order",
                "high",
                {
                    "trigger": {"intents": ["order"]},
                    "required_context": [],
                    "steps": [
                        {"observe": "get_order_facts", "max_attempts": 2},
                        {"evaluate": "order_change_policy"},
                        {"propose": "create_order_change_handoff"},
                    ],
                    "guards": {
                        "allow_external_write": False,
                        "external_write_requires_verified_postcondition": True,
                    },
                    "handoff": {"when": ["identity_missing", "state_conflict", "tool_unavailable"]},
                    "success": {"postcondition": "order_state_verified"},
                },
            ),
        )
        inserted = 0
        for sop_key, name, intent, risk_level, dsl in templates:
            with self.db.connect() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM sop_definitions WHERE tenant_id=? AND sop_key=?",
                    (tenant_id, sop_key),
                ).fetchone()
            if exists:
                continue
            created = self.create(
                tenant_id,
                SopCreateRequest(
                    sop_key=sop_key,
                    name=name,
                    intent=intent,
                    risk_level=risk_level,
                    dsl=SopDsl.model_validate(dsl),
                ),
                "builtin",
            )
            version_id = created["versions"][0]["id"]
            evaluated = self.evaluate(
                tenant_id, version_id, SopTransitionRequest(expected_record_version=1), "builtin"
            )
            approved = self.approve(
                tenant_id,
                version_id,
                SopTransitionRequest(
                    expected_record_version=evaluated["definition"]["record_version"]
                ),
                "builtin",
            )
            self.activate(
                tenant_id,
                version_id,
                SopTransitionRequest(
                    expected_record_version=approved["definition"]["record_version"]
                ),
                "builtin",
            )
            inserted += 1
        return inserted

    def list_definitions(self, tenant_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT d.*, COUNT(v.id) AS version_count
                FROM sop_definitions d LEFT JOIN sop_versions v ON v.definition_id=d.id
                WHERE d.tenant_id=? GROUP BY d.id ORDER BY d.updated_at DESC
                """,
                (tenant_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def detail(self, tenant_id: str, definition_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            definition = conn.execute(
                "SELECT * FROM sop_definitions WHERE id=? AND tenant_id=?",
                (definition_id, tenant_id),
            ).fetchone()
            if definition is None:
                return None
            versions = conn.execute(
                "SELECT * FROM sop_versions WHERE definition_id=? ORDER BY version DESC",
                (definition_id,),
            ).fetchall()
        return {
            "definition": dict(definition),
            "versions": [self._version_view(row) for row in versions],
        }

    def create(self, tenant_id: str, request: SopCreateRequest, actor: str) -> dict[str, Any]:
        definition_id = f"sop-{uuid.uuid4().hex}"
        version_id = f"sopv-{uuid.uuid4().hex}"
        now = utc_now()
        payload = self._dsl_json(request.dsl)
        with self.db._write_lock, self.db.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO sop_definitions(
                        id, tenant_id, sop_key, name, intent, risk_level,
                        current_active_version, record_version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, 1, ?, ?)
                    """,
                    (
                        definition_id, tenant_id, request.sop_key, request.name,
                        request.intent, request.risk_level, now, now,
                    ),
                )
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    raise SopError("SOP key already exists") from exc
                raise
            conn.execute(
                """
                INSERT INTO sop_versions(
                    id, definition_id, version, dsl_json, checksum, status,
                    evaluation_json, created_by, approved_by, created_at,
                    evaluated_at, approved_at, activated_at, retired_at
                ) VALUES (?, ?, 1, ?, ?, 'draft', NULL, ?, NULL, ?, NULL, NULL, NULL, NULL)
                """,
                (version_id, definition_id, payload, checksum(payload), actor, now),
            )
        self.db.audit("sop.draft_created", actor, version_id, {"sop_key": request.sop_key}, tenant_id)
        return self.detail(tenant_id, definition_id) or {}

    def revise(
        self, tenant_id: str, definition_id: str, request: SopReviseRequest, actor: str
    ) -> dict[str, Any]:
        definition = self._require_definition(tenant_id, definition_id)
        if definition["record_version"] != request.expected_record_version:
            raise SopError("SOP version conflict")
        if definition["intent"] not in request.dsl.trigger.intents:
            raise SopError("definition intent must be listed in dsl.trigger.intents")
        payload = self._dsl_json(request.dsl)
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            next_version = int(
                conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM sop_versions WHERE definition_id=?",
                    (definition_id,),
                ).fetchone()[0]
            )
            cursor = conn.execute(
                "UPDATE sop_definitions SET record_version=record_version+1, updated_at=? "
                "WHERE id=? AND tenant_id=? AND record_version=?",
                (now, definition_id, tenant_id, request.expected_record_version),
            )
            if cursor.rowcount != 1:
                raise SopError("SOP version conflict")
            version_id = f"sopv-{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO sop_versions(
                    id, definition_id, version, dsl_json, checksum, status,
                    evaluation_json, created_by, approved_by, created_at,
                    evaluated_at, approved_at, activated_at, retired_at
                ) VALUES (?, ?, ?, ?, ?, 'draft', NULL, ?, NULL, ?, NULL, NULL, NULL, NULL)
                """,
                (version_id, definition_id, next_version, payload, checksum(payload), actor, now),
            )
        self.db.audit("sop.version_created", actor, version_id, {"version": next_version}, tenant_id)
        return self.detail(tenant_id, definition_id) or {}

    def evaluate(
        self, tenant_id: str, version_id: str, request: SopTransitionRequest, actor: str
    ) -> dict[str, Any]:
        version, definition = self._require_version(tenant_id, version_id)
        report = self._evaluation_report(
            SopDsl.model_validate(json.loads(version["dsl_json"])),
            risk_level=str(definition["risk_level"]),
        )
        return self._transition(
            tenant_id, version_id, definition, request, actor,
            from_status="draft", to_status="evaluated", event="sop.evaluated",
            extra={"evaluation_json": json.dumps(report, ensure_ascii=False), "evaluated_at": utc_now()},
        )

    def approve(
        self, tenant_id: str, version_id: str, request: SopTransitionRequest, actor: str
    ) -> dict[str, Any]:
        version, definition = self._require_version(tenant_id, version_id)
        try:
            evaluation = json.loads(version.get("evaluation_json") or "{}")
        except ValueError as exc:
            raise SopError("SOP evaluation report is invalid") from exc
        if evaluation.get("passed") is not True:
            raise SopError("SOP evaluation must pass before approval")
        return self._transition(
            tenant_id, version_id, definition, request, actor,
            from_status="evaluated", to_status="approved", event="sop.approved",
            extra={"approved_by": actor, "approved_at": utc_now()},
        )

    def activate(
        self, tenant_id: str, version_id: str, request: SopTransitionRequest, actor: str
    ) -> dict[str, Any]:
        version, definition = self._require_version(tenant_id, version_id)
        if version["status"] != "approved":
            raise SopError("SOP version must be approved before activation")
        if definition["record_version"] != request.expected_record_version:
            raise SopError("SOP version conflict")
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                "UPDATE sop_versions SET status='retired', retired_at=? "
                "WHERE definition_id=? AND status='active'",
                (now, definition["id"]),
            )
            cursor = conn.execute(
                "UPDATE sop_versions SET status='active', activated_at=?, retired_at=NULL "
                "WHERE id=? AND status='approved'",
                (now, version_id),
            )
            definition_cursor = conn.execute(
                """
                UPDATE sop_definitions SET current_active_version=?,
                    record_version=record_version+1, updated_at=?
                WHERE id=? AND tenant_id=? AND record_version=?
                """,
                (
                    version["version"], now, definition["id"], tenant_id,
                    request.expected_record_version,
                ),
            )
            if cursor.rowcount != 1 or definition_cursor.rowcount != 1:
                raise SopError("SOP activation conflict")
        self.db.audit("sop.activated", actor, version_id, {"note": request.note}, tenant_id)
        return self.detail(tenant_id, definition["id"]) or {}

    def retire(
        self, tenant_id: str, version_id: str, request: SopTransitionRequest, actor: str
    ) -> dict[str, Any]:
        version, definition = self._require_version(tenant_id, version_id)
        if version["status"] != "active":
            raise SopError("only an active SOP can be retired")
        if definition["record_version"] != request.expected_record_version:
            raise SopError("SOP version conflict")
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                "UPDATE sop_versions SET status='retired', retired_at=? WHERE id=? AND status='active'",
                (now, version_id),
            )
            definition_cursor = conn.execute(
                """
                UPDATE sop_definitions SET current_active_version=NULL,
                    record_version=record_version+1, updated_at=?
                WHERE id=? AND tenant_id=? AND record_version=?
                """,
                (now, definition["id"], tenant_id, request.expected_record_version),
            )
            if cursor.rowcount != 1 or definition_cursor.rowcount != 1:
                raise SopError("SOP retirement conflict")
        self.db.audit("sop.retired", actor, version_id, {"note": request.note}, tenant_id)
        return self.detail(tenant_id, definition["id"]) or {}

    def rollback(
        self, tenant_id: str, version_id: str, request: SopTransitionRequest, actor: str
    ) -> dict[str, Any]:
        version, definition = self._require_version(tenant_id, version_id)
        if version["status"] != "retired":
            raise SopError("rollback target must be a retired SOP version")
        if definition["record_version"] != request.expected_record_version:
            raise SopError("SOP version conflict")
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                "UPDATE sop_versions SET status='retired', retired_at=? "
                "WHERE definition_id=? AND status='active'",
                (now, definition["id"]),
            )
            cursor = conn.execute(
                "UPDATE sop_versions SET status='active', activated_at=?, retired_at=NULL "
                "WHERE id=? AND status='retired'",
                (now, version_id),
            )
            definition_cursor = conn.execute(
                """
                UPDATE sop_definitions SET current_active_version=?,
                    record_version=record_version+1, updated_at=?
                WHERE id=? AND tenant_id=? AND record_version=?
                """,
                (
                    version["version"], now, definition["id"], tenant_id,
                    request.expected_record_version,
                ),
            )
            if cursor.rowcount != 1 or definition_cursor.rowcount != 1:
                raise SopError("SOP rollback conflict")
        self.db.audit("sop.rolled_back", actor, version_id, {"note": request.note}, tenant_id)
        return self.detail(tenant_id, definition["id"]) or {}

    def catalog_for_context(self, tenant_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT d.id, d.sop_key, d.name, d.intent, d.risk_level,
                       v.id AS version_id, v.version, v.dsl_json
                FROM sop_definitions d JOIN sop_versions v
                  ON v.definition_id=d.id AND v.version=d.current_active_version
                WHERE d.tenant_id=? AND v.status='active'
                ORDER BY d.intent, d.sop_key
                """,
                (tenant_id,),
            ).fetchall()
        catalog = []
        for row in rows:
            dsl = json.loads(row["dsl_json"])
            catalog.append(
                {
                    "id": row["id"], "sop_key": row["sop_key"], "name": row["name"],
                    "intent": row["intent"], "risk_level": row["risk_level"],
                    "version_id": row["version_id"], "version": row["version"],
                    "required_context": dsl.get("required_context", []),
                    "guards": dsl.get("guards", {}), "handoff": dsl.get("handoff", {}),
                }
            )
        return catalog

    def resolve_for_session(
        self,
        tenant_id: str,
        session_id: str,
        intent: str,
        *,
        create_run: bool = True,
    ) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            pinned = conn.execute(
                """
                SELECT d.id, d.sop_key, d.name, d.intent, d.risk_level,
                       v.id AS version_id, v.version, v.dsl_json, r.id AS run_id,
                       r.status AS run_status, r.current_step_index,
                       r.record_version AS run_record_version, r.last_error
                FROM sop_runs r JOIN sop_definitions d ON d.id=r.definition_id
                JOIN sop_versions v ON v.id=r.sop_version_id
                WHERE r.tenant_id=? AND r.session_id=? AND d.intent=?
                ORDER BY r.started_at LIMIT 1
                """,
                (tenant_id, session_id, intent),
            ).fetchone()
            active = pinned or conn.execute(
                """
                SELECT d.id, d.sop_key, d.name, d.intent, d.risk_level,
                       v.id AS version_id, v.version, v.dsl_json, NULL AS run_id,
                       NULL AS run_status, NULL AS current_step_index,
                       NULL AS run_record_version, NULL AS last_error
                FROM sop_definitions d JOIN sop_versions v
                  ON v.definition_id=d.id AND v.version=d.current_active_version
                WHERE d.tenant_id=? AND d.intent=? AND v.status='active'
                ORDER BY d.updated_at DESC LIMIT 1
                """,
                (tenant_id, intent),
            ).fetchone()
        if active is None:
            return None
        result = dict(active)
        dsl = SopDsl.model_validate(json.loads(result.pop("dsl_json")))
        result["dsl"] = dsl.model_dump(mode="json", exclude_none=True)
        if result["run_id"] is None and create_run:
            run_id = f"soprun-{uuid.uuid4().hex}"
            now = utc_now()
            with self.db._write_lock, self.db.connect() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO sop_runs(
                        id, tenant_id, session_id, definition_id, sop_version_id,
                        status, outcome_json, started_at, completed_at,
                        current_step_index, record_version, updated_at, last_error
                    ) VALUES (?, ?, ?, ?, ?, 'active', '{}', ?, NULL, 0, 1, ?, NULL)
                    """,
                    (
                        run_id,
                        tenant_id,
                        session_id,
                        result["id"],
                        result["version_id"],
                        now,
                        now,
                    ),
                )
                saved = conn.execute(
                    """
                    SELECT id, status, current_step_index, record_version, last_error
                    FROM sop_runs WHERE session_id=? AND definition_id=?
                    """,
                    (session_id, result["id"]),
                ).fetchone()
                self._initialize_step_rows(
                    conn,
                    tenant_id=tenant_id,
                    run_id=str(saved["id"]),
                    dsl=dsl,
                    now=now,
                )
            result["run_id"] = saved["id"]
            result["run_status"] = saved["status"]
            result["current_step_index"] = saved["current_step_index"]
            result["run_record_version"] = saved["record_version"]
            result["last_error"] = saved["last_error"]
            self.db.audit(
                "sop.version_pinned", "agent", result["version_id"],
                {"session_id": session_id, "version": result["version"]}, tenant_id,
            )
        return result

    @staticmethod
    def _initialize_step_rows(
        conn: Any,
        *,
        tenant_id: str,
        run_id: str,
        dsl: SopDsl,
        now: str,
    ) -> None:
        for index, step in enumerate(dsl.steps):
            conn.execute(
                """
                INSERT OR IGNORE INTO sop_step_runs(
                    id, tenant_id, run_id, step_id, step_index, operation,
                    capability, status, attempt_count, max_attempts,
                    input_hash, idempotency_key, result_json,
                    postcondition_met, error_code, compensation_tool,
                    requires_approval, record_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL,
                          '{}', 0, NULL, ?, ?, 1, ?)
                """,
                (
                    f"sopstep-{uuid.uuid4().hex}",
                    tenant_id,
                    run_id,
                    step.id,
                    index,
                    step.operation,
                    step.capability,
                    step.max_attempts,
                    step.compensate_with,
                    int(step.requires_approval),
                    now,
                ),
            )

    @staticmethod
    def validate_action(
        sop: dict[str, Any],
        *,
        tool_name: str | None,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[bool, str, list[str]]:
        dsl = sop["dsl"]
        missing = [
            field
            for field in dsl.get("required_context", [])
            if not context.get(field)
        ]
        if missing:
            return False, "sop_required_context_missing", missing
        if dsl.get("guards", {}).get("allow_external_write") is not True:
            return False, "sop_external_write_not_allowed", []
        allowed_tools = [
            step["act"] for step in dsl.get("steps", []) if "act" in step
        ]
        if not tool_name or tool_name not in allowed_tools:
            return False, "tool_not_allowed_by_sop", []
        return True, "sop_action_allowed", []

    def complete_run(self, run_id: str, status: str, outcome: dict[str, Any]) -> None:
        if status not in {"completed", "handoff", "failed"}:
            raise SopError("invalid SOP run terminal status")
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                UPDATE sop_runs SET status=?, outcome_json=?, completed_at=?,
                    updated_at=?, record_version=record_version+1
                WHERE id=? AND status='active'
                """,
                (
                    status,
                    json.dumps(outcome, ensure_ascii=False),
                    utc_now(),
                    utc_now(),
                    run_id,
                ),
            )

    def begin_step(
        self,
        *,
        tenant_id: str,
        run_id: str,
        requested_mode: Literal["observe", "act"],
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        input_hash = checksum(
            json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT * FROM sop_runs WHERE id=? AND tenant_id=?",
                (run_id, tenant_id),
            ).fetchone()
            if run is None:
                raise SopError("SOP run not found")
            if run["status"] != "active":
                return self._gate(False, "sop_run_not_active")

            while True:
                step = conn.execute(
                    "SELECT * FROM sop_step_runs WHERE run_id=? AND step_index=?",
                    (run_id, run["current_step_index"]),
                ).fetchone()
                if step is None:
                    self._set_run_terminal(conn, run_id, "completed", None, now)
                    return self._gate(False, "sop_run_already_completed")
                status = str(step["status"])
                operation = str(step["operation"])
                capability = str(step["capability"])
                if status in {"succeeded", "skipped", "compensated"}:
                    self._advance_run(conn, run_id, int(step["step_index"]), now)
                    run = conn.execute("SELECT * FROM sop_runs WHERE id=?", (run_id,)).fetchone()
                    if run["status"] != "active":
                        return self._gate(False, "sop_run_already_completed")
                    continue
                if operation == "clarify_if_missing":
                    if context.get(capability):
                        conn.execute(
                            """
                            UPDATE sop_step_runs SET status='skipped', completed_at=?,
                                updated_at=?, error_code=NULL, record_version=record_version+1
                            WHERE id=? AND status IN ('pending','waiting_input')
                            """,
                            (now, now, step["id"]),
                        )
                        self._advance_run(conn, run_id, int(step["step_index"]), now)
                        run = conn.execute("SELECT * FROM sop_runs WHERE id=?", (run_id,)).fetchone()
                        if run["status"] != "active":
                            return self._gate(False, "sop_run_already_completed")
                        continue
                    conn.execute(
                        """
                        UPDATE sop_step_runs SET status='waiting_input', updated_at=?,
                            error_code='required_context_missing', record_version=record_version+1
                        WHERE id=? AND status IN ('pending','waiting_input')
                        """,
                        (now, step["id"]),
                    )
                    waiting_step = conn.execute(
                        "SELECT * FROM sop_step_runs WHERE id=?", (step["id"],)
                    ).fetchone()
                    return self._gate(
                        False,
                        "sop_step_context_missing",
                        [capability],
                        self._step_view(waiting_step),
                    )
                if operation in {"evaluate", "propose"}:
                    if status == "pending":
                        conn.execute(
                            """
                            UPDATE sop_step_runs SET status='waiting_approval', updated_at=?,
                                error_code='operator_decision_required',
                                record_version=record_version+1 WHERE id=? AND status='pending'
                            """,
                            (now, step["id"]),
                        )
                        step = conn.execute(
                            "SELECT * FROM sop_step_runs WHERE id=?", (step["id"],)
                        ).fetchone()
                    return self._gate(
                        False, "sop_step_approval_required", [], self._step_view(step)
                    )
                if status == "waiting_approval":
                    return self._gate(
                        False, "sop_step_approval_required", [], self._step_view(step)
                    )
                if status != "pending":
                    return self._gate(
                        False, "sop_step_requires_resolution", [], self._step_view(step)
                    )
                if operation != requested_mode or capability != tool_name:
                    return self._gate(
                        False, "sop_step_order_mismatch", [], self._step_view(step)
                    )
                if bool(step["requires_approval"]) and not step["approved_at"]:
                    conn.execute(
                        """
                        UPDATE sop_step_runs SET status='waiting_approval', updated_at=?,
                            error_code='operator_approval_required',
                            record_version=record_version+1 WHERE id=? AND status='pending'
                        """,
                        (now, step["id"]),
                    )
                    step = conn.execute(
                        "SELECT * FROM sop_step_runs WHERE id=?", (step["id"],)
                    ).fetchone()
                    return self._gate(
                        False, "sop_step_approval_required", [], self._step_view(step)
                    )
                if int(step["attempt_count"]) >= int(step["max_attempts"]):
                    conn.execute(
                        """
                        UPDATE sop_step_runs SET status='failed', error_code='attempts_exhausted',
                            completed_at=?, updated_at=?, record_version=record_version+1
                        WHERE id=? AND status='pending'
                        """,
                        (now, now, step["id"]),
                    )
                    self._set_run_terminal(conn, run_id, "handoff", "attempts_exhausted", now)
                    return self._gate(False, "sop_step_attempts_exhausted")
                idempotency_key = checksum(tenant_id, run_id, str(step["step_id"]), input_hash)
                cursor = conn.execute(
                    """
                    UPDATE sop_step_runs SET status='running',
                        attempt_count=attempt_count+1, input_hash=?, idempotency_key=?,
                        started_at=COALESCE(started_at, ?), completed_at=NULL,
                        updated_at=?, error_code=NULL, record_version=record_version+1
                    WHERE id=? AND status='pending' AND record_version=?
                    """,
                    (
                        input_hash,
                        idempotency_key,
                        now,
                        now,
                        step["id"],
                        step["record_version"],
                    ),
                )
                if cursor.rowcount != 1:
                    return self._gate(False, "sop_step_concurrent_claim")
                claimed = conn.execute(
                    "SELECT * FROM sop_step_runs WHERE id=?", (step["id"],)
                ).fetchone()
                return self._gate(True, "sop_step_started", [], self._step_view(claimed))

    def record_step_result(
        self,
        *,
        tenant_id: str,
        run_id: str,
        step_run_id: str,
        result: ToolResult,
    ) -> dict[str, Any]:
        now = utc_now()
        result_json = self._safe_result_json(result)
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            step = conn.execute(
                "SELECT * FROM sop_step_runs WHERE id=? AND run_id=? AND tenant_id=?",
                (step_run_id, run_id, tenant_id),
            ).fetchone()
            if step is None:
                raise SopError("SOP step run not found")
            if step["status"] != "running":
                raise SopError("SOP step is not running")

            if result.status == "success" and result.postcondition_met:
                status = "succeeded"
                error_code = None
                postcondition_met = 1
            elif result.status == "uncertain":
                status = "uncertain"
                error_code = result.error_code or "tool_result_uncertain"
                postcondition_met = 0
            elif (
                step["operation"] == "observe"
                and result.retryable
                and int(step["attempt_count"]) < int(step["max_attempts"])
            ):
                status = "pending"
                error_code = result.error_code or "retryable_observation_failure"
                postcondition_met = 0
            else:
                status = "failed"
                error_code = result.error_code or "postcondition_not_met"
                postcondition_met = 0

            conn.execute(
                """
                UPDATE sop_step_runs SET status=?, result_json=?, postcondition_met=?,
                    error_code=?, completed_at=?, updated_at=?,
                    record_version=record_version+1 WHERE id=? AND status='running'
                """,
                (
                    status,
                    result_json,
                    postcondition_met,
                    None if status == "pending" else error_code,
                    None if status == "pending" else now,
                    now,
                    step_run_id,
                ),
            )
            if status == "succeeded":
                self._advance_run(conn, run_id, int(step["step_index"]), now)
            elif status in {"failed", "uncertain"}:
                self._set_run_terminal(conn, run_id, "handoff", error_code, now)
            else:
                conn.execute(
                    """
                    UPDATE sop_runs SET updated_at=?, last_error=?,
                        record_version=record_version+1 WHERE id=? AND status='active'
                    """,
                    (now, error_code, run_id),
                )
        self.db.audit(
            "sop.step_completed",
            "agent",
            step_run_id,
            {
                "run_id": run_id,
                "status": status,
                "postcondition_met": bool(postcondition_met),
                "error_code": error_code,
            },
            tenant_id,
        )
        return self.get_run(tenant_id, run_id)

    def mark_handoff(self, run_id: str, reason: str) -> None:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                UPDATE sop_runs SET status='handoff', last_error=?, completed_at=?,
                    updated_at=?, record_version=record_version+1
                WHERE id=? AND status='active'
                """,
                (reason, now, now, run_id),
            )

    def list_runs(
        self, tenant_id: str, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        where = "r.tenant_id=?"
        params: list[Any] = [tenant_id]
        if status:
            where += " AND r.status=?"
            params.append(status)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT r.*, d.sop_key, d.name, d.intent, d.risk_level,
                       v.version AS sop_version,
                       sr.step_id AS current_step_id,
                       sr.operation AS current_step_operation,
                       sr.capability AS current_step_capability,
                       sr.status AS current_step_status,
                       sr.record_version AS current_step_record_version,
                       sr.compensation_tool AS current_step_compensation_tool,
                       sr.attempt_count AS current_step_attempt_count,
                       sr.max_attempts AS current_step_max_attempts
                FROM sop_runs r JOIN sop_definitions d ON d.id=r.definition_id
                JOIN sop_versions v ON v.id=r.sop_version_id
                LEFT JOIN sop_step_runs sr ON sr.run_id=r.id
                    AND sr.step_index=r.current_step_index
                WHERE {where} ORDER BY r.updated_at DESC LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [self._run_view(row, include_steps=False) for row in rows]

    def get_run(self, tenant_id: str, run_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT r.*, d.sop_key, d.name, d.intent, d.risk_level,
                       v.version AS sop_version
                FROM sop_runs r JOIN sop_definitions d ON d.id=r.definition_id
                JOIN sop_versions v ON v.id=r.sop_version_id
                WHERE r.id=? AND r.tenant_id=?
                """,
                (run_id, tenant_id),
            ).fetchone()
            if row is None:
                raise SopError("SOP run not found")
            steps = conn.execute(
                "SELECT * FROM sop_step_runs WHERE run_id=? ORDER BY step_index",
                (run_id,),
            ).fetchall()
        view = self._run_view(row, include_steps=True)
        view["steps"] = [self._step_view(step) for step in steps]
        return view

    def resolve_step(
        self,
        tenant_id: str,
        run_id: str,
        step_id: str,
        request: SopStepResolutionRequest,
        actor: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            step = conn.execute(
                "SELECT * FROM sop_step_runs WHERE run_id=? AND step_id=? AND tenant_id=?",
                (run_id, step_id, tenant_id),
            ).fetchone()
            run = conn.execute(
                "SELECT * FROM sop_runs WHERE id=? AND tenant_id=?", (run_id, tenant_id)
            ).fetchone()
            if step is None or run is None:
                raise SopError("SOP run or step not found")
            if int(step["record_version"]) != request.expected_record_version:
                raise SopError("SOP step version conflict")

            resolution = request.resolution
            status = str(step["status"])
            if resolution == "approve":
                if status != "waiting_approval":
                    raise SopError("only a waiting SOP step can be approved")
                if step["operation"] == "act":
                    new_status = "pending"
                    completed_at = None
                    advance = False
                else:
                    new_status = "succeeded"
                    completed_at = now
                    advance = True
                conn.execute(
                    """
                    UPDATE sop_step_runs SET status=?, approved_by=?, approved_at=?,
                        resolution_note=?, error_code=NULL, completed_at=?, updated_at=?,
                        record_version=record_version+1 WHERE id=? AND record_version=?
                    """,
                    (
                        new_status,
                        actor,
                        now,
                        request.note,
                        completed_at,
                        now,
                        step["id"],
                        request.expected_record_version,
                    ),
                )
                if advance:
                    self._advance_run(conn, run_id, int(step["step_index"]), now)
                else:
                    self._resume_run(conn, run_id, now)
            elif resolution == "confirm_succeeded":
                if status == "uncertain":
                    new_status = "succeeded"
                    run_status = "active"
                    advance = True
                elif status == "compensation_uncertain":
                    new_status = "compensated"
                    run_status = "failed"
                    advance = False
                else:
                    raise SopError("only an uncertain SOP step can be confirmed")
                conn.execute(
                    """
                    UPDATE sop_step_runs SET status=?, postcondition_met=1,
                        resolution_note=?, error_code=NULL, completed_at=?, updated_at=?,
                        record_version=record_version+1 WHERE id=? AND record_version=?
                    """,
                    (
                        new_status,
                        request.note,
                        now,
                        now,
                        step["id"],
                        request.expected_record_version,
                    ),
                )
                if advance:
                    self._resume_run(conn, run_id, now)
                    self._advance_run(conn, run_id, int(step["step_index"]), now)
                else:
                    self._set_run_terminal(
                        conn, run_id, run_status, "action_compensated", now
                    )
            elif resolution == "confirm_failed":
                if status not in {"uncertain", "compensation_uncertain"}:
                    raise SopError("only an uncertain SOP step can be resolved as failed")
                new_status = "failed" if status == "uncertain" else "compensation_failed"
                conn.execute(
                    """
                    UPDATE sop_step_runs SET status=?, postcondition_met=0,
                        resolution_note=?, error_code='operator_confirmed_failed',
                        completed_at=?, updated_at=?, record_version=record_version+1
                    WHERE id=? AND record_version=?
                    """,
                    (
                        new_status,
                        request.note,
                        now,
                        now,
                        step["id"],
                        request.expected_record_version,
                    ),
                )
                self._set_run_terminal(
                    conn, run_id, "failed", "operator_confirmed_failed", now
                )
            else:
                if status != "failed" or step["operation"] != "observe":
                    raise SopError("only a failed observe step can be retried")
                if int(step["attempt_count"]) >= int(step["max_attempts"]):
                    raise SopError("SOP step attempts exhausted")
                conn.execute(
                    """
                    UPDATE sop_step_runs SET status='pending', resolution_note=?,
                        error_code=NULL, completed_at=NULL, updated_at=?,
                        record_version=record_version+1 WHERE id=? AND record_version=?
                    """,
                    (request.note, now, step["id"], request.expected_record_version),
                )
                self._resume_run(conn, run_id, now)
        self.db.audit(
            "sop.step_resolved",
            actor,
            str(step["id"]),
            {"run_id": run_id, "resolution": request.resolution},
            tenant_id,
        )
        return self.get_run(tenant_id, run_id)

    def compensate_step(
        self,
        tenant_id: str,
        run_id: str,
        step_id: str,
        request: SopCompensationRequest,
        actor: str,
    ) -> dict[str, Any]:
        if self.tools is None:
            raise SopError("SOP compensation tools are not available")
        with self.db.connect() as conn:
            step = conn.execute(
                "SELECT * FROM sop_step_runs WHERE run_id=? AND step_id=? AND tenant_id=?",
                (run_id, step_id, tenant_id),
            ).fetchone()
            run = conn.execute(
                "SELECT * FROM sop_runs WHERE id=? AND tenant_id=?", (run_id, tenant_id)
            ).fetchone()
        if step is None or run is None:
            raise SopError("SOP run or step not found")
        if step["operation"] != "act" or step["status"] != "succeeded":
            raise SopError("only a succeeded action can be compensated")
        if not step["compensation_tool"]:
            raise SopError("SOP action has no compensation tool")
        if int(step["record_version"]) != request.expected_record_version:
            raise SopError("SOP step version conflict")

        execution_context = ToolExecutionContext(
            tenant_id=tenant_id,
            client_id=actor,
            session_id=str(run["session_id"]),
            trace_id=f"sop-comp-{uuid.uuid4().hex}",
            trusted_context=dict(request.trusted_context),
        )
        try:
            spec, arguments = self.tools.validate_selection(
                name=str(step["compensation_tool"]),
                arguments=request.arguments,
                requested_mode="act",
                context=execution_context,
            )
        except ValueError as exc:
            raise SopError(f"compensation_tool_invalid:{exc}") from exc

        input_hash = checksum(
            json.dumps(request.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        idempotency_key = checksum(tenant_id, run_id, step_id, "compensate", input_hash)
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE sop_step_runs SET status='compensating',
                    compensation_input_hash=?, compensation_idempotency_key=?,
                    compensation_attempt_count=compensation_attempt_count+1,
                    resolution_note=?, compensation_error_code=NULL,
                    updated_at=?, record_version=record_version+1
                WHERE id=? AND tenant_id=? AND status='succeeded' AND record_version=?
                """,
                (
                    input_hash,
                    idempotency_key,
                    request.note,
                    now,
                    step["id"],
                    tenant_id,
                    request.expected_record_version,
                ),
            )
            if cursor.rowcount != 1:
                raise SopError("SOP compensation conflict")

        result = self.tools.execute(spec=spec, arguments=arguments, context=execution_context)
        result_json = self._safe_result_json(result)
        completed_at = utc_now()
        if result.status == "success" and result.postcondition_met:
            compensation_status = "compensated"
            run_status = "failed"
            error_code = None
            run_error = "action_compensated"
        elif result.status == "uncertain":
            compensation_status = "compensation_uncertain"
            run_status = "handoff"
            error_code = result.error_code or "compensation_uncertain"
            run_error = error_code
        else:
            compensation_status = "compensation_failed"
            run_status = "handoff"
            error_code = result.error_code or "compensation_failed"
            run_error = error_code
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE sop_step_runs SET status=?, compensation_result_json=?,
                    compensation_error_code=?, completed_at=?, updated_at=?,
                    record_version=record_version+1
                WHERE id=? AND tenant_id=? AND status='compensating'
                """,
                (
                    compensation_status,
                    result_json,
                    error_code,
                    completed_at,
                    completed_at,
                    step["id"],
                    tenant_id,
                ),
            )
            if cursor.rowcount != 1:
                raise SopError("SOP compensation completion conflict")
            self._set_run_terminal(conn, run_id, run_status, run_error, completed_at)
        self.db.audit(
            "sop.step_compensated",
            actor,
            str(step["id"]),
            {
                "run_id": run_id,
                "tool": step["compensation_tool"],
                "status": compensation_status,
            },
            tenant_id,
        )
        return self.get_run(tenant_id, run_id)

    def recover_interrupted_runs(self) -> dict[str, int]:
        now = utc_now()
        report = {"observations_requeued": 0, "actions_uncertain": 0, "compensations_uncertain": 0}
        recovered: list[tuple[str, str, str, str]] = []
        with self.db._write_lock, self.db.connect() as conn:
            running = conn.execute(
                "SELECT * FROM sop_step_runs WHERE status IN ('running','compensating')"
            ).fetchall()
            for step in running:
                if step["status"] == "compensating":
                    new_status = "compensation_uncertain"
                    error = "process_interrupted_during_compensation"
                    report["compensations_uncertain"] += 1
                    run_status = "handoff"
                elif step["operation"] == "observe" and int(step["attempt_count"]) < int(step["max_attempts"]):
                    new_status = "pending"
                    error = "process_interrupted_before_observation_completed"
                    report["observations_requeued"] += 1
                    run_status = "active"
                elif step["operation"] == "observe":
                    new_status = "failed"
                    error = "observation_attempts_exhausted_after_restart"
                    run_status = "handoff"
                else:
                    new_status = "uncertain"
                    error = "process_interrupted_during_action"
                    report["actions_uncertain"] += 1
                    run_status = "handoff"
                conn.execute(
                    """
                    UPDATE sop_step_runs SET status=?, error_code=?, updated_at=?,
                        completed_at=CASE WHEN ?='pending' THEN NULL ELSE ? END,
                        record_version=record_version+1 WHERE id=?
                    """,
                    (new_status, error, now, new_status, now, step["id"]),
                )
                if run_status == "active":
                    self._resume_run(conn, str(step["run_id"]), now)
                else:
                    self._set_run_terminal(
                        conn, str(step["run_id"]), run_status, error, now
                    )
                recovered.append(
                    (
                        str(step["tenant_id"]),
                        str(step["id"]),
                        str(step["run_id"]),
                        new_status,
                    )
                )
        for tenant_id, step_run_id, run_id, status in recovered:
            self.db.audit(
                "sop.step_recovered",
                "startup-recovery",
                step_run_id,
                {"run_id": run_id, "status": status},
                tenant_id,
            )
        return report

    @staticmethod
    def _gate(
        allowed: bool,
        reason: str,
        missing_fields: list[str] | None = None,
        step: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "allowed": allowed,
            "reason": reason,
            "missing_fields": missing_fields or [],
            "step": step,
        }

    @staticmethod
    def _advance_run(conn: Any, run_id: str, step_index: int, now: str) -> None:
        next_step = conn.execute(
            "SELECT 1 FROM sop_step_runs WHERE run_id=? AND step_index>? LIMIT 1",
            (run_id, step_index),
        ).fetchone()
        if next_step is None:
            SopService._set_run_terminal(conn, run_id, "completed", None, now)
        else:
            conn.execute(
                """
                UPDATE sop_runs SET status='active', current_step_index=?, last_error=NULL,
                    completed_at=NULL, updated_at=?, record_version=record_version+1
                WHERE id=?
                """,
                (step_index + 1, now, run_id),
            )

    @staticmethod
    def _resume_run(conn: Any, run_id: str, now: str) -> None:
        conn.execute(
            """
            UPDATE sop_runs SET status='active', completed_at=NULL, last_error=NULL,
                updated_at=?, record_version=record_version+1 WHERE id=?
            """,
            (now, run_id),
        )

    @staticmethod
    def _set_run_terminal(
        conn: Any, run_id: str, status: str, error: str | None, now: str
    ) -> None:
        conn.execute(
            """
            UPDATE sop_runs SET status=?, last_error=?, completed_at=?, updated_at=?,
                record_version=record_version+1 WHERE id=?
            """,
            (status, error, now, now, run_id),
        )

    @staticmethod
    def _safe_result_json(result: ToolResult) -> str:
        sensitive_keys = {
            "access_token",
            "api_key",
            "authorization",
            "cookie",
            "credential",
            "credentials",
            "otp",
            "passcode",
            "password",
            "refresh_token",
            "secret",
            "token",
            "verification_code",
            "verify_code",
            "验证码",
            "口令",
            "密码",
        }

        def sanitize(value: Any, *, key: str | None = None, depth: int = 0) -> tuple[Any, bool]:
            normalized_key = (key or "").strip().lower().replace("-", "_")
            sensitive_suffixes = (
                "_api_key",
                "_credential",
                "_otp",
                "_passcode",
                "_password",
                "_secret",
                "_token",
            )
            if (
                normalized_key in sensitive_keys
                or normalized_key.endswith(sensitive_suffixes)
            ) and value is not None:
                return "[REDACTED]", True
            if depth >= 8:
                return "[TRUNCATED]", True
            if isinstance(value, dict):
                cleaned: dict[str, Any] = {}
                changed = len(value) > 100
                for item_key, item_value in list(value.items())[:100]:
                    safe_value, item_changed = sanitize(
                        item_value, key=str(item_key), depth=depth + 1
                    )
                    cleaned[str(item_key)] = safe_value
                    changed = changed or item_changed
                return cleaned, changed
            if isinstance(value, (list, tuple)):
                cleaned_items = []
                changed = len(value) > 100
                for item in list(value)[:100]:
                    safe_value, item_changed = sanitize(item, depth=depth + 1)
                    cleaned_items.append(safe_value)
                    changed = changed or item_changed
                return cleaned_items, changed
            if isinstance(value, str):
                return redact_sensitive(value)
            if value is None or isinstance(value, (bool, int, float)):
                return value, False
            safe_value, _ = redact_sensitive(str(value))
            return safe_value, True

        sanitized_output, redacted = sanitize(result.output)
        safe_output = json.dumps(
            sanitized_output,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return json.dumps(
            {
                "status": result.status,
                "error_code": result.error_code,
                "retryable": result.retryable,
                "postcondition_met": result.postcondition_met,
                "output_redacted": safe_output[:4000],
                "redacted": redacted,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _run_view(row: Any, *, include_steps: bool) -> dict[str, Any]:
        item = dict(row)
        item["outcome"] = json.loads(item.pop("outcome_json") or "{}")
        item["has_step_details"] = include_steps
        return item

    @staticmethod
    def _step_view(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["result"] = json.loads(item.pop("result_json") or "{}")
        item["compensation_result"] = json.loads(
            item.pop("compensation_result_json") or "{}"
        )
        item["postcondition_met"] = bool(item["postcondition_met"])
        item["requires_approval"] = bool(item["requires_approval"])
        return item

    def _transition(
        self,
        tenant_id: str,
        version_id: str,
        definition: dict[str, Any],
        request: SopTransitionRequest,
        actor: str,
        *,
        from_status: str,
        to_status: str,
        event: str,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        if definition["record_version"] != request.expected_record_version:
            raise SopError("SOP version conflict")
        allowed_columns = {"evaluation_json", "evaluated_at", "approved_by", "approved_at"}
        if set(extra) - allowed_columns:
            raise SopError("invalid SOP lifecycle fields")
        assignments = ["status=?", *[f"{column}=?" for column in extra]]
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            version_cursor = conn.execute(
                f"UPDATE sop_versions SET {', '.join(assignments)} WHERE id=? AND status=?",
                (to_status, *extra.values(), version_id, from_status),
            )
            definition_cursor = conn.execute(
                "UPDATE sop_definitions SET record_version=record_version+1, updated_at=? "
                "WHERE id=? AND tenant_id=? AND record_version=?",
                (now, definition["id"], tenant_id, request.expected_record_version),
            )
            if version_cursor.rowcount != 1 or definition_cursor.rowcount != 1:
                raise SopError("invalid SOP transition or version conflict")
        self.db.audit(event, actor, version_id, {"note": request.note}, tenant_id)
        return self.detail(tenant_id, definition["id"]) or {}

    def _require_definition(self, tenant_id: str, definition_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sop_definitions WHERE id=? AND tenant_id=?",
                (definition_id, tenant_id),
            ).fetchone()
        if row is None:
            raise SopError("SOP definition not found")
        return dict(row)

    def _require_version(
        self, tenant_id: str, version_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT v.*, d.tenant_id, d.record_version AS definition_record_version,
                       d.sop_key, d.name, d.intent, d.risk_level
                FROM sop_versions v JOIN sop_definitions d ON d.id=v.definition_id
                WHERE v.id=? AND d.tenant_id=?
                """,
                (version_id, tenant_id),
            ).fetchone()
        if row is None:
            raise SopError("SOP version not found")
        version = dict(row)
        definition = self._require_definition(tenant_id, version["definition_id"])
        return version, definition

    @staticmethod
    def _dsl_json(dsl: SopDsl) -> str:
        return json.dumps(
            dsl.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _evaluation_report(self, dsl: SopDsl, *, risk_level: str) -> dict[str, Any]:
        action_steps = [step for step in dsl.steps if step.operation == "act"]
        observe_steps = [step for step in dsl.steps if step.operation == "observe"]
        tool_checks_enabled = self.tools is not None

        def registered_as(name: str, kind: str) -> bool:
            if not tool_checks_enabled:
                return True
            spec = self.tools.get(name) if self.tools else None
            return spec is not None and spec.kind == kind

        paths = {
            "normal": bool(dsl.steps and dsl.success.get("postcondition")),
            "missing_context": True,
            "handoff": bool(dsl.handoff.get("when")),
            "guards": bool(dsl.guards),
            "step_ids_unique": len({step.id for step in dsl.steps}) == len(dsl.steps),
            "write_guard": not action_steps
            or dsl.guards.get("allow_external_write") is True,
            "high_risk_approval": risk_level not in {"high", "critical"}
            or all(step.requires_approval for step in action_steps),
            "single_shot_actions": all(step.max_attempts == 1 for step in action_steps),
            "observe_tools_registered": all(
                registered_as(step.capability, "read") for step in observe_steps
            ),
            "action_tools_registered": all(
                registered_as(step.capability, "write") for step in action_steps
            ),
            "compensation_tools_registered": all(
                not step.compensate_with
                or registered_as(step.compensate_with, "write")
                for step in action_steps
            ),
        }
        return {"passed": all(paths.values()), "paths": paths, "schema": "sop-dsl-v2"}

    @staticmethod
    def _version_view(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["dsl"] = json.loads(item.pop("dsl_json"))
        item["evaluation"] = json.loads(item.pop("evaluation_json") or "null")
        return item
