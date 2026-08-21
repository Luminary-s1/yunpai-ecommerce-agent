from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from ..business_calendar import StoreBusinessCalendarService
from ..connectors import (
    ConnectorRegistry,
    PullRequest,
    SourceProvenanceResolver,
    VirtualTaobaoConnector,
    unknown_source_provenance,
)
from ..database import Database, utc_now
from ..forecasting.engine import ForecastPolicy
from ..forecasting.planning import (
    InventoryPlanningError,
    InventoryPlanningPolicy,
    InventoryPlanningService,
)
from ..forecasting.run_service import ForecastRunError, ForecastRunService
from ..forecasting.service import DemandFactService
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
from ..product_lifecycle.service import RecommendationPersistenceService
from ..product_lifecycle.schemas import RecommendationState
from ..product_read_model.query import ProductReadQuery

if TYPE_CHECKING:
    from ..traffic_lab import TrafficAnalysisInterpreter


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


class ForecastEvidenceToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku_id: str = Field(min_length=1, max_length=128)
    store_id: str | None = Field(default=None, max_length=128)


class RecommendationListToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str | None = Field(default=None, max_length=128)
    state: str | None = None  # draft/awaiting_review/approved/rejected/observed/closed
    limit: int = Field(default=50, ge=1, le=200)


class RecommendationDetailToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendation_id: str = Field(min_length=1, max_length=128)
    store_id: str = Field(min_length=1, max_length=128)


def _model_unavailable_diagnosis(
    sku_id: str, facts: DiagnosisFacts
) -> Diagnosis:
    """R3（D-034 默认路径）：模型语义不可用时返回占位诊断。

    不给强方向结论（不允许 Ruleset 阈值决定经营语义）。返回
    EVIDENCE_INSUFFICIENT + reason=model_unavailable + degraded=True，
    evidence_facts 保留固化证据供下游消费。
    """
    from ..product_diagnosis.diagnosis import Diagnosis, DiagnosisType

    return Diagnosis(
        diagnosis_type=DiagnosisType.EVIDENCE_INSUFFICIENT,
        sku_id=sku_id,
        reason="model_unavailable",
        evidence_facts={
            "evidence_state": facts.evidence_state,
            "freshness": facts.freshness,
            "quality_gate": facts.quality_gate,
            "quality_gate_issues": list(facts.quality_gate_issues),
            "exposures": facts.exposures,
            "clicks": facts.clicks,
            "conversions": facts.conversions,
            "stockout": facts.stockout,
            "pollution": facts.pollution,
        },
        degraded=True,
    )


class OperationsService:
    def __init__(
        self,
        db: Database,
        *,
        traffic_analysis_interpreter: TrafficAnalysisInterpreter | None = None,
        recommendation_interpreter: Any = None,
        diagnosis_interpreter: Any = None,
        model_semantic_enabled: bool = False,
    ):
        from ..traffic_lab import TrafficAnalysisEngine, TrafficLabIngestionService

        self.db = db
        self.business_calendars = StoreBusinessCalendarService(db)
        self.connectors = ConnectorRegistry()
        self.connectors.register(VirtualTaobaoConnector())
        self.source_provenance = SourceProvenanceResolver(self.connectors)
        self.catalog = CatalogService(db)
        self.orders = OrderService(db)
        self.inventory = InventoryService(db)
        self.forecasting = DemandFactService(
            db,
            orders=self.orders,
            inventory=self.inventory,
            source_provenance_resolver=self.source_provenance,
        )
        self.forecast_runs = ForecastRunService(db, facts=self.forecasting)
        self.inventory_plans = InventoryPlanningService(
            db,
            forecasts=self.forecast_runs,
            inventory=self.inventory,
            source_provenance_resolver=self.source_provenance,
        )
        self.competitive = CompetitiveIntelligenceService(db)
        self.competitive_report = CompetitiveReportService(db)
        self.marketing = MarketingService(db)
        self.finance = FinanceService(db)
        self.ops_assistant = OpsAssistantService(db)
        self.traffic_lab = TrafficLabIngestionService(
            db,
            business_calendars=self.business_calendars,
        )
        self.traffic_analysis = TrafficAnalysisEngine(
            db,
            interpreter=traffic_analysis_interpreter,
            source_provenance_resolver=self.source_provenance,
            business_calendars=self.business_calendars,
        )
        self.metrics = MetricsService(db, self.inventory)
        self.recommendations = RecommendationPersistenceService(db)
        self.product_read = ProductReadQuery(db)
        # WP2 门禁生产消费者（B1）：EvidenceBridge 统一证据视图 + 确定性 Gate。
        # product_diagnosis 不反向 import business，无循环依赖。
        from ..product_diagnosis.bridge import EvidenceBridge

        self.evidence_bridge = EvidenceBridge(self.traffic_lab.domain)
        # WP2 诊断语义解释器（D-034：模型可注入，未注入则 Ruleset 降级）。
        # 生产诊断入口通过 self.diagnose() 调用，模型可用时走 DiagnosisModelInterpreter。
        from ..product_diagnosis.interpreter import (
            DiagnosisInterpreter,
            RulesetDiagnosisInterpreter,
            run_interpretation,
        )

        self._diagnosis_interpreter: DiagnosisInterpreter = (
            diagnosis_interpreter or RulesetDiagnosisInterpreter()
        )
        # R3（D-034 默认路径）：模型语义是否可用。False（默认）时 diagnose()
        # 不给强方向诊断（返回 evidence_insufficient + model_unavailable），
        # 不允许 Ruleset 阈值直接决定经营语义。True 时才走模型解释器。
        self._model_semantic_enabled = model_semantic_enabled
        # WP3 闭环补缺：诊断 → 建议 生成引擎（D-034：模型可注入，未注入则
        # Ruleset 降级）。引擎纯能力（不接路由/不自动触发），由工作台/agent 工具
        # 显式调用后走 RecommendationPersistenceService.create 落库。
        from ..product_lifecycle.engine import RecommendationEngine

        self.recommendation_engine = RecommendationEngine(
            self.inventory, interpreter=recommendation_interpreter
        )

    def diagnose(
        self,
        tenant_id: str,
        *,
        store_id: str,
        item_id: str,
        sku_id: str,
    ) -> dict[str, Any]:
        """生产诊断入口（D-034 语义链）：读模型 → 门禁 → 诊断（模型解释器）。

        返回结构化诊断（diagnosis_type/reason/degraded/evidence_facts），
        不落库、不产生平台写。缺证据/门禁未过 → 显式 missing/blocked，不编造。
        """
        from ..product_diagnosis.diagnosis import build_diagnosis_facts
        from ..product_diagnosis.interpreter import run_interpretation

        model = self.product_read.sku_read_model(
            tenant_id, store_id=store_id, item_id=item_id, sku_id=sku_id
        )
        gate_view = self.evidence_bridge.latest_revision_view(
            tenant_id, store_id=store_id, sku_id=sku_id, item_id=item_id
        )
        all_passed, gates = self.evidence_bridge.run_gates(gate_view)
        # T2.3（P3 修复）：gate 结论必须成为诊断输入，而非响应附件。
        # 原始 gate_view.quality_gate 可能 status="passed" 但显式 gate（aa/sample/
        # window/control）失败 → run_all 返回 all_passed=False。诊断 facts 必须消费
        # 组合结论（blocked），使 conclusion_allowed 拒绝强方向——"gate 是闸门不是装饰品"。
        gate_quality = (
            "passed"
            if all_passed
            else {"status": "blocked", "issues": list(
                g.reason for g in gates if not g.passed
            )}
        )
        facts = build_diagnosis_facts(
            sku_id,
            {
                "evidence_state": gate_view.get("evidence_state"),
                "freshness": gate_view.get("freshness"),
                "quality_gate": gate_quality,
                "exposures": model.impressions.value
                if model.impressions.evidence_state.value != "missing" else None,
                "clicks": model.clicks.value
                if model.clicks.evidence_state.value != "missing" else None,
                "conversions": model.payments.value
                if model.payments.evidence_state.value != "missing" else None,
            },
        )
        # R3（D-034 默认路径）：模型语义不可用时不给强方向诊断。
        # 不允许 Ruleset 阈值直接决定经营语义（那违反任务书"模型决定语义下一步"）。
        # 返回 evidence_insufficient + model_unavailable 占位，degraded=True。
        # 模型可用（model_semantic_enabled=True）时才走模型解释器/ruleset 降级。
        if not self._model_semantic_enabled:
            diag = _model_unavailable_diagnosis(sku_id, facts)
        else:
            diag = run_interpretation(facts, self._diagnosis_interpreter)
        # R3（负责人阻断项 3 修复）：顶层 degradation_reasons 结构化暴露降级原因。
        # reason 保持稳定码 "model_unavailable"（不引越权词），结构化原因列表供前端/
        # 下游程序化消费：模型不可用 + 门禁 blocked → 双原因。facts.quality_gate 已被
        # build_diagnosis_facts 归一化为 "passed"/"blocked"/None（diagnosis.py L71）。
        degradation_reasons: list[str] = []
        if diag.degraded:
            degradation_reasons.append("evidence_insufficient")
        if not self._model_semantic_enabled:
            degradation_reasons.append("model_unavailable")
        if facts.quality_gate == "blocked":
            degradation_reasons.append("quality_gate_blocked")
        return {
            "sku_id": sku_id,
            "diagnosis_type": diag.diagnosis_type.value,
            "reason": diag.reason,
            "degraded": diag.degraded,
            "degradation_reasons": degradation_reasons,
            "evidence_facts": diag.evidence_facts,
            "gates": {
                "all_passed": all_passed,
                "results": [
                    {"name": g.name, "passed": g.passed, "reason": g.reason}
                    for g in gates
                ],
            },
        }

    def generate_and_persist_recommendation(
        self,
        tenant_id: str,
        *,
        store_id: str,
        item_id: str,
        sku_id: str,
        recommendation_id: str,
        actor: str = "admin",
    ) -> dict[str, Any]:
        """生产语义链闭环（P3 修复，阻断3）：诊断 → 引擎建议 → 校验 → 落库。

        任务书要求"基于固化事实和流量诊断，由模型产生语义建议，经代码校验后固化"——
        这是唯一生产入口。recommendation_engine.generate 在生产路径只有一个调用点（此处），
        workbench_api 的 POST /recommendations 是管理员手工提交旁路，不走模型语义链。

        流程：diagnose()（读模型→门禁→诊断）→ engine.generate()（解释器→facts→校验）
        → recommendations.create()（落库 DRAFT + 审计）。零平台写动作（B4）。
        """
        from datetime import UTC, datetime

        from ..product_diagnosis.diagnosis import Diagnosis, DiagnosisType
        from ..product_lifecycle.engine import RecommendationEngine

        # 1. 诊断（复用生产 diagnose 门禁链）
        diagnosis_result = self.diagnose(
            tenant_id, store_id=store_id, item_id=item_id, sku_id=sku_id
        )
        # 2. 引擎产出建议候选（模型解释器或 Ruleset 降级）。
        #    从 diagnose() 输出构造冻结 Diagnosis（evidence_facts 已固化证据）。
        diag = Diagnosis(
            diagnosis_type=DiagnosisType(diagnosis_result["diagnosis_type"]),
            sku_id=sku_id,
            reason=diagnosis_result.get("reason"),
            evidence_facts=diagnosis_result["evidence_facts"],
            degraded=diagnosis_result["degraded"],
        )
        model = self.product_read.sku_read_model(
            tenant_id, store_id=store_id, item_id=item_id, sku_id=sku_id
        )
        engine: RecommendationEngine = self.recommendation_engine
        recommendation = engine.generate(
            tenant_id=tenant_id,
            diagnosis=diag,
            sku=model,
            recommendation_id=recommendation_id,
            created_at=datetime.now(UTC),
        )
        # 3. 落库（create 内部校验 + 幂等）
        created = self.recommendations.create(
            tenant_id, recommendation, actor=actor
        )
        # B4（盲点 #6 修复）：同 SKU 旧建议自动 stale——事实更新后旧建议作废，
        # 不原地改写历史（任务书 WP3 L365"事实更新后旧建议标 stale"）。
        # 新落库的建议除外，其余同 SKU 非终态建议标记 stale。
        self._mark_older_recommendations_stale(
            tenant_id,
            store_id=store_id,
            sku_id=sku_id,
            keep_recommendation_id=str(created.get("recommendation_id") or recommendation_id),
            actor=actor,
        )
        return created

    def _mark_older_recommendations_stale(
        self,
        tenant_id: str,
        *,
        store_id: str,
        sku_id: str,
        keep_recommendation_id: str,
        actor: str,
    ) -> None:
        """B4：把同 SKU 其他非终态建议标记 stale（新建议保留）。

        终态（STALE/CLOSED）不动；只对 DRAFT/AWAITING_REVIEW/APPROVED/REJECTED/
        OBSERVED 等非终态旧建议标记 stale，使工作台展示的总是最新结论。
        """
        from datetime import UTC, datetime

        from ..product_lifecycle.schemas import RecommendationState
        from ..product_lifecycle.service import RecommendationPersistenceService
        from ..product_lifecycle.state_machine import TransitionAction

        stale_states = (
            RecommendationState.STALE,
            RecommendationState.CLOSED,
        )
        active_states = [
            s.value
            for s in RecommendationState
            if s not in stale_states
        ]
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT recommendation_id FROM product_recommendations
                WHERE tenant_id=? AND store_id=? AND sku_id=?
                  AND state IN ({",".join("?" for _ in active_states)})
                """,
                (tenant_id, store_id, sku_id, *active_states),
            ).fetchall()
        if isinstance(self.recommendations, RecommendationPersistenceService):
            for row in rows:
                old_id = str(row["recommendation_id"])
                if old_id == keep_recommendation_id:
                    continue
                try:
                    self.recommendations.record_transition(
                        tenant_id,
                        old_id,
                        action=TransitionAction.MARK_STALE,
                        actor=actor,
                        at=datetime.now(UTC),
                    )
                except Exception:  # noqa: BLE001 — 旧建议标记 stale 失败不阻断主流程
                    pass

    def modules(self) -> list[dict[str, Any]]:
        return [item.model_dump() for item in business_module_catalog()]

    def connector_catalog(self) -> list[dict[str, Any]]:
        return [item.model_dump() for item in self.connectors.catalog()]

    def configure_forecasting_policies(
        self,
        tenant_id: str,
        *,
        store_id: str,
        sku_id: str,
        forecast_policy: ForecastPolicy,
        inventory_policy: InventoryPlanningPolicy,
    ) -> dict[str, Any]:
        forecast_planning_contract = (
            self.inventory_plans.validate_forecast_contract(
                forecast_policy,
                inventory_policy,
            )
        )
        created_at = utc_now()
        forecast_evidence = self.forecast_runs._policy_evidence(forecast_policy)
        inventory_evidence = self.inventory_plans._policy_evidence(inventory_policy)
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            forecast_id, forecast_status = self.forecast_runs._ensure_policy(
                conn,
                tenant_id,
                store_id,
                sku_id,
                forecast_evidence,
                created_at,
            )
            inventory_id, inventory_status = self.inventory_plans._ensure_policy(
                conn,
                tenant_id,
                store_id,
                sku_id,
                inventory_policy,
                inventory_evidence,
                created_at,
            )
        return {
            "tenant_id": tenant_id,
            "store_id": store_id,
            "sku_id": sku_id,
            "forecast_planning_contract": forecast_planning_contract,
            "forecast_policy": {
                "policy_id": forecast_id,
                **forecast_evidence,
                "write_status": forecast_status,
            },
            "inventory_policy": {
                "policy_id": inventory_id,
                **inventory_evidence,
                "write_status": inventory_status,
            },
        }

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
                    policy=self._catalog_store_scope_policy,
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
                    # 宽松 scope：store_id 可选，有 trusted 才校验冲突（缺省由服务端处理）
                    policy=self._catalog_store_scope_policy,
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
                    policy=self._catalog_store_scope_policy,
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
                    policy=self._catalog_store_scope_policy,
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
                    policy=self._catalog_store_scope_policy,
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
                    policy=self._catalog_store_scope_policy,
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
                    policy=self._forecast_store_scope_policy,
                    metadata={"domain": "traffic_lab", "risk_level": "L0"},
                )
            )
        if registry.get("get_demand_forecast") is None:
            registry.register(
                ToolSpec(
                    name="get_demand_forecast",
                    description=(
                        "读取指定 SKU 最新已固化的需求预测、区间、回测指标和数据质量证据；"
                        "不运行预测，不修改模型选择或数值"
                    ),
                    kind="read",
                    input_model=ForecastEvidenceToolInput,
                    handler=self._demand_forecast_tool,
                    policy=self._forecast_store_scope_policy,
                    metadata={"domain": "forecasting", "risk_level": "L0"},
                )
            )
        if registry.get("get_inventory_plan") is None:
            registry.register(
                ToolSpec(
                    name="get_inventory_plan",
                    description=(
                        "读取指定 SKU 最新已固化的库存快照、策略、缺货风险和建议量；"
                        "只返回 advisory 证据，不创建采购单、不付款、不调整库存"
                    ),
                    kind="read",
                    input_model=ForecastEvidenceToolInput,
                    handler=self._inventory_plan_tool,
                    policy=self._forecast_store_scope_policy,
                    metadata={"domain": "forecasting", "risk_level": "L0"},
                )
            )
        if registry.get("list_recommendations") is None:
            registry.register(
                ToolSpec(
                    name="list_recommendations",
                    description=(
                        "读取当前租户的生命周期建议列表（选品/上新/诊断/实验/定价/活动/"
                        "补货/清仓），可按店铺/状态过滤；只返回只读建议证据，"
                        "不创建/批准/修改任何建议，不触发平台动作"
                    ),
                    kind="read",
                    input_model=RecommendationListToolInput,
                    handler=self._list_recommendations_tool,
                    policy=self._catalog_store_scope_policy,
                    metadata={"domain": "lifecycle", "risk_level": "L0"},
                )
            )
        if registry.get("get_recommendation_audit_trail") is None:
            registry.register(
                ToolSpec(
                    name="get_recommendation_audit_trail",
                    description=(
                        "读取单条生命周期建议的完整状态流转审计（draft→…→closed）；"
                        "只读审计记录，不可变，不修改任何建议"
                    ),
                    kind="read",
                    input_model=RecommendationDetailToolInput,
                    handler=self._recommendation_audit_trail_tool,
                    policy=self._recommendation_store_scope_policy,
                    metadata={"domain": "lifecycle", "risk_level": "L0"},
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

    @classmethod
    def _forecast_store_scope_policy(
        cls, arguments: BaseModel, context: ToolExecutionContext
    ) -> str | None:
        denial = cls._catalog_store_scope_policy(arguments, context)
        if denial:
            return denial
        if not arguments.model_dump().get("store_id") and not cls._trusted_store_id(
            context
        ):
            return "store_scope_required"
        return None

    @classmethod
    def _recommendation_store_scope_policy(
        cls, arguments: BaseModel, context: ToolExecutionContext
    ) -> str | None:
        """详情工具必须落在可信店铺内：store_id 与可信 store 冲突即拒。"""
        denial = cls._catalog_store_scope_policy(arguments, context)
        if denial:
            return denial
        if not cls._trusted_store_id(context):
            return "store_scope_required"
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
        provenance = output["source_provenance"]
        return ToolResult(
            status="success",
            output={
                **output,
                "source_type": provenance["source_type"],
                "virtual": provenance["virtual"],
                "references": {"source_provenance": provenance},
            },
        )

    def _demand_forecast_tool(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        value = ForecastEvidenceToolInput.model_validate(arguments.model_dump())
        store_id = value.store_id or self._trusted_store_id(context)
        forecast = self.forecast_runs.latest_run(
            context.tenant_id, sku_id=value.sku_id, store_id=store_id
        )
        provenance = forecast["source_provenance"]
        return ToolResult(
            status="success",
            output={
                "evidence_source": "forecast_runs",
                "computed_now": False,
                "forecast": forecast,
                "freshness": forecast["freshness"],
                "source_type": provenance["source_type"],
                "virtual": provenance["virtual"],
                "references": {
                    "forecast_run_id": forecast["run_id"],
                    "data_hash": forecast["data_hash"],
                    "demand_policy_version": forecast["demand_policy_version"],
                    "forecast_policy_version": forecast["forecast_policy_version"],
                    "model_version": forecast["model_version"],
                    "data_quality": forecast["status"],
                    "anomalies": forecast["anomalies"],
                    "freshness": forecast["freshness"],
                    "source_provenance": provenance,
                },
            },
        )

    def _inventory_plan_tool(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        value = ForecastEvidenceToolInput.model_validate(arguments.model_dump())
        store_id = value.store_id or self._trusted_store_id(context)
        try:
            plan = self.inventory_plans.latest_plan(
                context.tenant_id, sku_id=value.sku_id, store_id=store_id
            )
        except InventoryPlanningError as exc:
            reason_code = str(exc)
            if reason_code not in {
                "inventory_plan_not_found",
                "inventory_plan_current_not_found",
            }:
                raise
            try:
                current_forecast = self.forecast_runs.latest_run(
                    context.tenant_id,
                    sku_id=value.sku_id,
                    store_id=store_id,
                )
            except ForecastRunError:
                current_forecast = None
            provenance = (
                current_forecast["source_provenance"]
                if current_forecast is not None
                else unknown_source_provenance(
                    basis="inventory_plan_current_not_found"
                )
            )
            return ToolResult(
                status="failed",
                error_code=reason_code,
                retryable=False,
                output={
                    "evidence_source": "inventory_plans",
                    "computed_now": False,
                    "action_allowed": False,
                    "current_plan_available": False,
                    "inventory_plan": None,
                    "current_forecast_run_id": (
                        None
                        if current_forecast is None
                        else current_forecast["run_id"]
                    ),
                    "reason_code": reason_code,
                    "source_type": provenance["source_type"],
                    "virtual": provenance["virtual"],
                    "references": {
                        "forecast_freshness": (
                            None
                            if current_forecast is None
                            else current_forecast["freshness"]
                        ),
                        "source_provenance": provenance,
                    },
                },
            )
        provenance = plan["source_provenance"]
        return ToolResult(
            status="success",
            output={
                "evidence_source": "inventory_plans",
                "computed_now": False,
                "action_allowed": False,
                "inventory_plan": plan,
                "freshness": plan["freshness"],
                "source_type": provenance["source_type"],
                "virtual": provenance["virtual"],
                "references": {
                    "plan_id": plan["plan_id"],
                    "forecast_run_id": plan["forecast_run_id"],
                    "inventory_snapshot_hash": plan["inventory_snapshot_hash"],
                    "inventory_as_of": plan["inventory_as_of"],
                    "planning_policy_version": plan["planning_policy_version"],
                    "plan_quality": plan["plan_quality"],
                    "quality_issues": plan["quality_issues"],
                    "action_mode": plan["action_mode"],
                    "freshness": plan["freshness"],
                    "source_provenance": provenance,
                },
            },
        )

    def _list_recommendations_tool(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        value = RecommendationListToolInput.model_validate(arguments.model_dump())
        state = RecommendationState(value.state) if value.state else None
        # E3 修正：缺 store_id 时回落 trusted store（对齐 _product_search_tool 等），
        # 避免缺省 → 返回该租户全店铺建议（跨店范围风险）。
        store_id = value.store_id or self._trusted_store_id(context)
        items = self.recommendations.list(
            context.tenant_id,
            store_id=store_id,
            state=state,
            limit=value.limit,
        )
        return ToolResult(
            status="success",
            output={"items": items, "count": len(items)},
        )

    def _recommendation_audit_trail_tool(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        value = RecommendationDetailToolInput.model_validate(arguments.model_dump())
        # 归属校验：建议必须属于请求的店铺（缺陷 1：防跨店铺读审计）
        try:
            rec = self.recommendations.get(
                context.tenant_id, value.recommendation_id
            )
        except Exception:
            return ToolResult(status="failed", error_code="recommendation_not_found")
        if rec["target"]["store_id"] != value.store_id:
            return ToolResult(status="failed", error_code="store_scope_mismatch")
        trail = self.recommendations.audit_trail(
            context.tenant_id, value.recommendation_id
        )
        return ToolResult(
            status="success",
            output={"items": trail, "count": len(trail)},
        )
