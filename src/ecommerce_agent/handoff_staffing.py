from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .database import Database, session_scope_condition, utc_now
from .schemas import (
    HandoffOperatorPresenceUpdate,
    HandoffOperatorQueueView,
    HandoffOperatorUpsert,
    HandoffOperatorView,
    HandoffOperatorHeartbeat,
    HandoffPresenceSessionStart,
    HandoffPresenceSessionView,
    HandoffRecurringShiftCreate,
    HandoffShiftCancelRequest,
    HandoffShiftCreate,
    HandoffShiftView,
)
from .text_utils import redact_sensitive


class StaffingError(ValueError):
    pass


ACTIVE_TASK_STATUSES = ("accepted", "working", "input_required", "review")
SKILL_TOKEN = re.compile(r"^[A-Za-z0-9_\-:.\u4e00-\u9fff]{1,64}$")


class HandoffStaffingService:
    def __init__(self, db: Database):
        self.db = db
        self._dispatch_waker: Callable[[str, str], None] | None = None

    def set_dispatch_waker(self, waker: Callable[[str, str], None]) -> None:
        self._dispatch_waker = waker

    def ensure_bootstrap_operator(
        self, *, tenant_id: str, operator_id: str, display_name: str
    ) -> HandoffOperatorView:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        expires_at = (now_dt + timedelta(hours=8)).isoformat()
        with self.db._write_lock, self.db.connect() as conn:
            admin = self._active_admin(conn, tenant_id, operator_id)
            if admin is None:
                raise StaffingError("bootstrap operator credential is not active")
            queues = conn.execute(
                """
                SELECT id, queue_key FROM handoff_queues
                WHERE tenant_id=? AND status='active'
                ORDER BY routing_order, queue_key
                """,
                (tenant_id,),
            ).fetchall()
            if not queues:
                raise StaffingError("bootstrap operator requires an active handoff queue")
            profile = conn.execute(
                """
                SELECT * FROM handoff_operator_profiles
                WHERE tenant_id=? AND admin_id=?
                """,
                (tenant_id, operator_id),
            ).fetchone()
            if profile is None:
                profile_id = f"operator-{uuid.uuid4().hex}"
                conn.execute(
                    """
                INSERT INTO handoff_operator_profiles(
                    id, tenant_id, admin_id, display_name, status, presence,
                    dispatch_mode, schedule_mode, max_active_tasks, skills_json,
                    record_version, presence_version,
                    presence_updated_at, presence_expires_at, created_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', 'available', 'automatic',
                          'unrestricted', 20, '[]', 1, 1,
                              ?, ?, 'bootstrap', ?, ?)
                    """,
                    (
                        profile_id,
                        tenant_id,
                        operator_id,
                        display_name.strip(),
                        now,
                        expires_at,
                        now,
                        now,
                    ),
                )
            else:
                profile_id = str(profile["id"])
            for queue in queues:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO handoff_operator_queue_memberships(
                        operator_profile_id, tenant_id, queue_id, skill_level,
                        is_primary, created_at, updated_at
                    ) VALUES (?, ?, ?, 3, ?, ?, ?)
                    """,
                    (
                        profile_id,
                        tenant_id,
                        queue["id"],
                        int(queue["queue_key"] == "general"),
                        now,
                        now,
                    ),
                )
        saved = self.get(tenant_id=tenant_id, operator_id=operator_id)
        if saved is None:
            raise StaffingError("bootstrap operator profile was not created")
        return saved

    def upsert(
        self,
        *,
        tenant_id: str,
        operator_id: str,
        value: HandoffOperatorUpsert,
        actor: str,
    ) -> HandoffOperatorView:
        skills = self._skills(value.skills)
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        effective_presence = value.presence if value.status == "active" else "offline"
        expires_at = self._expiry(
            effective_presence, value.presence_ttl_seconds, now_dt
        )
        with self.db._write_lock, self.db.connect() as conn:
            if self._active_admin(conn, tenant_id, operator_id) is None:
                raise StaffingError("operator requires an active administrator credential")
            existing = conn.execute(
                """
                SELECT * FROM handoff_operator_profiles
                WHERE tenant_id=? AND admin_id=?
                """,
                (tenant_id, operator_id),
            ).fetchone()
            if existing is None and value.expected_record_version != 0:
                raise StaffingError("operator profile version conflict")
            if existing is not None and int(existing["record_version"]) != value.expected_record_version:
                raise StaffingError("operator profile version conflict")
            queues: list[tuple[str, int, bool]] = []
            for assignment in value.queue_assignments:
                queue = conn.execute(
                    """
                    SELECT id FROM handoff_queues
                    WHERE tenant_id=? AND queue_key=? AND status='active'
                    """,
                    (tenant_id, assignment.queue_key),
                ).fetchone()
                if queue is None:
                    raise StaffingError("operator queue is missing or inactive")
                queues.append(
                    (str(queue["id"]), assignment.skill_level, assignment.is_primary)
                )
            if value.status == "inactive" and existing is not None:
                active = self._active_count(conn, tenant_id, operator_id, None, None)
                if active:
                    raise StaffingError("reassign active handoff tasks before disabling operator")
            next_version = 1 if existing is None else int(existing["record_version"]) + 1
            next_presence_version = (
                1 if existing is None else int(existing["presence_version"]) + 1
            )
            profile_id = (
                f"operator-{uuid.uuid4().hex}" if existing is None else str(existing["id"])
            )
            created_at = now if existing is None else str(existing["created_at"])
            created_by = actor if existing is None else str(existing["created_by"])
            conn.execute(
                """
                INSERT INTO handoff_operator_profiles(
                    id, tenant_id, admin_id, display_name, status, presence,
                    dispatch_mode, schedule_mode, max_active_tasks, skills_json,
                    record_version, presence_version, presence_session_id,
                    presence_sequence,
                    presence_updated_at, presence_expires_at, created_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, admin_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    status=excluded.status,
                    presence=excluded.presence,
                    dispatch_mode=excluded.dispatch_mode,
                    schedule_mode=excluded.schedule_mode,
                    max_active_tasks=excluded.max_active_tasks,
                    skills_json=excluded.skills_json,
                    record_version=excluded.record_version,
                    presence_version=excluded.presence_version,
                    presence_session_id=NULL,
                    presence_sequence=0,
                    presence_updated_at=excluded.presence_updated_at,
                    presence_expires_at=excluded.presence_expires_at,
                    updated_at=excluded.updated_at
                """,
                (
                    profile_id,
                    tenant_id,
                    operator_id,
                    value.display_name.strip(),
                    value.status,
                    effective_presence,
                    value.dispatch_mode,
                    value.schedule_mode,
                    value.max_active_tasks,
                    json.dumps(skills, ensure_ascii=False, separators=(",", ":")),
                    next_version,
                    next_presence_version,
                    now,
                    expires_at,
                    created_by,
                    created_at,
                    now,
                ),
            )
            conn.execute(
                "DELETE FROM handoff_operator_queue_memberships WHERE operator_profile_id=?",
                (profile_id,),
            )
            for queue_id, skill_level, is_primary in queues:
                conn.execute(
                    """
                    INSERT INTO handoff_operator_queue_memberships(
                        operator_profile_id, tenant_id, queue_id, skill_level,
                        is_primary, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile_id,
                        tenant_id,
                        queue_id,
                        skill_level,
                        int(is_primary),
                        now,
                        now,
                    ),
                )
        self.db.audit(
            "handoff.operator_configured",
            actor,
            operator_id,
            {
                "status": value.status,
                "presence": effective_presence,
                "dispatch_mode": value.dispatch_mode,
                "schedule_mode": value.schedule_mode,
                "queue_count": len(queues),
                "record_version": next_version,
            },
            tenant_id,
        )
        saved = self.get(tenant_id=tenant_id, operator_id=operator_id)
        if saved is None:
            raise StaffingError("operator profile was not saved")
        self._wake_dispatch(tenant_id, operator_id)
        return saved

    def update_presence(
        self,
        *,
        tenant_id: str,
        operator_id: str,
        value: HandoffOperatorPresenceUpdate,
        actor: str,
    ) -> HandoffOperatorView:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        expires_at = self._expiry(value.presence, value.presence_ttl_seconds, now_dt)
        with self.db._write_lock, self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM handoff_operator_profiles
                WHERE tenant_id=? AND admin_id=?
                """,
                (tenant_id, operator_id),
            ).fetchone()
            if row is None:
                raise StaffingError("operator profile not found")
            if str(row["status"]) != "active":
                raise StaffingError("inactive operator cannot update presence")
            if int(row["record_version"]) != value.expected_record_version:
                raise StaffingError("operator profile version conflict")
            next_version = value.expected_record_version + 1
            next_presence_version = int(row["presence_version"]) + 1
            cursor = conn.execute(
                """
                UPDATE handoff_operator_profiles
                SET presence=?, presence_updated_at=?, presence_expires_at=?,
                    presence_session_id=NULL, presence_sequence=0,
                    presence_version=?, record_version=?, updated_at=?
                WHERE id=? AND tenant_id=? AND record_version=? AND status='active'
                """,
                (
                    value.presence,
                    now,
                    expires_at,
                    next_presence_version,
                    next_version,
                    now,
                    row["id"],
                    tenant_id,
                    value.expected_record_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StaffingError("operator profile version conflict")
        self.db.audit(
            "handoff.operator_presence_changed",
            actor,
            operator_id,
            {"presence": value.presence, "record_version": next_version},
            tenant_id,
        )
        saved = self.get(tenant_id=tenant_id, operator_id=operator_id)
        if saved is None:
            raise StaffingError("operator profile not found")
        self._wake_dispatch(tenant_id, operator_id)
        return saved

    def start_presence_session(
        self,
        *,
        tenant_id: str,
        operator_id: str,
        value: HandoffPresenceSessionStart,
        actor: str,
    ) -> HandoffPresenceSessionView:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        expires_at = self._expiry(value.presence, value.presence_ttl_seconds, now_dt)
        with self.db._write_lock, self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT p.*, c.status AS credential_status
                FROM handoff_operator_profiles p
                JOIN api_clients c ON c.id=p.admin_id AND c.tenant_id=p.tenant_id
                WHERE p.tenant_id=? AND p.admin_id=?
                """,
                (tenant_id, operator_id),
            ).fetchone()
            if row is None:
                raise StaffingError("operator profile not found")
            if str(row["status"]) != "active" or str(row["credential_status"]) != "active":
                raise StaffingError("disabled operator cannot start a presence session")
            if int(row["record_version"]) != value.expected_record_version:
                raise StaffingError("operator profile version conflict")
            next_record_version = int(row["record_version"]) + 1
            next_presence_version = int(row["presence_version"]) + 1
            cursor = conn.execute(
                """
                UPDATE handoff_operator_profiles
                SET presence=?, presence_updated_at=?, presence_expires_at=?,
                    presence_session_id=?, presence_sequence=0,
                    presence_version=?, record_version=?, updated_at=?
                WHERE id=? AND tenant_id=? AND record_version=? AND status='active'
                """,
                (
                    value.presence,
                    now,
                    expires_at,
                    value.session_id,
                    next_presence_version,
                    next_record_version,
                    now,
                    row["id"],
                    tenant_id,
                    value.expected_record_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StaffingError("operator profile version conflict")
        self.db.audit(
            "handoff.operator_presence_session_started",
            actor,
            operator_id,
            {
                "presence": value.presence,
                "presence_version": next_presence_version,
                "record_version": next_record_version,
            },
            tenant_id,
        )
        operator = self.get(tenant_id=tenant_id, operator_id=operator_id)
        if operator is None or expires_at is None:
            raise StaffingError("operator presence session was not saved")
        self._wake_dispatch(tenant_id, operator_id)
        return HandoffPresenceSessionView(
            session_id=value.session_id,
            presence_version=next_presence_version,
            sequence=0,
            presence_expires_at=expires_at,
            operator=operator,
        )

    def heartbeat(
        self,
        *,
        tenant_id: str,
        operator_id: str,
        value: HandoffOperatorHeartbeat,
        actor: str,
    ) -> HandoffPresenceSessionView:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        expires_at = self._expiry(value.presence, value.presence_ttl_seconds, now_dt)
        with self.db._write_lock, self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT p.*, c.status AS credential_status
                FROM handoff_operator_profiles p
                JOIN api_clients c ON c.id=p.admin_id AND c.tenant_id=p.tenant_id
                WHERE p.tenant_id=? AND p.admin_id=?
                """,
                (tenant_id, operator_id),
            ).fetchone()
            if row is None:
                raise StaffingError("operator profile not found")
            if str(row["status"]) != "active" or str(row["credential_status"]) != "active":
                raise StaffingError("disabled operator cannot heartbeat")
            if str(row["presence_session_id"] or "") != value.session_id:
                raise StaffingError("operator presence session mismatch")
            current_sequence = int(row["presence_sequence"])
            current_presence_version = int(row["presence_version"])
            if value.sequence == current_sequence:
                if value.presence != str(row["presence"]):
                    raise StaffingError("heartbeat sequence replay payload differs")
                saved = self._view(conn, dict(row))
                if row["presence_expires_at"] is None:
                    raise StaffingError("operator heartbeat lease is missing")
                return HandoffPresenceSessionView(
                    session_id=value.session_id,
                    presence_version=current_presence_version,
                    sequence=current_sequence,
                    presence_expires_at=str(row["presence_expires_at"]),
                    operator=saved,
                )
            if value.sequence < current_sequence:
                raise StaffingError("heartbeat sequence is stale")
            if value.sequence != current_sequence + 1:
                raise StaffingError("heartbeat sequence must be contiguous")
            if current_presence_version != value.expected_presence_version:
                raise StaffingError("operator presence version conflict")
            next_presence_version = current_presence_version + 1
            cursor = conn.execute(
                """
                UPDATE handoff_operator_profiles
                SET presence=?, presence_updated_at=?, presence_expires_at=?,
                    presence_sequence=?, presence_version=?, updated_at=?
                WHERE id=? AND tenant_id=? AND presence_session_id=?
                  AND presence_sequence=? AND presence_version=? AND status='active'
                """,
                (
                    value.presence,
                    now,
                    expires_at,
                    value.sequence,
                    next_presence_version,
                    now,
                    row["id"],
                    tenant_id,
                    value.session_id,
                    current_sequence,
                    current_presence_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StaffingError("operator heartbeat conflict")
        self.db.audit(
            "handoff.operator_heartbeat",
            actor,
            operator_id,
            {
                "presence": value.presence,
                "sequence": value.sequence,
                "presence_version": next_presence_version,
            },
            tenant_id,
        )
        operator = self.get(tenant_id=tenant_id, operator_id=operator_id)
        if operator is None or expires_at is None:
            raise StaffingError("operator heartbeat was not saved")
        self._wake_dispatch(tenant_id, operator_id)
        return HandoffPresenceSessionView(
            session_id=value.session_id,
            presence_version=next_presence_version,
            sequence=value.sequence,
            presence_expires_at=expires_at,
            operator=operator,
        )

    def create_shift(
        self,
        *,
        tenant_id: str,
        operator_id: str,
        value: HandoffShiftCreate,
        actor: str,
    ) -> HandoffShiftView:
        starts_at = value.starts_at.astimezone(UTC).isoformat()
        ends_at = value.ends_at.astimezone(UTC).isoformat()
        now = utc_now()
        shift_id = f"shift-{uuid.uuid4().hex}"
        with self.db._write_lock, self.db.connect() as conn:
            profile = conn.execute(
                """
                SELECT id FROM handoff_operator_profiles
                WHERE tenant_id=? AND admin_id=?
                """,
                (tenant_id, operator_id),
            ).fetchone()
            if profile is None:
                raise StaffingError("operator profile not found")
            overlap = conn.execute(
                """
                SELECT 1 FROM handoff_operator_shifts
                WHERE tenant_id=? AND operator_profile_id=? AND status='scheduled'
                  AND starts_at < ? AND ends_at > ?
                LIMIT 1
                """,
                (tenant_id, profile["id"], ends_at, starts_at),
            ).fetchone()
            if overlap is not None:
                raise StaffingError("operator shift overlaps an existing shift")
            conn.execute(
                """
                INSERT INTO handoff_operator_shifts(
                    id, tenant_id, operator_profile_id, starts_at, ends_at,
                    status, record_version, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'scheduled', 1, ?, ?, ?)
                """,
                (shift_id, tenant_id, profile["id"], starts_at, ends_at, actor, now, now),
            )
        self.db.audit(
            "handoff.operator_shift_created",
            actor,
            shift_id,
            {"operator_id": operator_id, "starts_at": starts_at, "ends_at": ends_at},
            tenant_id,
        )
        self._wake_dispatch(tenant_id, operator_id)
        return self.get_shift(tenant_id=tenant_id, shift_id=shift_id)

    def create_recurring_shifts(
        self,
        *,
        tenant_id: str,
        operator_id: str,
        value: HandoffRecurringShiftCreate,
        actor: str,
    ) -> list[HandoffShiftView]:
        interval = timedelta(weeks=value.repeat_every_weeks)
        windows = [
            (
                (value.starts_at + interval * index).astimezone(UTC).isoformat(),
                (value.ends_at + interval * index).astimezone(UTC).isoformat(),
            )
            for index in range(value.occurrences)
        ]
        shift_ids = [f"shift-{uuid.uuid4().hex}" for _ in windows]
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            profile = conn.execute(
                """
                SELECT id FROM handoff_operator_profiles
                WHERE tenant_id=? AND admin_id=?
                """,
                (tenant_id, operator_id),
            ).fetchone()
            if profile is None:
                raise StaffingError("operator profile not found")
            for starts_at, ends_at in windows:
                overlap = conn.execute(
                    """
                    SELECT 1 FROM handoff_operator_shifts
                    WHERE tenant_id=? AND operator_profile_id=? AND status='scheduled'
                      AND starts_at < ? AND ends_at > ?
                    LIMIT 1
                    """,
                    (tenant_id, profile["id"], ends_at, starts_at),
                ).fetchone()
                if overlap is not None:
                    raise StaffingError("operator shift overlaps an existing shift")
            conn.executemany(
                """
                INSERT INTO handoff_operator_shifts(
                    id, tenant_id, operator_profile_id, starts_at, ends_at,
                    status, record_version, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'scheduled', 1, ?, ?, ?)
                """,
                [
                    (
                        shift_id,
                        tenant_id,
                        profile["id"],
                        starts_at,
                        ends_at,
                        actor,
                        now,
                        now,
                    )
                    for shift_id, (starts_at, ends_at) in zip(shift_ids, windows)
                ],
            )
        self.db.audit(
            "handoff.operator_recurring_shifts_created",
            actor,
            operator_id,
            {
                "shift_ids": shift_ids,
                "repeat_every_weeks": value.repeat_every_weeks,
                "occurrences": value.occurrences,
                "first_starts_at": windows[0][0],
                "last_ends_at": windows[-1][1],
            },
            tenant_id,
        )
        self._wake_dispatch(tenant_id, operator_id)
        return [
            self.get_shift(tenant_id=tenant_id, shift_id=shift_id)
            for shift_id in shift_ids
        ]

    def cancel_shift(
        self,
        *,
        tenant_id: str,
        operator_id: str,
        shift_id: str,
        value: HandoffShiftCancelRequest,
        actor: str,
    ) -> HandoffShiftView:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT s.* FROM handoff_operator_shifts s
                JOIN handoff_operator_profiles p ON p.id=s.operator_profile_id
                WHERE s.id=? AND s.tenant_id=? AND p.admin_id=?
                """,
                (shift_id, tenant_id, operator_id),
            ).fetchone()
            if row is None:
                raise StaffingError("operator shift not found")
            if str(row["status"]) != "scheduled":
                raise StaffingError("operator shift is already cancelled")
            if int(row["record_version"]) != value.expected_record_version:
                raise StaffingError("operator shift version conflict")
            cursor = conn.execute(
                """
                UPDATE handoff_operator_shifts
                SET status='cancelled', record_version=record_version+1, updated_at=?
                WHERE id=? AND tenant_id=? AND status='scheduled' AND record_version=?
                """,
                (now, shift_id, tenant_id, value.expected_record_version),
            )
            if cursor.rowcount != 1:
                raise StaffingError("operator shift version conflict")
        self.db.audit(
            "handoff.operator_shift_cancelled",
            actor,
            shift_id,
            {"operator_id": operator_id, "note": redact_sensitive(value.note[:200])[0]},
            tenant_id,
        )
        return self.get_shift(tenant_id=tenant_id, shift_id=shift_id)

    def list_shifts(
        self,
        *,
        tenant_id: str,
        operator_id: str,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
    ) -> list[HandoffShiftView]:
        conditions = ["s.tenant_id=?", "p.admin_id=?"]
        params: list[Any] = [tenant_id, operator_id]
        if from_at is not None:
            conditions.append("s.ends_at>?")
            params.append(from_at.astimezone(UTC).isoformat())
        if to_at is not None:
            conditions.append("s.starts_at<?")
            params.append(to_at.astimezone(UTC).isoformat())
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT s.*, p.admin_id AS operator_id
                FROM handoff_operator_shifts s
                JOIN handoff_operator_profiles p ON p.id=s.operator_profile_id
                WHERE {' AND '.join(conditions)}
                ORDER BY s.starts_at, s.id
                """,
                tuple(params),
            ).fetchall()
        return [self._shift_view(dict(row)) for row in rows]

    def get_shift(self, *, tenant_id: str, shift_id: str) -> HandoffShiftView:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT s.*, p.admin_id AS operator_id
                FROM handoff_operator_shifts s
                JOIN handoff_operator_profiles p ON p.id=s.operator_profile_id
                WHERE s.id=? AND s.tenant_id=?
                """,
                (shift_id, tenant_id),
            ).fetchone()
        if row is None:
            raise StaffingError("operator shift not found")
        return self._shift_view(dict(row))

    def list(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        presence: str | None = None,
        queue_key: str | None = None,
    ) -> list[HandoffOperatorView]:
        if status not in {None, "active", "inactive"}:
            raise StaffingError("invalid operator status")
        if presence not in {None, "available", "away", "offline"}:
            raise StaffingError("invalid operator presence")
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT p.*, c.status AS credential_status
                FROM handoff_operator_profiles p
                JOIN api_clients c ON c.id=p.admin_id AND c.tenant_id=p.tenant_id
                WHERE p.tenant_id=?
                ORDER BY p.status, p.display_name, p.admin_id
                """,
                (tenant_id,),
            ).fetchall()
            views = [self._view(conn, dict(row)) for row in rows]
        if status:
            views = [item for item in views if item.status == status]
        if presence:
            views = [item for item in views if item.effective_presence == presence]
        if queue_key:
            views = [
                item
                for item in views
                if any(q.queue_key == queue_key for q in item.queue_assignments)
            ]
        return views

    def get(self, *, tenant_id: str, operator_id: str) -> HandoffOperatorView | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT p.*, c.status AS credential_status
                FROM handoff_operator_profiles p
                JOIN api_clients c ON c.id=p.admin_id AND c.tenant_id=p.tenant_id
                WHERE p.tenant_id=? AND p.admin_id=?
                """,
                (tenant_id, operator_id),
            ).fetchone()
            return None if row is None else self._view(conn, dict(row))

    def require_eligible(
        self,
        conn: Any,
        *,
        tenant_id: str,
        queue_id: str,
        operator_id: str,
        exclude_handoff_id: str | None,
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT p.*, c.status AS credential_status, m.skill_level, m.is_primary,
                   q.max_active_per_operator
            FROM handoff_operator_profiles p
            JOIN api_clients c ON c.id=p.admin_id AND c.tenant_id=p.tenant_id
            JOIN handoff_operator_queue_memberships m
              ON m.operator_profile_id=p.id AND m.tenant_id=p.tenant_id
            JOIN handoff_queues q ON q.id=m.queue_id AND q.tenant_id=m.tenant_id
            WHERE p.tenant_id=? AND p.admin_id=? AND q.id=?
            """,
            (tenant_id, operator_id, queue_id),
        ).fetchone()
        if row is None:
            raise StaffingError("operator is not assigned to this handoff queue")
        if str(row["credential_status"]) != "active" or str(row["status"]) != "active":
            raise StaffingError("operator is disabled")
        if self.effective_presence(dict(row)) != "available":
            raise StaffingError("operator is not currently available")
        if not self.is_on_shift(conn, dict(row)):
            raise StaffingError("operator is outside the configured shift")
        global_active = self._active_count(
            conn, tenant_id, operator_id, None, exclude_handoff_id
        )
        if global_active >= int(row["max_active_tasks"]):
            raise StaffingError("operator global active-task capacity reached")
        queue_active = self._active_count(
            conn, tenant_id, operator_id, queue_id, exclude_handoff_id
        )
        if queue_active >= int(row["max_active_per_operator"]):
            raise StaffingError("operator active-task capacity reached for this queue")
        return dict(row)

    def rank_candidates(
        self,
        conn: Any,
        *,
        tenant_id: str,
        queue_id: str,
        automatic_only: bool = False,
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT p.*, c.status AS credential_status, m.skill_level, m.is_primary,
                   q.max_active_per_operator
            FROM handoff_operator_profiles p
            JOIN api_clients c ON c.id=p.admin_id AND c.tenant_id=p.tenant_id
            JOIN handoff_operator_queue_memberships m
              ON m.operator_profile_id=p.id AND m.tenant_id=p.tenant_id
            JOIN handoff_queues q ON q.id=m.queue_id AND q.tenant_id=m.tenant_id
            WHERE p.tenant_id=? AND q.id=? AND p.status='active'
              AND c.status='active'
            """,
            (tenant_id, queue_id),
        ).fetchall()
        candidates: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            if self.effective_presence(row) != "available":
                continue
            if automatic_only and str(row["dispatch_mode"]) != "automatic":
                continue
            if not self.is_on_shift(conn, row):
                continue
            global_active = self._active_count(
                conn, tenant_id, str(row["admin_id"]), None, None
            )
            queue_active = self._active_count(
                conn, tenant_id, str(row["admin_id"]), queue_id, None
            )
            if global_active >= int(row["max_active_tasks"]):
                continue
            if queue_active >= int(row["max_active_per_operator"]):
                continue
            row["active_tasks"] = global_active
            row["queue_active_tasks"] = queue_active
            row["load_ratio"] = global_active / int(row["max_active_tasks"])
            candidates.append(row)
        candidates.sort(
            key=lambda item: (
                item["load_ratio"],
                -int(item["is_primary"]),
                -int(item["skill_level"]),
                int(item["active_tasks"]),
                str(item["admin_id"]),
            )
        )
        return candidates

    def queue_counts(self, *, tenant_id: str) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT q.id AS queue_id, p.*,
                       c.status AS credential_status
                FROM handoff_operator_queue_memberships m
                JOIN handoff_operator_profiles p ON p.id=m.operator_profile_id
                JOIN api_clients c ON c.id=p.admin_id AND c.tenant_id=p.tenant_id
                JOIN handoff_queues q ON q.id=m.queue_id
                WHERE m.tenant_id=?
                """,
                (tenant_id,),
            ).fetchall()
            for raw in rows:
                row = dict(raw)
                bucket = counts.setdefault(
                    str(row["queue_id"]), {"total": 0, "available": 0}
                )
                if (
                    str(row["status"]) == "active"
                    and str(row["credential_status"]) == "active"
                ):
                    bucket["total"] += 1
                    if (
                        self.effective_presence(row) == "available"
                        and self.is_on_shift(conn, row)
                    ):
                        bucket["available"] += 1
        return counts

    @staticmethod
    def is_on_shift(conn: Any, row: dict[str, Any], at: datetime | None = None) -> bool:
        if str(row.get("schedule_mode") or "unrestricted") == "unrestricted":
            return True
        current = (at or datetime.now(UTC)).astimezone(UTC).isoformat()
        found = conn.execute(
            """
            SELECT 1 FROM handoff_operator_shifts
            WHERE tenant_id=? AND operator_profile_id=? AND status='scheduled'
              AND starts_at<=? AND ends_at>?
            LIMIT 1
            """,
            (row["tenant_id"], row["id"], current, current),
        ).fetchone()
        return found is not None

    @classmethod
    def effective_presence(cls, row: dict[str, Any]) -> str:
        presence = str(row.get("presence") or "offline")
        if str(row.get("status") or "inactive") != "active":
            return "offline"
        if str(row.get("credential_status") or "active") != "active":
            return "offline"
        if presence in {"available", "away"} and cls._is_expired(
            row.get("presence_expires_at"), datetime.now(UTC)
        ):
            return "offline"
        return presence

    def _view(self, conn: Any, row: dict[str, Any]) -> HandoffOperatorView:
        active_tasks = self._active_count(
            conn, str(row["tenant_id"]), str(row["admin_id"]), None, None
        )
        memberships = conn.execute(
            f"""
            SELECT q.queue_key, q.name AS queue_name, m.skill_level, m.is_primary,
                   SUM(CASE WHEN h.status IN ('accepted','working','input_required','review')
                            THEN 1 ELSE 0 END) AS active_tasks
            FROM handoff_operator_queue_memberships m
            JOIN handoff_queues q ON q.id=m.queue_id AND q.tenant_id=m.tenant_id
            LEFT JOIN handoff_tasks h ON h.tenant_id=m.tenant_id
              AND h.queue_id=m.queue_id AND h.assigned_to=?
              AND EXISTS (
                SELECT 1 FROM sessions s
                WHERE s.id=h.session_id AND {session_scope_condition('operational')}
              )
            WHERE m.operator_profile_id=? AND m.tenant_id=?
            GROUP BY q.id, q.queue_key, q.name, m.skill_level, m.is_primary
            ORDER BY m.is_primary DESC, m.skill_level DESC, q.routing_order, q.queue_key
            """,
            (row["admin_id"], row["id"], row["tenant_id"]),
        ).fetchall()
        effective = self.effective_presence(row)
        on_shift = self.is_on_shift(conn, row)
        next_shift = conn.execute(
            """
            SELECT starts_at FROM handoff_operator_shifts
            WHERE tenant_id=? AND operator_profile_id=? AND status='scheduled'
              AND starts_at>?
            ORDER BY starts_at LIMIT 1
            """,
            (row["tenant_id"], row["id"], utc_now()),
        ).fetchone()
        maximum = int(row["max_active_tasks"])
        return HandoffOperatorView(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            operator_id=str(row["admin_id"]),
            display_name=str(row["display_name"]),
            status=str(row["status"]),
            configured_presence=str(row["presence"]),
            effective_presence=effective,
            credential_status=str(row["credential_status"]),
            dispatch_mode=str(row["dispatch_mode"]),
            schedule_mode=str(row["schedule_mode"]),
            on_shift=on_shift,
            next_shift_start=(str(next_shift["starts_at"]) if next_shift else None),
            max_active_tasks=maximum,
            active_tasks=active_tasks,
            load_ratio=round(active_tasks / maximum, 4),
            available_for_claim=(
                str(row["status"]) == "active"
                and str(row["credential_status"]) == "active"
                and effective == "available"
                and on_shift
                and active_tasks < maximum
            ),
            skills=self._load_list(row["skills_json"]),
            queue_assignments=[
                HandoffOperatorQueueView(
                    queue_key=str(item["queue_key"]),
                    queue_name=str(item["queue_name"]),
                    skill_level=int(item["skill_level"]),
                    is_primary=bool(item["is_primary"]),
                    active_tasks=int(item["active_tasks"] or 0),
                )
                for item in memberships
            ],
            record_version=int(row["record_version"]),
            presence_version=int(row["presence_version"]),
            presence_updated_at=str(row["presence_updated_at"]),
            presence_expires_at=row["presence_expires_at"],
            created_by=str(row["created_by"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _shift_view(row: dict[str, Any]) -> HandoffShiftView:
        return HandoffShiftView(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            operator_id=str(row["operator_id"]),
            starts_at=str(row["starts_at"]),
            ends_at=str(row["ends_at"]),
            status=str(row["status"]),
            record_version=int(row["record_version"]),
            created_by=str(row["created_by"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _wake_dispatch(self, tenant_id: str, operator_id: str) -> None:
        if self._dispatch_waker is not None:
            self._dispatch_waker(tenant_id, operator_id)

    @staticmethod
    def _active_admin(conn: Any, tenant_id: str, operator_id: str) -> Any:
        return conn.execute(
            """
            SELECT id, name FROM api_clients
            WHERE id=? AND tenant_id=? AND role='admin' AND status='active'
            """,
            (operator_id, tenant_id),
        ).fetchone()

    @staticmethod
    def _active_count(
        conn: Any,
        tenant_id: str,
        operator_id: str,
        queue_id: str | None,
        exclude_handoff_id: str | None,
    ) -> int:
        sql = f"""
            SELECT COUNT(*) FROM handoff_tasks h JOIN sessions s ON s.id=h.session_id
            WHERE h.tenant_id=? AND h.assigned_to=?
              AND h.status IN ('accepted','working','input_required','review')
              AND {session_scope_condition('operational')}
        """
        params: list[Any] = [tenant_id, operator_id]
        if queue_id is not None:
            sql += " AND h.queue_id=?"
            params.append(queue_id)
        if exclude_handoff_id is not None:
            sql += " AND h.id<>?"
            params.append(exclude_handoff_id)
        return int(conn.execute(sql, tuple(params)).fetchone()[0])

    @staticmethod
    def _expiry(presence: str, ttl_seconds: int, now: datetime) -> str | None:
        if presence == "offline":
            return None
        return (now + timedelta(seconds=ttl_seconds)).isoformat()

    @staticmethod
    def _is_expired(value: Any, now: datetime) -> bool:
        if not value:
            return True
        try:
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed <= now
        except ValueError:
            return True

    @staticmethod
    def _skills(values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            token = value.strip()
            if not SKILL_TOKEN.fullmatch(token):
                raise StaffingError("invalid operator skill token")
            if token not in cleaned:
                cleaned.append(token)
        return cleaned

    @staticmethod
    def _load_list(value: str | None) -> list[str]:
        try:
            loaded = json.loads(value or "[]")
        except json.JSONDecodeError:
            return []
        return [str(item) for item in loaded] if isinstance(loaded, list) else []
