from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .database import Database, utc_now
from .text_utils import redact_sensitive


class OutboxError(ValueError):
    pass


class OutboxReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: Literal["confirmed", "not_delivered", "rejected"]
    expected_record_version: int = Field(ge=1)
    note: str = Field(min_length=8, max_length=500)


class DurableOutbox:
    def __init__(
        self,
        db: Database,
        *,
        lease_seconds: int,
        max_attempts: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
        platform: str | None = None,
    ):
        self.db = db
        self.lease_seconds = max(5, lease_seconds)
        self.max_attempts = max(1, max_attempts)
        self.retry_base_seconds = max(0, retry_base_seconds)
        self.retry_max_seconds = max(self.retry_base_seconds, retry_max_seconds)
        # A platform-scoped outbox only claims items whose outbound event
        # belongs to that platform, so channel workers never steal each
        # other's deliveries.
        self.platform = platform

    def enqueue(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        event_id: str,
        idempotency_key: str,
        content_redacted: str,
        payload_ciphertext: str,
        actor: str,
        allow_bot: bool,
    ) -> dict[str, Any]:
        outbox_id = f"outbox-{uuid.uuid4().hex}"
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            inserted = False
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO channel_outbox(
                        id, tenant_id, conversation_id, event_id, source_event_id,
                        idempotency_key,
                        content_redacted, status, attempt_count, platform_result_json,
                        last_error, created_at, updated_at, delivery_state,
                        payload_ciphertext, actor, allow_bot, max_attempts,
                        next_attempt_at, lease_owner, lease_until, dispatch_started_at,
                        last_attempt_at, dead_letter_at, reconciled_at, reconciled_by,
                        reconciliation_note, error_kind, record_version
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, 'queued', 0, NULL, NULL, ?, ?, 'queued',
                              ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                              NULL, NULL, 1)
                    """,
                    (
                        outbox_id,
                        tenant_id,
                        conversation_id,
                        event_id,
                        idempotency_key,
                        content_redacted,
                        now,
                        now,
                        payload_ciphertext,
                        actor,
                        int(allow_bot),
                        self.max_attempts,
                        now,
                    ),
                )
                inserted = cursor.rowcount == 1
            except sqlite3.IntegrityError:
                pass
            if inserted:
                source_event = conn.execute(
                    "SELECT platform, shop_id, message_type FROM channel_events "
                    "WHERE id=? AND tenant_id=? AND conversation_id=?",
                    (event_id, tenant_id, conversation_id),
                ).fetchone()
                if source_event is None:
                    raise OutboxError("outbox source event not found")
                outbound_event_id = f"event-{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO channel_events(
                        id, tenant_id, platform, shop_id, conversation_id,
                        external_event_id, direction, message_type, content_redacted,
                        payload_hash, routing_ciphertext, request_id, action_mode,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'outbound', ?, ?, ?, NULL, NULL, NULL,
                              'queued', ?, ?)
                    """,
                    (
                        outbound_event_id,
                        tenant_id,
                        source_event["platform"],
                        source_event["shop_id"],
                        conversation_id,
                        outbox_id,
                        source_event["message_type"],
                        content_redacted,
                        hashlib.sha256(content_redacted.encode("utf-8")).hexdigest(),
                        now,
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE channel_outbox SET event_id=? WHERE id=?",
                    (outbound_event_id, outbox_id),
                )
            row = conn.execute(
                "SELECT * FROM channel_outbox WHERE tenant_id=? AND idempotency_key=?",
                (tenant_id, idempotency_key),
            ).fetchone()
        if row is None:
            raise OutboxError("outbox enqueue failed")
        if row["conversation_id"] != conversation_id:
            raise OutboxError("outbox idempotency key belongs to another conversation")
        return dict(row)

    def get(self, outbox_id: str, tenant_id: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM channel_outbox WHERE id=?"
        params: tuple[Any, ...] = (outbox_id,)
        if tenant_id is not None:
            query += " AND tenant_id=?"
            params += (tenant_id,)
        with self.db.connect() as conn:
            row = conn.execute(query, params).fetchone()
        return dict(row) if row is not None else None

    def get_by_key(self, tenant_id: str, idempotency_key: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM channel_outbox WHERE tenant_id=? AND idempotency_key=?",
                (tenant_id, idempotency_key),
            ).fetchone()
        return dict(row) if row is not None else None

    def recover_expired_leases(self, *, now: datetime | None = None) -> dict[str, int]:
        current = (now or datetime.now(UTC)).isoformat()
        recovered = 0
        uncertain = 0
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM channel_outbox
                WHERE status='sending' AND lease_until IS NOT NULL AND lease_until<=?
                """,
                (current,),
            ).fetchall()
            for row in rows:
                if row["dispatch_started_at"]:
                    conn.execute(
                        """
                        UPDATE channel_outbox
                        SET status='failed', delivery_state='uncertain',
                            error_kind='worker_lost_after_dispatch',
                            last_error='worker lease expired after platform dispatch started',
                            lease_owner=NULL, lease_until=NULL, updated_at=?,
                            record_version=record_version+1
                        WHERE id=? AND status='sending'
                        """,
                        (current, row["id"]),
                    )
                    self._update_draft_locked(
                        conn,
                        str(row["id"]),
                        status="failed",
                        error="delivery outcome is uncertain after worker interruption",
                        now=current,
                    )
                    self._update_event_locked(conn, str(row["id"]), "failed", current)
                    uncertain += 1
                else:
                    conn.execute(
                        """
                        UPDATE channel_outbox
                        SET status='queued', delivery_state='queued',
                            error_kind='worker_lost_before_dispatch',
                            last_error='worker lease expired before platform dispatch started',
                            attempt_count=CASE WHEN attempt_count>0 THEN attempt_count-1 ELSE 0 END,
                            next_attempt_at=?, lease_owner=NULL, lease_until=NULL,
                            updated_at=?, record_version=record_version+1
                        WHERE id=? AND status='sending'
                        """,
                        (current, current, row["id"]),
                    )
                    self._update_event_locked(conn, str(row["id"]), "queued", current)
                    recovered += 1
        return {"requeued_before_dispatch": recovered, "uncertain_after_dispatch": uncertain}

    def claim_due(
        self,
        worker_id: str,
        *,
        limit: int,
        outbox_id: str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        current_dt = now or datetime.now(UTC)
        current = current_dt.isoformat()
        lease_until = (current_dt + timedelta(seconds=self.lease_seconds)).isoformat()
        claimed: list[dict[str, Any]] = []
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            query = (
                "SELECT * FROM channel_outbox WHERE status='queued' "
                "AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
                "AND (lease_until IS NULL OR lease_until<=?)"
            )
            params: list[Any] = [current, current]
            if self.platform is not None:
                query += (
                    " AND event_id IN (SELECT id FROM channel_events WHERE platform=?)"
                )
                params.append(self.platform)
            if outbox_id is not None:
                query += " AND id=?"
                params.append(outbox_id)
            query += " ORDER BY created_at, id LIMIT ?"
            params.append(max(1, limit))
            rows = conn.execute(query, tuple(params)).fetchall()
            for row in rows:
                cursor = conn.execute(
                    """
                    UPDATE channel_outbox
                    SET status='sending', delivery_state='dispatching',
                        attempt_count=attempt_count+1, lease_owner=?, lease_until=?,
                        dispatch_started_at=NULL, last_attempt_at=?, updated_at=?,
                        error_kind=NULL, last_error=NULL, record_version=record_version+1
                    WHERE id=? AND status='queued' AND record_version=?
                    """,
                    (
                        worker_id,
                        lease_until,
                        current,
                        current,
                        row["id"],
                        row["record_version"],
                    ),
                )
                if cursor.rowcount == 1:
                    saved = conn.execute(
                        "SELECT * FROM channel_outbox WHERE id=?", (row["id"],)
                    ).fetchone()
                    claimed.append(dict(saved))
        return claimed

    def mark_dispatch_started(self, outbox_id: str, worker_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE channel_outbox SET dispatch_started_at=?, updated_at=?,
                    record_version=record_version+1
                WHERE id=? AND status='sending' AND lease_owner=?
                """,
                (now, now, outbox_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise OutboxError("outbox lease was lost before dispatch")
            row = conn.execute("SELECT * FROM channel_outbox WHERE id=?", (outbox_id,)).fetchone()
        return dict(row)

    def mark_confirmed(
        self,
        outbox_id: str,
        worker_id: str,
        platform_result: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE channel_outbox
                SET status='sent', delivery_state='confirmed', platform_result_json=?,
                    last_error=NULL, error_kind=NULL, lease_owner=NULL, lease_until=NULL,
                    next_attempt_at=NULL, updated_at=?, record_version=record_version+1
                WHERE id=? AND status='sending' AND lease_owner=?
                """,
                (json.dumps(platform_result, ensure_ascii=False), now, outbox_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise OutboxError("outbox lease was lost before confirmation")
            self._update_draft_locked(conn, outbox_id, status="sent", error=None, now=now)
            self._update_event_locked(conn, outbox_id, "sent", now)
            row = conn.execute("SELECT * FROM channel_outbox WHERE id=?", (outbox_id,)).fetchone()
        return dict(row)

    def mark_failed(
        self,
        outbox_id: str,
        worker_id: str,
        *,
        kind: Literal["retryable", "rejected", "uncertain", "cancelled"],
        error: str,
        platform_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        with self.db._write_lock, self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM channel_outbox WHERE id=? AND status='sending' AND lease_owner=?",
                (outbox_id, worker_id),
            ).fetchone()
            if row is None:
                raise OutboxError("outbox lease was lost before failure recording")
            result_json = (
                json.dumps(platform_result, ensure_ascii=False) if platform_result is not None else None
            )
            if kind == "retryable" and int(row["attempt_count"]) < int(row["max_attempts"]):
                delay = min(
                    self.retry_max_seconds,
                    self.retry_base_seconds * (2 ** max(0, int(row["attempt_count"]) - 1)),
                )
                next_attempt = (now_dt + timedelta(seconds=delay)).isoformat()
                status = "queued"
                delivery_state = "retry_scheduled"
                dead_letter_at = None
                draft_status = "sending"
            elif kind == "retryable":
                next_attempt = None
                status = "failed"
                delivery_state = "dead_letter"
                dead_letter_at = now
                draft_status = "failed"
            else:
                next_attempt = None
                status = "failed"
                delivery_state = kind
                dead_letter_at = None
                draft_status = "failed"
            conn.execute(
                """
                UPDATE channel_outbox
                SET status=?, delivery_state=?, platform_result_json=COALESCE(?, platform_result_json),
                    last_error=?, error_kind=?, next_attempt_at=?, lease_owner=NULL,
                    lease_until=NULL, dead_letter_at=?, updated_at=?,
                    record_version=record_version+1
                WHERE id=? AND status='sending' AND lease_owner=?
                """,
                (
                    status,
                    delivery_state,
                    result_json,
                    error[:500],
                    kind,
                    next_attempt,
                    dead_letter_at,
                    now,
                    outbox_id,
                    worker_id,
                ),
            )
            self._update_draft_locked(
                conn, outbox_id, status=draft_status, error=error[:500], now=now
            )
            self._update_event_locked(conn, outbox_id, "queued" if status == "queued" else "failed", now)
            saved = conn.execute("SELECT * FROM channel_outbox WHERE id=?", (outbox_id,)).fetchone()
        return dict(saved)

    def reconcile(
        self,
        outbox_id: str,
        tenant_id: str,
        request: OutboxReconcileRequest,
        operator: str,
    ) -> dict[str, Any]:
        now = utc_now()
        safe_note, _ = redact_sensitive(request.note)
        with self.db._write_lock, self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM channel_outbox WHERE id=? AND tenant_id=?",
                (outbox_id, tenant_id),
            ).fetchone()
            if row is None:
                raise OutboxError("outbox item not found")
            if int(row["record_version"]) != request.expected_record_version:
                raise OutboxError("outbox record version conflict")
            delivery_state = str(row["delivery_state"])
            if delivery_state not in {"uncertain", "dead_letter"}:
                raise OutboxError("only uncertain or dead-letter items can be reconciled")
            if delivery_state == "dead_letter" and request.resolution != "not_delivered":
                raise OutboxError("dead-letter items can only be requeued as confirmed not delivered")
            if request.resolution == "confirmed":
                status = "sent"
                target_state = "confirmed"
                next_attempt = None
                attempt_count = int(row["attempt_count"])
                draft_status = "sent"
            elif request.resolution == "not_delivered":
                status = "queued"
                target_state = "retry_scheduled"
                next_attempt = now
                attempt_count = 0
                draft_status = "sending"
            else:
                status = "failed"
                target_state = "rejected"
                next_attempt = None
                attempt_count = int(row["attempt_count"])
                draft_status = "failed"
            cursor = conn.execute(
                """
                UPDATE channel_outbox
                SET status=?, delivery_state=?, next_attempt_at=?, attempt_count=?,
                    lease_owner=NULL, lease_until=NULL, dispatch_started_at=NULL,
                    dead_letter_at=NULL, reconciled_at=?, reconciled_by=?,
                    reconciliation_note=?, last_error=NULL, error_kind=NULL,
                    updated_at=?, record_version=record_version+1
                WHERE id=? AND tenant_id=? AND record_version=?
                """,
                (
                    status,
                    target_state,
                    next_attempt,
                    attempt_count,
                    now,
                    operator,
                    safe_note,
                    now,
                    outbox_id,
                    tenant_id,
                    request.expected_record_version,
                ),
            )
            if cursor.rowcount != 1:
                raise OutboxError("outbox reconciliation conflict")
            self._update_draft_locked(
                conn, outbox_id, status=draft_status, error=None, now=now
            )
            self._update_event_locked(
                conn,
                outbox_id,
                "sent" if status == "sent" else "queued" if status == "queued" else "failed",
                now,
            )
            saved = conn.execute("SELECT * FROM channel_outbox WHERE id=?", (outbox_id,)).fetchone()
        self.db.audit(
            "channel.outbox.reconciled",
            operator,
            outbox_id,
            {"resolution": request.resolution, "note_length": len(safe_note)},
            tenant_id,
        )
        return dict(saved)

    def list_items(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        delivery_state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if status:
            clauses.append("status=?")
            params.append(status)
        if delivery_state:
            clauses.append("delivery_state=?")
            params.append(delivery_state)
        params.append(max(1, min(500, limit)))
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM channel_outbox WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self, tenant_id: str) -> dict[str, Any]:
        now = datetime.now(UTC)
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT status, delivery_state, COUNT(*) AS count, MIN(created_at) AS oldest
                FROM channel_outbox WHERE tenant_id=? GROUP BY status, delivery_state
                """,
                (tenant_id,),
            ).fetchall()
            due = conn.execute(
                """
                SELECT COUNT(*) FROM channel_outbox
                WHERE tenant_id=? AND status='queued'
                  AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                """,
                (tenant_id, now.isoformat()),
            ).fetchone()[0]
        counts: dict[str, int] = {}
        oldest_pending: datetime | None = None
        for row in rows:
            counts[f"{row['status']}/{row['delivery_state']}"] = int(row["count"])
            if row["status"] in {"queued", "sending"} and row["oldest"]:
                candidate = datetime.fromisoformat(str(row["oldest"]))
                oldest_pending = min(oldest_pending, candidate) if oldest_pending else candidate
        return {
            "counts": counts,
            "due": int(due),
            "oldest_pending_seconds": (
                max(0, int((now - oldest_pending).total_seconds())) if oldest_pending else 0
            ),
            "requires_reconciliation": counts.get("failed/uncertain", 0),
            "dead_letters": counts.get("failed/dead_letter", 0),
        }

    @staticmethod
    def public_view(row: dict[str, Any]) -> dict[str, Any]:
        visible = (
            "id",
            "tenant_id",
            "conversation_id",
            "event_id",
            "source_event_id",
            "idempotency_key",
            "content_redacted",
            "status",
            "delivery_state",
            "attempt_count",
            "max_attempts",
            "next_attempt_at",
            "lease_owner",
            "lease_until",
            "dispatch_started_at",
            "last_attempt_at",
            "last_error",
            "error_kind",
            "dead_letter_at",
            "reconciled_at",
            "reconciled_by",
            "reconciliation_note",
            "record_version",
            "created_at",
            "updated_at",
        )
        return {key: row.get(key) for key in visible}

    @staticmethod
    def _update_draft_locked(
        conn: sqlite3.Connection,
        outbox_id: str,
        *,
        status: str,
        error: str | None,
        now: str,
    ) -> None:
        if status == "sent":
            conn.execute(
                """
                UPDATE channel_reply_drafts
                SET status='sent', last_error=NULL, sent_at=COALESCE(sent_at, ?),
                    updated_at=?, record_version=record_version+1
                WHERE outbox_id=? AND status IN ('sending','failed')
                """,
                (now, now, outbox_id),
            )
        elif status == "sending":
            conn.execute(
                """
                UPDATE channel_reply_drafts
                SET status='sending', last_error=NULL, updated_at=?,
                    record_version=record_version+1
                WHERE outbox_id=? AND status='failed'
                """,
                (now, outbox_id),
            )
        else:
            conn.execute(
                """
                UPDATE channel_reply_drafts
                SET status='failed', last_error=?, updated_at=?,
                    record_version=record_version+1
                WHERE outbox_id=? AND status='sending'
                """,
                (error, now, outbox_id),
            )

    @staticmethod
    def _update_event_locked(
        conn: sqlite3.Connection,
        outbox_id: str,
        status: str,
        now: str,
    ) -> None:
        conn.execute(
            """
            UPDATE channel_events SET status=?, updated_at=?
            WHERE id=(
                SELECT event_id FROM channel_outbox
                WHERE id=? AND event_id IS NOT NULL
                  AND (source_event_id IS NULL OR event_id<>source_event_id)
            )
            """,
            (status, now, outbox_id),
        )
