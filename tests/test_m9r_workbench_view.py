"""M9-R WP4 复审反证：工作台 JSON view + 机制 Eval 接线 + revision 下钻。

覆盖（WP5 复审计划批次 4）：
- workbench JSON view 含四态徽标 + why_not_recommended（D1/D4）
- 机制 Eval 生产端点可达（D2）
- read-model 路由 revision 下钻（D3）
- 建议详情含 reason_not_recommended（D4）
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.database import Database
from ecommerce_agent.product_lifecycle import (
    Recommendation,
    RecommendationState,
    RecommendationType,
    TargetObject,
)
from ecommerce_agent.service import AgentService

from conftest import make_settings

ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}


def _rec(*, recommendation_id: str = "rec-1") -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        type=RecommendationType.KEEP_OBSERVE,
        target=TargetObject(store_id="store-a"),
        facts_snapshot={},
        rationale="observe",
        alternatives=[RecommendationType.EXPERIMENT],
        state=RecommendationState.DRAFT,
        degraded=False,
        created_at=datetime(2026, 8, 18, tzinfo=UTC),
        updated_at=datetime(2026, 8, 18, tzinfo=UTC),
    )


def test_mechanism_eval_endpoint(tmp_path) -> None:
    """机制 Eval 生产端点：返回冻结场景逐场景结果 + 汇总。"""
    settings = make_settings(tmp_path)
    svc = AgentService(settings)
    svc.close()
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(
            "/v1/admin/evaluations/mechanism",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 7
    assert data["all_passed"] is True
    assert all("name" in s and "passed" in s for s in data["scenes"])


def test_workbench_view_endpoint(tmp_path) -> None:
    """workbench JSON view：含 metrics + evidence_gates + why_not_recommended。"""
    settings = make_settings(tmp_path)
    svc = AgentService(settings)
    svc.close()
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(
            "/v1/products/store-a/item-a/sku-a/workbench",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 200
    data = response.json()
    assert "metrics" in data
    assert "evidence_gates" in data
    # 缺数据 → 缺数据指标 evidence_state=missing + why_not_recommended 非空
    assert data["metrics"]["impressions"]["evidence_state"] == "missing"
    assert data["why_not_recommended"], "缺数据应给出暂不能建议原因"


def test_read_model_revision_drilldown(tmp_path) -> None:
    """read-model 路由 revision 下钻：revision 进 composite_key（D3）。"""
    settings = make_settings(tmp_path)
    svc = AgentService(settings)
    svc.close()
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(
            "/v1/products/store-a/item-a/sku-a/read-model?revision=3",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 200
    data = response.json()
    assert data["composite_key"] == ["tenant-test", "store-a", "item-a", "sku-a", 3]


def test_recommendation_detail_has_reason_not_recommended(tmp_path) -> None:
    """建议详情含 reason_not_recommended（D4：为什么暂不能建议）。"""
    settings = make_settings(tmp_path)
    svc = AgentService(settings)
    svc.operations.recommendations.create("tenant-test", _rec())
    svc.close()
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(
            "/v1/products/recommendations/rec-1?store_id=store-a",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 200
    data = response.json()
    assert "reason_not_recommended" in data
    # DRAFT 未批准 → 应列出"尚未人工批准"
    assert any("人工批准" in r for r in data["reason_not_recommended"])
