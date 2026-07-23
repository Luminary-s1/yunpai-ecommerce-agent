from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import AdminPrincipal
from .knowledge_management import (
    KnowledgeCreateRequest,
    KnowledgeLifecycleError,
    KnowledgeReviseRequest,
    KnowledgeTransitionRequest,
)
from .quality import QualityError, QualityReviewRequest, QualityRunRequest
from .service import AgentService
from .sops import (
    SopCompensationRequest,
    SopCreateRequest,
    SopError,
    SopReviseRequest,
    SopStepResolutionRequest,
    SopTransitionRequest,
)


def build_governance_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/admin", tags=["governance"])

    @router.get("/knowledge")
    def list_knowledge(
        status: str | None = Query(default=None, pattern=r"^(active|candidate|retired)$"),
        layer: str | None = Query(
            default=None, pattern=r"^(platform|industry|store|product|evolution)$"
        ),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.knowledge_management.list_items(
            admin.tenant_id, status=status, layer=layer, limit=limit
        )

    @router.post("/knowledge", status_code=201)
    def create_knowledge(
        payload: KnowledgeCreateRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.knowledge_management.create(
                admin.tenant_id, payload, admin.admin_id
            )
        except KnowledgeLifecycleError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/knowledge/{item_id}")
    def get_knowledge(
        item_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = service.knowledge_management.get_item(admin.tenant_id, item_id)
        if result is None:
            raise HTTPException(status_code=404, detail="knowledge item not found")
        return result

    @router.post("/knowledge/{item_id}/versions", status_code=201)
    def revise_knowledge(
        item_id: str,
        payload: KnowledgeReviseRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _knowledge_action(
            service.knowledge_management.revise, admin, item_id, payload
        )

    @router.post("/knowledge/{item_id}/evaluate")
    def evaluate_knowledge(
        item_id: str,
        payload: KnowledgeTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _knowledge_action(
            service.knowledge_management.evaluate, admin, item_id, payload
        )

    @router.post("/knowledge/{item_id}/approve")
    def approve_knowledge(
        item_id: str,
        payload: KnowledgeTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _knowledge_action(
            service.knowledge_management.approve, admin, item_id, payload
        )

    @router.post("/knowledge/{item_id}/retire")
    def retire_knowledge(
        item_id: str,
        payload: KnowledgeTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _knowledge_action(
            service.knowledge_management.retire, admin, item_id, payload
        )

    @router.post("/knowledge/{item_id}/rollback")
    def rollback_knowledge(
        item_id: str,
        payload: KnowledgeTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _knowledge_action(
            service.knowledge_management.rollback, admin, item_id, payload
        )

    @router.get("/sops")
    def list_sops(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.sops.list_definitions(admin.tenant_id)

    @router.post("/sops", status_code=201)
    def create_sop(
        payload: SopCreateRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.sops.create(admin.tenant_id, payload, admin.admin_id)
        except SopError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/sops/{definition_id}")
    def get_sop(
        definition_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = service.sops.detail(admin.tenant_id, definition_id)
        if result is None:
            raise HTTPException(status_code=404, detail="SOP definition not found")
        return result

    @router.post("/sops/{definition_id}/versions", status_code=201)
    def revise_sop(
        definition_id: str,
        payload: SopReviseRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.sops.revise(
                admin.tenant_id, definition_id, payload, admin.admin_id
            )
        except SopError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/sop-versions/{version_id}/evaluate")
    def evaluate_sop(
        version_id: str,
        payload: SopTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _sop_action(service.sops.evaluate, admin, version_id, payload)

    @router.post("/sop-versions/{version_id}/approve")
    def approve_sop(
        version_id: str,
        payload: SopTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _sop_action(service.sops.approve, admin, version_id, payload)

    @router.post("/sop-versions/{version_id}/activate")
    def activate_sop(
        version_id: str,
        payload: SopTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _sop_action(service.sops.activate, admin, version_id, payload)

    @router.post("/sop-versions/{version_id}/retire")
    def retire_sop(
        version_id: str,
        payload: SopTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _sop_action(service.sops.retire, admin, version_id, payload)

    @router.post("/sop-versions/{version_id}/rollback")
    def rollback_sop(
        version_id: str,
        payload: SopTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _sop_action(service.sops.rollback, admin, version_id, payload)

    @router.get("/sop-runs")
    def list_sop_runs(
        status: str | None = Query(
            default=None, pattern=r"^(active|completed|handoff|failed)$"
        ),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.sops.list_runs(admin.tenant_id, status=status, limit=limit)

    @router.get("/sop-runs/{run_id}")
    def get_sop_run(
        run_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.sops.get_run(admin.tenant_id, run_id)
        except SopError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/sop-runs/{run_id}/steps/{step_id}/resolve")
    def resolve_sop_step(
        run_id: str,
        step_id: str,
        payload: SopStepResolutionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.sops.resolve_step(
                admin.tenant_id, run_id, step_id, payload, admin.admin_id
            )
        except SopError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/sop-runs/{run_id}/steps/{step_id}/compensate")
    def compensate_sop_step(
        run_id: str,
        step_id: str,
        payload: SopCompensationRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.sops.compensate_step(
                admin.tenant_id, run_id, step_id, payload, admin.admin_id
            )
        except SopError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/qa/runs", status_code=201)
    def run_quality(
        payload: QualityRunRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.quality.run(admin.tenant_id, payload, admin.admin_id)
        except QualityError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/qa/results")
    def quality_results(
        review_status: str | None = Query(
            default=None, pattern=r"^(pending|confirmed|dismissed)$"
        ),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.quality.list_results(
            admin.tenant_id, review_status=review_status, limit=limit
        )

    @router.post("/qa/results/{result_id}/review")
    def review_quality(
        result_id: str,
        payload: QualityReviewRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.quality.review(
                admin.tenant_id, result_id, payload, admin.admin_id
            )
        except QualityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/voc/overview")
    def voc_overview(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.quality.summary(admin.tenant_id)

    return router


def _knowledge_action(action, admin, item_id, payload):
    try:
        return action(admin.tenant_id, item_id, payload, admin.admin_id)
    except KnowledgeLifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _sop_action(action, admin, version_id, payload):
    try:
        return action(admin.tenant_id, version_id, payload, admin.admin_id)
    except SopError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
