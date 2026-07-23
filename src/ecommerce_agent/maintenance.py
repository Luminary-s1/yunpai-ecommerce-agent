from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Settings
from .database import Database, utc_now


class MaintenanceService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    def purge_expired(self, *, actor: str, dry_run: bool) -> dict[str, Any]:
        now = datetime.now(UTC)
        message_cutoff = (now - timedelta(days=self.settings.message_retention_days)).isoformat()
        audit_cutoff = (now - timedelta(days=self.settings.audit_retention_days)).isoformat()
        terminal = ("completed", "failed", "canceled", "rejected")

        with self.db._write_lock, self.db.connect() as conn:
            messages_delete = conn.execute(
                """
                SELECT COUNT(*) FROM messages m
                WHERE m.created_at < ?
                  AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.message_id=m.id)
                  AND NOT EXISTS (
                      SELECT 1 FROM handoff_tasks h
                      WHERE h.session_id=m.session_id AND h.status NOT IN (?, ?, ?, ?)
                  )
                """,
                (message_cutoff, *terminal),
            ).fetchone()[0]
            messages_redact = conn.execute(
                """
                SELECT COUNT(*) FROM messages m
                WHERE m.created_at < ?
                  AND EXISTS (SELECT 1 FROM feedback f WHERE f.message_id=m.id)
                  AND m.content != '[PURGED_BY_RETENTION]'
                  AND NOT EXISTS (
                      SELECT 1 FROM handoff_tasks h
                      WHERE h.session_id=m.session_id AND h.status NOT IN (?, ?, ?, ?)
                  )
                """,
                (message_cutoff, *terminal),
            ).fetchone()[0]
            handoffs_redact = conn.execute(
                """
                SELECT COUNT(*) FROM handoff_tasks
                WHERE updated_at < ? AND status IN (?, ?, ?, ?)
                  AND payload_json != '{"purged":true}'
                """,
                (message_cutoff, *terminal),
            ).fetchone()[0]
            context_snapshots_delete = conn.execute(
                """
                SELECT COUNT(*) FROM context_snapshots c
                WHERE c.created_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM handoff_tasks h
                      WHERE h.session_id=c.session_id AND h.status NOT IN (?, ?, ?, ?)
                  )
                """,
                (message_cutoff, *terminal),
            ).fetchone()[0]
            metrics_delete = conn.execute(
                "SELECT COUNT(*) FROM request_metrics WHERE created_at < ?", (message_cutoff,)
            ).fetchone()[0]
            audit_delete = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE created_at < ?", (audit_cutoff,)
            ).fetchone()[0]
            expired_sessions = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT s.id FROM sessions s
                    WHERE s.status='active' AND s.last_seen_at < ?
                      AND NOT EXISTS (
                          SELECT 1 FROM handoff_tasks h
                          WHERE h.session_id=s.id AND h.status NOT IN (?, ?, ?, ?)
                      )
                    """,
                    (message_cutoff, *terminal),
                ).fetchall()
            ]

            report = {
                "dry_run": dry_run,
                "message_cutoff": message_cutoff,
                "audit_cutoff": audit_cutoff,
                "messages_deleted": int(messages_delete),
                "messages_redacted": int(messages_redact),
                "handoffs_redacted": int(handoffs_redact),
                "context_snapshots_deleted": int(context_snapshots_delete),
                "metrics_deleted": int(metrics_delete),
                "audit_events_deleted": int(audit_delete),
                "sessions_closed": len(expired_sessions),
                "expired_session_ids": expired_sessions,
            }
            if dry_run:
                return report

            conn.execute(
                """
                UPDATE messages SET content='[PURGED_BY_RETENTION]', sources_json='[]', redacted=1
                WHERE created_at < ?
                  AND EXISTS (SELECT 1 FROM feedback f WHERE f.message_id=messages.id)
                  AND NOT EXISTS (
                      SELECT 1 FROM handoff_tasks h
                      WHERE h.session_id=messages.session_id AND h.status NOT IN (?, ?, ?, ?)
                  )
                """,
                (message_cutoff, *terminal),
            )
            conn.execute(
                """
                UPDATE messages SET context_snapshot_id=NULL
                WHERE context_snapshot_id IN (
                    SELECT c.id FROM context_snapshots c
                    WHERE c.created_at < ?
                      AND NOT EXISTS (
                          SELECT 1 FROM handoff_tasks h
                          WHERE h.session_id=c.session_id AND h.status NOT IN (?, ?, ?, ?)
                      )
                )
                """,
                (message_cutoff, *terminal),
            )
            conn.execute(
                """
                DELETE FROM messages
                WHERE created_at < ?
                  AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.message_id=messages.id)
                  AND NOT EXISTS (
                      SELECT 1 FROM handoff_tasks h
                      WHERE h.session_id=messages.session_id AND h.status NOT IN (?, ?, ?, ?)
                  )
                """,
                (message_cutoff, *terminal),
            )
            conn.execute(
                """
                UPDATE handoff_tasks SET payload_json='{"purged":true}'
                WHERE updated_at < ? AND status IN (?, ?, ?, ?)
                """,
                (message_cutoff, *terminal),
            )
            conn.execute(
                """
                DELETE FROM context_snapshots
                WHERE created_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM handoff_tasks h
                      WHERE h.session_id=context_snapshots.session_id
                        AND h.status NOT IN (?, ?, ?, ?)
                  )
                """,
                (message_cutoff, *terminal),
            )
            conn.execute("DELETE FROM request_metrics WHERE created_at < ?", (message_cutoff,))
            conn.execute("DELETE FROM audit_log WHERE created_at < ?", (audit_cutoff,))
            if expired_sessions:
                placeholders = ",".join("?" for _ in expired_sessions)
                conn.execute(
                    f"UPDATE sessions SET status='closed' WHERE id IN ({placeholders})",
                    tuple(expired_sessions),
                )
            run_id = f"retention-{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO retention_runs VALUES (?, ?, ?, ?)",
                (run_id, actor, json.dumps(report, ensure_ascii=False), utc_now()),
            )
        return report
