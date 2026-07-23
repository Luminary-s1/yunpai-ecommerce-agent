from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .auth import AdminPrincipal
from .releases import (
    ReleaseError,
    ReleasePolicyCreateRequest,
    ReleaseReplayRequest,
    ReleaseTransitionRequest,
)
from .service import AgentService


def build_release_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/admin/releases", tags=["release-gates"])

    def invoke(method, admin: AdminPrincipal, *args):
        try:
            return method(admin.tenant_id, *args, admin.admin_id)
        except ReleaseError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("")
    def list_releases(
        release_status: str | None = Query(
            default=None,
            alias="status",
            pattern=r"^(draft|evaluated|approved|active|paused|retired)$",
        ),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict]:
        return service.releases.list_policies(
            admin.tenant_id, status=release_status
        )

    @router.post("", status_code=status.HTTP_201_CREATED)
    def create_release(
        payload: ReleasePolicyCreateRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return invoke(service.releases.create, admin, payload)

    @router.get("/assignment")
    def assignment(
        platform: str = Query(min_length=2, max_length=32),
        store_id: str = Query(min_length=1, max_length=128),
        conversation_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return service.releases.assignment(
            admin.tenant_id, platform, store_id, conversation_id
        )

    @router.get("/{release_id}")
    def get_release(
        release_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            return service.releases.get_policy(admin.tenant_id, release_id)
        except ReleaseError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/{release_id}/replay")
    def replay_release(
        release_id: str,
        payload: ReleaseReplayRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return invoke(service.run_release_replay, admin, release_id, payload)

    @router.post("/{release_id}/approve")
    def approve_release(
        release_id: str,
        payload: ReleaseTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return invoke(service.releases.approve, admin, release_id, payload)

    @router.post("/{release_id}/activate")
    def activate_release(
        release_id: str,
        payload: ReleaseTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return invoke(service.releases.activate, admin, release_id, payload)

    @router.post("/{release_id}/pause")
    def pause_release(
        release_id: str,
        payload: ReleaseTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return invoke(service.releases.pause, admin, release_id, payload)

    @router.post("/{release_id}/retire")
    def retire_release(
        release_id: str,
        payload: ReleaseTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return invoke(service.releases.retire, admin, release_id, payload)

    @router.get("/{release_id}/runtime")
    def release_runtime(
        release_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            return service.releases.runtime_summary(admin.tenant_id, release_id)
        except ReleaseError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/{release_id}/observations")
    def release_observations(
        release_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict]:
        try:
            return service.releases.list_observations(
                admin.tenant_id, release_id, limit=limit
            )
        except ReleaseError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
