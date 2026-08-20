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
from ecommerce_agent.product_lifecycle.schemas import (
    Recommendation,
    RecommendationState,
    RecommendationType,
    TargetObject,
)
from ecommerce_agent.product_lifecycle.state_machine import TransitionAction
from ecommerce_agent.service import AgentService


def _redact_json(value: Any) -> Any:
    """递归脱敏任意 JSON 值（dict/list/str/scalar），PII 不入自由文本/嵌套字段。

    入口唯一集中处：HTTP create 对所有持久化自由字段统一调用，保证
    rationale、facts_snapshot（含嵌套）、missing_evidence 一致脱敏。
    """
    from ecommerce_agent.text_utils import redact_sensitive

    if isinstance(value, dict):
        return {k: _redact_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_json(v) for v in value]
    if isinstance(value, str):
        redacted, _ = redact_sensitive(value)
        return redacted
    return value


class CreateRecommendationRequest(BaseModel):
    """POST 创建建议的请求（created_at/updated_at 服务端生成，强制 DRAFT）。

    安全审查 #8：字段有界（防超长 payload DoS 与脏数据）。
    """

    model_config = ConfigDict(extra="forbid")

    recommendation_id: str = Field(min_length=1, max_length=128)
    type: RecommendationType
    target: TargetObject
    facts_snapshot: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=4000)
    missing_evidence: list[str] = Field(default_factory=list, max_length=100)
    alternatives: list[RecommendationType] = Field(default_factory=list, max_length=20)
    degraded: bool = False


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

    def call(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/recommendations")
    def create_recommendation(
        payload: CreateRecommendationRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """创建生命周期建议（强制 DRAFT，B3 alternatives 校验，写审计）。

        安全 #9：rationale / facts_snapshot（含嵌套字段）/ missing_evidence
        统一脱敏后落库（模型幻觉 PII 不入库，任何自由文本/嵌套 JSON 字段都不例外）。
        """
        from ecommerce_agent.text_utils import redact_sensitive

        now = datetime.now(UTC)
        rationale, _ = redact_sensitive(payload.rationale)
        facts_snapshot = _redact_json(payload.facts_snapshot)
        missing_evidence = _redact_json(payload.missing_evidence)
        rec = Recommendation(
            recommendation_id=payload.recommendation_id,
            type=payload.type,
            target=payload.target,
            facts_snapshot=facts_snapshot,
            rationale=rationale,
            missing_evidence=missing_evidence,
            alternatives=payload.alternatives,
            degraded=payload.degraded,
            created_at=now,
            updated_at=now,
        )
        try:
            result = recommendations.create(
                admin.tenant_id, rec, actor=admin.admin_id
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return result

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
        return {
            "composite_key": model.composite_key(),
            "impressions": _metric(model.impressions),
            "clicks": _metric(model.clicks),
            "add_to_cart": _metric(model.add_to_cart),
            "orders": _metric(model.orders),
            "payments": _metric(model.payments),
            "refunds": _metric(model.refunds),
            "net_sales": _metric(model.net_sales),
            "sellable_stock": _metric(model.sellable_stock),
            "in_transit_stock": _metric(model.in_transit_stock),
        }

    @router.get("/{store_id}/{item_id}/{sku_id}/workbench")
    def workbench_view(
        store_id: str,
        item_id: str,
        sku_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """商品经营工作台 JSON view（D1：含四态徽标 + "为什么暂不能建议"）。

        组装读模型 + 门禁报告 + 最新建议，前端可直接渲染。纯只读。
        """
        model = product_read.sku_read_model(
            admin.tenant_id, store_id=store_id, item_id=item_id, sku_id=sku_id
        )
        gate_view = service.operations.evidence_bridge.latest_revision_view(
            admin.tenant_id, store_id=store_id, sku_id=sku_id, item_id=item_id
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
            "composite_key": model.composite_key(),
            "metrics": {
                "impressions": _metric(model.impressions),
                "clicks": _metric(model.clicks),
                "add_to_cart": _metric(model.add_to_cart),
                "orders": _metric(model.orders),
                "payments": _metric(model.payments),
                "refunds": _metric(model.refunds),
                "net_sales": _metric(model.net_sales),
                "sellable_stock": _metric(model.sellable_stock),
                "in_transit_stock": _metric(model.in_transit_stock),
            },
            "evidence_gates": {
                "evidence_state": gate_view.get("evidence_state"),
                "all_passed": all_passed,
                "gates": [
                    {"name": g.name, "passed": g.passed, "reason": g.reason}
                    for g in gates
                ],
            },
            "why_not_recommended": not_recommended,
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
        rec["audit_trail"] = recommendations.audit_trail(
            admin.tenant_id, recommendation_id
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
        return recommendations.audit_trail(admin.tenant_id, recommendation_id)

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
        "reason": value.reason,
    }


__all__ = [
    "build_workbench_router",
]
