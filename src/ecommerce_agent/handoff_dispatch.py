from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .database import Database, session_scope_condition, utc_now
from .handoff import HandoffError, HandoffService
from .handoff_staffing import HandoffStaffingService
from .schemas import (
    HandoffDispatchAlertAction,
    HandoffDispatchAlertView,
    HandoffDispatchJobView,
    HandoffDispatchRetryRequest,
)
from .text_utils import redact_sensitive


class DispatchError(ValueError):
    pass


WORKER_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
JOB_STATUSES = {"pending", "leased", "waiting", "assigned", "cancelled", "failed"}
ALERT_STATUSES = {"open", "acknowledged", "resolved"}


class HandoffDispatchService:
    def __init__(
        self,
        db: Database,
        handoffs: HandoffService,
        staffing: HandoffStaffingService,
        *,
        lease_seconds: int = 30,
        max_attempts: int = 5,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 300,
    ):
        self.db = db
        self.handoffs = handoffs
        self.staffing = staffing
        self.lease_seconds = max(10, lease_seconds)
        self.max_attempts = max(1, max_attempts)
        self.retry_base_seconds = max(1, retry_base_seconds)
        self.retry_max_seconds = max(self.retry_base_seconds, retry_max_seconds)

    def ensure_pending_jobs(self) -> int:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO handoff_dispatch_jobs(
                    id, tenant_id, handoff_id, queue_id, priority, status,
                    attempt_count, available_at, record_version, created_at, updated_at
                )
                SELECT 'dispatch-' || lower(hex(randomblob(16))), tenant_id, id,
                       queue_id, priority, 'pending', 0, ?, 1, created_at, ?
                FROM handoff_tasks
                WHERE status='proposed' AND assigned_to IS NULL AND queue_id IS NOT NULL
                """,
                (now, now),
            )
        return max(0, int(cursor.rowcount))

    @staticmethod
    def enqueue_in_transaction(
        conn: Any,
        *,
        tenant_id: str,
        handoff_id: str,
        queue_id: str,
        priority: str,
        created_at: str,
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO handoff_dispatch_jobs(
                id, tenant_id, handoff_id, queue_id, priority, status,
                attempt_count, available_at, record_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, 1, ?, ?)
            """,
            (
                f"dispatch-{uuid.uuid4().hex}",
                tenant_id,
                handoff_id,
                queue_id,
                priority,
                created_at,
                created_at,
                created_at,
            ),
        )

    @staticmethod
    def mark_assigned_in_transaction(
        conn: Any,
        *,
        tenant_id: str,
        handoff_id: str,
        assigned_to: str,
        now: str,
    ) -> None:
        conn.execute(
            """
            UPDATE handoff_dispatch_jobs
            SET status='assigned', assigned_to=?, lease_owner=NULL,
                lease_expires_at=NULL, last_error=NULL,
                record_version=record_version+1, updated_at=?, completed_at=?
            WHERE tenant_id=? AND handoff_id=? AND status<>'assigned'
            """,
            (assigned_to, now, now, tenant_id, handoff_id),
        )
        conn.execute(
            """
            UPDATE handoff_dispatch_alerts
            SET status='resolved', resolved_at=?, last_seen_at=?,
                record_version=record_version+1
            WHERE tenant_id=? AND handoff_id=? AND status<>'resolved'
            """,
            (now, now, tenant_id, handoff_id),
        )

    @staticmethod
    def cancel_in_transaction(
        conn: Any, *, tenant_id: str, handoff_id: str, now: str
    ) -> None:
        conn.execute(
            """
            UPDATE handoff_dispatch_jobs
            SET status='cancelled', lease_owner=NULL, lease_expires_at=NULL,
                record_version=record_version+1, updated_at=?, completed_at=?
            WHERE tenant_id=? AND handoff_id=?
              AND status NOT IN ('assigned','cancelled')
            """,
            (now, now, tenant_id, handoff_id),
        )
        conn.execute(
            """
            UPDATE handoff_dispatch_alerts
            SET status='resolved', resolved_at=?, last_seen_at=?,
                record_version=record_version+1
            WHERE tenant_id=? AND handoff_id=? AND status<>'resolved'
            """,
            (now, now, tenant_id, handoff_id),
        )

    def run_once(
        self,
        *,
        worker_id: str,
        limit: int = 20,
        tenant_id: str | None = None,
        scope: str = "operational",
    ) -> dict[str, Any]:
        if not WORKER_TOKEN.fullmatch(worker_id):
            raise DispatchError("invalid dispatch worker id")
        if limit < 1 or limit > 100:
            raise DispatchError("dispatch batch size must be between 1 and 100")
        session_scope_condition(scope)
        report = {
            "claimed": 0,
            "assigned": 0,
            "waiting": 0,
            "cancelled": 0,
            "failed": 0,
            "run_at": utc_now(),
        }
        for _ in range(limit):
            job = self._claim(worker_id=worker_id, tenant_id=tenant_id, scope=scope)
            if job is None:
                break
            report["claimed"] += 1
            outcome = self._process(job, worker_id)
            report[outcome] += 1
        return report

    def _claim(
        self, *, worker_id: str, tenant_id: str | None, scope: str = "operational"
    ) -> dict[str, Any] | None:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        lease_expires_at = (now_dt + timedelta(seconds=self.lease_seconds)).isoformat()
        tenant_sql = " AND tenant_id=?" if tenant_id else ""
        scope_sql = f"""
            AND EXISTS (
                SELECT 1 FROM handoff_tasks h JOIN sessions s ON s.id=h.session_id
                WHERE h.id=handoff_dispatch_jobs.handoff_id
                  AND {session_scope_condition(scope)}
            )
        """
        params: list[Any] = [worker_id, lease_expires_at, now, now, now]
        if tenant_id:
            params.append(tenant_id)
        params.extend([now, now])
        if tenant_id:
            params.append(tenant_id)
        with self.db._write_lock, self.db.connect() as conn:
            row = conn.execute(
                f"""
                UPDATE handoff_dispatch_jobs
                SET status='leased', lease_owner=?, lease_expires_at=?,
                    attempt_count=attempt_count+1,
                    record_version=record_version+1, updated_at=?
                WHERE id=(
                    SELECT id FROM handoff_dispatch_jobs
                    WHERE available_at<=?
                      AND (status IN ('pending','waiting')
                           OR (status='leased' AND lease_expires_at<=?))
                      {tenant_sql}
                      {scope_sql}
                    ORDER BY CASE priority
                               WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                               WHEN 'normal' THEN 2 ELSE 3 END,
                             created_at, id
                    LIMIT 1
                )
                  AND available_at<=?
                  AND (status IN ('pending','waiting')
                       OR (status='leased' AND lease_expires_at<=?))
                  {tenant_sql}
                  {scope_sql}
                RETURNING *
                """,
                tuple(params),
            ).fetchone()
        return None if row is None else dict(row)

    def _process(self, job: dict[str, Any], worker_id: str) -> str:
        with self.db.connect() as conn:
            task = conn.execute(
                """
                SELECT id, tenant_id, status, assigned_to, version
                FROM handoff_tasks WHERE id=? AND tenant_id=?
                """,
                (job["handoff_id"], job["tenant_id"]),
            ).fetchone()
        if task is None or str(task["status"]) != "proposed":
            if task is not None and task["assigned_to"]:
                self._reconcile_assigned(job, worker_id, str(task["assigned_to"]))
                return "assigned"
            self._cancel(job, worker_id, "handoff is no longer assignable")
            return "cancelled"
        try:
            self.handoffs.auto_assign(
                tenant_id=str(job["tenant_id"]),
                handoff_id=str(job["handoff_id"]),
                expected_version=int(task["version"]),
                actor=worker_id,
                note="automatic dispatch selected an eligible on-shift operator",
                automatic_only=True,
            )
            return "assigned"
        except HandoffError as exc:
            message = str(exc)
            if message == "no available operator is eligible for this queue":
                self._wait(job, worker_id, message, reason="no_available_operator")
                return "waiting"
            with self.db.connect() as conn:
                latest = conn.execute(
                    "SELECT status, assigned_to FROM handoff_tasks WHERE id=? AND tenant_id=?",
                    (job["handoff_id"], job["tenant_id"]),
                ).fetchone()
            if latest is not None and latest["assigned_to"]:
                self._reconcile_assigned(job, worker_id, str(latest["assigned_to"]))
                return "assigned"
            if latest is None or str(latest["status"]) != "proposed":
                self._cancel(job, worker_id, "handoff changed during dispatch")
                return "cancelled"
            self._wait(job, worker_id, message, reason="dispatch_error")
            return "failed" if int(job["attempt_count"]) >= self.max_attempts else "waiting"
        except Exception as exc:
            self._wait(
                job,
                worker_id,
                f"{type(exc).__name__}: {str(exc)[:240]}",
                reason="dispatch_error",
            )
            return "failed" if int(job["attempt_count"]) >= self.max_attempts else "waiting"

    def _wait(
        self,
        job: dict[str, Any],
        worker_id: str,
        error: str,
        *,
        reason: str,
    ) -> None:
        attempts = int(job["attempt_count"])
        terminal = reason == "dispatch_error" and attempts >= self.max_attempts
        delay = min(
            self.retry_max_seconds,
            self.retry_base_seconds * (2 ** max(0, min(attempts - 1, 10))),
        )
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        available_at = (now_dt + timedelta(seconds=delay)).isoformat()
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE handoff_dispatch_jobs
                SET status=?, available_at=?, lease_owner=NULL, lease_expires_at=NULL,
                    last_error=?, record_version=record_version+1, updated_at=?
                WHERE id=? AND tenant_id=? AND status='leased' AND lease_owner=?
                """,
                (
                    "failed" if terminal else "waiting",
                    available_at,
                    error[:500],
                    now,
                    job["id"],
                    job["tenant_id"],
                    worker_id,
                ),
            )
            if cursor.rowcount != 1:
                raise DispatchError("dispatch job lease was lost")
            self._open_alert_in_transaction(
                conn,
                job=job,
                reason=reason,
                detail={"attempt_count": attempts, "last_error": error[:240]},
                now=now,
            )

    def _cancel(self, job: dict[str, Any], worker_id: str, reason: str) -> None:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE handoff_dispatch_jobs
                SET status='cancelled', lease_owner=NULL, lease_expires_at=NULL,
                    last_error=?, record_version=record_version+1,
                    updated_at=?, completed_at=?
                WHERE id=? AND tenant_id=? AND status='leased' AND lease_owner=?
                """,
                (reason[:500], now, now, job["id"], job["tenant_id"], worker_id),
            )
            if cursor.rowcount != 1:
                raise DispatchError("dispatch job lease was lost")
            conn.execute(
                """
                UPDATE handoff_dispatch_alerts
                SET status='resolved', resolved_at=?, last_seen_at=?,
                    record_version=record_version+1
                WHERE tenant_id=? AND handoff_id=? AND status<>'resolved'
                """,
                (now, now, job["tenant_id"], job["handoff_id"]),
            )

    def _reconcile_assigned(
        self, job: dict[str, Any], worker_id: str, assigned_to: str
    ) -> None:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE handoff_dispatch_jobs
                SET status='assigned', assigned_to=?, lease_owner=NULL,
                    lease_expires_at=NULL, last_error=NULL,
                    record_version=record_version+1, updated_at=?, completed_at=?
                WHERE id=? AND tenant_id=? AND status='leased' AND lease_owner=?
                """,
                (
                    assigned_to,
                    now,
                    now,
                    job["id"],
                    job["tenant_id"],
                    worker_id,
                ),
            )
            if cursor.rowcount != 1:
                raise DispatchError("dispatch job lease was lost")
            conn.execute(
                """
                UPDATE handoff_dispatch_alerts
                SET status='resolved', resolved_at=?, last_seen_at=?,
                    record_version=record_version+1
                WHERE tenant_id=? AND handoff_id=? AND status<>'resolved'
                """,
                (now, now, job["tenant_id"], job["handoff_id"]),
            )

    @staticmethod
    def _open_alert_in_transaction(
        conn: Any,
        *,
        job: dict[str, Any],
        reason: str,
        detail: dict[str, Any],
        now: str,
    ) -> None:
        alert = conn.execute(
            """
            SELECT * FROM handoff_dispatch_alerts
            WHERE tenant_id=? AND handoff_id=?
            """,
            (job["tenant_id"], job["handoff_id"]),
        ).fetchone()
        detail_json = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
        if alert is None:
            conn.execute(
                """
                INSERT INTO handoff_dispatch_alerts(
                    id, tenant_id, handoff_id, queue_id, status, reason,
                    occurrence_count, detail_json, first_seen_at, last_seen_at,
                    record_version
                ) VALUES (?, ?, ?, ?, 'open', ?, 1, ?, ?, ?, 1)
                """,
                (
                    f"dispatch-alert-{uuid.uuid4().hex}",
                    job["tenant_id"],
                    job["handoff_id"],
                    job["queue_id"],
                    reason,
                    detail_json,
                    now,
                    now,
                ),
            )
            return
        conn.execute(
            """
            UPDATE handoff_dispatch_alerts
            SET status='open', reason=?, occurrence_count=occurrence_count+1,
                detail_json=?, last_seen_at=?, acknowledged_by=NULL,
                acknowledged_at=NULL, resolved_at=NULL,
                record_version=record_version+1
            WHERE id=?
            """,
            (reason, detail_json, now, alert["id"]),
        )

    def wake_for_operator(self, tenant_id: str, operator_id: str) -> int:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            profile = conn.execute(
                """
                SELECT p.*, c.status AS credential_status
                FROM handoff_operator_profiles p
                JOIN api_clients c ON c.id=p.admin_id AND c.tenant_id=p.tenant_id
                WHERE p.tenant_id=? AND p.admin_id=?
                """,
                (tenant_id, operator_id),
            ).fetchone()
            if profile is None:
                return 0
            profile_data = dict(profile)
            if (
                str(profile_data["dispatch_mode"]) != "automatic"
                or self.staffing.effective_presence(profile_data) != "available"
                or not self.staffing.is_on_shift(conn, profile_data)
            ):
                return 0
            cursor = conn.execute(
                """
                UPDATE handoff_dispatch_jobs
                SET status='pending', available_at=?, lease_owner=NULL,
                    lease_expires_at=NULL, record_version=record_version+1,
                    updated_at=?
                WHERE tenant_id=? AND status='waiting'
                  AND queue_id IN (
                      SELECT queue_id FROM handoff_operator_queue_memberships
                      WHERE tenant_id=? AND operator_profile_id=?
                  )
                """,
                (now, now, tenant_id, tenant_id, profile_data["id"]),
            )
        return max(0, int(cursor.rowcount))

    def list_jobs(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        scope: str = "operational",
        limit: int = 200,
    ) -> list[HandoffDispatchJobView]:
        if status is not None and status not in JOB_STATUSES:
            raise DispatchError("invalid dispatch job status")
        conditions = ["j.tenant_id=?", session_scope_condition(scope)]
        params: list[Any] = [tenant_id]
        if status:
            conditions.append("j.status=?")
            params.append(status)
        params.append(max(1, min(limit, 500)))
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT j.*, q.queue_key, s.source_type, s.source_reference
                FROM handoff_dispatch_jobs j
                JOIN handoff_queues q ON q.id=j.queue_id AND q.tenant_id=j.tenant_id
                JOIN handoff_tasks h ON h.id=j.handoff_id AND h.tenant_id=j.tenant_id
                JOIN sessions s ON s.id=h.session_id
                WHERE {' AND '.join(conditions)}
                ORDER BY CASE j.priority
                           WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                           WHEN 'normal' THEN 2 ELSE 3 END,
                         j.created_at, j.id
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._job_view(dict(row)) for row in rows]

    def list_alerts(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        scope: str = "operational",
        limit: int = 200,
    ) -> list[HandoffDispatchAlertView]:
        if status is not None and status not in ALERT_STATUSES:
            raise DispatchError("invalid dispatch alert status")
        conditions = ["a.tenant_id=?", session_scope_condition(scope)]
        params: list[Any] = [tenant_id]
        if status:
            conditions.append("a.status=?")
            params.append(status)
        params.append(max(1, min(limit, 500)))
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT a.*, q.queue_key, s.source_type, s.source_reference
                FROM handoff_dispatch_alerts a
                JOIN handoff_queues q ON q.id=a.queue_id AND q.tenant_id=a.tenant_id
                JOIN handoff_tasks h ON h.id=a.handoff_id AND h.tenant_id=a.tenant_id
                JOIN sessions s ON s.id=h.session_id
                WHERE {' AND '.join(conditions)}
                ORDER BY CASE a.status WHEN 'open' THEN 0
                         WHEN 'acknowledged' THEN 1 ELSE 2 END,
                         a.last_seen_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._alert_view(dict(row)) for row in rows]

    def acknowledge_alert(
        self,
        *,
        tenant_id: str,
        alert_id: str,
        value: HandoffDispatchAlertAction,
        actor: str,
    ) -> HandoffDispatchAlertView:
        now = utc_now()
        note = redact_sensitive(value.note)[0]
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE handoff_dispatch_alerts
                SET status='acknowledged', acknowledged_by=?, acknowledged_at=?,
                    detail_json=json_set(detail_json, '$.acknowledgement', ?),
                    record_version=record_version+1, last_seen_at=?
                WHERE id=? AND tenant_id=? AND status='open' AND record_version=?
                """,
                (actor, now, note, now, alert_id, tenant_id, value.expected_record_version),
            )
            if cursor.rowcount != 1:
                raise DispatchError("dispatch alert version or status conflict")
        self.db.audit(
            "handoff.dispatch_alert_acknowledged",
            actor,
            alert_id,
            {"record_version": value.expected_record_version + 1},
            tenant_id,
        )
        return self._get_alert(tenant_id, alert_id)

    def retry_job(
        self,
        *,
        tenant_id: str,
        job_id: str,
        value: HandoffDispatchRetryRequest,
        actor: str,
    ) -> HandoffDispatchJobView:
        now = utc_now()
        note = redact_sensitive(value.note)[0]
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE handoff_dispatch_jobs
                SET status='pending', available_at=?, lease_owner=NULL,
                    lease_expires_at=NULL, last_error=NULL,
                    record_version=record_version+1, updated_at=?
                WHERE id=? AND tenant_id=? AND status IN ('waiting','failed')
                  AND record_version=?
                """,
                (now, now, job_id, tenant_id, value.expected_record_version),
            )
            if cursor.rowcount != 1:
                raise DispatchError("dispatch job version or status conflict")
        self.db.audit(
            "handoff.dispatch_job_retried",
            actor,
            job_id,
            {"note": note, "record_version": value.expected_record_version + 1},
            tenant_id,
        )
        return self._get_job(tenant_id, job_id)

    def summary(self, *, tenant_id: str, scope: str = "operational") -> dict[str, Any]:
        scope_condition = session_scope_condition(scope)
        with self.db.connect() as conn:
            counts = {
                str(row["status"]): int(row["total"])
                for row in conn.execute(
                    f"""
                    SELECT j.status, COUNT(*) AS total
                    FROM handoff_dispatch_jobs j
                    JOIN handoff_tasks h ON h.id=j.handoff_id AND h.tenant_id=j.tenant_id
                    JOIN sessions s ON s.id=h.session_id
                    WHERE j.tenant_id=? AND {scope_condition} GROUP BY j.status
                    """,
                    (tenant_id,),
                ).fetchall()
            }
            alert_counts = {
                str(row["status"]): int(row["total"])
                for row in conn.execute(
                    f"""
                    SELECT a.status, COUNT(*) AS total
                    FROM handoff_dispatch_alerts a
                    JOIN handoff_tasks h ON h.id=a.handoff_id AND h.tenant_id=a.tenant_id
                    JOIN sessions s ON s.id=h.session_id
                    WHERE a.tenant_id=? AND {scope_condition} GROUP BY a.status
                    """,
                    (tenant_id,),
                ).fetchall()
            }
            oldest = conn.execute(
                f"""
                SELECT MIN(j.created_at) FROM handoff_dispatch_jobs j
                JOIN handoff_tasks h ON h.id=j.handoff_id AND h.tenant_id=j.tenant_id
                JOIN sessions s ON s.id=h.session_id
                WHERE j.tenant_id=? AND j.status IN ('pending','waiting','leased')
                  AND {scope_condition}
                """,
                (tenant_id,),
            ).fetchone()[0]
        return {"jobs": counts, "alerts": alert_counts, "oldest_pending_at": oldest}

    def _get_job(self, tenant_id: str, job_id: str) -> HandoffDispatchJobView:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT j.*, q.queue_key, s.source_type, s.source_reference
                FROM handoff_dispatch_jobs j
                JOIN handoff_queues q ON q.id=j.queue_id AND q.tenant_id=j.tenant_id
                JOIN handoff_tasks h ON h.id=j.handoff_id AND h.tenant_id=j.tenant_id
                JOIN sessions s ON s.id=h.session_id
                WHERE j.id=? AND j.tenant_id=?
                """,
                (job_id, tenant_id),
            ).fetchone()
        if row is None:
            raise DispatchError("dispatch job not found")
        return self._job_view(dict(row))

    def _get_alert(self, tenant_id: str, alert_id: str) -> HandoffDispatchAlertView:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT a.*, q.queue_key, s.source_type, s.source_reference
                FROM handoff_dispatch_alerts a
                JOIN handoff_queues q ON q.id=a.queue_id AND q.tenant_id=a.tenant_id
                JOIN handoff_tasks h ON h.id=a.handoff_id AND h.tenant_id=a.tenant_id
                JOIN sessions s ON s.id=h.session_id
                WHERE a.id=? AND a.tenant_id=?
                """,
                (alert_id, tenant_id),
            ).fetchone()
        if row is None:
            raise DispatchError("dispatch alert not found")
        return self._alert_view(dict(row))

    @staticmethod
    def _job_view(row: dict[str, Any]) -> HandoffDispatchJobView:
        return HandoffDispatchJobView(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            handoff_id=str(row["handoff_id"]),
            source_type=str(row.get("source_type") or "api"),
            source_reference=row.get("source_reference"),
            queue_key=str(row["queue_key"]),
            priority=str(row["priority"]),
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            available_at=str(row["available_at"]),
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            assigned_to=row["assigned_to"],
            last_error=row["last_error"],
            record_version=int(row["record_version"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _alert_view(row: dict[str, Any]) -> HandoffDispatchAlertView:
        try:
            detail = json.loads(row.get("detail_json") or "{}")
        except json.JSONDecodeError:
            detail = {}
        return HandoffDispatchAlertView(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            handoff_id=str(row["handoff_id"]),
            source_type=str(row.get("source_type") or "api"),
            source_reference=row.get("source_reference"),
            queue_key=str(row["queue_key"]),
            status=str(row["status"]),
            reason=str(row["reason"]),
            occurrence_count=int(row["occurrence_count"]),
            detail=detail if isinstance(detail, dict) else {},
            first_seen_at=str(row["first_seen_at"]),
            last_seen_at=str(row["last_seen_at"]),
            acknowledged_by=row["acknowledged_by"],
            acknowledged_at=row["acknowledged_at"],
            resolved_at=row["resolved_at"],
            record_version=int(row["record_version"]),
        )
