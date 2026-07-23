from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import (
    AdminOperatorCreateRequest,
    AdminOperatorStatusRequest,
    AdminPrincipal,
    AuthError,
)
from .service import AgentService


def build_admin_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/admin", tags=["admin-console"])

    @router.get("/overview")
    def overview(
        scope: str = Query(
            default="operational",
            pattern=r"^(operational|simulation|evaluation|all)$",
        ),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.admin.overview(admin.tenant_id, scope=scope)

    @router.get("/operators")
    def operators(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.auth.list_admin_operators(admin.tenant_id)

    @router.post("/operators", status_code=201)
    def create_operator(
        payload: AdminOperatorCreateRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.auth.create_admin_operator(
                admin.tenant_id, payload, admin.admin_id
            )
        except AuthError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/operators/{admin_id}/disable")
    def disable_operator(
        admin_id: str,
        payload: AdminOperatorStatusRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.auth.disable_admin_operator(
                admin.tenant_id, admin_id, payload, admin.admin_id
            )
        except AuthError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/conversations")
    def conversations(
        status: str | None = Query(default=None, pattern=r"^(active|closed)$"),
        query: str | None = Query(default=None, max_length=128),
        scope: str = Query(
            default="operational",
            pattern=r"^(operational|simulation|evaluation|all)$",
        ),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.admin.list_conversations(
            admin.tenant_id,
            status=status,
            query=query,
            scope=scope,
            limit=limit,
            offset=offset,
        )

    @router.get("/conversations/{session_id}")
    def conversation(
        session_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = service.admin.conversation(admin.tenant_id, session_id)
        if result is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return result

    @router.get("/context-snapshots/{snapshot_id}")
    def context_snapshot(
        snapshot_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = service.admin.context_snapshot(admin.tenant_id, snapshot_id)
        if result is None:
            raise HTTPException(status_code=404, detail="context snapshot not found")
        return result

    @router.get("/audit")
    def audit_events(
        event_type: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.admin.audit_events(
            admin.tenant_id, event_type=event_type, limit=limit
        )

    return router
