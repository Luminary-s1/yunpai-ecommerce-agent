from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import AdminPrincipal
from .business import (
    CatalogItemUpsert,
    CompetitiveAlertTransition,
    CompetitiveEntityMatchCreate,
    CompetitiveMatchTransition,
    CompetitiveMonitorUpsert,
    CompetitiveSignalCreate,
    ContentDraftUpsert,
    FinanceReportQuery,
    CompetitorObservationCreate,
    InventoryBalanceUpsert,
    MarketingDiagnosisQuery,
    MarketingPerformanceUpsert,
    MetricQuery,
    OperatingExpenseUpsert,
    OrderUpsert,
    ReconciliationTaskTransition,
    SettlementStatementUpsert,
)
from .connectors import ExternalAction
from .service import AgentService


class ConnectorSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: str = Field(min_length=1, max_length=64)
    cursor: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=100, ge=1, le=500)


def build_operations_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["operations"])

    @router.get("/modules")
    def list_modules(admin: AdminPrincipal = Depends(require_admin)) -> list[dict[str, Any]]:
        return service.operations.modules()

    @router.get("/connectors/catalog")
    def connector_catalog(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.operations.connector_catalog()

    @router.post("/connectors/{connector_id}/test")
    def test_connector(
        connector_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.operations.connectors.get(connector_id).test_connection().model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/connectors/{connector_id}/sync")
    def sync_connector(
        connector_id: str,
        payload: ConnectorSyncRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.operations.sync(
                tenant_id=admin.tenant_id,
                connector_id=connector_id,
                resource=payload.resource,
                cursor=payload.cursor,
                limit=payload.limit,
                actor=admin.admin_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/connectors/{connector_id}/actions")
    def execute_connector_action(
        connector_id: str,
        payload: ExternalAction,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            connector = service.operations.connectors.get(connector_id)
            result = connector.execute(payload)
            verification = connector.verify(payload, result)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "connector.virtual_action.executed",
            admin.admin_id,
            result.external_request_id,
            {
                "connector_id": connector_id,
                "action": payload.action,
                "dry_run": payload.dry_run,
                "verified": verification.verified,
            },
            admin.tenant_id,
        )
        return {"result": result.model_dump(), "verification": verification.model_dump()}

    @router.post("/connectors/{connector_id}/webhook")
    async def connector_webhook(connector_id: str, request: Request) -> dict[str, Any]:
        try:
            connector = service.operations.connectors.get(connector_id)
            headers = {key.lower(): value for key, value in request.headers.items()}
            event = connector.verify_webhook(headers, await request.body())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return event.model_dump()

    @router.post("/inventory/balances")
    def upsert_inventory_balance(
        payload: InventoryBalanceUpsert,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.inventory.upsert(admin.tenant_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "inventory.balance.upserted",
            admin.admin_id,
            result["id"],
            {"store_id": result["store_id"], "sku_id": result["sku_id"]},
            admin.tenant_id,
        )
        return result

    @router.post("/catalog/items")
    def upsert_catalog_item(
        payload: CatalogItemUpsert,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.catalog.upsert(admin.tenant_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "catalog.item.upserted",
            admin.admin_id,
            result["id"],
            {
                "store_id": result["store_id"],
                "sku_id": result["sku_id"],
                "write_status": result["write_status"],
            },
            admin.tenant_id,
        )
        return result

    @router.get("/catalog/items")
    def list_catalog_items(
        store_id: str | None = Query(default=None, max_length=128),
        sku_id: str | None = Query(default=None, max_length=128),
        status: str | None = Query(default=None, pattern=r"^(draft|active|inactive|deleted)$"),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.operations.catalog.list_items(
            admin.tenant_id,
            store_id=store_id,
            sku_id=sku_id,
            status=status,  # type: ignore[arg-type]
            limit=limit,
        )

    @router.post("/orders")
    def upsert_order(
        payload: OrderUpsert,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.orders.upsert(admin.tenant_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "order.fact.upserted",
            admin.admin_id,
            result["id"],
            {
                "store_id": result["store_id"],
                "order_id": result["order_id"],
                "write_status": result["write_status"],
            },
            admin.tenant_id,
        )
        return result

    @router.get("/orders")
    def list_orders(
        store_id: str | None = Query(default=None, max_length=128),
        order_id: str | None = Query(default=None, max_length=128),
        order_status: str | None = Query(
            default=None,
            pattern=r"^(created|paid|fulfilling|shipped|delivered|closed|canceled)$",
        ),
        scope: str = Query(
            default="operational",
            pattern=r"^(operational|simulation|evaluation|all)$",
        ),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.operations.orders.list_orders(
            admin.tenant_id,
            store_id=store_id,
            order_id=order_id,
            order_status=order_status,  # type: ignore[arg-type]
            limit=limit,
            service_scope=scope,
        )

    @router.get("/orders/{order_id}/history")
    def order_history(
        order_id: str,
        store_id: str | None = Query(default=None, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        try:
            return service.operations.orders.history(
                admin.tenant_id, order_id, store_id=store_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/marketing/performance")
    def upsert_marketing_performance(
        payload: MarketingPerformanceUpsert,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.marketing.upsert_performance(admin.tenant_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "marketing.performance.upserted",
            admin.admin_id,
            result["id"],
            {"store_id": result["store_id"], "campaign_id": result["campaign_id"], "write_status": result["write_status"]},
            admin.tenant_id,
        )
        return result

    @router.get("/marketing/performance")
    def list_marketing_performance(
        store_id: str | None = Query(default=None, max_length=128),
        start_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
        end_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
        limit: int = Query(default=300, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        try:
            query = MarketingDiagnosisQuery(
                store_id=store_id,
                start_date=start_date,
                end_date=end_date,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return service.operations.marketing.list_performance(
            admin.tenant_id,
            store_id=query.store_id,
            start_date=query.start_date,
            end_date=query.end_date,
            limit=limit,
        )

    @router.post("/marketing/diagnosis")
    def diagnose_marketing(
        payload: MarketingDiagnosisQuery,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.operations.marketing.diagnose(admin.tenant_id, payload)

    @router.post("/marketing/content-drafts")
    def save_marketing_content_draft(
        payload: ContentDraftUpsert,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.marketing.save_content_draft(admin.tenant_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "marketing.content_draft.saved",
            admin.admin_id,
            result["id"],
            {"draft_key": result["draft_key"], "fact_check": result["fact_check"]["status"]},
            admin.tenant_id,
        )
        return result

    @router.get("/marketing/content-drafts")
    def list_marketing_content_drafts(
        store_id: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.operations.marketing.list_content_drafts(
            admin.tenant_id, store_id=store_id, limit=limit
        )

    @router.post("/finance/expenses")
    def upsert_operating_expense(
        payload: OperatingExpenseUpsert,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.finance.upsert_expense(admin.tenant_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "finance.expense.upserted",
            admin.admin_id,
            result["id"],
            {"store_id": result["store_id"], "category": result["category"], "write_status": result["write_status"]},
            admin.tenant_id,
        )
        return result

    @router.get("/finance/expenses")
    def list_operating_expenses(
        store_id: str | None = Query(default=None, max_length=128),
        start_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
        end_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
        limit: int = Query(default=500, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        try:
            query = FinanceReportQuery(store_id=store_id, start_date=start_date, end_date=end_date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return service.operations.finance.list_expenses(
            admin.tenant_id,
            store_id=query.store_id,
            start_date=query.start_date,
            end_date=query.end_date,
            limit=limit,
        )

    @router.post("/finance/statements")
    def upsert_settlement_statement(
        payload: SettlementStatementUpsert,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.finance.upsert_statement(admin.tenant_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "finance.statement.upserted",
            admin.admin_id,
            result["id"],
            {"store_id": result["store_id"], "statement_key": result["statement_key"], "write_status": result["write_status"]},
            admin.tenant_id,
        )
        return result

    @router.get("/finance/statements")
    def list_settlement_statements(
        store_id: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.operations.finance.list_statements(
            admin.tenant_id, store_id=store_id, limit=limit
        )

    @router.post("/finance/profit")
    def report_management_profit(
        payload: FinanceReportQuery,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.operations.finance.profit_report(admin.tenant_id, payload)

    @router.post("/finance/reconciliation/run")
    def run_reconciliation(
        payload: FinanceReportQuery,
        tolerance_amount: float = Query(default=1.0, ge=0, le=1000000),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = service.operations.finance.run_reconciliation(
            admin.tenant_id,
            payload,
            tolerance_amount=Decimal(str(tolerance_amount)),
        )
        service.db.audit(
            "finance.reconciliation.run",
            admin.admin_id,
            "all",
            {"store_id": payload.store_id, "tasks_created": result["tasks_created"], "tasks_updated": result["tasks_updated"]},
            admin.tenant_id,
        )
        return result

    @router.get("/finance/reconciliation/tasks")
    def list_reconciliation_tasks(
        store_id: str | None = Query(default=None, max_length=128),
        status: str | None = Query(default=None, pattern=r"^(open|reviewing|resolved|ignored)$"),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.operations.finance.list_reconciliation_tasks(
            admin.tenant_id,
            store_id=store_id,
            status=status,  # type: ignore[arg-type]
            limit=limit,
        )

    @router.post("/finance/reconciliation/tasks/{task_id}/transition")
    def transition_reconciliation_task(
        task_id: str,
        payload: ReconciliationTaskTransition,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.operations.finance.transition_reconciliation_task(
                admin.tenant_id, task_id, payload, actor=admin.admin_id
            )
        except ValueError as exc:
            status_code = 404 if str(exc).endswith("not_found") else 409
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @router.get("/metrics/catalog")
    def metrics_catalog(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, str]]:
        return service.operations.metrics.catalog()

    @router.post("/metrics/query")
    def query_metric(
        payload: MetricQuery,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.operations.metrics.query(admin.tenant_id, payload)

    @router.get("/inventory/balances")
    def list_inventory_balances(
        store_id: str | None = Query(default=None, max_length=128),
        sku_id: str | None = Query(default=None, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.operations.inventory.list_balances(
            admin.tenant_id, store_id=store_id, sku_id=sku_id
        )

    @router.get("/inventory/risks")
    def inventory_risks(
        store_id: str | None = Query(default=None, max_length=128),
        sku_id: str | None = Query(default=None, max_length=128),
        reorder_lead_days: int = Query(default=7, ge=1, le=180),
        target_days: int = Query(default=30, ge=1, le=365),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.operations.inventory.risks(
            admin.tenant_id,
            store_id=store_id,
            sku_id=sku_id,
            reorder_lead_days=reorder_lead_days,
            target_days=target_days,
        )

    @router.post("/competitive/matches")
    def record_competitive_match(
        payload: CompetitiveEntityMatchCreate,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.competitive.record_entity_match(
                admin.tenant_id, payload
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "competitive.match.recorded",
            admin.admin_id,
            result["id"],
            {
                "store_id": result["store_id"],
                "subject_sku": result["subject_sku"],
                "competitor_sku": result["competitor_sku"],
                "score": result["score"],
                "recommended_status": result["recommended_status"],
                "write_status": result["write_status"],
            },
            admin.tenant_id,
        )
        return result

    @router.get("/competitive/matches")
    def list_competitive_matches(
        store_id: str | None = Query(default=None, max_length=128),
        subject_sku: str | None = Query(default=None, max_length=128),
        status: str | None = Query(
            default=None, pattern=r"^(pending|approved|rejected)$"
        ),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.operations.competitive.list_entity_matches(
            admin.tenant_id,
            store_id=store_id,
            subject_sku=subject_sku,
            status=status,  # type: ignore[arg-type]
            limit=limit,
        )

    @router.get("/competitive/matches/{match_id}")
    def get_competitive_match(
        match_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.operations.competitive.get_entity_match(
                admin.tenant_id, match_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/competitive/matches/{match_id}/transition")
    def transition_competitive_match(
        match_id: str,
        payload: CompetitiveMatchTransition,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.competitive.transition_entity_match(
                admin.tenant_id,
                match_id,
                payload,
                actor=admin.admin_id,
            )
        except ValueError as exc:
            status_code = 404 if str(exc).endswith("not_found") else 409
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        service.db.audit(
            f"competitive.match.{payload.target_status}",
            admin.admin_id,
            match_id,
            {
                "record_version": result["record_version"],
                "note": payload.note,
            },
            admin.tenant_id,
        )
        return result

    @router.post("/competitive/signals")
    def record_competitive_signal(
        payload: CompetitiveSignalCreate,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.competitive.record_signal(
                admin.tenant_id, payload
            )
        except ValueError as exc:
            status_code = 404 if str(exc).endswith("not_found") else 409
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        service.db.audit(
            "competitive.signal.recorded",
            admin.admin_id,
            result["id"],
            {
                "match_id": result["match_id"],
                "signal_type": result["signal_type"],
                "entity_role": result["entity_role"],
                "write_status": result["write_status"],
                "redacted": result["redacted"],
            },
            admin.tenant_id,
        )
        return result

    @router.get("/competitive/signals")
    def list_competitive_signals(
        store_id: str | None = Query(default=None, max_length=128),
        subject_sku: str | None = Query(default=None, max_length=128),
        signal_type: str | None = Query(
            default=None, pattern=r"^(product_claim|review_summary)$"
        ),
        eligible_only: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.operations.competitive.list_signals(
            admin.tenant_id,
            store_id=store_id,
            subject_sku=subject_sku,
            signal_type=signal_type,  # type: ignore[arg-type]
            eligible_only=eligible_only,
            limit=limit,
        )

    @router.get("/competitive/quality")
    def competitive_quality_overview(
        store_id: str | None = Query(default=None, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.operations.competitive.competitive_quality_overview(
            admin.tenant_id, store_id=store_id
        )

    @router.post("/competitive/observations")
    def record_competitor_observation(
        payload: CompetitorObservationCreate,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.competitive.record(admin.tenant_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "competitive.observation.recorded",
            admin.admin_id,
            result["id"],
            {
                "subject_sku": result["subject_sku"],
                "source_type": result["source_type"],
                "is_estimate": result["is_estimate"],
            },
            admin.tenant_id,
        )
        return result

    @router.get("/competitive/observations")
    def list_competitor_observations(
        subject_sku: str | None = Query(default=None, max_length=128),
        store_id: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.operations.competitive.list_observations(
            admin.tenant_id,
            subject_sku=subject_sku,
            store_id=store_id,
            limit=limit,
        )

    @router.put("/competitive/monitors")
    def upsert_competitive_monitor(
        payload: CompetitiveMonitorUpsert,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.competitive.upsert_monitor(
                admin.tenant_id, payload, actor=admin.admin_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "competitive.monitor.upserted",
            admin.admin_id,
            result["id"],
            {
                "store_id": result["store_id"],
                "subject_sku": result["subject_sku"],
                "enabled": result["enabled"],
                "record_version": result["record_version"],
            },
            admin.tenant_id,
        )
        return result

    @router.get("/competitive/monitors")
    def list_competitive_monitors(
        store_id: str | None = Query(default=None, max_length=128),
        subject_sku: str | None = Query(default=None, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.operations.competitive.list_monitors(
            admin.tenant_id, store_id=store_id, subject_sku=subject_sku
        )

    @router.post("/competitive/monitors/evaluate-all")
    def evaluate_all_competitive_monitors(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = service.operations.competitive.evaluate_all(admin.tenant_id)
        service.db.audit(
            "competitive.monitors.evaluated",
            admin.admin_id,
            "all",
            {
                "evaluated": result["evaluated"],
                "created": result["created"],
                "updated": result["updated"],
                "auto_resolved": result["auto_resolved"],
            },
            admin.tenant_id,
        )
        return result

    @router.post("/competitive/monitors/{monitor_id}/evaluate")
    def evaluate_competitive_monitor(
        monitor_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.competitive.evaluate_monitor(
                admin.tenant_id, monitor_id
            )
        except ValueError as exc:
            status_code = 404 if str(exc).endswith("not_found") else 409
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        service.db.audit(
            "competitive.monitor.evaluated",
            admin.admin_id,
            monitor_id,
            {
                "created": result["created"],
                "updated": result["updated"],
                "auto_resolved": result["auto_resolved"],
                "active_alerts": result["active_alerts"],
            },
            admin.tenant_id,
        )
        return result

    @router.get("/competitive/alerts")
    def list_competitive_alerts(
        store_id: str | None = Query(default=None, max_length=128),
        subject_sku: str | None = Query(default=None, max_length=128),
        status: str | None = Query(
            default=None, pattern=r"^(open|acknowledged|resolved)$"
        ),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.operations.competitive.list_alerts(
            admin.tenant_id,
            store_id=store_id,
            subject_sku=subject_sku,
            status=status,  # type: ignore[arg-type]
            limit=limit,
        )

    @router.post("/competitive/alerts/{alert_id}/transition")
    def transition_competitive_alert(
        alert_id: str,
        payload: CompetitiveAlertTransition,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.competitive.transition_alert(
                admin.tenant_id, alert_id, payload, actor=admin.admin_id
            )
        except ValueError as exc:
            status_code = 404 if str(exc).endswith("not_found") else 409
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        service.db.audit(
            f"competitive.alert.{payload.target_status}",
            admin.admin_id,
            alert_id,
            {
                "subject_sku": result["subject_sku"],
                "alert_code": result["alert_code"],
                "record_version": result["record_version"],
            },
            admin.tenant_id,
        )
        return result

    @router.get("/competitive/overview")
    def competitive_overview(
        store_id: str | None = Query(default=None, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.operations.competitive.overview(
            admin.tenant_id, store_id=store_id
        )

    @router.get("/competitive/analysis")
    def competitive_analysis(
        subject_sku: str = Query(min_length=1, max_length=128),
        store_id: str | None = Query(default=None, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.operations.competitive.analyze_prices(
            admin.tenant_id, subject_sku, store_id=store_id
        )

    return router
