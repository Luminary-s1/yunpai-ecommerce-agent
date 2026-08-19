from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import AdminPrincipal
from .readonly_data import DataScope
from .readonly_readiness import ReadonlyDemoLoadRequest, ReadonlyDemoService

if TYPE_CHECKING:
    from .service import AgentService


ReadonlyStoreIdQuery = Annotated[
    str,
    Query(min_length=1, max_length=128, pattern=r"^\S(?:.*\S)?$"),
]


def build_readonly_data_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/readonly-data", tags=["readonly-data"])

    @router.get("/readiness")
    def readiness(
        store_id: ReadonlyStoreIdQuery,
        scope: DataScope = DataScope.OPERATIONAL,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.readonly_readiness.project(
            admin.tenant_id,
            store_id=store_id,
            scope=scope,
        )

    @router.get("/imports")
    def imports(
        store_id: ReadonlyStoreIdQuery,
        scope: DataScope = DataScope.OPERATIONAL,
        limit: int = Query(default=100, ge=1, le=1000),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return {
            "store_id": store_id,
            "scope": scope.value,
            "items": service.readonly_data.list_imports(
                admin.tenant_id,
                store_id=store_id,
                scope=scope,
                limit=limit,
            ),
        }

    @router.get("/row-issues")
    def row_issues(
        store_id: ReadonlyStoreIdQuery,
        import_id: str | None = Query(default=None, min_length=1, max_length=128),
        scope: DataScope = DataScope.OPERATIONAL,
        limit: int = Query(default=100, ge=1, le=1000),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return {
            "store_id": store_id,
            "scope": scope.value,
            "items": service.readonly_data.list_row_issues(
                admin.tenant_id,
                store_id=store_id,
                import_id=import_id,
                scope=scope,
                limit=limit,
            ),
        }

    @router.get("/field-evidence")
    def field_evidence(
        store_id: ReadonlyStoreIdQuery,
        scope: DataScope = DataScope.OPERATIONAL,
        limit: int = Query(default=100, ge=1, le=1000),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return {
            "store_id": store_id,
            "scope": scope.value,
            "items": service.readonly_data.list_field_evidence(
                admin.tenant_id,
                store_id=store_id,
                scope=scope,
                limit=limit,
            ),
        }

    @router.get("/mappings")
    def mappings(
        store_id: ReadonlyStoreIdQuery,
        scope: DataScope = DataScope.OPERATIONAL,
        latest_only: bool = True,
        limit: int = Query(default=100, ge=1, le=1000),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return {
            "store_id": store_id,
            "scope": scope.value,
            "latest_only": latest_only,
            "items": service.product_identity.list_mappings(
                admin.tenant_id,
                store_id=store_id,
                scope=scope,
                latest_only=latest_only,
                limit=limit,
            ),
        }

    @router.get("/reconciliations")
    def reconciliations(
        store_id: ReadonlyStoreIdQuery,
        scope: DataScope = DataScope.OPERATIONAL,
        limit: int = Query(default=100, ge=1, le=1000),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return {
            "store_id": store_id,
            "scope": scope.value,
            "items": service.product_identity.list_reconciliations(
                admin.tenant_id,
                store_id=store_id,
                scope=scope,
                limit=limit,
            ),
        }

    @router.get("/demo")
    def demo_fixture(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        del admin
        return ReadonlyDemoService.fixture_summary()

    @router.post("/demo/load")
    def load_demo(
        payload: ReadonlyDemoLoadRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.readonly_demo.load(
                admin.tenant_id,
                payload,
                actor=admin.admin_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "readonly_data.demo.loaded",
            admin.admin_id,
            payload.store_id,
            {
                "fixture_id": result["fixture_id"],
                "reports_applied": result["summary"]["reports_applied"],
                "reports_idempotent": result["summary"]["reports_idempotent"],
            },
            admin.tenant_id,
        )
        return result

    return router


__all__ = ["build_readonly_data_router"]
