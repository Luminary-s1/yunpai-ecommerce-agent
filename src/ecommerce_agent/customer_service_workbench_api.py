from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .auth import AdminPrincipal
from .customer_service_workbench import (
    SHADOW_SOURCE_PREFIX,
    ShadowFeedbackRequest,
    ShadowRunRequest,
    ensure_m8r_frozen_suite,
    load_m8r_eval_definition,
    run_m8r_eval,
    run_shadow_case,
)
from .evaluation import EvaluationError
from .evolution import EvolutionError, EvolutionService
from .schemas import FeedbackRequest
from .service import AgentService
from .text_utils import redact_sensitive


def build_customer_service_workbench_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(
        prefix="/v1/admin/customer-service-shadow",
        tags=["customer-service-shadow"],
    )
    evolution = EvolutionService(service.db, service.knowledge)

    @router.get("/scenarios")
    def scenarios(admin: AdminPrincipal = Depends(require_admin)) -> dict[str, Any]:
        del admin
        definition = load_m8r_eval_definition()
        return {
            key: definition[key]
            for key in (
                "contract_version",
                "fixture_id",
                "virtual",
                "cases",
                "input_hash",
                "oracle_hash",
                "runner_contract",
            )
        }

    @router.post("/scenarios/{case_key}/runs", status_code=status.HTTP_201_CREATED)
    def run_scenario(
        case_key: str,
        payload: ShadowRunRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return run_shadow_case(
                service,
                tenant_id=admin.tenant_id,
                actor=admin.admin_id,
                case_key=case_key,
                request=payload,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/runs")
    def list_shadow_runs(
        limit: int = Query(default=50, ge=1, le=200),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        with service.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.id AS internal_session_id, s.external_session_id,
                       s.source_reference, s.last_seen_at, m.id AS message_id,
                       m.content AS answer, m.intent, m.risk_level, m.route_reason,
                       m.context_snapshot_id, m.created_at,
                       (SELECT a.detail_json FROM audit_log a
                        WHERE a.tenant_id=m.tenant_id AND a.subject_id=m.id
                          AND a.event_type='chat.completed'
                        ORDER BY a.created_at DESC LIMIT 1) AS audit_json
                FROM sessions s
                JOIN messages m ON m.session_id=s.id AND m.role='assistant'
                WHERE s.tenant_id=? AND s.source_type='simulation'
                  AND s.source_reference LIKE ?
                ORDER BY m.created_at DESC LIMIT ?
                """,
                (admin.tenant_id, f"{SHADOW_SOURCE_PREFIX}%", limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            raw_audit = item.pop("audit_json", None)
            try:
                audit = json.loads(raw_audit) if raw_audit else {}
            except ValueError:
                audit = {}
            item["suggestion"] = audit.get("suggestion")
            result.append(item)
        return result

    @router.post("/messages/{message_id}/feedback", status_code=status.HTTP_201_CREATED)
    def submit_feedback(
        message_id: str,
        payload: ShadowFeedbackRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        corrected, corrected_redacted = redact_sensitive(payload.corrected_answer or "")
        note, note_redacted = redact_sensitive(payload.note or "")
        evidence, evidence_redacted = redact_sensitive(payload.evidence_source or "")
        request = FeedbackRequest(
            message_id=message_id,
            rating=payload.rating,
            corrected_answer=corrected or None,
            note=note or None,
            evidence_source=evidence or None,
            submitted_by=admin.admin_id,
        )
        try:
            saved = evolution.submit_feedback(request, tenant_id=admin.tenant_id)
        except EvolutionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            **saved.model_dump(mode="json"),
            "redacted": corrected_redacted or note_redacted or evidence_redacted,
        }

    @router.get("/feedback")
    def list_feedback(
        limit: int = Query(default=50, ge=1, le=200),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        with service.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT f.id, f.message_id, f.rating, f.corrected_answer, f.note,
                       f.submitted_by, f.evidence_source, f.created_at,
                       s.external_session_id, s.source_reference,
                       (SELECT ec.id FROM evolution_candidates ec
                        WHERE ec.feedback_id=f.id LIMIT 1) AS candidate_id,
                       (SELECT ec.status FROM evolution_candidates ec
                        WHERE ec.feedback_id=f.id LIMIT 1) AS candidate_status
                FROM feedback f
                JOIN messages m ON m.id=f.message_id AND m.tenant_id=f.tenant_id
                JOIN sessions s ON s.id=m.session_id AND s.tenant_id=f.tenant_id
                WHERE f.tenant_id=? AND s.source_type='simulation'
                  AND s.source_reference LIKE ?
                ORDER BY f.created_at DESC LIMIT ?
                """,
                (admin.tenant_id, f"{SHADOW_SOURCE_PREFIX}%", limit),
            ).fetchall()
        return [dict(row) for row in rows]

    @router.post("/evaluations/prepare", status_code=status.HTTP_201_CREATED)
    def prepare_evaluation(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return ensure_m8r_frozen_suite(service, admin.tenant_id, admin.admin_id)
        except (EvaluationError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/evaluations/runs", status_code=status.HTTP_201_CREATED)
    def run_evaluation(
        payload: ShadowRunRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return run_m8r_eval(
                service,
                tenant_id=admin.tenant_id,
                actor=admin.admin_id,
                run_key=payload.run_key,
            )
        except (EvaluationError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router
