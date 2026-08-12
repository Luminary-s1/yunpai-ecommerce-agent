from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..connectors import ConnectorRegistry, PullRequest, VirtualTaobaoConnector
from ..database import Database, utc_now
from ..forecasting import DemandFactService, ForecastRunService, InventoryPlanningService
from ..tools import ToolExecutionContext, ToolRegistry, ToolResult, ToolSpec
from .catalog import CatalogItemUpsert, CatalogService, CatalogStatus
from .competitive import CompetitiveIntelligenceService, CompetitorObservationCreate
from .competitive_report import CompetitiveReportService
from .finance import FinanceReportQuery, FinanceService
from .inventory import InventoryBalanceUpsert, InventoryService
from .marketing import MarketingDiagnosisQuery, MarketingService
from .metrics import MetricQuery, MetricsService
from .ops_assistant import OpsAssistantService
from .orders import OrderService, OrderUpsert
from .registry import business_module_catalog


class InventoryRiskToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku_id: str = Field(min_length=1, max_length=128)
    store_id: str | None = Field(default=None, max_length=128)
    reorder_lead_days: int = Field(default=7, ge=1, le=180)
    target_days: int = Field(default=30, ge=1, le=365)


class CompetitorPriceToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_sku: str = Field(min_length=1, max_length=128)
    store_id: str | None = Field(default=None, max_length=128)


class ProductFactsToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku_id: str = Field(min_length=1, max_length=128)
    store_id: str | None = Field(default=None, max_length=128)


class ProductSearchToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: str = Field(min_length=1, max_length=200)
    store_id: str | None = Field(default=None, max_length=128)
    status: CatalogStatus | None = None
    limit: int = Field(default=5, ge=1, le=20)


class OrderFactsToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(min_length=1, max_length=128)
    store_id: str = Field(min_length=1, max_length=128)


class ListingTrafficInsightsToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku_id: str = Field(min_length=1, max_length=128)
    store_id: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=20, ge=1, le=100)


class OperationsService:
    def __init__(self, db: Database):
        from ..traffic_lab import TrafficAnalysisEngine, TrafficLabIngestionService

        self.db = db
        self.catalog = CatalogService(db)
        self.orders = OrderService(db)
        self.inventory = InventoryService(db)
        self.forecasting = DemandFactService(
            db, orders=self.orders, inventory=self.inventory
        )
        self.forecast_runs = ForecastRunService(db, facts=self.forecasting)
        self.inventory_plans = InventoryPlanningService(
            db, forecasts=self.forecast_runs, inventory=self.inventory
        )
        self.competitive = CompetitiveIntelligenceService(db)
        self.competitive_report = CompetitiveReportService(db)
        self.marketing = MarketingService(db)
        self.finance = FinanceService(db)
        self.ops_assistant = OpsAssistantService(db)
        self.traffic_lab = TrafficLabIngestionService(db)
        self.traffic_analysis = TrafficAnalysisEngine(db)
        self.metrics = MetricsService(db, self.inventory)
        self.connectors = ConnectorRegistry()
        self.connectors.register(VirtualTaobaoConnector())

    def modules(self) -> list[dict[str, Any]]:
        return [item.model_dump() for item in business_module_catalog()]

    def connector_catalog(self) -> list[dict[str, Any]]:
        return [item.model_dump() for item in self.connectors.catalog()]

    def sync(
        self,
        *,
        tenant_id: str,
        connector_id: str,
        resource: str,
        cursor: str | None = None,
        limit: int = 100,
        actor: str = "system",
    ) -> dict[str, Any]:
        connector = self.connectors.get(connector_id)
        capabilities = connector.capabilities()
        if resource not in capabilities.resources:
            raise ValueError(f"resource not supported by {connector_id}: {resource}")
        run_id = f"sync-{uuid.uuid4().hex}"
        started_at = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO connector_sync_runs(
                    id, tenant_id, connector_id, resource, status, cursor_before,
                    cursor_after, items_received, items_applied, data_as_of,
                    error, created_at, completed_at
                ) VALUES (?, ?, ?, ?, 'running', ?, NULL, 0, 0, NULL, NULL, ?, NULL)
                """,
                (run_id, tenant_id, connector_id, resource, cursor, started_at),
            )
        try:
            batch = connector.pull(PullRequest(resource=resource, cursor=cursor, limit=limit))
            applied = 0
            idempotent = 0
            quarantined = 0
            receipts: list[dict[str, Any]] = []
            for record in batch.records:
                if resource == "catalog":
                    result = self.catalog.upsert(
                        tenant_id,
                        CatalogItemUpsert(
                            connector_id=connector_id,
                            source_updated_at=record.source_version,
                            source_id=record.source_id,
                            **record.payload,
                        ),
                    )
                elif resource == "orders":
                    result = self.orders.upsert(
                        tenant_id,
                        OrderUpsert(
                            connector_id=connector_id,
                            source_updated_at=record.source_version,
                            source_id=record.source_id,
                            **record.payload,
                        ),
                    )
                elif resource == "inventory":
                    result = self.inventory.upsert(
                        tenant_id,
                        InventoryBalanceUpsert(
                            connector_id=connector_id,
                            source_updated_at=record.source_version,
                            source_id=record.source_id,
                            **record.payload,
                        ),
                    )
                elif resource == "competitor_price":
                    result = self.competitive.record(
                        tenant_id,
                        CompetitorObservationCreate(
                            connector_id=connector_id,
                            store_id=str(record.payload.get("store_id") or "virtual-shop-001"),
                            observed_at=record.occurred_at,
                            source_id=record.source_id,
                            **record.payload,
                        ),
                    )
                elif resource == "listing_revision":
                    result = self.traffic_lab.ingest_listing_revision_record(
                        tenant_id,
                        connector_id=connector_id,
                        record=record,
                    )
                    receipts.append(result["receipt"])
                elif resource == "traffic_metrics":
                    result = self.traffic_lab.ingest_metric_record(
                        tenant_id,
                        connector_id=connector_id,
                        record=record,
                    )
                else:
                    raise ValueError(f"no normalizer is implemented for resource: {resource}")
                applied += int(result["write_status"] == "applied")
                idempotent += int(result["write_status"] == "idempotent")
                quarantined += int(result.get("disposition") == "quarantined")
        except Exception as exc:
            with self.db._write_lock, self.db.connect() as conn:
                conn.execute(
                    """
                    UPDATE connector_sync_runs
                    SET status='failed', error=?, completed_at=? WHERE id=?
                    """,
                    (str(exc)[:500], utc_now(), run_id),
                )
            self.db.audit(
                "connector.sync.failed",
                actor,
                run_id,
                {"connector_id": connector_id, "resource": resource, "error_type": type(exc).__name__},
                tenant_id,
            )
            raise
        completed_at = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                UPDATE connector_sync_runs
                SET status='succeeded', cursor_after=?, items_received=?,
                    items_applied=?, data_as_of=?, completed_at=? WHERE id=?
                """,
                (
                    batch.next_cursor,
                    len(batch.records),
                    applied,
                    batch.data_as_of,
                    completed_at,
                    run_id,
                ),
            )
        self.db.audit(
            "connector.sync.succeeded",
            actor,
            run_id,
            {
                "connector_id": connector_id,
                "virtual": capabilities.virtual,
                "resource": resource,
                "items_received": len(batch.records),
                "items_applied": applied,
                "items_idempotent": idempotent,
                "items_quarantined": quarantined,
            },
            tenant_id,
        )
        return {
            "run_id": run_id,
            "connector_id": connector_id,
            "virtual": capabilities.virtual,
            "resource": resource,
            "status": "succeeded",
            "items_received": len(batch.records),
            "items_applied": applied,
            "items_idempotent": idempotent,
            "items_quarantined": quarantined,
            "receipts": receipts,
            "next_cursor": batch.next_cursor,
            "has_more": batch.has_more,
            "data_as_of": batch.data_as_of,
        }

    def register_agent_tools(self, registry: ToolRegistry) -> None:
        if registry.get("search_products") is None:
            registry.register(
                ToolSpec(
                    name="search_products",
                    description=(
                        "按顾客的自然语言描述（商品名称、品类、型号、颜色、容量等）检索本店在售商品，"
                        "返回候选 SKU、售价、状态和属性。顾客不知道 SKU 编号时必须先用这个工具解析，"
                        "不要向顾客索要 SKU"
                    ),
                    kind="read",
                    input_model=ProductSearchToolInput,
                    handler=self._product_search_tool,
                    policy=self._catalog_store_scope_policy,
                    metadata={"domain": "catalog", "risk_level": "L0"},
                )
            )
        if registry.get("get_product_facts") is None:
            registry.register(
                ToolSpec(
                    name="get_product_facts",
                    description=(
                        "查询当前租户指定 SKU 的商品主数据、售价、状态和来源版本；"
                        "只接受已知的精确 SKU，顾客只描述商品时先用 search_products 解析"
                    ),
                    kind="read",
                    input_model=ProductFactsToolInput,
                    handler=self._product_facts_tool,
                    metadata={"domain": "catalog", "risk_level": "L0"},
                )
            )
        if registry.get("get_order_facts") is None:
            registry.register(
                ToolSpec(
                    name="get_order_facts",
                    description="查询已由可信上游绑定的订单、物流和售后事实",
                    kind="read",
                    input_model=OrderFactsToolInput,
                    handler=self._order_facts_tool,
                    required_context_fields=("authorized", "order_id", "shop_id"),
                    policy=self._order_scope_policy,
                    metadata={"domain": "orders", "risk_level": "L1"},
                )
            )
        if registry.get("get_business_metric") is None:
            registry.register(
                ToolSpec(
                    name="get_business_metric",
                    description="通过固定指标定义查询经营数据，不接受 SQL 或任意表达式",
                    kind="read",
                    input_model=MetricQuery,
                    handler=self._metric_tool,
                    metadata={"domain": "metrics", "risk_level": "L0"},
                )
            )
        if registry.get("get_inventory_risk") is None:
            registry.register(
                ToolSpec(
                    name="get_inventory_risk",
                    description="查询当前租户指定 SKU 的库存、缺货、滞销和补货风险",
                    kind="read",
                    input_model=InventoryRiskToolInput,
                    handler=self._inventory_risk_tool,
                    metadata={"domain": "inventory", "risk_level": "L0"},
                )
            )
        if registry.get("get_competitor_price_analysis") is None:
            registry.register(
                ToolSpec(
                    name="get_competitor_price_analysis",
                    description="查询已批准同款匹配下的竞品价格差；未批准证据只计入质量门禁，不进入模型上下文",
                    kind="read",
                    input_model=CompetitorPriceToolInput,
                    handler=self._competitor_price_tool,
                    metadata={"domain": "competitive_intelligence", "risk_level": "L0"},
                )
            )
        if registry.get("get_competitive_intelligence") is None:
            registry.register(
                ToolSpec(
                    name="get_competitive_intelligence",
                    description="查询已批准同款匹配下的价格、商品卖点与聚合口碑证据，不返回评论者或原始评论",
                    kind="read",
                    input_model=CompetitorPriceToolInput,
                    handler=self._competitor_price_tool,
                    metadata={"domain": "competitive_intelligence", "risk_level": "L0"},
                )
            )
        if registry.get("get_marketing_diagnosis") is None:
            registry.register(
                ToolSpec(
                    name="get_marketing_diagnosis",
                    description="Read archived campaign metrics and return diagnoses without bids, budget changes, or publication.",
                    kind="read",
                    input_model=MarketingDiagnosisQuery,
                    handler=self._marketing_diagnosis_tool,
                    metadata={"domain": "marketing", "risk_level": "L0"},
                )
            )
        if registry.get("get_profit_reconciliation") is None:
            registry.register(
                ToolSpec(
                    name="get_profit_reconciliation",
                    description="Read management profit and reconciliation tasks without changing accounting or funds.",
                    kind="read",
                    input_model=FinanceReportQuery,
                    handler=self._profit_reconciliation_tool,
                    metadata={"domain": "finance", "risk_level": "L0"},
                )
            )
        if registry.get("get_listing_traffic_insights") is None:
            registry.register(
                ToolSpec(
                    name="get_listing_traffic_insights",
                    description=(
                        "读取指定 SKU 已固化的流量实验统计证据、区间、滞后分析与反证；"
                        "不重算统计，不代表平台权重或因果机制"
                    ),
                    kind="read",
                    input_model=ListingTrafficInsightsToolInput,
                    handler=self._listing_traffic_insights_tool,
                    policy=self._catalog_store_scope_policy,
                    metadata={"domain": "traffic_lab", "risk_level": "L0"},
                )
            )

    def _inventory_risk_tool(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        value = InventoryRiskToolInput.model_validate(arguments.model_dump())
        risks = self.inventory.risks(
            context.tenant_id,
            store_id=value.store_id,
            sku_id=value.sku_id,
            reorder_lead_days=value.reorder_lead_days,
            target_days=value.target_days,
        )
        return ToolResult(status="success", output={"risks": risks})

    def _product_facts_tool(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        value = ProductFactsToolInput.model_validate(arguments.model_dump())
        items = self.catalog.list_items(
            context.tenant_id, store_id=value.store_id, sku_id=value.sku_id
        )
        return ToolResult(status="success", output={"items": items})

    @staticmethod
    def _trusted_store_id(context: ToolExecutionContext) -> str | None:
        trusted = context.trusted_context
        return str(trusted.get("store_id") or trusted.get("shop_id") or "") or None

    @classmethod
    def _catalog_store_scope_policy(
        cls, arguments: BaseModel, context: ToolExecutionContext
    ) -> str | None:
        payload = arguments.model_dump()
        trusted_store = cls._trusted_store_id(context)
        requested_store = payload.get("store_id")
        if trusted_store and requested_store and str(requested_store) != trusted_store:
            return "store_scope_mismatch"
        return None

    def _product_search_tool(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        value = ProductSearchToolInput.model_validate(arguments.model_dump())
        # The customer is talking to one shop; keep the lookup inside it unless the
        # caller has no trusted shop at all.
        store_id = value.store_id or self._trusted_store_id(context)
        items = self.catalog.search_items(
            context.tenant_id,
            keyword=value.keyword,
            store_id=store_id,
            status=value.status,
            limit=value.limit,
        )
        resolution = (
            "no_match" if not items else "resolved" if len(items) == 1 else "ambiguous"
        )
        return ToolResult(
            status="success",
            output={
                "keyword": value.keyword,
                "store_id": store_id,
                "resolution": resolution,
                "match_count": len(items),
                "items": items,
            },
        )

    @staticmethod
    def _order_scope_policy(arguments: BaseModel, context: ToolExecutionContext) -> str | None:
        value = OrderFactsToolInput.model_validate(arguments.model_dump())
        if value.order_id != str(context.trusted_context.get("order_id") or ""):
            return "order_scope_mismatch"
        if value.store_id != str(context.trusted_context.get("shop_id") or ""):
            return "store_scope_mismatch"
        return None

    def _order_facts_tool(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        value = OrderFactsToolInput.model_validate(arguments.model_dump())
        orders = self.orders.list_orders(
            context.tenant_id,
            store_id=value.store_id,
            order_id=value.order_id,
        )
        return ToolResult(status="success", output={"orders": orders})

    def _metric_tool(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        value = MetricQuery.model_validate(arguments.model_dump())
        return ToolResult(status="success", output=self.metrics.query(context.tenant_id, value))

    def _competitor_price_tool(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        value = CompetitorPriceToolInput.model_validate(arguments.model_dump())
        analysis = self.competitive.analyze_prices(
            context.tenant_id,
            value.subject_sku,
            store_id=value.store_id,
        )
        safe_observations = [
            item for item in analysis["observations"] if item.get("actionable")
        ]
        safe_observation_ids = {
            str(item["evidence"]["observation_id"]) for item in safe_observations
        }
        safe_trends = []
        for trend in analysis["trends"]:
            points = [item for item in trend["points"] if item.get("actionable")]
            if points:
                safe_trends.append({**trend, "points": points})
        safe_alerts = [
            item
            for item in analysis["alerts"]
            if (
                item.get("observation_id") in safe_observation_ids
                or (
                    item.get("alert_code") == "data_stale"
                    and item.get("competitor_sku") == "__monitor__"
                )
            )
        ]
        output = {
            **analysis,
            "observations": safe_observations,
            "trends": safe_trends,
            "alerts": safe_alerts,
            "quality_gate": {
                "approved_match_required": True,
                "eligible_competitors": len(safe_observations),
                "excluded_unverified_competitors": analysis["summary"].get(
                    "unverified_competitors", 0
                ),
            },
        }
        return ToolResult(status="success", output=output)

    def _marketing_diagnosis_tool(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        value = MarketingDiagnosisQuery.model_validate(arguments.model_dump())
        return ToolResult(status="success", output=self.marketing.diagnose(context.tenant_id, value))

    def _profit_reconciliation_tool(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        value = FinanceReportQuery.model_validate(arguments.model_dump())
        return ToolResult(
            status="success",
            output={
                "profit": self.finance.profit_report(context.tenant_id, value),
                "reconciliation_tasks": self.finance.list_reconciliation_tasks(
                    context.tenant_id, store_id=value.store_id
                ),
            },
        )

    def _listing_traffic_insights_tool(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        value = ListingTrafficInsightsToolInput.model_validate(arguments.model_dump())
        store_id = value.store_id or self._trusted_store_id(context)
        output = self.traffic_lab.domain.listing_traffic_insights(
            context.tenant_id,
            value.sku_id,
            store_id=store_id,
            limit=value.limit,
        )
        return ToolResult(status="success", output=output)
