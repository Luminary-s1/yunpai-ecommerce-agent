"""M9-R WP4 工作台路由测试（WP5 验收修复：实际 FastAPI 路由）。

覆盖：
- /v1/products/{store}/{item}/{sku}/read-model 200（读模型投影）
- /v1/products/recommendations 200（建议列表，只读）
- /v1/recommendations/{id}/audit 200（审计链）
- 无凭据 → 401（鉴权门）
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.product_lifecycle import (
    Recommendation,
    RecommendationPersistenceService,
    RecommendationState,
    RecommendationType,
    TargetObject,
)

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


def test_read_model_endpoint_returns_200(tmp_path) -> None:
    """读模型路由 200（缺数据 → MISSING 语义，不抛）。"""
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get(
            "/v1/products/store-a/item-a/sku-a/read-model",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 200
    data = response.json()
    assert data["composite_key"] == ["tenant-test", "store-a", "item-a", "sku-a", 1]
    assert data["impressions"]["evidence_state"] == "missing"


def test_recommendations_list_endpoint_returns_200(tmp_path) -> None:
    """建议列表路由 200（只读，含已创建的建议）。"""
    settings = make_settings(tmp_path)
    from ecommerce_agent.service import AgentService
    svc = AgentService(settings)
    svc.operations.recommendations.create("tenant-test", _rec())
    svc.close()  # 释放 data_dir 锁，供 create_app 复用同一目录

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(
            "/v1/products/recommendations",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 200
    items = response.json()
    assert any(item["recommendation_id"] == "rec-1" for item in items)


def test_recommendation_audit_endpoint_returns_200(tmp_path) -> None:
    """审计链路由 200。"""
    settings = make_settings(tmp_path)
    from ecommerce_agent.service import AgentService
    svc = AgentService(settings)
    svc.operations.recommendations.create("tenant-test", _rec())
    svc.close()

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(
            "/v1/products/recommendations/rec-1/audit?store_id=store-a",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 200
    assert response.json() == []


def test_recommendation_detail_requires_store_id(tmp_path) -> None:
    """详情路由 store_id 必填（E2）：缺 → 422。"""
    settings = make_settings(tmp_path)
    from ecommerce_agent.service import AgentService
    svc = AgentService(settings)
    svc.operations.recommendations.create("tenant-test", _rec())
    svc.close()

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(
            "/v1/products/recommendations/rec-1",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 422


def test_recommendation_detail_cross_store_rejected(tmp_path) -> None:
    """详情路由跨店铺 → 409（归属校验）。"""
    settings = make_settings(tmp_path)
    from ecommerce_agent.service import AgentService
    svc = AgentService(settings)
    svc.operations.recommendations.create("tenant-test", _rec())
    svc.close()

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(
            "/v1/products/recommendations/rec-1?store_id=store-b",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 409


def test_recommendations_list_state_invalid_returns_400(tmp_path) -> None:
    """list 的 state 参数非法 → 400（E1，不 500）。"""
    settings = make_settings(tmp_path)
    from ecommerce_agent.service import AgentService
    svc = AgentService(settings)
    svc.close()

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(
            "/v1/products/recommendations?state=bogus",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 400


def test_create_recommendation_endpoint(tmp_path) -> None:
    """POST 创建建议（C1 生产入口）：强制 DRAFT + 落库。"""
    settings = make_settings(tmp_path)
    from ecommerce_agent.service import AgentService
    svc = AgentService(settings)
    svc.close()

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/v1/products/recommendations",
            headers=ADMIN_HEADERS,
            json={
                "recommendation_id": "rec-new",
                "type": "保持观察",
                "target": {"store_id": "store-a"},
                "facts_snapshot": {},
                "rationale": "observe",
                "alternatives": ["受控实验"],
            },
        )
    assert response.status_code == 200
    data = response.json()
    assert data["recommendation_id"] == "rec-new"
    assert data["state"] == RecommendationState.DRAFT.value
    assert data["write_status"] == "applied"


def test_recommendation_transition_endpoint(tmp_path) -> None:
    """POST 状态流转（C1 人工审核入口）：submit → awaiting_review。"""
    settings = make_settings(tmp_path)
    from ecommerce_agent.service import AgentService
    svc = AgentService(settings)
    svc.operations.recommendations.create(
        "tenant-test", _rec(), actor="admin-test"
    )
    svc.close()

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/v1/products/recommendations/rec-1/transition?store_id=store-a",
            headers=ADMIN_HEADERS,
            json={"action": "submit"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["recommendation"]["state"] == RecommendationState.AWAITING_REVIEW.value
    assert data["write_status"] == "applied"


def test_recommendation_transition_cross_store_rejected(tmp_path) -> None:
    """transition 跨店铺 → 409（归属校验，agentops 复审补齐）。"""
    settings = make_settings(tmp_path)
    from ecommerce_agent.service import AgentService
    svc = AgentService(settings)
    svc.operations.recommendations.create(
        "tenant-test", _rec(), actor="admin-test"
    )
    svc.close()

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/v1/products/recommendations/rec-1/transition?store_id=store-b",
            headers=ADMIN_HEADERS,
            json={"action": "submit"},
        )
    assert response.status_code == 409


def test_endpoints_require_admin(tmp_path) -> None:
    """无凭据 → 401（鉴权门）。"""
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get(
            "/v1/products/store-a/item-a/sku-a/read-model",
        )
    assert response.status_code in (401, 403)
