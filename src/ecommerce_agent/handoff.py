from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .database import Database, session_scope_condition, utc_now
from .handoff_staffing import HandoffStaffingService, StaffingError
from .schemas import (
    HandoffEventView,
    HandoffQueueUpsert,
    HandoffQueueView,
    HandoffView,
)
from .text_utils import redact_sensitive

if TYPE_CHECKING:
    from .handoff_dispatch import HandoffDispatchService


class HandoffError(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"accepted", "rejected", "failed", "canceled"},
    "accepted": {"working", "failed", "canceled"},
    "working": {"input_required", "review", "failed", "canceled"},
    "input_required": {"working", "failed", "canceled"},
    "review": {"working", "completed", "failed", "canceled"},
    "completed": set(),
    "rejected": set(),
    "failed": set(),
    "canceled": set(),
}
OPEN_STATUSES = {"proposed", "accepted", "working", "input_required", "review"}
ASSIGNED_STATUSES = {"accepted", "working", "input_required", "review"}
TERMINAL_STATUSES = {"completed", "rejected", "failed", "canceled"}
PRIORITY_RANK = {"low": 0, "normal": 1, "high": 2, "urgent": 3}
ROUTING_TOKEN = re.compile(r"^[a-z0-9_.:-]+\*?$")


DEFAULT_QUEUES: tuple[dict[str, Any], ...] = (
    {
        "queue_key": "complaints",
        "name": "投诉与高风险",
        "description": "投诉、重大风险与紧急升级任务",
        "default_priority": "urgent",
        "first_response_sla_minutes": 5,
        "resolution_sla_minutes": 60,
        "max_active_per_operator": 8,
        "match_reasons": ["customer_escalation", "complaint", "safety_*"],
        "match_intents": ["complaint"],
        "match_risk_levels": ["critical", "blocked"],
        "routing_order": 10,
        "escalation_queue_key": None,
    },
    {
        "queue_key": "after_sales",
        "name": "售后处理",
        "description": "退款、退换货、订单与物流异常",
        "default_priority": "high",
        "first_response_sla_minutes": 10,
        "resolution_sla_minutes": 240,
        "max_active_per_operator": 15,
        "match_reasons": ["authorized_order_context_missing"],
        "match_intents": ["refund", "return", "after_sales", "order", "logistics"],
        "match_risk_levels": [],
        "routing_order": 20,
        "escalation_queue_key": "complaints",
    },
    {
        "queue_key": "technical",
        "name": "技术异常",
        "description": "模型、工具、上下文和渠道链路故障",
        "default_priority": "high",
        "first_response_sla_minutes": 15,
        "resolution_sla_minutes": 240,
        "max_active_per_operator": 20,
        "match_reasons": ["tool_*", "model_*", "react_*", "context_*"],
        "match_intents": [],
        "match_risk_levels": [],
        "routing_order": 30,
        "escalation_queue_key": "complaints",
    },
    {
        "queue_key": "general",
        "name": "通用接管队列",
        "description": "客户主动转人工及未命中特定规则的任务",
        "default_priority": "normal",
        "first_response_sla_minutes": 30,
        "resolution_sla_minutes": 480,
        "max_active_per_operator": 20,
        "match_reasons": [],
        "match_intents": [],
        "match_risk_levels": [],
        "routing_order": 999,
        "escalation_queue_key": "complaints",
    },
)


class HandoffService:
    def __init__(
        self, db: Database, staffing: HandoffStaffingService | None = None
    ):
        self.db = db
        self.staffing = staffing or HandoffStaffingService(db)
        self.dispatcher: HandoffDispatchService | None = None

    def set_dispatcher(self, dispatcher: "HandoffDispatchService") -> None:
        self.dispatcher = dispatcher

    def ensure_default_queues(self, tenant_id: str) -> None:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            for queue in DEFAULT_QUEUES:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO handoff_queues(
                        id, tenant_id, queue_key, name, description, status,
                        default_priority, first_response_sla_minutes,
                        resolution_sla_minutes, max_active_per_operator,
                        escalation_queue_id, match_reasons_json, match_intents_json,
                        match_risk_levels_json, routing_order, record_version,
                        created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, NULL, ?, ?, ?, ?, 1,
                              'system-default', ?, ?)
                    """,
                    (
                        f"queue-{uuid.uuid4().hex}",
                        tenant_id,
                        queue["queue_key"],
                        queue["name"],
                        queue["description"],
                        queue["default_priority"],
                        queue["first_response_sla_minutes"],
                        queue["resolution_sla_minutes"],
                        queue["max_active_per_operator"],
                        self._json(queue["match_reasons"]),
                        self._json(queue["match_intents"]),
                        self._json(queue["match_risk_levels"]),
                        queue["routing_order"],
                        now,
                        now,
                    ),
                )
            complaint = conn.execute(
                "SELECT id FROM handoff_queues WHERE tenant_id=? AND queue_key='complaints'",
                (tenant_id,),
            ).fetchone()
            if complaint is not None:
                conn.execute(
                    """
                    UPDATE handoff_queues SET escalation_queue_id=?, updated_at=?
                    WHERE tenant_id=? AND queue_key IN ('after_sales','technical','general')
                      AND escalation_queue_id IS NULL
                    """,
                    (complaint["id"], now, tenant_id),
                )

    def upsert_queue(
        self,
        *,
        tenant_id: str,
        value: HandoffQueueUpsert,
        actor: str,
    ) -> HandoffQueueView:
        if value.resolution_sla_minutes < value.first_response_sla_minutes:
            raise HandoffError("resolution SLA cannot be shorter than first-response SLA")
        if not value.name.strip():
            raise HandoffError("handoff queue name cannot be blank")
        self.ensure_default_queues(tenant_id)
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM handoff_queues WHERE tenant_id=? AND queue_key=?",
                (tenant_id, value.queue_key),
            ).fetchone()
            escalation_queue_id = None
            if value.escalation_queue_key:
                if value.escalation_queue_key == value.queue_key:
                    raise HandoffError("a queue cannot escalate to itself")
                escalation = conn.execute(
                    """
                    SELECT id FROM handoff_queues
                    WHERE tenant_id=? AND queue_key=? AND status='active'
                    """,
                    (tenant_id, value.escalation_queue_key),
                ).fetchone()
                if escalation is None:
                    raise HandoffError("escalation queue not found or inactive")
                escalation_queue_id = str(escalation["id"])
            clean_reasons = self._routing_values(value.match_reasons)
            clean_intents = self._routing_values(value.match_intents)
            if existing is None:
                if value.expected_record_version != 0:
                    raise HandoffError("handoff queue version conflict")
                queue_id = f"queue-{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO handoff_queues(
                        id, tenant_id, queue_key, name, description, status,
                        default_priority, first_response_sla_minutes,
                        resolution_sla_minutes, max_active_per_operator,
                        escalation_queue_id, match_reasons_json, match_intents_json,
                        match_risk_levels_json, routing_order, record_version,
                        created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        queue_id,
                        tenant_id,
                        value.queue_key,
                        value.name.strip(),
                        value.description.strip(),
                        value.status,
                        value.default_priority,
                        value.first_response_sla_minutes,
                        value.resolution_sla_minutes,
                        value.max_active_per_operator,
                        escalation_queue_id,
                        self._json(clean_reasons),
                        self._json(clean_intents),
                        self._json(value.match_risk_levels),
                        value.routing_order,
                        actor,
                        now,
                        now,
                    ),
                )
                event = "handoff.queue_created"
            else:
                if int(existing["record_version"]) != value.expected_record_version:
                    raise HandoffError("handoff queue version conflict")
                queue_id = str(existing["id"])
                cursor = conn.execute(
                    """
                    UPDATE handoff_queues
                    SET name=?, description=?, status=?, default_priority=?,
                        first_response_sla_minutes=?, resolution_sla_minutes=?,
                        max_active_per_operator=?, escalation_queue_id=?,
                        match_reasons_json=?, match_intents_json=?,
                        match_risk_levels_json=?, routing_order=?,
                        record_version=record_version+1, updated_at=?
                    WHERE id=? AND tenant_id=? AND record_version=?
                    """,
                    (
                        value.name.strip(),
                        value.description.strip(),
                        value.status,
                        value.default_priority,
                        value.first_response_sla_minutes,
                        value.resolution_sla_minutes,
                        value.max_active_per_operator,
                        escalation_queue_id,
                        self._json(clean_reasons),
                        self._json(clean_intents),
                        self._json(value.match_risk_levels),
                        value.routing_order,
                        now,
                        queue_id,
                        tenant_id,
                        value.expected_record_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise HandoffError("handoff queue version conflict")
                event = "handoff.queue_updated"
            catchall = conn.execute(
                """
                SELECT 1 FROM handoff_queues
                WHERE tenant_id=? AND status='active'
                  AND match_reasons_json='[]' AND match_intents_json='[]'
                  AND match_risk_levels_json='[]'
                LIMIT 1
                """,
                (tenant_id,),
            ).fetchone()
            if catchall is None:
                raise HandoffError("at least one active catch-all handoff queue is required")
            row = self._queue_row(conn, tenant_id, queue_id)
        self.db.audit(event, actor, queue_id, {"queue_key": value.queue_key}, tenant_id)
        return self._queue_view(dict(row))

    def list_queues(
        self, *, tenant_id: str, scope: str = "operational"
    ) -> list[HandoffQueueView]:
        self.ensure_default_queues(tenant_id)
        scope_condition = session_scope_condition(scope)
        staffing_counts = self.staffing.queue_counts(tenant_id=tenant_id)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT q.*, eq.queue_key AS escalation_queue_key,
                       SUM(CASE WHEN h.status IN ('proposed','accepted','working',
                           'input_required','review') THEN 1 ELSE 0 END) AS open_tasks,
                       SUM(CASE WHEN h.status IN ('accepted','working','input_required','review')
                           THEN 1 ELSE 0 END) AS assigned_tasks
                FROM handoff_queues q
                LEFT JOIN handoff_queues eq ON eq.id=q.escalation_queue_id
                LEFT JOIN handoff_tasks h ON h.queue_id=q.id AND h.tenant_id=q.tenant_id
                  AND EXISTS (
                    SELECT 1 FROM sessions s
                    WHERE s.id=h.session_id AND {scope_condition}
                  )
                WHERE q.tenant_id=?
                GROUP BY q.id
                ORDER BY q.routing_order, q.queue_key
                """,
                (tenant_id,),
            ).fetchall()
            task_rows = conn.execute(
                f"""
                SELECT h.*, q.queue_key, q.name AS queue_name
                FROM handoff_tasks h JOIN handoff_queues q ON q.id=h.queue_id
                JOIN sessions s ON s.id=h.session_id
                WHERE h.tenant_id=? AND h.status IN ('proposed','accepted','working',
                    'input_required','review') AND {scope_condition}
                """,
                (tenant_id,),
            ).fetchall()
        breached: dict[str, int] = {}
        for task in task_rows:
            if self._sla(dict(task))["sla_status"] == "breached":
                key = str(task["queue_id"])
                breached[key] = breached.get(key, 0) + 1
        return [
            self._queue_view(
                dict(row),
                breached_tasks=breached.get(str(row["id"]), 0),
                total_operators=staffing_counts.get(str(row["id"]), {}).get("total", 0),
                available_operators=staffing_counts.get(str(row["id"]), {}).get(
                    "available", 0
                ),
            )
            for row in rows
        ]

    def create(
        self,
        *,
        tenant_id: str,
        session_id: str,
        message_id: str,
        reason: str,
        payload: dict[str, Any],
        acceptance_criteria: str = "人工核对问题、记录处理结果并完成复核",
        deadline_hours: int | None = None,
        max_retries: int = 2,
        queue_key: str | None = None,
        priority: str | None = None,
    ) -> HandoffView:
        if priority is not None and priority not in PRIORITY_RANK:
            raise HandoffError("invalid handoff priority")
        self.ensure_default_queues(tenant_id)
        with self.db._write_lock, self.db.connect() as conn:
            existing = conn.execute(
                """
                SELECT h.*, s.external_session_id, q.queue_key, q.name AS queue_name,
                       p.display_name AS assigned_operator_name,
                       p.presence AS assigned_operator_presence,
                       p.status AS operator_status,
                       p.presence_expires_at AS operator_presence_expires_at,
                       c.status AS operator_credential_status
                FROM handoff_tasks h
                JOIN sessions s ON s.id=h.session_id
                JOIN handoff_queues q ON q.id=h.queue_id
                LEFT JOIN handoff_operator_profiles p
                  ON p.tenant_id=h.tenant_id AND p.admin_id=h.assigned_to
                LEFT JOIN api_clients c
                  ON c.tenant_id=p.tenant_id AND c.id=p.admin_id
                WHERE h.message_id=?
                """,
                (message_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["tenant_id"]) != tenant_id:
                    raise HandoffError("handoff idempotency scope conflict")
                return self._view(dict(existing))
            queue = self._select_queue(conn, tenant_id, reason, payload, queue_key)
            chosen_priority = priority or str(queue["default_priority"])
            risk_level = str(payload.get("risk_level", "")).lower()
            if risk_level in {"critical", "blocked"}:
                chosen_priority = "urgent"
            elif risk_level == "high" and PRIORITY_RANK[chosen_priority] < PRIORITY_RANK["high"]:
                chosen_priority = "high"
            now_dt = datetime.now(UTC)
            first_deadline = now_dt + timedelta(
                minutes=int(queue["first_response_sla_minutes"])
            )
            resolution_minutes = int(queue["resolution_sla_minutes"])
            if deadline_hours is not None:
                resolution_minutes = max(
                    int(queue["first_response_sla_minutes"]), deadline_hours * 60
                )
            resolution_deadline = now_dt + timedelta(minutes=resolution_minutes)
            handoff_id = f"handoff-{uuid.uuid4().hex}"
            now = now_dt.isoformat()
            conn.execute(
                """
                INSERT INTO handoff_tasks(
                    id, tenant_id, session_id, message_id, status, reason,
                    payload_json, acceptance_criteria, assigned_to, deadline_at,
                    max_retries, retry_count, version, created_at, updated_at, completed_at,
                    queue_id, priority, sla_first_response_at, sla_resolution_at,
                    acknowledged_at, started_at, review_started_at, escalated_at,
                    escalation_level, escalation_reason
                ) VALUES (?, ?, ?, ?, 'proposed', ?, ?, ?, NULL, ?, ?, 0, 1, ?, ?, NULL,
                          ?, ?, ?, ?, NULL, NULL, NULL, NULL, 0, NULL)
                """,
                (
                    handoff_id,
                    tenant_id,
                    session_id,
                    message_id,
                    reason,
                    self._json(payload),
                    acceptance_criteria,
                    resolution_deadline.isoformat(),
                    max_retries,
                    now,
                    now,
                    queue["id"],
                    chosen_priority,
                    first_deadline.isoformat(),
                    resolution_deadline.isoformat(),
                ),
            )
            self._insert_event(
                conn,
                tenant_id=tenant_id,
                handoff_id=handoff_id,
                event_type="created",
                from_status=None,
                to_status="proposed",
                from_queue_id=None,
                to_queue_id=str(queue["id"]),
                from_assignee=None,
                to_assignee=None,
                task_version=1,
                actor="agent",
                note=reason,
            )
            if self.dispatcher is not None:
                self.dispatcher.enqueue_in_transaction(
                    conn,
                    tenant_id=tenant_id,
                    handoff_id=handoff_id,
                    queue_id=str(queue["id"]),
                    priority=chosen_priority,
                    created_at=now,
                )
            row = self._task_row(conn, tenant_id, handoff_id)
        self.db.audit(
            "handoff.proposed",
            "agent",
            handoff_id,
            {
                "message_id": message_id,
                "reason": reason,
                "queue_key": queue["queue_key"],
                "priority": chosen_priority,
            },
            tenant_id,
        )
        return self._view(dict(row))

    def list(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        queue_key: str | None = None,
        priority: str | None = None,
        assigned_to: str | None = None,
        sla: str | None = None,
        scope: str = "operational",
        limit: int = 200,
    ) -> list[HandoffView]:
        if status is not None and status not in ALLOWED_TRANSITIONS:
            raise HandoffError("invalid handoff status")
        if priority is not None and priority not in PRIORITY_RANK:
            raise HandoffError("invalid handoff priority")
        if sla not in {None, "on_track", "due_soon", "breached", "met", "unassigned"}:
            raise HandoffError("invalid handoff SLA filter")
        self.ensure_default_queues(tenant_id)
        conditions = ["h.tenant_id=?", session_scope_condition(scope)]
        params: list[Any] = [tenant_id]
        for field, value in (
            ("h.status", status),
            ("q.queue_key", queue_key),
            ("h.priority", priority),
            ("h.assigned_to", assigned_to),
        ):
            if value:
                conditions.append(f"{field}=?")
                params.append(value)
        params.append(max(1, min(500, limit)))
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT h.*, s.external_session_id, s.source_type, s.source_reference,
                       q.queue_key, q.name AS queue_name,
                       p.display_name AS assigned_operator_name,
                       p.presence AS assigned_operator_presence,
                       p.status AS operator_status,
                       p.presence_expires_at AS operator_presence_expires_at,
                       c.status AS operator_credential_status
                FROM handoff_tasks h
                JOIN sessions s ON s.id=h.session_id
                JOIN handoff_queues q ON q.id=h.queue_id
                LEFT JOIN handoff_operator_profiles p
                  ON p.tenant_id=h.tenant_id AND p.admin_id=h.assigned_to
                LEFT JOIN api_clients c
                  ON c.tenant_id=p.tenant_id AND c.id=p.admin_id
                WHERE {' AND '.join(conditions)}
                ORDER BY CASE h.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                         WHEN 'normal' THEN 2 ELSE 3 END,
                         COALESCE(h.sla_first_response_at, h.sla_resolution_at), h.created_at
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        views = [self._view(dict(row)) for row in rows]
        if sla == "unassigned":
            return [item for item in views if item.status == "proposed" and not item.assigned_to]
        if sla:
            return [item for item in views if item.sla_status == sla]
        return views

    def get(self, *, tenant_id: str, handoff_id: str) -> HandoffView:
        with self.db.connect() as conn:
            row = self._task_row(conn, tenant_id, handoff_id)
        if row is None:
            raise HandoffError("handoff task not found")
        return self._view(dict(row))

    def claim(
        self,
        *,
        tenant_id: str,
        handoff_id: str,
        operator: str,
        expected_version: int,
        note: str | None = None,
    ) -> HandoffView:
        clean_note = self._clean_note(note)
        with self.db._write_lock, self.db.connect() as conn:
            row = self._task_row(conn, tenant_id, handoff_id)
            if row is None:
                raise HandoffError("handoff task not found")
            if int(row["version"]) != expected_version:
                raise HandoffError("handoff version conflict")
            if str(row["status"]) != "proposed" or row["assigned_to"] is not None:
                raise HandoffError("handoff task is no longer claimable")
            try:
                self.staffing.require_eligible(
                    conn,
                    tenant_id=tenant_id,
                    queue_id=str(row["queue_id"]),
                    operator_id=operator,
                    exclude_handoff_id=None,
                )
            except StaffingError as exc:
                raise HandoffError(str(exc)) from exc
            now = utc_now()
            next_version = expected_version + 1
            cursor = conn.execute(
                """
                UPDATE handoff_tasks
                SET status='accepted', assigned_to=?,
                    acknowledged_at=COALESCE(acknowledged_at, ?), version=?, updated_at=?
                WHERE id=? AND tenant_id=? AND status='proposed' AND assigned_to IS NULL
                  AND version=?
                """,
                (operator, now, next_version, now, handoff_id, tenant_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise HandoffError("handoff claim conflict")
            self._insert_event(
                conn,
                tenant_id=tenant_id,
                handoff_id=handoff_id,
                event_type="claimed",
                from_status="proposed",
                to_status="accepted",
                from_queue_id=str(row["queue_id"]),
                to_queue_id=str(row["queue_id"]),
                from_assignee=None,
                to_assignee=operator,
                task_version=next_version,
                actor=operator,
                note=clean_note,
            )
            if self.dispatcher is not None:
                self.dispatcher.mark_assigned_in_transaction(
                    conn,
                    tenant_id=tenant_id,
                    handoff_id=handoff_id,
                    assigned_to=operator,
                    now=now,
                )
            updated = self._task_row(conn, tenant_id, handoff_id)
        self.db.audit(
            "handoff.claimed",
            operator,
            handoff_id,
            {"queue_key": row["queue_key"], "version": next_version},
            tenant_id,
        )
        return self._view(dict(updated))

    def auto_assign(
        self,
        *,
        tenant_id: str,
        handoff_id: str,
        expected_version: int,
        actor: str,
        note: str,
        automatic_only: bool = False,
    ) -> HandoffView:
        clean_note = self._require_note(note)
        with self.db._write_lock, self.db.connect() as conn:
            row = self._task_row(conn, tenant_id, handoff_id)
            if row is None:
                raise HandoffError("handoff task not found")
            if int(row["version"]) != expected_version:
                raise HandoffError("handoff version conflict")
            if str(row["status"]) != "proposed" or row["assigned_to"] is not None:
                raise HandoffError("handoff task is no longer assignable")
            candidates = self.staffing.rank_candidates(
                conn,
                tenant_id=tenant_id,
                queue_id=str(row["queue_id"]),
                automatic_only=automatic_only,
            )
            if not candidates:
                raise HandoffError("no available operator is eligible for this queue")
            selected = candidates[0]
            operator = str(selected["admin_id"])
            now = utc_now()
            next_version = expected_version + 1
            cursor = conn.execute(
                """
                UPDATE handoff_tasks
                SET status='accepted', assigned_to=?,
                    acknowledged_at=COALESCE(acknowledged_at, ?), version=?, updated_at=?
                WHERE id=? AND tenant_id=? AND status='proposed' AND assigned_to IS NULL
                  AND version=?
                """,
                (operator, now, next_version, now, handoff_id, tenant_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise HandoffError("handoff assignment conflict")
            self._insert_event(
                conn,
                tenant_id=tenant_id,
                handoff_id=handoff_id,
                event_type="claimed",
                from_status="proposed",
                to_status="accepted",
                from_queue_id=str(row["queue_id"]),
                to_queue_id=str(row["queue_id"]),
                from_assignee=None,
                to_assignee=operator,
                task_version=next_version,
                actor=actor,
                note=clean_note,
            )
            if self.dispatcher is not None:
                self.dispatcher.mark_assigned_in_transaction(
                    conn,
                    tenant_id=tenant_id,
                    handoff_id=handoff_id,
                    assigned_to=operator,
                    now=now,
                )
            updated = self._task_row(conn, tenant_id, handoff_id)
        self.db.audit(
            "handoff.auto_assigned",
            actor,
            handoff_id,
            {
                "assigned_to": operator,
                "queue_key": row["queue_key"],
                "load_ratio": selected["load_ratio"],
                "skill_level": selected["skill_level"],
                "version": next_version,
            },
            tenant_id,
        )
        return self._view(dict(updated))

    def transition(
        self,
        *,
        tenant_id: str,
        handoff_id: str,
        target_status: str,
        operator: str,
        expected_version: int,
        note: str | None,
    ) -> HandoffView:
        if target_status == "accepted":
            return self.claim(
                tenant_id=tenant_id,
                handoff_id=handoff_id,
                operator=operator,
                expected_version=expected_version,
                note=note,
            )
        with self.db._write_lock, self.db.connect() as conn:
            row = self._task_row(conn, tenant_id, handoff_id)
            if row is None:
                raise HandoffError("handoff task not found")
            current = str(row["status"])
            if int(row["version"]) != expected_version:
                raise HandoffError("handoff version conflict")
            if target_status not in ALLOWED_TRANSITIONS[current]:
                raise HandoffError(f"illegal handoff transition: {current} -> {target_status}")
            if current in {"accepted", "working", "input_required"}:
                if row["assigned_to"] != operator:
                    raise HandoffError("only the assigned operator can advance this task")
            clean_note = self._clean_note(note)
            if target_status in TERMINAL_STATUSES and not clean_note:
                raise HandoffError("a resolution note is required for terminal transitions")
            next_version = expected_version + 1
            now = utc_now()
            completed_at = now if target_status in TERMINAL_STATUSES else None
            retry_count = int(row["retry_count"])
            if current == "input_required" and target_status == "working":
                retry_count += 1
                if retry_count > int(row["max_retries"]):
                    raise HandoffError("handoff retry budget exhausted")
            started_at = row["started_at"]
            review_started_at = row["review_started_at"]
            if target_status == "working" and started_at is None:
                started_at = now
            if target_status == "review":
                review_started_at = now
            cursor = conn.execute(
                """
                UPDATE handoff_tasks
                SET status=?, retry_count=?, version=?, updated_at=?, completed_at=?,
                    started_at=?, review_started_at=?
                WHERE id=? AND tenant_id=? AND version=?
                """,
                (
                    target_status,
                    retry_count,
                    next_version,
                    now,
                    completed_at,
                    started_at,
                    review_started_at,
                    handoff_id,
                    tenant_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise HandoffError("handoff version conflict")
            self._insert_event(
                conn,
                tenant_id=tenant_id,
                handoff_id=handoff_id,
                event_type="transitioned",
                from_status=current,
                to_status=target_status,
                from_queue_id=str(row["queue_id"]),
                to_queue_id=str(row["queue_id"]),
                from_assignee=row["assigned_to"],
                to_assignee=row["assigned_to"],
                task_version=next_version,
                actor=operator,
                note=clean_note,
            )
            if target_status in TERMINAL_STATUSES and self.dispatcher is not None:
                self.dispatcher.cancel_in_transaction(
                    conn, tenant_id=tenant_id, handoff_id=handoff_id, now=now
                )
            updated = self._task_row(conn, tenant_id, handoff_id)
        self.db.audit(
            "handoff.transitioned",
            operator,
            handoff_id,
            {"from": current, "to": target_status, "note": clean_note, "version": next_version},
            tenant_id,
        )
        return self._view(dict(updated))

    def reassign(
        self,
        *,
        tenant_id: str,
        handoff_id: str,
        assigned_to: str,
        expected_version: int,
        actor: str,
        note: str,
        queue_key: str | None = None,
    ) -> HandoffView:
        clean_note = self._require_note(note)
        with self.db._write_lock, self.db.connect() as conn:
            row = self._task_row(conn, tenant_id, handoff_id)
            if row is None:
                raise HandoffError("handoff task not found")
            if int(row["version"]) != expected_version:
                raise HandoffError("handoff version conflict")
            if str(row["status"]) not in ASSIGNED_STATUSES:
                raise HandoffError("only active claimed tasks can be reassigned")
            target_queue = row
            if queue_key:
                target_queue = conn.execute(
                    """
                    SELECT * FROM handoff_queues
                    WHERE tenant_id=? AND queue_key=? AND status='active'
                    """,
                    (tenant_id, queue_key),
                ).fetchone()
                if target_queue is None:
                    raise HandoffError("target handoff queue not found or inactive")
            target_queue_id = str(target_queue["id"] if queue_key else row["queue_id"])
            try:
                self.staffing.require_eligible(
                    conn,
                    tenant_id=tenant_id,
                    queue_id=target_queue_id,
                    operator_id=assigned_to,
                    exclude_handoff_id=handoff_id,
                )
            except StaffingError as exc:
                raise HandoffError(str(exc)) from exc
            next_version = expected_version + 1
            now = utc_now()
            cursor = conn.execute(
                """
                UPDATE handoff_tasks SET assigned_to=?, queue_id=?, version=?, updated_at=?
                WHERE id=? AND tenant_id=? AND version=?
                """,
                (
                    assigned_to,
                    target_queue_id,
                    next_version,
                    now,
                    handoff_id,
                    tenant_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise HandoffError("handoff version conflict")
            self._insert_event(
                conn,
                tenant_id=tenant_id,
                handoff_id=handoff_id,
                event_type="reassigned",
                from_status=str(row["status"]),
                to_status=str(row["status"]),
                from_queue_id=str(row["queue_id"]),
                to_queue_id=target_queue_id,
                from_assignee=row["assigned_to"],
                to_assignee=assigned_to,
                task_version=next_version,
                actor=actor,
                note=clean_note,
            )
            updated = self._task_row(conn, tenant_id, handoff_id)
        self.db.audit(
            "handoff.reassigned",
            actor,
            handoff_id,
            {"assigned_to": assigned_to, "queue_id": target_queue_id, "note": clean_note},
            tenant_id,
        )
        return self._view(dict(updated))

    def escalate(
        self,
        *,
        tenant_id: str,
        handoff_id: str,
        expected_version: int,
        actor: str,
        note: str,
        queue_key: str | None = None,
    ) -> HandoffView:
        return self._escalate_task(
            tenant_id=tenant_id,
            handoff_id=handoff_id,
            expected_version=expected_version,
            actor=actor,
            note=self._require_note(note),
            queue_key=queue_key,
            target_level=None,
        )

    def add_note(
        self,
        *,
        tenant_id: str,
        handoff_id: str,
        expected_version: int,
        actor: str,
        note: str,
    ) -> HandoffView:
        clean_note = self._require_note(note)
        with self.db._write_lock, self.db.connect() as conn:
            row = self._task_row(conn, tenant_id, handoff_id)
            if row is None:
                raise HandoffError("handoff task not found")
            if int(row["version"]) != expected_version:
                raise HandoffError("handoff version conflict")
            next_version = expected_version + 1
            cursor = conn.execute(
                """
                UPDATE handoff_tasks SET version=?, updated_at=?
                WHERE id=? AND tenant_id=? AND version=?
                """,
                (next_version, utc_now(), handoff_id, tenant_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise HandoffError("handoff version conflict")
            self._insert_event(
                conn,
                tenant_id=tenant_id,
                handoff_id=handoff_id,
                event_type="note_added",
                from_status=str(row["status"]),
                to_status=str(row["status"]),
                from_queue_id=str(row["queue_id"]),
                to_queue_id=str(row["queue_id"]),
                from_assignee=row["assigned_to"],
                to_assignee=row["assigned_to"],
                task_version=next_version,
                actor=actor,
                note=clean_note,
            )
            updated = self._task_row(conn, tenant_id, handoff_id)
        self.db.audit(
            "handoff.note_added", actor, handoff_id, {"note": clean_note}, tenant_id
        )
        return self._view(dict(updated))

    def history(self, *, tenant_id: str, handoff_id: str) -> list[HandoffEventView]:
        self.get(tenant_id=tenant_id, handoff_id=handoff_id)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT e.*, fq.queue_key AS from_queue_key, tq.queue_key AS to_queue_key
                FROM handoff_task_events e
                LEFT JOIN handoff_queues fq ON fq.id=e.from_queue_id
                LEFT JOIN handoff_queues tq ON tq.id=e.to_queue_id
                WHERE e.tenant_id=? AND e.handoff_id=?
                ORDER BY e.task_version, e.created_at
                """,
                (tenant_id, handoff_id),
            ).fetchall()
        return [
            HandoffEventView(
                id=row["id"],
                handoff_id=row["handoff_id"],
                event_type=row["event_type"],
                from_status=row["from_status"],
                to_status=row["to_status"],
                from_queue_key=row["from_queue_key"],
                to_queue_key=row["to_queue_key"],
                from_assignee=row["from_assignee"],
                to_assignee=row["to_assignee"],
                task_version=row["task_version"],
                actor=row["actor"],
                note=row["note_redacted"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def escalate_due(
        self, *, tenant_id: str | None = None, scope: str = "operational"
    ) -> dict[str, Any]:
        conditions = [
            "h.status IN ('proposed','accepted','working','input_required','review')",
            session_scope_condition(scope),
        ]
        params: list[Any] = []
        if tenant_id:
            conditions.append("h.tenant_id=?")
            params.append(tenant_id)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT h.* FROM handoff_tasks h JOIN sessions s ON s.id=h.session_id
                WHERE {' AND '.join(conditions)}
                """,
                tuple(params),
            ).fetchall()
        now = datetime.now(UTC)
        escalated = 0
        skipped = 0
        conflicts = 0
        for item in rows:
            row = dict(item)
            level = int(row["escalation_level"])
            target_level = level
            first_deadline = self._parse_dt(row.get("sla_first_response_at"))
            resolution_deadline = self._parse_dt(row.get("sla_resolution_at"))
            if row.get("acknowledged_at") is None and first_deadline and now > first_deadline:
                target_level = max(target_level, 1)
            if resolution_deadline and now > resolution_deadline:
                target_level = 2
            if target_level <= level:
                skipped += 1
                continue
            note = (
                "resolution SLA breached; automatic level-2 escalation"
                if target_level == 2
                else "first-response SLA breached; automatic level-1 escalation"
            )
            try:
                self._escalate_task(
                    tenant_id=str(row["tenant_id"]),
                    handoff_id=str(row["id"]),
                    expected_version=int(row["version"]),
                    actor="handoff-sla-worker",
                    note=note,
                    queue_key=None,
                    target_level=target_level,
                )
                escalated += 1
            except HandoffError:
                conflicts += 1
        return {
            "evaluated": len(rows),
            "escalated": escalated,
            "skipped": skipped,
            "conflicts": conflicts,
            "run_at": utc_now(),
        }

    def summary(self, *, tenant_id: str, scope: str = "operational") -> dict[str, Any]:
        self.ensure_default_queues(tenant_id)
        scope_condition = session_scope_condition(scope)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT h.*, s.external_session_id, s.source_type, s.source_reference,
                       q.queue_key, q.name AS queue_name
                FROM handoff_tasks h
                JOIN sessions s ON s.id=h.session_id
                JOIN handoff_queues q ON q.id=h.queue_id
                WHERE h.tenant_id=? AND {scope_condition}
                """,
                (tenant_id,),
            ).fetchall()
        tasks = [self._view(dict(row)) for row in rows]
        open_tasks = [task for task in tasks if task.status in OPEN_STATUSES]
        completed = [task for task in tasks if task.status == "completed"]
        response_seconds: list[float] = []
        resolution_seconds: list[float] = []
        for task in tasks:
            created = self._parse_dt(task.created_at)
            acknowledged = self._parse_dt(task.acknowledged_at)
            finished = self._parse_dt(task.completed_at)
            if created and acknowledged:
                response_seconds.append((acknowledged - created).total_seconds())
            if created and finished and task.status == "completed":
                resolution_seconds.append((finished - created).total_seconds())
        operators = self.staffing.list(tenant_id=tenant_id)
        return {
            "total": len(tasks),
            "open": len(open_tasks),
            "unassigned": sum(task.status == "proposed" for task in open_tasks),
            "breached": sum(task.sla_status == "breached" for task in open_tasks),
            "due_soon": sum(task.sla_status == "due_soon" for task in open_tasks),
            "escalated": sum(task.escalation_level > 0 for task in open_tasks),
            "completed": len(completed),
            "average_first_response_seconds": (
                round(sum(response_seconds) / len(response_seconds), 2)
                if response_seconds
                else None
            ),
            "average_resolution_seconds": (
                round(sum(resolution_seconds) / len(resolution_seconds), 2)
                if resolution_seconds
                else None
            ),
            "operators": {
                "total": len(operators),
                "active": sum(item.status == "active" for item in operators),
                "available": sum(
                    item.status == "active"
                    and item.effective_presence == "available"
                    and item.available_for_claim
                    for item in operators
                ),
                "away": sum(
                    item.status == "active" and item.effective_presence == "away"
                    for item in operators
                ),
                "offline": sum(
                    item.effective_presence == "offline" for item in operators
                ),
            },
            "queues": [queue.model_dump() for queue in self.list_queues(tenant_id=tenant_id)],
        }

    def _escalate_task(
        self,
        *,
        tenant_id: str,
        handoff_id: str,
        expected_version: int,
        actor: str,
        note: str,
        queue_key: str | None,
        target_level: int | None,
    ) -> HandoffView:
        with self.db._write_lock, self.db.connect() as conn:
            row = self._task_row(conn, tenant_id, handoff_id)
            if row is None:
                raise HandoffError("handoff task not found")
            if int(row["version"]) != expected_version:
                raise HandoffError("handoff version conflict")
            if str(row["status"]) not in OPEN_STATUSES:
                raise HandoffError("terminal handoff tasks cannot be escalated")
            level = target_level or min(2, int(row["escalation_level"]) + 1)
            if level <= int(row["escalation_level"]):
                raise HandoffError("handoff task is already escalated at this level")
            target_queue_id = str(row["queue_id"])
            if queue_key:
                target = conn.execute(
                    """
                    SELECT id FROM handoff_queues
                    WHERE tenant_id=? AND queue_key=? AND status='active'
                    """,
                    (tenant_id, queue_key),
                ).fetchone()
                if target is None:
                    raise HandoffError("target handoff queue not found or inactive")
                target_queue_id = str(target["id"])
            elif row["escalation_queue_id"]:
                target = conn.execute(
                    """
                    SELECT id FROM handoff_queues
                    WHERE tenant_id=? AND id=? AND status='active'
                    """,
                    (tenant_id, row["escalation_queue_id"]),
                ).fetchone()
                if target is not None:
                    target_queue_id = str(target["id"])
            priority = "urgent" if level >= 2 else self._higher_priority(
                str(row["priority"]), "high"
            )
            next_version = expected_version + 1
            now = utc_now()
            cursor = conn.execute(
                """
                UPDATE handoff_tasks
                SET queue_id=?, priority=?, escalation_level=?, escalated_at=?,
                    escalation_reason=?, version=?, updated_at=?
                WHERE id=? AND tenant_id=? AND version=?
                """,
                (
                    target_queue_id,
                    priority,
                    level,
                    now,
                    note,
                    next_version,
                    now,
                    handoff_id,
                    tenant_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise HandoffError("handoff version conflict")
            self._insert_event(
                conn,
                tenant_id=tenant_id,
                handoff_id=handoff_id,
                event_type="escalated",
                from_status=str(row["status"]),
                to_status=str(row["status"]),
                from_queue_id=str(row["queue_id"]),
                to_queue_id=target_queue_id,
                from_assignee=row["assigned_to"],
                to_assignee=row["assigned_to"],
                task_version=next_version,
                actor=actor,
                note=note,
            )
            updated = self._task_row(conn, tenant_id, handoff_id)
        self.db.audit(
            "handoff.escalated",
            actor,
            handoff_id,
            {"level": level, "queue_id": target_queue_id, "note": note},
            tenant_id,
        )
        return self._view(dict(updated))

    @staticmethod
    def _task_row(conn: Any, tenant_id: str, handoff_id: str) -> Any:
        return conn.execute(
            """
            SELECT h.*, s.external_session_id, q.queue_key, q.name AS queue_name,
                   s.source_type, s.source_reference, q.escalation_queue_id,
                   p.display_name AS assigned_operator_name,
                   p.presence AS assigned_operator_presence,
                   p.status AS operator_status,
                   p.presence_expires_at AS operator_presence_expires_at,
                   c.status AS operator_credential_status
            FROM handoff_tasks h
            JOIN sessions s ON s.id=h.session_id
            JOIN handoff_queues q ON q.id=h.queue_id
            LEFT JOIN handoff_operator_profiles p
              ON p.tenant_id=h.tenant_id AND p.admin_id=h.assigned_to
            LEFT JOIN api_clients c
              ON c.tenant_id=p.tenant_id AND c.id=p.admin_id
            WHERE h.id=? AND h.tenant_id=?
            """,
            (handoff_id, tenant_id),
        ).fetchone()

    @staticmethod
    def _queue_row(conn: Any, tenant_id: str, queue_id: str) -> Any:
        return conn.execute(
            """
            SELECT q.*, eq.queue_key AS escalation_queue_key,
                   0 AS open_tasks, 0 AS assigned_tasks
            FROM handoff_queues q
            LEFT JOIN handoff_queues eq ON eq.id=q.escalation_queue_id
            WHERE q.tenant_id=? AND q.id=?
            """,
            (tenant_id, queue_id),
        ).fetchone()

    def _select_queue(
        self,
        conn: Any,
        tenant_id: str,
        reason: str,
        payload: dict[str, Any],
        queue_key: str | None,
    ) -> Any:
        if queue_key:
            row = conn.execute(
                """
                SELECT * FROM handoff_queues
                WHERE tenant_id=? AND queue_key=? AND status='active'
                """,
                (tenant_id, queue_key),
            ).fetchone()
            if row is None:
                raise HandoffError("handoff queue not found or inactive")
            return row
        queues = conn.execute(
            """
            SELECT * FROM handoff_queues
            WHERE tenant_id=? AND status='active'
            ORDER BY routing_order, queue_key
            """,
            (tenant_id,),
        ).fetchall()
        intent = str(payload.get("intent", "")).strip().lower()
        risk = str(payload.get("risk_level", "")).strip().lower()
        normalized_reason = reason.strip().lower()
        catchall = None
        for row in queues:
            reasons = self._load_list(row["match_reasons_json"])
            intents = self._load_list(row["match_intents_json"])
            risks = self._load_list(row["match_risk_levels_json"])
            if not reasons and not intents and not risks:
                catchall = catchall or row
                continue
            if (
                any(self._route_match(normalized_reason, pattern) for pattern in reasons)
                or intent in intents
                or risk in risks
            ):
                return row
        if catchall is not None:
            return catchall
        raise HandoffError("no active handoff queue can accept this task")

    @staticmethod
    def _insert_event(
        conn: Any,
        *,
        tenant_id: str,
        handoff_id: str,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        from_queue_id: str | None,
        to_queue_id: str | None,
        from_assignee: str | None,
        to_assignee: str | None,
        task_version: int,
        actor: str,
        note: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO handoff_task_events(
                id, tenant_id, handoff_id, event_type, from_status, to_status,
                from_queue_id, to_queue_id, from_assignee, to_assignee,
                task_version, actor, note_redacted, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"event-{uuid.uuid4().hex}",
                tenant_id,
                handoff_id,
                event_type,
                from_status,
                to_status,
                from_queue_id,
                to_queue_id,
                from_assignee,
                to_assignee,
                task_version,
                actor,
                note,
                utc_now(),
            ),
        )

    def _view(self, row: dict[str, Any]) -> HandoffView:
        sla = self._sla(row)
        return HandoffView(
            id=row["id"],
            tenant_id=row["tenant_id"],
            external_session_id=row.get("external_session_id")
            or self.db.external_session_id(row["session_id"]),
            source_type=row.get("source_type") or "api",
            source_reference=row.get("source_reference"),
            message_id=row["message_id"],
            status=row["status"],
            reason=row["reason"],
            payload=self._load_object(row.get("payload_json")),
            acceptance_criteria=row["acceptance_criteria"],
            queue_id=row["queue_id"],
            queue_key=row.get("queue_key") or "unknown",
            queue_name=row.get("queue_name") or "Unknown queue",
            priority=row["priority"],
            assigned_to=row["assigned_to"],
            assigned_operator_name=row.get("assigned_operator_name"),
            assigned_operator_presence=(
                self.staffing.effective_presence(
                    {
                        "presence": row.get("assigned_operator_presence"),
                        "status": row.get("operator_status"),
                        "presence_expires_at": row.get("operator_presence_expires_at"),
                        "credential_status": row.get("operator_credential_status"),
                    }
                )
                if row.get("assigned_to") and row.get("assigned_operator_name")
                else None
            ),
            deadline_at=row["deadline_at"],
            sla_first_response_at=row.get("sla_first_response_at"),
            sla_resolution_at=row.get("sla_resolution_at"),
            sla_status=sla["sla_status"],
            sla_remaining_seconds=sla["sla_remaining_seconds"],
            first_response_breached=sla["first_response_breached"],
            resolution_breached=sla["resolution_breached"],
            acknowledged_at=row.get("acknowledged_at"),
            started_at=row.get("started_at"),
            review_started_at=row.get("review_started_at"),
            escalated_at=row.get("escalated_at"),
            escalation_level=row.get("escalation_level", 0),
            escalation_reason=row.get("escalation_reason"),
            max_retries=row["max_retries"],
            retry_count=row["retry_count"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    def _queue_view(
        self,
        row: dict[str, Any],
        *,
        breached_tasks: int = 0,
        total_operators: int = 0,
        available_operators: int = 0,
    ) -> HandoffQueueView:
        return HandoffQueueView(
            id=row["id"],
            tenant_id=row["tenant_id"],
            queue_key=row["queue_key"],
            name=row["name"],
            description=row["description"],
            status=row["status"],
            default_priority=row["default_priority"],
            first_response_sla_minutes=row["first_response_sla_minutes"],
            resolution_sla_minutes=row["resolution_sla_minutes"],
            max_active_per_operator=row["max_active_per_operator"],
            escalation_queue_key=row.get("escalation_queue_key"),
            match_reasons=self._load_list(row["match_reasons_json"]),
            match_intents=self._load_list(row["match_intents_json"]),
            match_risk_levels=self._load_list(row["match_risk_levels_json"]),
            routing_order=row["routing_order"],
            record_version=row["record_version"],
            open_tasks=int(row.get("open_tasks") or 0),
            assigned_tasks=int(row.get("assigned_tasks") or 0),
            breached_tasks=breached_tasks,
            total_operators=total_operators,
            available_operators=available_operators,
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @classmethod
    def _sla(cls, row: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC)
        status = str(row["status"])
        terminal_at = cls._parse_dt(row.get("completed_at"))
        acknowledged_at = cls._parse_dt(row.get("acknowledged_at"))
        first_deadline = cls._parse_dt(row.get("sla_first_response_at"))
        resolution_deadline = cls._parse_dt(row.get("sla_resolution_at"))
        response_end = acknowledged_at or terminal_at or now
        resolution_end = terminal_at or now
        first_breached = bool(first_deadline and response_end > first_deadline)
        resolution_breached = bool(resolution_deadline and resolution_end > resolution_deadline)
        if status in TERMINAL_STATUSES:
            sla_status = "breached" if first_breached or resolution_breached else "met"
            remaining = None
        else:
            active_deadline = first_deadline if acknowledged_at is None else resolution_deadline
            remaining = int((active_deadline - now).total_seconds()) if active_deadline else None
            created_at = cls._parse_dt(row.get("created_at"))
            sla_window_seconds = (
                (active_deadline - created_at).total_seconds()
                if active_deadline and created_at
                else 4500
            )
            due_soon_threshold = max(60, min(900, int(sla_window_seconds * 0.2)))
            if first_breached or resolution_breached:
                sla_status = "breached"
            elif remaining is not None and remaining <= due_soon_threshold:
                sla_status = "due_soon"
            else:
                sla_status = "on_track"
        return {
            "sla_status": sla_status,
            "sla_remaining_seconds": remaining,
            "first_response_breached": first_breached,
            "resolution_breached": resolution_breached,
        }

    @staticmethod
    def _parse_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _route_match(value: str, pattern: str) -> bool:
        normalized = pattern.strip().lower()
        if normalized.endswith("*"):
            return value.startswith(normalized[:-1])
        return value == normalized

    @staticmethod
    def _routing_values(values: list[str]) -> list[str]:
        normalized = sorted({value.strip().lower() for value in values if value.strip()})
        if any(len(value) > 64 or not ROUTING_TOKEN.fullmatch(value) for value in normalized):
            raise HandoffError("invalid handoff routing token")
        return normalized

    @staticmethod
    def _higher_priority(current: str, requested: str) -> str:
        return requested if PRIORITY_RANK[requested] > PRIORITY_RANK[current] else current

    @staticmethod
    def _clean_note(note: str | None) -> str | None:
        if note is None or not note.strip():
            return None
        clean, _ = redact_sensitive(note.strip())
        return clean[:1000]

    @classmethod
    def _require_note(cls, note: str) -> str:
        clean = cls._clean_note(note)
        if not clean:
            raise HandoffError("handoff operation note is required")
        return clean

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _load_list(value: str | None) -> list[str]:
        try:
            parsed = json.loads(value or "[]")
        except json.JSONDecodeError:
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []

    @staticmethod
    def _load_object(value: str | None) -> dict[str, Any]:
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
