from __future__ import annotations

import json
import re
import uuid
from typing import Any

from .database import Database, utc_now
from .evals import RETRIEVAL_CASES, run_offline_evaluation
from .policy import is_business_action_request, review_output
from .rag import KnowledgeBase
from .schemas import CandidateView, FeedbackRequest, FeedbackResponse
from .text_utils import cosine_similarity, hash_embedding, normalize_text, search_terms


class EvolutionError(ValueError):
    pass


class EvolutionService:
    def __init__(self, db: Database, knowledge: KnowledgeBase):
        self.db = db
        self.knowledge = knowledge

    def submit_feedback(
        self, request: FeedbackRequest, *, tenant_id: str | None = None
    ) -> FeedbackResponse:
        pair = self.db.get_message_pair(request.message_id, tenant_id)
        if pair is None:
            raise EvolutionError("assistant message not found")
        user_message, assistant_message = pair
        feedback_id = f"feedback-{uuid.uuid4().hex}"
        corrected = normalize_text(request.corrected_answer or "") or None
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO feedback(
                    id, message_id, rating, corrected_answer, note, submitted_by,
                    evidence_source, created_at, tenant_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback_id, request.message_id, request.rating, corrected,
                    request.note, request.submitted_by, request.evidence_source,
                    utc_now(), tenant_id,
                ),
            )

        candidate_id: str | None = None
        if request.rating == -1 and corrected:
            candidate_id = f"candidate-{uuid.uuid4().hex}"
            with self.db._write_lock, self.db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO evolution_candidates(
                        id, feedback_id, question, proposed_answer, evidence_source, intent,
                        source_message_id, status, gate_passed, gate_report_json,
                        resulting_knowledge_id, created_at, decided_at, decided_by,
                        decision_note, tenant_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL, ?, NULL, NULL, NULL, ?)
                    """,
                    (
                        candidate_id, feedback_id, user_message["content"], corrected,
                        request.evidence_source,
                        assistant_message.get("intent") or "general", request.message_id,
                        utc_now(), tenant_id,
                    ),
                )
        self.db.audit(
            "feedback.submitted",
            request.submitted_by,
            feedback_id,
            {"rating": request.rating, "candidate_id": candidate_id},
            tenant_id,
        )
        return FeedbackResponse(
            feedback_id=feedback_id,
            candidate_id=candidate_id,
            status="candidate_pending" if candidate_id else "recorded",
        )

    def list_candidates(
        self, status: str | None = None, tenant_id: str | None = None
    ) -> list[CandidateView]:
        query = "SELECT * FROM evolution_candidates"
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if tenant_id:
            conditions.append("tenant_id = ?")
            params.append(tenant_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC"
        with self.db.connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._view(dict(row)) for row in rows]

    def evaluate(self, candidate_id: str, tenant_id: str | None = None) -> CandidateView:
        candidate = self._get_candidate(candidate_id, tenant_id)
        if candidate["status"] not in {"pending", "evaluated"}:
            raise EvolutionError(f"candidate cannot be evaluated from status {candidate['status']}")

        checks: dict[str, Any] = {}
        answer = candidate["proposed_answer"]
        question = candidate["question"]
        checks["length"] = 8 <= len(answer) <= 600
        checks["no_sensitive_numbers"] = not bool(
            re.search(r"(?<!\d)1\d{10}(?!\d)|\b\d{16,19}\b|\b\d{17}[\dXx]\b", answer)
        )
        neighbors = self.knowledge.retrieve(
            question,
            top_k=3,
            min_score=0.05,
            intent=candidate["intent"],
            tenant_id=tenant_id,
        )
        source_traceable = bool(normalize_text(candidate.get("evidence_source") or ""))
        candidate_score = self.knowledge.candidate_score(
            question,
            intent=candidate["intent"],
            question=question,
            answer=answer,
        )
        checks["source_traceable"] = source_traceable
        checks["candidate_retrievable"] = candidate_score >= 0.35
        checks["candidate_retrieval_score"] = candidate_score
        checks["retrieval_fit"] = candidate_score >= 0.35
        checks["nearest_documents"] = [item["id"] for item in neighbors]
        evidence = " ".join(item["answer"] for item in neighbors)
        output_passed, output_reason = review_output(
            answer,
            evidence,
            question=question,
        )
        checks["output_policy"] = output_passed
        checks["output_policy_reason"] = output_reason

        if is_business_action_request(question):
            checks["high_risk_handoff_preserved"] = any(
                phrase in answer for phrase in ("人工", "不能直接", "无法直接", "核对", "确认")
            )
        else:
            checks["high_risk_handoff_preserved"] = True

        answer_vector = hash_embedding(answer)
        alignment = max(
            (
                cosine_similarity(answer_vector, hash_embedding(item["answer"]))
                for item in neighbors
            ),
            default=0.0,
        )
        answer_terms = set(search_terms(answer))
        evidence_terms = set(search_terms(evidence))
        lexical_overlap = len(answer_terms & evidence_terms) / max(1, len(answer_terms))
        contradiction = bool(
            re.search(r"完全相反|无视.{0,6}(规则|说明)|忽略.{0,6}(规则|尺寸表)|随便选择", answer)
        )
        checks["semantic_alignment"] = (
            alignment >= 0.08
            or lexical_overlap >= 0.15
            or (source_traceable and candidate_score >= 0.35)
        )
        checks["semantic_alignment_score"] = round(alignment, 4)
        checks["lexical_evidence_overlap"] = round(lexical_overlap, 4)
        checks["no_contradiction_markers"] = not contradiction

        collision_failures: list[str] = []
        for regression_query, expected_intent in RETRIEVAL_CASES:
            current = self.knowledge.retrieve(
                regression_query,
                top_k=1,
                min_score=0.05,
                intent=expected_intent,
                tenant_id=tenant_id,
            )
            shadow_score = self.knowledge.candidate_score(
                regression_query,
                intent=candidate["intent"],
                query_intent=expected_intent,
                question=question,
                answer=answer,
            )
            if current and shadow_score >= current[0]["score"]:
                collision_failures.append(regression_query)
        checks["retrieval_collision_free"] = not collision_failures
        checks["retrieval_collision_failures"] = collision_failures

        regression = run_offline_evaluation(self.knowledge, tenant_id=tenant_id)
        checks["baseline_regression"] = regression["passed"]
        gate_passed = all(
            value is True
            for key, value in checks.items()
            if key not in {
                "output_policy_reason", "nearest_documents",
                "semantic_alignment_score", "lexical_evidence_overlap",
                "candidate_retrieval_score", "retrieval_collision_failures",
            }
        )
        report = {"passed": gate_passed, "checks": checks, "baseline": regression["summary"]}
        run_id = f"evolution-run-{uuid.uuid4().hex}"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                UPDATE evolution_candidates
                SET status='evaluated', gate_passed=?, gate_report_json=? WHERE id=?
                """,
                (int(gate_passed), json.dumps(report, ensure_ascii=False), candidate_id),
            )
            conn.execute(
                """
                INSERT INTO evolution_runs(
                    id, candidate_id, passed, report_json, created_at, tenant_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, candidate_id, int(gate_passed),
                    json.dumps(report, ensure_ascii=False), utc_now(), candidate.get("tenant_id"),
                ),
            )
        self.db.audit(
            "evolution.evaluated", "system", candidate_id, report, candidate.get("tenant_id")
        )
        return self._view(self._get_candidate(candidate_id, tenant_id))

    def approve(
        self, candidate_id: str, operator: str, note: str | None, tenant_id: str | None = None
    ) -> CandidateView:
        candidate = self._get_candidate(candidate_id, tenant_id)
        if candidate["status"] != "evaluated" or candidate["gate_passed"] != 1:
            raise EvolutionError("candidate must pass evaluation before approval")
        knowledge_id = self.knowledge.add_document(
            category="进化话术",
            intent=candidate["intent"],
            question=candidate["question"],
            answer=candidate["proposed_answer"],
            keywords=candidate["question"],
            risk_level="low",
            source=f"evolution:{candidate_id}",
            version=self.knowledge.next_version(candidate["intent"], tenant_id),
            status="active",
            approved_by=operator,
            tenant_id=tenant_id,
        )
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                UPDATE evolution_candidates
                SET status='approved', resulting_knowledge_id=?, decided_at=?,
                    decided_by=?, decision_note=? WHERE id=?
                """,
                (knowledge_id, utc_now(), operator, note, candidate_id),
            )
        self.db.audit(
            "evolution.approved",
            operator,
            candidate_id,
            {"knowledge_id": knowledge_id, "note": note},
            candidate.get("tenant_id"),
        )
        return self._view(self._get_candidate(candidate_id, tenant_id))

    def reject(
        self, candidate_id: str, operator: str, note: str | None, tenant_id: str | None = None
    ) -> CandidateView:
        candidate = self._get_candidate(candidate_id, tenant_id)
        if candidate["status"] == "approved":
            raise EvolutionError("approved candidate must be rolled back through its knowledge version")
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                UPDATE evolution_candidates SET status='rejected', decided_at=?,
                    decided_by=?, decision_note=? WHERE id=?
                """,
                (utc_now(), operator, note, candidate_id),
            )
        self.db.audit(
            "evolution.rejected", operator, candidate_id, {"note": note}, candidate.get("tenant_id")
        )
        return self._view(self._get_candidate(candidate_id, tenant_id))

    def rollback(
        self,
        knowledge_id: str,
        operator: str,
        note: str | None,
        tenant_id: str | None = None,
    ) -> bool:
        document = self.knowledge.get_document(knowledge_id, tenant_id)
        if document is None or not str(document["source"]).startswith("evolution:"):
            raise EvolutionError("only evolution-created knowledge can be rolled back here")
        source_candidate_id = str(document["source"]).removeprefix("evolution:")
        self._get_candidate(source_candidate_id, tenant_id)
        changed = self.knowledge.retire_document(knowledge_id, operator, tenant_id)
        if changed:
            self.db.audit(
                "evolution.rolled_back", operator, knowledge_id, {"note": note}, tenant_id
            )
        return changed

    def _get_candidate(
        self, candidate_id: str, tenant_id: str | None = None
    ) -> dict[str, Any]:
        query = "SELECT * FROM evolution_candidates WHERE id=?"
        params: tuple[Any, ...] = (candidate_id,)
        if tenant_id:
            query += " AND tenant_id=?"
            params = (candidate_id, tenant_id)
        with self.db.connect() as conn:
            row = conn.execute(query, params).fetchone()
        if row is None:
            raise EvolutionError("candidate not found")
        return dict(row)

    @staticmethod
    def _view(candidate: dict[str, Any]) -> CandidateView:
        raw_report = candidate.get("gate_report_json")
        return CandidateView(
            id=candidate["id"],
            question=candidate["question"],
            proposed_answer=candidate["proposed_answer"],
            evidence_source=candidate.get("evidence_source"),
            intent=candidate["intent"],
            status=candidate["status"],
            gate_passed=None if candidate["gate_passed"] is None else bool(candidate["gate_passed"]),
            gate_report=json.loads(raw_report) if raw_report else None,
            created_at=candidate["created_at"],
        )
