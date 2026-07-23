from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from fastapi import APIRouter, Depends

from .auth import AdminPrincipal
from .simulation import VirtualStoreSimulation, VirtualStoreSimulationRequest

if TYPE_CHECKING:
    from .service import AgentService


def build_simulation_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/simulations", tags=["simulations"])

    @router.get("/virtual-store")
    def virtual_store_fixture(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        del admin
        return VirtualStoreSimulation.fixture_summary()

    @router.post("/virtual-store/run")
    def run_virtual_store_simulation(
        payload: VirtualStoreSimulationRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return VirtualStoreSimulation(service).run(
            tenant_id=admin.tenant_id,
            actor=admin.admin_id,
            include_customer_service=payload.include_customer_service,
        )

    return router
