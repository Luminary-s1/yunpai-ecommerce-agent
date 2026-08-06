from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .auth import AdminPrincipal
from .business import (
    CopywritingRegenerateRequest,
    CopywritingRequest,
    OpsOperationRecordUpsert,
    OpsReportQuery,
)
from .service import AgentService


def build_ops_assistant_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/ops-assistant", tags=["ops-assistant"])

    @router.post("/datasets/import")
    async def import_operations_dataset(
        request: Request,
        dataset_key: str = Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"),
        store_id: str = Query(min_length=1, max_length=128),
        source_format: Literal["csv", "json"] = Query(),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        raw = await request.body()
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=422, detail="ops_dataset_not_utf8") from exc
        try:
            result = service.operations.ops_assistant.parse_dataset(
                admin.tenant_id,
                dataset_key=dataset_key,
                store_id=store_id,
                source_format=source_format,
                content=content,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        service.db.audit(
            "ops.dataset.imported",
            admin.admin_id,
            dataset_key,
            {
                "store_id": store_id,
                "source_format": source_format,
                "total_rows": result["total_rows"],
                "accepted_rows": result["accepted_rows"],
                "rejected_rows": result["rejected_rows"],
            },
            admin.tenant_id,
        )
        return result

    @router.post("/records")
    def upsert_operations_record(
        payload: OpsOperationRecordUpsert,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.ops_assistant.upsert_record(admin.tenant_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "ops.record.upserted",
            admin.admin_id,
            result["id"],
            {
                "dataset_key": result["dataset_key"],
                "record_date": result["record_date"],
                "channel": result["channel"],
                "write_status": result["write_status"],
            },
            admin.tenant_id,
        )
        return result

    @router.get("/records")
    def list_operations_records(
        dataset_key: str | None = Query(default=None, max_length=128),
        store_id: str | None = Query(default=None, max_length=128),
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.operations.ops_assistant.list_records(
            admin.tenant_id,
            dataset_key=dataset_key,
            store_id=store_id,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )

    @router.post("/copywriting/generate")
    def generate_marketing_copy(
        payload: CopywritingRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = service.operations.ops_assistant.generate_copy(admin.tenant_id, payload)
        service.db.audit(
            "ops.copywriting.generated",
            admin.admin_id,
            payload.product_name,
            {
                "store_id": payload.store_id,
                "styles": list(payload.styles),
                "length": payload.length,
                "batch_size": result["batch_size"],
                "needs_review": any(item["needs_review"] for item in result["variants"]),
            },
            admin.tenant_id,
        )
        return result

    @router.post("/copywriting/regenerate")
    def regenerate_marketing_copy(
        payload: CopywritingRegenerateRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = service.operations.ops_assistant.regenerate_copy(admin.tenant_id, payload)
        service.db.audit(
            "ops.copywriting.regenerated",
            admin.admin_id,
            payload.product_name,
            {
                "store_id": payload.store_id,
                "styles": list(payload.styles),
                "length": payload.length,
                "batch_size": result["batch_size"],
                "needs_review": any(item["needs_review"] for item in result["variants"]),
            },
            admin.tenant_id,
        )
        return result

    @router.post("/reports/analysis")
    def generate_analysis_report(
        payload: OpsReportQuery,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = service.operations.ops_assistant.analysis_report(admin.tenant_id, payload)
        service.db.audit(
            "ops.report.generated",
            admin.admin_id,
            payload.dataset_key or payload.store_id or "all",
            {
                "dataset_key": payload.dataset_key,
                "store_id": payload.store_id,
                "record_count": result["data_quality"]["record_count"],
                "finding_codes": [item["code"] for item in result["findings"]],
                "narrative_generator": result["narrative_generator"],
            },
            admin.tenant_id,
        )
        return result

    return router
