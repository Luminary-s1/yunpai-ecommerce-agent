from __future__ import annotations

import json
from typing import Any

from .database import Database, session_scope_condition
from .context_builder import ContextBuilder


class AdminConsoleService:
    def __init__(self, db: Database, contexts: ContextBuilder | None = None):
        self.db = db
        self.contexts = contexts or ContextBuilder(db)

    def overview(
        self, tenant_id: str, *, scope: str = "operational"
    ) -> dict[str, Any]:
        operational = session_scope_condition(scope)
        with self.db.connect() as conn:
            counts = {
                "conversations": conn.execute(
                    f"SELECT COUNT(*) FROM sessions s WHERE tenant_id=? AND {operational}",
                    (tenant_id,),
                ).fetchone()[0],
                "messages": conn.execute(
                    f"""
                    SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id=m.session_id
                    WHERE m.tenant_id=? AND {operational}
                    """,
                    (tenant_id,),
                ).fetchone()[0],
                "open_handoffs": conn.execute(
                    f"""
                    SELECT COUNT(*) FROM handoff_tasks h JOIN sessions s ON s.id=h.session_id
                    WHERE h.tenant_id=? AND {operational}
                      AND h.status NOT IN ('completed','rejected','failed','canceled')
                    """,
                    (tenant_id,),
                ).fetchone()[0],
                "pending_learning": conn.execute(
                    """
                    SELECT COUNT(*) FROM evolution_candidates
                    WHERE tenant_id=? AND status IN ('pending','evaluated')
                    """,
                    (tenant_id,),
                ).fetchone()[0],
                "channel_conversations": conn.execute(
                    "SELECT COUNT(*) FROM channel_conversations WHERE tenant_id=?",
                    (tenant_id,),
                ).fetchone()[0],
                "active_tenant_knowledge": conn.execute(
                    "SELECT COUNT(*) FROM knowledge WHERE tenant_id=? AND status='active'",
                    (tenant_id,),
                ).fetchone()[0],
                "active_sops": conn.execute(
                    """
                    SELECT COUNT(*) FROM sop_definitions d JOIN sop_versions v
                      ON v.definition_id=d.id AND v.version=d.current_active_version
                    WHERE d.tenant_id=? AND v.status='active'
                    """,
                    (tenant_id,),
                ).fetchone()[0],
                "pending_qa_reviews": conn.execute(
                    "SELECT COUNT(*) FROM qa_results WHERE tenant_id=? AND review_status='pending'",
                    (tenant_id,),
                ).fetchone()[0],
                "active_releases": conn.execute(
                    "SELECT COUNT(*) FROM release_policies WHERE tenant_id=? AND status='active'",
                    (tenant_id,),
                ).fetchone()[0],
                "paused_releases": conn.execute(
                    "SELECT COUNT(*) FROM release_policies WHERE tenant_id=? AND status='paused'",
                    (tenant_id,),
                ).fetchone()[0],
                "pending_channel_agent_jobs": conn.execute(
                    """
                    SELECT COUNT(*) FROM channel_agent_jobs
                    WHERE tenant_id=? AND status IN ('queued','running','retry')
                    """,
                    (tenant_id,),
                ).fetchone()[0],
                "dead_letter_channel_agent_jobs": conn.execute(
                    """
                    SELECT COUNT(*) FROM channel_agent_jobs
                    WHERE tenant_id=? AND status='dead_letter'
                    """,
                    (tenant_id,),
                ).fetchone()[0],
            }
            intent_rows = conn.execute(
                f"""
                SELECT r.intent, COUNT(*) AS total,
                       SUM(r.requires_human) AS handoffs,
                       SUM(CASE WHEN r.success=0 THEN 1 ELSE 0 END) AS failures
                FROM request_metrics r JOIN sessions s ON s.id=r.session_id
                WHERE r.tenant_id=? AND {operational}
                GROUP BY r.intent ORDER BY total DESC, r.intent LIMIT 12
                """,
                (tenant_id,),
            ).fetchall()
            handoff_rows = conn.execute(
                f"""
                SELECT h.status, COUNT(*) AS total
                FROM handoff_tasks h JOIN sessions s ON s.id=h.session_id
                WHERE h.tenant_id=? AND {operational}
                GROUP BY h.status ORDER BY total DESC
                """,
                (tenant_id,),
            ).fetchall()
            recent_activity = conn.execute(
                """
                SELECT id, event_type, actor, subject_id, created_at
                FROM audit_log WHERE tenant_id=?
                ORDER BY created_at DESC LIMIT 10
                """,
                (tenant_id,),
            ).fetchall()
            excluded_rows = conn.execute(
                """
                SELECT source_type, COUNT(*) AS total FROM sessions
                WHERE tenant_id=? AND source_type IN ('simulation','evaluation')
                GROUP BY source_type ORDER BY source_type
                """,
                (tenant_id,),
            ).fetchall()
        return {
            "counts": counts,
            "metrics": self.db.metric_summary(tenant_id, scope=scope),
            "intents": [dict(row) for row in intent_rows],
            "handoff_statuses": [dict(row) for row in handoff_rows],
            "recent_activity": [dict(row) for row in recent_activity],
            "data_scope": scope,
            "excluded_sessions": {
                str(row["source_type"]): int(row["total"]) for row in excluded_rows
            },
        }

    def list_conversations(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        query: str | None = None,
        scope: str = "operational",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        conditions = ["s.tenant_id=?", session_scope_condition(scope)]
        params: list[Any] = [tenant_id]
        if status:
            conditions.append("s.status=?")
            params.append(status)
        if query:
            conditions.append("s.external_session_id LIKE ?")
            params.append(f"%{query}%")
        where = " AND ".join(conditions)
        with self.db.connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM sessions s WHERE {where}", tuple(params)
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT s.id, s.external_session_id, s.status, s.client_id,
                       s.source_type, s.source_reference,
                       s.created_at, s.last_seen_at,
                       COUNT(m.id) AS message_count,
                       MAX(m.created_at) AS last_message_at,
                       (SELECT content FROM messages lm
                        WHERE lm.session_id=s.id ORDER BY lm.created_at DESC LIMIT 1) AS last_message,
                       (SELECT intent FROM messages lm
                        WHERE lm.session_id=s.id AND lm.role='assistant'
                        ORDER BY lm.created_at DESC LIMIT 1) AS last_intent,
                       (SELECT risk_level FROM messages lm
                        WHERE lm.session_id=s.id AND lm.role='assistant'
                        ORDER BY lm.created_at DESC LIMIT 1) AS last_risk_level,
                       (SELECT status FROM handoff_tasks h
                        WHERE h.session_id=s.id
                        ORDER BY h.created_at DESC LIMIT 1) AS handoff_status
                FROM sessions s LEFT JOIN messages m ON m.session_id=s.id
                WHERE {where}
                GROUP BY s.id ORDER BY s.last_seen_at DESC LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["last_message"] = str(item.get("last_message") or "")[:160]
            items.append(item)
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "scope": scope,
        }

    def conversation(self, tenant_id: str, session_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            session = conn.execute(
                """
                SELECT id, external_session_id, status, client_id, source_type,
                       source_reference, created_at, last_seen_at
                FROM sessions WHERE id=? AND tenant_id=?
                """,
                (session_id, tenant_id),
            ).fetchone()
            if session is None:
                return None
            messages = conn.execute(
                """
                SELECT m.id, m.trace_id, m.role, m.content, m.intent, m.risk_level,
                       m.route_reason, m.sources_json, m.model_fallback, m.redacted,
                       m.context_snapshot_id, m.created_at,
                       (SELECT a.detail_json FROM audit_log a
                        WHERE a.tenant_id=m.tenant_id AND a.subject_id=m.id
                          AND a.event_type='chat.completed'
                        ORDER BY a.created_at DESC LIMIT 1) AS decision_audit_json
                FROM messages m WHERE m.session_id=? AND m.tenant_id=?
                ORDER BY m.created_at ASC
                """,
                (session_id, tenant_id),
            ).fetchall()
            handoffs = conn.execute(
                """
                SELECT id, message_id, status, reason, acceptance_criteria, assigned_to,
                       deadline_at, retry_count, max_retries, version, created_at, updated_at
                FROM handoff_tasks WHERE session_id=? AND tenant_id=?
                ORDER BY created_at DESC
                """,
                (session_id, tenant_id),
            ).fetchall()
        message_views = []
        for row in messages:
            item = dict(row)
            audit_detail = item.pop("decision_audit_json", None)
            decision: dict[str, Any] | None = None
            if audit_detail:
                try:
                    parsed = json.loads(audit_detail)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    decision = {
                        key: parsed.get(key)
                        for key in (
                            "decision_mode",
                            "selected_tool",
                            "tool_status",
                            "context_readiness",
                            "trace",
                        )
                        if parsed.get(key) is not None
                    }
            try:
                item["sources"] = json.loads(item.pop("sources_json") or "[]")
            except ValueError:
                item["sources"] = []
            item["model_fallback"] = bool(item["model_fallback"])
            item["redacted"] = bool(item["redacted"])
            item["decision"] = decision
            snapshot_id = item.get("context_snapshot_id")
            snapshot = self.contexts.get(tenant_id, snapshot_id) if snapshot_id else None
            item["context"] = snapshot.summary() if snapshot else None
            message_views.append(item)
        return {
            "session": dict(session),
            "messages": message_views,
            "handoffs": [dict(row) for row in handoffs],
        }

    def context_snapshot(self, tenant_id: str, snapshot_id: str) -> dict[str, Any] | None:
        snapshot = self.contexts.get(tenant_id, snapshot_id)
        if snapshot is None:
            return None
        return {
            **snapshot.summary(),
            "trace_id": snapshot.trace_id,
            "session_id": snapshot.session_id,
            "bundle": snapshot.bundle,
            "evidence": snapshot.evidence,
            "conflicts": snapshot.conflicts,
        }

    def audit_events(
        self,
        tenant_id: str,
        *,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if event_type:
            conditions.append("event_type=?")
            params.append(event_type)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, event_type, actor, subject_id, detail_json, created_at
                FROM audit_log WHERE {' AND '.join(conditions)}
                ORDER BY created_at DESC LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        events = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.pop("detail_json") or "{}")
            except ValueError:
                item["detail"] = {}
            events.append(item)
        return events
