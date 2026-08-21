from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .auth import AdminPrincipal
from .customer_service_content import (
    CustomerServiceContentImportRequest,
    CustomerServiceContextRequest,
)
from .knowledge_engine.memory_service import MEMORY_CATEGORIES, KnowledgeMemoryService
from .knowledge_management import (
    KnowledgeCreateRequest,
    KnowledgeLifecycleError,
    KnowledgeReviseRequest,
    KnowledgeRolloutBeginRequest,
    KnowledgeRolloutTransitionRequest,
    KnowledgeRolloutUpdateRequest,
    KnowledgeTransitionRequest,
)
from .quality import QualityError, QualityReviewRequest, QualityRunRequest
from .service import AgentService
from .sops import (
    SopCompensationRequest,
    SopCreateRequest,
    SopError,
    SopReviseRequest,
    SopRolloutBeginRequest,
    SopRolloutTransitionRequest,
    SopRolloutUpdateRequest,
    SopStepResolutionRequest,
    SopTransitionRequest,
)


class MemoryRecordRequest(BaseModel):
    """店铺长期记忆写入请求（A1）。"""

    store_id: str
    fact: str
    category: str = "frequent_issue"
    source: str = ""


def build_governance_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/admin", tags=["governance"])

    @router.post("/customer-service/content/import", status_code=201)
    def import_customer_service_content(
        payload: CustomerServiceContentImportRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.customer_service_content.import_content(
                admin.tenant_id,
                payload,
                actor=admin.admin_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/customer-service/content/context")
    def preview_customer_service_context(
        payload: CustomerServiceContextRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.customer_service_content.build_context(admin.tenant_id, payload)

    @router.get("/customer-service/content/{item_id}/trace")
    def trace_customer_service_content(
        item_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.customer_service_content.get_trace(admin.tenant_id, item_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/knowledge")
    def list_knowledge(
        status: str | None = Query(default=None, pattern=r"^(active|candidate|retired)$"),
        layer: str | None = Query(
            default=None, pattern=r"^(platform|industry|store|product|evolution)$"
        ),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.knowledge_management.list_items(
            admin.tenant_id, status=status, layer=layer, limit=limit
        )

    @router.post("/knowledge", status_code=201)
    def create_knowledge(
        payload: KnowledgeCreateRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.knowledge_management.create(
                admin.tenant_id, payload, admin.admin_id
            )
        except KnowledgeLifecycleError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/knowledge/import-assets")
    def import_assets(
        update: bool = Query(default=False, description="true=更新已存在内容（热更新）；false=幂等跳过"),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """热更新：把 02_clean 资产层知识导入运行时表（P1-3 + A6 修复）。

        - 默认（update=false）：kg-* 已存在跳过（幂等，重复调用不重复写）
        - update=true：已存在的 kg-* 行更新 answer/question 等内容字段
          （修复"假热更新"：此前即使传 update=true 也只在 runtime_bridge 层
          支持，端点从未接线，改 02_clean 后重导不更新内容）
        - 用于改 02_clean 后免重启热更新
        - 返回导入统计（含 updated 条数）
        """
        from .knowledge_engine.loader import load_clean_dir
        from .knowledge_engine.runtime_bridge import import_to_runtime

        clean_dir = (
            Path(__file__).resolve().parents[2]
            / "knowledge_graph_output"
            / "02_clean"
        )
        if not clean_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"资产层目录不存在: {clean_dir}")
        try:
            items = load_clean_dir(clean_dir)
            stats = import_to_runtime(
                items,
                service.knowledge,
                tenant_id=admin.tenant_id,
                default_store_id=admin.tenant_id,
                update_existing=update,
            )
            return {"ok": True, **stats}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"资产导入失败: {exc}") from exc

    # ---------- 店铺长期记忆管理（A1：P1-2 KnowledgeMemoryService 接线） ----------

    @router.get("/knowledge/memory/categories")
    def memory_categories(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, str]:
        """记忆类别字典（buyer_preference/frequent_issue/decision_note → 中文标签）。"""
        return dict(MEMORY_CATEGORIES)

    @router.post("/knowledge/memory")
    def record_memory(
        payload: MemoryRecordRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """记录一条店铺级长期记忆（layer=evolution，默认检索隔离）。

        参数（JSON body）：
            store_id:  店铺 id（必填；记忆按店铺隔离）
            fact:      记忆内容（必填）
            category:  记忆类别（默认 frequent_issue）
            source:    证据来源（默认 memory://manual）
        """
        if not payload.store_id or not payload.fact.strip():
            raise HTTPException(status_code=422, detail="store_id 和 fact 必填")
        try:
            memory_id = service.memory.record(
                payload.store_id,
                fact=payload.fact,
                category=payload.category,
                source=payload.source,
                tenant_id=admin.tenant_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "memory_id": memory_id}

    @router.get("/knowledge/memory")
    def recall_memory(
        store_id: str = Query(description="店铺 id"),
        q: str = Query(default="", description="关键词过滤（空=全部）"),
        limit: int = Query(default=10, ge=1, le=100),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """显式召回店铺记忆（默认隔离：普通检索不命中，此端点主动查）。"""
        if not store_id:
            raise HTTPException(status_code=422, detail="store_id 必填")
        items = service.memory.recall(
            store_id, query=q, limit=limit, tenant_id=admin.tenant_id
        )
        return {"ok": True, "count": len(items), "items": items}

    @router.delete("/knowledge/memory/{memory_id}")
    def forget_memory(
        memory_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """删除一条店铺记忆。"""
        removed = service.memory.forget(memory_id, tenant_id=admin.tenant_id)
        if not removed:
            raise HTTPException(status_code=404, detail="memory not found")
        return {"ok": True, "memory_id": memory_id}

    @router.get("/knowledge/{item_id}")
    def get_knowledge(
        item_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = service.knowledge_management.get_item(admin.tenant_id, item_id)
        if result is None:
            raise HTTPException(status_code=404, detail="knowledge item not found")
        return result

    @router.post("/knowledge/{item_id}/versions", status_code=201)
    def revise_knowledge(
        item_id: str,
        payload: KnowledgeReviseRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _knowledge_action(
            service.knowledge_management.revise, admin, item_id, payload
        )

    @router.post("/knowledge/{item_id}/evaluate")
    def evaluate_knowledge(
        item_id: str,
        payload: KnowledgeTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _knowledge_action(
            service.knowledge_management.evaluate, admin, item_id, payload
        )

    @router.post("/knowledge/{item_id}/approve")
    def approve_knowledge(
        item_id: str,
        payload: KnowledgeTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _knowledge_action(
            service.knowledge_management.approve, admin, item_id, payload
        )

    @router.post("/knowledge/{item_id}/retire")
    def retire_knowledge(
        item_id: str,
        payload: KnowledgeTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _knowledge_action(
            service.knowledge_management.retire, admin, item_id, payload
        )

    @router.post("/knowledge/{item_id}/rollback")
    def rollback_knowledge(
        item_id: str,
        payload: KnowledgeTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _knowledge_action(
            service.knowledge_management.rollback, admin, item_id, payload
        )

    @router.get("/knowledge-rollouts")
    def list_knowledge_rollouts(
        status: str | None = Query(
            default=None, pattern=r"^(active|completed|rolled_back)$"
        ),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.knowledge_management.list_rollouts(
            admin.tenant_id, status=status, limit=limit
        )

    @router.post("/knowledge/{item_id}/rollouts", status_code=201)
    def begin_knowledge_rollout(
        item_id: str,
        payload: KnowledgeRolloutBeginRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _knowledge_action(
            service.knowledge_management.begin_rollout, admin, item_id, payload
        )

    @router.post("/knowledge-rollouts/{rollout_id}/traffic")
    def update_knowledge_rollout(
        rollout_id: str,
        payload: KnowledgeRolloutUpdateRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _knowledge_action(
            service.knowledge_management.update_rollout, admin, rollout_id, payload
        )

    @router.post("/knowledge-rollouts/{rollout_id}/complete")
    def complete_knowledge_rollout(
        rollout_id: str,
        payload: KnowledgeRolloutTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _knowledge_action(
            service.knowledge_management.complete_rollout, admin, rollout_id, payload
        )

    @router.post("/knowledge-rollouts/{rollout_id}/rollback")
    def rollback_knowledge_rollout(
        rollout_id: str,
        payload: KnowledgeRolloutTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _knowledge_action(
            service.knowledge_management.rollback_rollout, admin, rollout_id, payload
        )

    @router.get("/sops")
    def list_sops(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.sops.list_definitions(admin.tenant_id)

    @router.post("/sops", status_code=201)
    def create_sop(
        payload: SopCreateRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.sops.create(admin.tenant_id, payload, admin.admin_id)
        except SopError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/sops/{definition_id}")
    def get_sop(
        definition_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = service.sops.detail(admin.tenant_id, definition_id)
        if result is None:
            raise HTTPException(status_code=404, detail="SOP definition not found")
        return result

    @router.post("/sops/{definition_id}/versions", status_code=201)
    def revise_sop(
        definition_id: str,
        payload: SopReviseRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.sops.revise(
                admin.tenant_id, definition_id, payload, admin.admin_id
            )
        except SopError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/sop-versions/{version_id}/evaluate")
    def evaluate_sop(
        version_id: str,
        payload: SopTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _sop_action(service.sops.evaluate, admin, version_id, payload)

    @router.post("/sop-versions/{version_id}/approve")
    def approve_sop(
        version_id: str,
        payload: SopTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _sop_action(service.sops.approve, admin, version_id, payload)

    @router.post("/sop-versions/{version_id}/activate")
    def activate_sop(
        version_id: str,
        payload: SopTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _sop_action(service.sops.activate, admin, version_id, payload)

    @router.post("/sop-versions/{version_id}/retire")
    def retire_sop(
        version_id: str,
        payload: SopTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _sop_action(service.sops.retire, admin, version_id, payload)

    @router.post("/sop-versions/{version_id}/rollback")
    def rollback_sop(
        version_id: str,
        payload: SopTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _sop_action(service.sops.rollback, admin, version_id, payload)

    @router.get("/sop-rollouts")
    def list_sop_rollouts(
        status: str | None = Query(
            default=None, pattern=r"^(active|completed|rolled_back)$"
        ),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.sops.list_rollouts(admin.tenant_id, status=status, limit=limit)

    @router.post("/sop-versions/{version_id}/rollouts", status_code=201)
    def begin_sop_rollout(
        version_id: str,
        payload: SopRolloutBeginRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _sop_action(service.sops.begin_rollout, admin, version_id, payload)

    @router.post("/sop-rollouts/{rollout_id}/traffic")
    def update_sop_rollout(
        rollout_id: str,
        payload: SopRolloutUpdateRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _sop_action(service.sops.update_rollout, admin, rollout_id, payload)

    @router.post("/sop-rollouts/{rollout_id}/complete")
    def complete_sop_rollout(
        rollout_id: str,
        payload: SopRolloutTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _sop_action(service.sops.complete_rollout, admin, rollout_id, payload)

    @router.post("/sop-rollouts/{rollout_id}/rollback")
    def rollback_sop_rollout(
        rollout_id: str,
        payload: SopRolloutTransitionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return _sop_action(service.sops.rollback_rollout, admin, rollout_id, payload)

    @router.get("/sop-runs")
    def list_sop_runs(
        status: str | None = Query(
            default=None, pattern=r"^(active|completed|handoff|failed)$"
        ),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.sops.list_runs(admin.tenant_id, status=status, limit=limit)

    @router.get("/sop-runs/{run_id}")
    def get_sop_run(
        run_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.sops.get_run(admin.tenant_id, run_id)
        except SopError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/sop-runs/{run_id}/steps/{step_id}/resolve")
    def resolve_sop_step(
        run_id: str,
        step_id: str,
        payload: SopStepResolutionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.sops.resolve_step(
                admin.tenant_id, run_id, step_id, payload, admin.admin_id
            )
        except SopError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/sop-runs/{run_id}/steps/{step_id}/compensate")
    def compensate_sop_step(
        run_id: str,
        step_id: str,
        payload: SopCompensationRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.sops.compensate_step(
                admin.tenant_id, run_id, step_id, payload, admin.admin_id
            )
        except SopError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/qa/runs", status_code=201)
    def run_quality(
        payload: QualityRunRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.quality.run(admin.tenant_id, payload, admin.admin_id)
        except QualityError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/qa/results")
    def quality_results(
        review_status: str | None = Query(
            default=None, pattern=r"^(pending|confirmed|dismissed)$"
        ),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return service.quality.list_results(
            admin.tenant_id, review_status=review_status, limit=limit
        )

    @router.post("/qa/results/{result_id}/review")
    def review_quality(
        result_id: str,
        payload: QualityReviewRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.quality.review(
                admin.tenant_id, result_id, payload, admin.admin_id
            )
        except QualityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/voc/overview")
    def voc_overview(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.quality.summary(admin.tenant_id)

    return router


def _knowledge_action(action, admin, item_id, payload):
    try:
        return action(admin.tenant_id, item_id, payload, admin.admin_id)
    except KnowledgeLifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _sop_action(action, admin, version_id, payload):
    try:
        return action(admin.tenant_id, version_id, payload, admin.admin_id)
    except SopError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
