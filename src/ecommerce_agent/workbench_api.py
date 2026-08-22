"""M9-R WP4 工作台路由（WP5 复审修复：接入 FastAPI，含人工审核生产入口）。

边界声明：
- 读侧只读：不触发平台写（B2/B4 写屏障）。
- 写侧（人工审核生产入口）：POST 创建建议（强制 DRAFT）+ 状态流转
  （submit/approve/reject/observe/mark_stale/close）。写仅限建议记录/状态/审计，
  不触发任何平台动作；归属校验阻止跨店铺操作。
- 范围隔离：复用 AdminPrincipal.tenant_id + 店铺 scope。
- 失败暴露：建议不存在 → 404；scope 冲突 → 409；非法状态/参数 → 400/409；
  异常 → HTTPException（不静默）。
"""
from __future__ import annotations

from datetime import UTC, datetime
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ecommerce_agent.auth import AdminPrincipal
from ecommerce_agent.product_lifecycle.schemas import RecommendationState
from ecommerce_agent.product_lifecycle.state_machine import TransitionAction
from ecommerce_agent.product_workbench.pages import WorkbenchPages
from ecommerce_agent.service import AgentService


class RecommendationGenerateRequest(BaseModel):
    """POST 生成建议请求（P3 生产语义链：模型产建议，客户端只指定 ID）。

    诊断 → 引擎 → 校验 → 落库 全在服务端，客户端不提供 type/rationale/
    facts_snapshot（防旁路模型语义链）。recommendation_id 由客户端指定（幂等）。
    """

    model_config = ConfigDict(extra="forbid")

    recommendation_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(default=1, ge=1)


class TransitionRequest(BaseModel):
    """POST 状态流转请求（actor 服务端强制为 admin.admin_id，防审计归因伪造）。"""

    model_config = ConfigDict(extra="forbid")

    action: TransitionAction


def build_workbench_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/products", tags=["workbench"])
    recommendations = service.operations.recommendations
    product_read = service.operations.product_read
    pages = WorkbenchPages(recommendation_store=recommendations)

    @router.post("/recommendations")
    def create_recommendation(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """拒绝客户端提供语义建议；建议只能走服务端模型生成入口。"""
        raise HTTPException(
            status_code=409, detail="manual_recommendation_creation_not_allowed"
        )

    @router.post("/recommendations/{recommendation_id}/transition")
    def recommendation_transition(
        recommendation_id: str,
        payload: TransitionRequest,
        store_id: str = Query(...),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """状态流转（submit/approve/reject/observe/mark_stale/close），人工审核入口。

        归属校验（agentops 复审补齐）：建议必须属于请求的店铺，与 detail/audit
        路由的 store_scope_mismatch(409) 对齐，防止租户内跨店铺流转建议。
        审计归因（安全 #4）：actor 服务端强制为 admin.admin_id，客户端不可伪造。
        """
        try:
            rec = recommendations.get(admin.tenant_id, recommendation_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if rec["target"]["store_id"] != store_id:
            raise HTTPException(status_code=409, detail="store_scope_mismatch")
        try:
            result = recommendations.record_transition(
                admin.tenant_id,
                recommendation_id,
                action=payload.action,
                actor=admin.admin_id,
                at=datetime.now(UTC),
            )
        except Exception as exc:
            # 安全 #5：非法转换 400/409 明确暴露（不 500），not_found 404
            detail = str(exc)
            if "invalid_state_transition" in detail:
                raise HTTPException(status_code=409, detail=detail) from exc
            if "recommendation_not_found" in detail:
                raise HTTPException(status_code=404, detail=detail) from exc
            raise HTTPException(status_code=400, detail=detail) from exc
        return result

    @router.get("/{store_id}/{item_id}/{sku_id}/diagnosis")
    def sku_diagnosis(
        store_id: str,
        item_id: str,
        sku_id: str,
        revision: int = Query(default=1, ge=1),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """SKU 生产诊断（D-034 语义链：读模型 → 门禁 → 模型诊断）。

        返回结构化诊断（diagnosis_type/reason/degraded/evidence_facts + 门禁结果），
        不落库、不产生平台写。缺证据/门禁未过 → 显式 missing/blocked，不编造。
        """
        return service.operations.diagnose(
            admin.tenant_id,
            store_id=store_id,
            item_id=item_id,
            sku_id=sku_id,
            revision=revision,
        )

    @router.post("/{store_id}/{item_id}/{sku_id}/recommendation/generate")
    def generate_recommendation(
        store_id: str,
        item_id: str,
        sku_id: str,
        payload: RecommendationGenerateRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """生产语义链闭环（P3 修复，阻断3）：诊断 → 引擎建议 → 校验 → 落库。

        任务书"基于固化事实和流量诊断，由模型产生语义建议，经代码校验后固化"
        的唯一生产入口。POST /recommendations 固定拒绝手工语义建议。
        返回 DRAFT 建议 + 审计落痕。零平台写动作（B4）。
        """
        return service.operations.generate_and_persist_recommendation(
            admin.tenant_id,
            store_id=store_id,
            item_id=item_id,
            sku_id=sku_id,
            recommendation_id=payload.recommendation_id,
            revision=payload.revision,
            actor=admin.admin_id,
        )

    @router.get("/{store_id}/{item_id}/{sku_id}/read-model")
    def sku_read_model(
        store_id: str,
        item_id: str,
        sku_id: str,
        revision: int = Query(default=1, ge=1),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """SKU 权威读模型（流量/交易/库存，MISSING 语义；D3：revision 下钻）。"""
        model = product_read.sku_read_model(
            admin.tenant_id, store_id=store_id, item_id=item_id, sku_id=sku_id,
            revision=revision,
        )
        metrics = _model_metrics(model)
        return {
            "composite_key": model.composite_key(),
            "identity": _model_identity(model),
            "revision": _listing_revision(model),
            "metrics": metrics,
        }

    @router.get("/{store_id}/{item_id}/{sku_id}/workbench")
    def workbench_view(
        store_id: str,
        item_id: str,
        sku_id: str,
        revision: int = Query(default=1, ge=1),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """商品经营工作台 JSON view（D1：含四态徽标 + "为什么暂不能建议"）。

        组装读模型 + 门禁报告 + 最新建议，前端可直接渲染。纯只读。
        """
        model = product_read.sku_read_model(
            admin.tenant_id, store_id=store_id, item_id=item_id, sku_id=sku_id,
            revision=revision,
        )
        page = pages.product_detail(
            store_id=store_id,
            item_id=item_id,
            sku_id=sku_id,
            metrics=_model_metrics(model),
        )
        gate_view = (
            service.operations.evidence_bridge.get_revision_view(
                admin.tenant_id, model.listing_revision.revision_id
            )
            if model.listing_revision is not None
            else {
                "evidence_state": "missing",
                "reason": "traffic_revision_not_found",
                "freshness": None,
                "quality_gate": None,
            }
        )
        all_passed, gates = service.operations.evidence_bridge.run_gates(gate_view)
        # D4：由门禁/证据推导"为什么暂不能建议"
        not_recommended: list[str] = []
        if gate_view.get("evidence_state") == "missing":
            not_recommended.append(
                f"证据不足（{gate_view.get('reason') or 'traffic_evidence_not_found'}）"
            )
        for g in gates:
            if not g.passed:
                not_recommended.append(f"{g.name} 门禁未过（{g.reason}）")
        return {
            **page,
            "composite_key": model.composite_key(),
            "identity": _model_identity(model),
            "revision": _listing_revision(model),
            "evidence_gates": {
                "revision_id": gate_view.get("revision_id"),
                "evidence_state": gate_view.get("evidence_state"),
                "data_as_of": gate_view.get("data_as_of"),
                "freshness": gate_view.get("freshness"),
                "source_provenance": gate_view.get("source_provenance"),
                "quality_gate": gate_view.get("quality_gate"),
                "all_passed": all_passed,
                "gates": [
                    {"name": g.name, "passed": g.passed, "reason": g.reason}
                    for g in gates
                ],
            },
            "why_not_recommended": not_recommended,
        }

    @router.get("/{store_id}/{item_id}/{sku_id}/analysis-runs")
    def analysis_runs(
        store_id: str,
        item_id: str,
        sku_id: str,
        revision: int = Query(default=1, ge=1),
        limit: int = Query(default=20, ge=1, le=100),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """Analysis-run drill-down for the exact revision selected by the user."""
        model = product_read.sku_read_model(
            admin.tenant_id,
            store_id=store_id,
            item_id=item_id,
            sku_id=sku_id,
            revision=revision,
        )
        selected = model.listing_revision
        if selected is None:
            return {
                "revision": None,
                "experiments": [],
                "reason": "traffic_revision_not_found",
            }
        experiments = service.operations.traffic_lab.domain.list_experiments(
            admin.tenant_id, store_id=store_id, sku_id=sku_id, limit=limit
        )
        result: list[dict[str, Any]] = []
        for experiment in experiments:
            if selected.revision_id not in {
                str(experiment.get("control_revision_id")),
                str(experiment.get("treatment_revision_id")),
            }:
                continue
            experiment_id = str(experiment["experiment_id"])
            result.append(
                {
                    "experiment": experiment,
                    "analysis_runs": (
                        service.operations.evidence_bridge.list_analysis_runs_view(
                            admin.tenant_id, experiment_id, limit=limit
                        )
                    ),
                }
            )
        return {
            "revision": selected.model_dump(mode="json"),
            "experiments": result,
            "reason": None,
        }

    @router.get("/{store_id}/{item_id}/{sku_id}/insights")
    def listing_insights(
        store_id: str,
        item_id: str,
        sku_id: str,
        limit: int = Query(default=20, ge=1, le=100),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """SKU 流量证据（复用 M5-R listing_traffic_insights）。"""
        return service.operations.traffic_lab.domain.listing_traffic_insights(
            admin.tenant_id, sku_id, store_id=store_id, limit=limit
        )

    @router.get("/{store_id}/{item_id}/{sku_id}/evidence-gates")
    def evidence_gates(
        store_id: str,
        item_id: str,
        sku_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """SKU 最新 revision 的确定性门禁报告（WP2 门禁生产消费者入口）。

        返回 evidence_state / all_passed / 逐 gate 结果；缺数据 → 显式 missing，
        不强给结论（fail-closed）。
        """
        view = service.operations.evidence_bridge.latest_revision_view(
            admin.tenant_id, store_id=store_id, sku_id=sku_id, item_id=item_id
        )
        all_passed, gates = service.operations.evidence_bridge.run_gates(view)
        return {
            "store_id": store_id,
            "item_id": item_id,
            "sku_id": sku_id,
            "evidence_state": view.get("evidence_state"),
            "reason": view.get("reason"),
            "all_passed": all_passed,
            "gates": [
                {"name": g.name, "passed": g.passed, "reason": g.reason}
                for g in gates
            ],
        }

    @router.get("/recommendations")
    def list_recommendations(
        store_id: str | None = None,
        state: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        """生命周期建议列表（只读）。

        E1 修正：state 参数非法 → 400（不 500）。
        """
        state_value = None
        if state is not None:
            try:
                state_value = RecommendationState(state)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail=f"invalid_recommendation_state:{state}"
                )
        return recommendations.list(
            admin.tenant_id, store_id=store_id, state=state_value, limit=limit
        )

    @router.get("/recommendations/{recommendation_id}")
    def recommendation_detail(
        recommendation_id: str,
        store_id: str = Query(...),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """建议详情 + 审计链（校验店铺归属，E2：store_id 必填）。

        D4：返回 reason_not_recommended——由 degraded/missing_evidence/门禁推导
        "为什么暂不能建议"，不只给 red/green 分数。
        """
        try:
            rec = recommendations.get(admin.tenant_id, recommendation_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if rec["target"]["store_id"] != store_id:
            raise HTTPException(status_code=409, detail="store_scope_mismatch")
        rec["audit_trail"] = pages.recommendation_audit_trail(
            tenant_id=admin.tenant_id, recommendation_id=recommendation_id
        )
        # D4：由建议状态/降级/缺失证据推导"为什么暂不能建议"
        not_recommended: list[str] = []
        if rec["degraded"]:
            not_recommended.append("建议降级（degraded）：事实不足或污染，不输出正式结论")
        if rec.get("missing_evidence"):
            not_recommended.append(
                "缺失证据：" + ", ".join(str(m) for m in rec["missing_evidence"])
            )
        if not not_recommended and rec["state"] != RecommendationState.APPROVED.value:
            not_recommended.append(
                f"状态为 {rec['state']}，尚未人工批准（只有人工可批准/拒绝）"
            )
        rec["reason_not_recommended"] = not_recommended
        return rec

    @router.get("/recommendations/{recommendation_id}/audit")
    def recommendation_audit(
        recommendation_id: str,
        store_id: str = Query(...),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        """建议审计链（只读，E2：store_id 必填 + 归属校验）。"""
        try:
            rec = recommendations.get(admin.tenant_id, recommendation_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if rec["target"]["store_id"] != store_id:
            raise HTTPException(status_code=409, detail="store_scope_mismatch")
        return pages.recommendation_audit_trail(
            tenant_id=admin.tenant_id, recommendation_id=recommendation_id
        )

    return router


def _metric(value: Any) -> dict[str, Any]:
    """MetricValue → 展示视图（含四态徽标来源信息）。"""
    return {
        "evidence_state": value.evidence_state.value,
        "granularity": value.granularity.value,
        "aggregate_rule": value.aggregate_rule.value,
        "period_key": value.period_key,
        "value": value.value,
        "data_as_of": value.data_as_of.isoformat() if value.data_as_of else None,
        "data_trust": value.data_trust.value,
        "import_manifest_id": value.import_manifest_id,
        "authoritative_service": value.authoritative_service,
        "reason": value.reason,
    }


def _model_metrics(model: Any) -> dict[str, dict[str, Any]]:
    return {
        "impressions": _metric(model.impressions),
        "clicks": _metric(model.clicks),
        "add_to_cart": _metric(model.add_to_cart),
        "orders": _metric(model.orders),
        "payments": _metric(model.payments),
        "refunds": _metric(model.refunds),
        "net_sales": _metric(model.net_sales),
        "sellable_stock": _metric(model.sellable_stock),
        "in_transit_stock": _metric(model.in_transit_stock),
        "ad_spend": _metric(model.ad_spend),
        "competitor_price": _metric(model.competitor_price),
        "experiment_state": _metric(model.experiment_state),
    }


def _model_identity(model: Any) -> dict[str, Any]:
    evidence = model.product_identity_evidence
    return {
        "material_code": model.material_code,
        "title": model.title,
        "merchant_code": model.merchant_code,
        "evidence": evidence.model_dump(mode="json") if evidence else None,
    }


def _listing_revision(model: Any) -> dict[str, Any] | None:
    evidence = model.listing_revision
    return evidence.model_dump(mode="json") if evidence else None


__all__ = [
    "build_workbench_router",
]
