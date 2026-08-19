"""M9-R WP4 工作台路由（WP5 验收修复：接入 FastAPI，不再是纯内存组装器）。

边界声明：
- 只读：不创建/修改任何建议、不触发平台写（B2/B4 写屏障）。
- 范围隔离：复用 AdminPrincipal.tenant_id + 店铺 scope。
- 失败暴露：建议不存在 → 404；scope 冲突 → 409；异常 → HTTPException（不静默）。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ecommerce_agent.auth import AdminPrincipal
from ecommerce_agent.service import AgentService


def build_workbench_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/products", tags=["workbench"])
    recommendations = service.operations.recommendations
    product_read = service.operations.product_read

    @router.get("/{store_id}/{item_id}/{sku_id}/read-model")
    def sku_read_model(
        store_id: str,
        item_id: str,
        sku_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """SKU 权威读模型（流量/交易/库存，MISSING 语义）。"""
        model = product_read.sku_read_model(
            admin.tenant_id, store_id=store_id, item_id=item_id, sku_id=sku_id
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

    @router.get("/recommendations")
    def list_recommendations(
        store_id: str | None = None,
        state: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        """生命周期建议列表（只读）。"""
        return recommendations.list(
            admin.tenant_id, store_id=store_id, state=state, limit=limit
        )

    @router.get("/recommendations/{recommendation_id}")
    def recommendation_detail(
        recommendation_id: str,
        store_id: str | None = None,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """建议详情 + 审计链（校验店铺归属）。"""
        try:
            rec = recommendations.get(admin.tenant_id, recommendation_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if store_id is not None and rec["target"]["store_id"] != store_id:
            raise HTTPException(status_code=409, detail="store_scope_mismatch")
        rec["audit_trail"] = recommendations.audit_trail(
            admin.tenant_id, recommendation_id
        )
        return rec

    @router.get("/recommendations/{recommendation_id}/audit")
    def recommendation_audit(
        recommendation_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        """建议审计链（只读）。"""
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
