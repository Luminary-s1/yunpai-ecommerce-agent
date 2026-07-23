from __future__ import annotations

import json
import uuid
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .database import Database, utc_now


class QualityError(ValueError):
    pass


class QualityRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_type: Literal["agent", "channel"]
    conversation_id: str = Field(min_length=1, max_length=128)


class QualityReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_status: Literal["confirmed", "dismissed"]
    expected_record_version: int = Field(ge=1)
    correction: str | None = Field(default=None, max_length=2000)


class QualityService:
    RULESET_VERSION = "qa-rules-v1"
    PENALTIES = {"critical": 40, "high": 25, "medium": 12, "low": 5}

    def __init__(self, db: Database):
        self.db = db

    def run(
        self, tenant_id: str, request: QualityRunRequest, actor: str
    ) -> dict[str, Any]:
        if request.conversation_type == "agent":
            issues, message_id = self._inspect_agent(
                tenant_id, request.conversation_id
            )
        else:
            issues, message_id = self._inspect_channel(
                tenant_id, request.conversation_id
            )
        score = max(
            0,
            100 - sum(self.PENALTIES[issue["severity"]] for issue in issues),
        )
        result_id = f"qa-{uuid.uuid4().hex}"
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO qa_results(
                    id, tenant_id, conversation_type, conversation_id,
                    assistant_message_id, ruleset_version, issues_json, score,
                    review_status, reviewer, correction, record_version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, 1, ?, ?)
                """,
                (
                    result_id, tenant_id, request.conversation_type,
                    request.conversation_id, message_id, self.RULESET_VERSION,
                    json.dumps(issues, ensure_ascii=False), score, now, now,
                ),
            )
        self.db.audit(
            "qa.run_completed",
            actor,
            result_id,
            {"score": score, "issue_codes": [item["code"] for item in issues]},
            tenant_id,
        )
        return self._require(tenant_id, result_id)

    def list_results(
        self,
        tenant_id: str,
        *,
        review_status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = "tenant_id=?"
        params: list[Any] = [tenant_id]
        if review_status:
            where += " AND review_status=?"
            params.append(review_status)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM qa_results WHERE {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [self._view(row) for row in rows]

    def review(
        self,
        tenant_id: str,
        result_id: str,
        request: QualityReviewRequest,
        actor: str,
    ) -> dict[str, Any]:
        if request.review_status == "confirmed" and not request.correction:
            raise QualityError("confirmed quality issues require a correction")
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE qa_results SET review_status=?, reviewer=?, correction=?,
                    record_version=record_version+1, updated_at=?
                WHERE id=? AND tenant_id=? AND review_status='pending' AND record_version=?
                """,
                (
                    request.review_status, actor, request.correction, utc_now(),
                    result_id, tenant_id, request.expected_record_version,
                ),
            )
            if cursor.rowcount != 1:
                raise QualityError("quality review transition or version conflict")
        self.db.audit(
            "qa.reviewed", actor, result_id,
            {"review_status": request.review_status}, tenant_id,
        )
        return self._require(tenant_id, result_id)

    def summary(self, tenant_id: str) -> dict[str, Any]:
        results = self.list_results(tenant_id, limit=500)
        issue_counts: Counter[str] = Counter()
        severity_counts: Counter[str] = Counter()
        for result in results:
            for issue in result["issues"]:
                issue_counts[issue["code"]] += 1
                severity_counts[issue["severity"]] += 1
        total = len(results)
        return {
            "ruleset_version": self.RULESET_VERSION,
            "total_runs": total,
            "average_score": round(sum(item["score"] for item in results) / total, 2)
            if total else 0,
            "pending_reviews": sum(item["review_status"] == "pending" for item in results),
            "issues": [
                {"code": code, "count": count}
                for code, count in issue_counts.most_common()
            ],
            "severity_counts": dict(severity_counts),
        }

    def _inspect_agent(
        self, tenant_id: str, conversation_id: str
    ) -> tuple[list[dict[str, str]], str | None]:
        with self.db.connect() as conn:
            session = conn.execute(
                "SELECT id FROM sessions WHERE id=? AND tenant_id=?",
                (conversation_id, tenant_id),
            ).fetchone()
            if session is None:
                raise QualityError("agent conversation not found")
            messages = conn.execute(
                """
                SELECT id, risk_level, route_reason, sources_json, model_fallback, redacted
                FROM messages WHERE session_id=? AND tenant_id=? AND role='assistant'
                ORDER BY created_at
                """,
                (conversation_id, tenant_id),
            ).fetchall()
            handoffs = {
                row[0]
                for row in conn.execute(
                    "SELECT message_id FROM handoff_tasks WHERE session_id=? AND tenant_id=?",
                    (conversation_id, tenant_id),
                ).fetchall()
            }
        issues: list[dict[str, str]] = []
        for message in messages:
            try:
                sources = json.loads(message["sources_json"] or "[]")
            except ValueError:
                sources = []
            if message["route_reason"] == "knowledge_answer_allowed" and not sources:
                issues.append(self._issue("fact_evidence_missing", "high", message["id"]))
            if message["model_fallback"]:
                issues.append(self._issue("model_fallback", "medium", message["id"]))
            if message["risk_level"] == "high" and message["id"] not in handoffs:
                issues.append(self._issue("missed_handoff", "critical", message["id"]))
            if message["redacted"]:
                issues.append(self._issue("sensitive_data_redacted", "low", message["id"]))
        return issues, str(messages[-1]["id"]) if messages else None

    def _inspect_channel(
        self, tenant_id: str, conversation_id: str
    ) -> tuple[list[dict[str, str]], str | None]:
        with self.db.connect() as conn:
            conversation = conn.execute(
                "SELECT id, owner_mode, updated_at FROM channel_conversations "
                "WHERE id=? AND tenant_id=?",
                (conversation_id, tenant_id),
            ).fetchone()
            if conversation is None:
                raise QualityError("channel conversation not found")
            events = conn.execute(
                "SELECT id, direction, status, created_at FROM channel_events "
                "WHERE conversation_id=? ORDER BY created_at",
                (conversation_id,),
            ).fetchall()
            drafts = conn.execute(
                "SELECT id, status, risk_level FROM channel_reply_drafts "
                "WHERE conversation_id=? AND tenant_id=?",
                (conversation_id, tenant_id),
            ).fetchall()
        issues: list[dict[str, str]] = []
        for event in events:
            if event["direction"] == "outbound" and event["status"] == "failed":
                issues.append(self._issue("channel_send_failure", "high", event["id"]))
        for draft in drafts:
            if draft["status"] == "failed":
                issues.append(self._issue("draft_send_failure", "high", draft["id"]))
            if draft["risk_level"] in {"high", "critical"} and draft["status"] == "sent":
                issues.append(self._issue("high_risk_reply_sent", "critical", draft["id"]))
        last_id = str(events[-1]["id"]) if events else None
        return issues, last_id

    @staticmethod
    def _issue(code: str, severity: str, evidence_id: str) -> dict[str, str]:
        return {"code": code, "severity": severity, "evidence_id": evidence_id}

    def _require(self, tenant_id: str, result_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM qa_results WHERE id=? AND tenant_id=?",
                (result_id, tenant_id),
            ).fetchone()
        if row is None:
            raise QualityError("quality result not found")
        return self._view(row)

    @staticmethod
    def _view(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["issues"] = json.loads(item.pop("issues_json") or "[]")
        return item
