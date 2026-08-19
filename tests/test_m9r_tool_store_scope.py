"""M9-R WP5 验收缺陷 1 反证：生命周期工具店铺 scope 约束。

- list_recommendations：trusted store 冲突 → store_scope_mismatch
- get_recommendation_audit_trail：详情必带 store_id；建议不属于请求店铺 → 拒绝
- 归属校验：同租户跨店铺读审计被拒
"""
from __future__ import annotations

from pathlib import Path
import tempfile
from datetime import UTC, datetime
from types import SimpleNamespace

from ecommerce_agent.business.service import (
    OperationsService,
    RecommendationDetailToolInput,
    RecommendationListToolInput,
)
from ecommerce_agent.database import Database
from ecommerce_agent.product_lifecycle import (
    Recommendation,
    RecommendationState,
    RecommendationType,
    TargetObject,
)


def _rec(*, recommendation_id: str = "rec-1", store_id: str = "store-a") -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        type=RecommendationType.KEEP_OBSERVE,
        target=TargetObject(store_id=store_id),
        facts_snapshot={},
        rationale="x",
        alternatives=[RecommendationType.EXPERIMENT],
        state=RecommendationState.DRAFT,
        degraded=False,
        created_at=datetime(2026, 8, 18, tzinfo=UTC),
        updated_at=datetime(2026, 8, 18, tzinfo=UTC),
    )


class _Ctx:
    tenant_id = "tenant-a"


def _ops() -> OperationsService:
    db = Database(Path(tempfile.mkdtemp()) / "scope.sqlite3")
    db.initialize()
    ops = OperationsService(db)
    ops.recommendations.create("tenant-a", _rec())
    return ops


def test_list_recommendations_rejects_trusted_store_mismatch() -> None:
    """trusted store 是 store-b，请求 store-a → store_scope_mismatch。"""
    ops = _ops()
    args = RecommendationListToolInput(store_id="store-a", limit=10)
    ctx = _Ctx()
    ctx.trusted_context = {"store_id": "store-b"}
    denial = ops._catalog_store_scope_policy(args, ctx)
    assert denial == "store_scope_mismatch"


def test_list_recommendations_allows_trusted_store_match() -> None:
    """trusted store 与请求一致 → 放行。"""
    ops = _ops()
    args = RecommendationListToolInput(store_id="store-a", limit=10)
    ctx = _Ctx()
    ctx.trusted_context = {"store_id": "store-a"}
    denial = ops._catalog_store_scope_policy(args, ctx)
    assert denial is None


def test_audit_detail_requires_store_scope() -> None:
    """详情工具 policy：可信 store 存在才放行（缺 store → store_scope_required）。"""
    ops = _ops()
    args = RecommendationDetailToolInput(
        recommendation_id="rec-1", store_id="store-a"
    )
    ctx = _Ctx()
    ctx.trusted_context = {}
    denial = ops._recommendation_store_scope_policy(args, ctx)
    assert denial == "store_scope_required"


def test_audit_detail_rejects_cross_store() -> None:
    """可信 store 是 store-a，建议属于 store-a，但请求 store-b → 拒绝。"""
    ops = _ops()
    args = RecommendationDetailToolInput(
        recommendation_id="rec-1", store_id="store-b"
    )
    ctx = _Ctx()
    ctx.trusted_context = {"store_id": "store-a"}
    denial = ops._recommendation_store_scope_policy(args, ctx)
    assert denial == "store_scope_mismatch"


def test_audit_trail_handler_rejects_wrong_store() -> None:
    """handler 归属校验：建议属 store-a，请求 store-b → failed store_scope_mismatch。"""
    ops = _ops()
    args = RecommendationDetailToolInput(
        recommendation_id="rec-1", store_id="store-b"
    )
    ctx = _Ctx()
    ctx.trusted_context = {"store_id": "store-b"}
    result = ops._recommendation_audit_trail_tool(args, ctx)
    assert result.status == "failed"
    assert result.error_code == "store_scope_mismatch"


def test_audit_trail_handler_allows_own_store() -> None:
    """建议属 store-a，请求 store-a → success。"""
    ops = _ops()
    args = RecommendationDetailToolInput(
        recommendation_id="rec-1", store_id="store-a"
    )
    ctx = _Ctx()
    ctx.trusted_context = {"store_id": "store-a"}
    result = ops._recommendation_audit_trail_tool(args, ctx)
    assert result.status == "success"
    assert result.output["count"] == 0
