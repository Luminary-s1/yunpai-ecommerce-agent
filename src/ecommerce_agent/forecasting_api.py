from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .auth import AdminPrincipal
from .forecasting import (
    DEMAND_V1,
    DemandFactRebuild,
    ForecastPolicy,
    ForecastRunError,
    InventoryPlanningError,
    InventoryPlanningPolicy,
    SUPPORTED_FORECAST_MODELS,
)
from .service import AgentService


class ForecastRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    warehouse_id: str | None = Field(default=None, min_length=1, max_length=128)


class ForecastPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_version: str = Field(default="forecast-v1", min_length=1, max_length=128)
    horizons: tuple[int, ...] = (7, 14, 30)
    minimum_history_days: int = Field(default=14, ge=1, le=3650)
    backtest_windows: int = Field(default=4, ge=1, le=100)
    required_relative_improvement: float = Field(default=0.02, ge=0, lt=1)
    interval_levels: tuple[float, float, float] = (0.5, 0.8, 0.95)
    candidate_models: tuple[str, ...] = SUPPORTED_FORECAST_MODELS

    def to_domain(self) -> ForecastPolicy:
        return ForecastPolicy(**self.model_dump())


class ForecastPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=128)
    forecast_policy: ForecastPolicyRequest = Field(default_factory=ForecastPolicyRequest)
    inventory_policy: InventoryPlanningPolicy = Field(
        default_factory=InventoryPlanningPolicy
    )


def build_forecasting_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/forecasting", tags=["forecasting"])
    facts = service.operations.forecasting
    runs = service.operations.forecast_runs
    plans = service.operations.inventory_plans

    def call(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (ForecastRunError, InventoryPlanningError) as exc:
            status_code = 404 if str(exc).endswith("_not_found") else 409
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @router.post("/demand/rebuild")
    def rebuild_demand(
        payload: DemandFactRebuild,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = facts.rebuild(admin.tenant_id, payload)
        service.db.audit(
            "forecasting.demand.rebuilt",
            admin.admin_id,
            payload.sku_id or payload.store_id,
            {
                "store_id": payload.store_id,
                "sku_id": payload.sku_id,
                "mode": payload.mode,
                "window": result["window"],
                "facts_written": result["facts_written"],
                "facts_idempotent": result["facts_idempotent"],
            },
            admin.tenant_id,
        )
        return result

    @router.get("/demand")
    def demand_history(
        store_id: str = Query(min_length=1, max_length=128),
        sku_id: str | None = Query(default=None, min_length=1, max_length=128),
        start_date: date | None = None,
        end_date: date | None = None,
        include_history: bool = False,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        rows = facts.list_facts(
            admin.tenant_id,
            store_id=store_id,
            sku_id=sku_id,
            start_date=start_date,
            end_date=end_date,
            include_history=include_history,
        )
        stockout = Counter(str(row["stockout_flag"]) for row in rows)
        issue_codes = sorted(
            {
                str(code)
                for row in rows
                for code in row.get("quality_flags", [])
            }
        )
        degraded_flags = {
            "data_coverage_missing",
            "source_coverage_unconfirmed",
            "stockout_unknown",
        }
        return {
            "store_id": store_id,
            "sku_id": sku_id,
            "demand_policy": DEMAND_V1.evidence(),
            "facts": rows,
            "quality_summary": {
                "fact_count": len(rows),
                "stockout_states": dict(stockout),
                "issue_codes": issue_codes,
                "degraded": bool(
                    degraded_flags.intersection(issue_codes)
                    or stockout.get("true")
                    or stockout.get("unknown")
                ),
            },
        }

    @router.put("/policies/{sku_id}")
    def configure_policies(
        sku_id: str,
        payload: ForecastPolicyUpdate,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            forecast_policy = payload.forecast_policy.to_domain()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            result = service.operations.configure_forecasting_policies(
                admin.tenant_id,
                store_id=payload.store_id,
                sku_id=sku_id,
                forecast_policy=forecast_policy,
                inventory_policy=payload.inventory_policy,
            )
        except (ForecastRunError, InventoryPlanningError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "forecasting.policies.configured",
            admin.admin_id,
            sku_id,
            {
                "store_id": payload.store_id,
                "forecast_policy_version": forecast_policy.policy_version,
                "planning_policy_version": payload.inventory_policy.policy_version,
                "write_status": {
                    "forecast": result["forecast_policy"]["write_status"],
                    "inventory": result["inventory_policy"]["write_status"],
                },
            },
            admin.tenant_id,
        )
        return result

    @router.post("/runs")
    def run_forecast(
        payload: ForecastRunRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        configured_forecast = call(
            runs.resolve_policy,
            admin.tenant_id,
            store_id=payload.store_id,
            sku_id=payload.sku_id,
        )
        forecast = call(
            runs.run,
            admin.tenant_id,
            store_id=payload.store_id,
            sku_id=payload.sku_id,
            policy=configured_forecast,
        )
        planning_policy = call(
            plans.resolve_policy,
            admin.tenant_id,
            store_id=payload.store_id,
            sku_id=payload.sku_id,
            warehouse_id=payload.warehouse_id,
        ) or InventoryPlanningPolicy(warehouse_id=payload.warehouse_id)
        try:
            inventory_plan = plans.create_plan(
                admin.tenant_id, forecast["run_id"], planning_policy
            )
            inventory_plan_status = "created"
            inventory_plan_error = None
        except InventoryPlanningError as exc:
            inventory_plan = None
            inventory_plan_status = "unavailable"
            inventory_plan_error = str(exc)
        service.db.audit(
            "forecasting.run.completed",
            admin.admin_id,
            forecast["run_id"],
            {
                "store_id": payload.store_id,
                "sku_id": payload.sku_id,
                "forecast_status": forecast["status"],
                "inventory_plan_status": inventory_plan_status,
                "inventory_plan_error": inventory_plan_error,
            },
            admin.tenant_id,
        )
        return {
            "forecast": forecast,
            "inventory_plan": inventory_plan,
            "inventory_plan_status": inventory_plan_status,
            "inventory_plan_error": inventory_plan_error,
        }

    @router.get("/runs/{run_id}")
    def get_run(
        run_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return call(runs.get_run, admin.tenant_id, run_id)

    @router.get("/skus/{sku_id}/forecast")
    def latest_forecast(
        sku_id: str,
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return call(
            runs.latest_run,
            admin.tenant_id,
            sku_id=sku_id,
            store_id=store_id,
        )

    @router.get("/skus/{sku_id}/backtest")
    def latest_backtest(
        sku_id: str,
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return call(
            runs.latest_backtest,
            admin.tenant_id,
            sku_id=sku_id,
            store_id=store_id,
        )

    @router.get("/skus/{sku_id}/inventory-plan")
    def latest_inventory_plan(
        sku_id: str,
        store_id: str = Query(min_length=1, max_length=128),
        warehouse_id: str | None = Query(default=None, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return call(
            plans.latest_plan,
            admin.tenant_id,
            sku_id=sku_id,
            store_id=store_id,
            warehouse_id=warehouse_id,
        )

    @router.get("/risks")
    def inventory_risks(
        store_id: str | None = Query(default=None, max_length=128),
        sku_id: str | None = Query(default=None, max_length=128),
        risk_level: str | None = Query(
            default=None, pattern=r"^(low|medium|high|critical)$"
        ),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return call(
            plans.list_risks,
            admin.tenant_id,
            store_id=store_id,
            sku_id=sku_id,
            risk_level=risk_level,
            limit=limit,
        )

    return router
