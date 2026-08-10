from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .auth import AdminPrincipal
from .service import AgentService
from .traffic_lab import (
    CreativeAssetCreate,
    ListingRevisionCreate,
    TrafficExperimentCreate,
    TrafficExperimentWindowCreate,
    TrafficLabError,
)


class TrafficMetricImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1, max_length=128)
    source_format: str = Field(pattern=r"^(csv|json)$")
    content: str = Field(min_length=1, max_length=2_000_000)
    source_timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=128)


class TrafficHypothesisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_run_id: str = Field(min_length=1, max_length=128)


def build_traffic_lab_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/traffic-lab", tags=["traffic-lab"])
    ingestion = service.operations.traffic_lab
    domain = ingestion.domain
    analysis_engine = service.operations.traffic_analysis

    def call(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except TrafficLabError as exc:
            status = 404 if str(exc).endswith("_not_found") else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def audit(event_type: str, admin: AdminPrincipal, target_id: str, detail) -> None:
        service.db.audit(event_type, admin.admin_id, target_id, detail, admin.tenant_id)

    @router.post("/assets")
    def register_asset(
        payload: CreativeAssetCreate,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = call(domain.register_asset, admin.tenant_id, payload)
        audit("traffic_lab.asset.registered", admin, str(result["asset_id"]), {
            "write_status": result["write_status"]
        })
        return result

    @router.post("/revisions")
    def create_revision(
        payload: ListingRevisionCreate,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = call(domain.create_revision, admin.tenant_id, payload)
        audit("traffic_lab.revision.created", admin, str(result["id"]), {
            "write_status": result["write_status"], "sku_id": result["sku_id"]
        })
        return result

    @router.get("/revisions")
    def list_revisions(
        connector_id: str | None = None,
        store_id: str | None = None,
        item_id: str | None = None,
        sku_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return domain.list_revisions(
            admin.tenant_id,
            connector_id=connector_id,
            store_id=store_id,
            item_id=item_id,
            sku_id=sku_id,
            limit=limit,
        )

    @router.post("/metrics/import")
    def import_metrics(
        payload: TrafficMetricImportRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = call(
            ingestion.import_metrics,
            admin.tenant_id,
            connector_id=payload.connector_id,
            source_format=payload.source_format,
            content=payload.content,
            source_timezone=payload.source_timezone,
        )
        counts = {key: result[key] for key in (
            "total_rows", "accepted_rows", "quarantined_rows", "rejected_rows"
        )}
        audit("traffic_lab.metrics.imported", admin, payload.connector_id, counts)
        return result

    @router.post("/experiments")
    def create_experiment(
        payload: TrafficExperimentCreate,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = call(domain.create_experiment, admin.tenant_id, payload)
        audit("traffic_lab.experiment.created", admin, str(result["experiment_id"]), {
            "sku_id": result["sku_id"], "experiment_type": result["experiment_type"]
        })
        return result

    @router.post("/experiments/{experiment_id}/windows")
    def add_experiment_window(
        experiment_id: str,
        payload: TrafficExperimentWindowCreate,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = call(
            domain.add_experiment_window,
            admin.tenant_id,
            experiment_id,
            payload,
        )
        audit("traffic_lab.experiment_window.recorded", admin, str(result["window_id"]), {
            "experiment_id": experiment_id, "write_status": result["write_status"]
        })
        return result

    @router.get("/experiments/{experiment_id}")
    def get_experiment(
        experiment_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        experiment = call(domain.get_experiment, admin.tenant_id, experiment_id)
        return {
            "experiment": experiment,
            "windows": domain.list_experiment_windows(admin.tenant_id, experiment_id),
            "window_quality": domain.experiment_window_quality(
                admin.tenant_id, experiment_id
            ),
            "analysis_runs": domain.list_analysis_runs(
                admin.tenant_id, experiment_id
            ),
        }

    @router.post("/experiments/{experiment_id}/analyze")
    def analyze_experiment(
        experiment_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = call(
            analysis_engine.analyze_experiment,
            admin.tenant_id,
            experiment_id,
        )
        audit("traffic_lab.analysis.completed", admin, str(result["analysis_run_id"]), {
            "experiment_id": experiment_id
        })
        return result

    @router.get("/experiments/{experiment_id}/analysis")
    def list_analysis(
        experiment_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        call(domain.get_experiment, admin.tenant_id, experiment_id)
        return domain.list_analysis_runs(
            admin.tenant_id, experiment_id, limit=limit
        )

    @router.get("/items/{sku_id}/insights")
    def listing_insights(
        sku_id: str,
        store_id: str | None = None,
        limit: int = Query(default=20, ge=1, le=100),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return domain.listing_traffic_insights(
            admin.tenant_id,
            sku_id,
            store_id=store_id,
            limit=limit,
        )

    @router.post("/hypotheses")
    def persisted_hypotheses(
        payload: TrafficHypothesisRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        run = call(
            domain.get_analysis_run,
            admin.tenant_id,
            payload.analysis_run_id,
        )
        return {
            "analysis_run_id": run["analysis_run_id"],
            "experiment_id": run["experiment_id"],
            "hypotheses": run["hypotheses"],
            "evidence": run["evidence"],
            "counter_evidence": run["counter_evidence"],
            "evidence_source": "traffic_analysis_runs",
            "statistics_recomputed": False,
            "platform_weight_claim": False,
        }

    return router
