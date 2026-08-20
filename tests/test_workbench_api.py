"""M9-R WP4 工作台路由测试（WP5 验收修复：实际 FastAPI 路由）。

覆盖：
- /v1/products/{store}/{item}/{sku}/read-model 200（读模型投影）
- /v1/products/recommendations 200（建议列表，只读）
- /v1/recommendations/{id}/audit 200（审计链）
- 无凭据 → 401（鉴权门）
"""
from __future__ import annotations

from datetime import UTC, datetime
from html.parser import HTMLParser

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


def test_create_recommendation_pii_redacted_all_fields(tmp_path) -> None:
    """P1-2 反例：rationale / facts_snapshot（含嵌套）/ missing_evidence 统一脱敏。

    手机号出现在任意持久化自由字段都必须被掩码后落库，读回不得含原文。
    """
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
                "recommendation_id": "rec-pii",
                "type": "保持观察",
                "target": {"store_id": "store-a"},
                "facts_snapshot": {
                    "contact": {"phone": "13800138000", "name": "张三"},
                    "note": "客户电话 13800138000",
                },
                "rationale": "联系客户 13800138000",
                "missing_evidence": ["缺 13800138000 的确认"],
                "alternatives": ["受控实验"],
            },
        )
    assert response.status_code == 200, response.text
    # 读回详情：三个字段均不得含明文手机号
    detail = client.get(
        "/v1/products/recommendations/rec-pii?store_id=store-a",
        headers=ADMIN_HEADERS,
    )
    assert detail.status_code == 200, detail.text
    body = detail.text
    assert "13800138000" not in body, "持久化字段含明文手机号（PII 未脱敏）"
    assert "138****8000" in body, "掩码后落库格式不符"


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


def test_transition_approve_returns_approve_audit(tmp_path) -> None:
    """P1-6 反例：submit→approve 后，approve 响应返回的 audit 必须是 approve。

    复验指出"submit→approve 后 approve 响应中的 audit 仍是 submit"——
    必须返回本次动作的审计记录，不得回读上一次动作的旧 audit。
    """
    settings = make_settings(tmp_path)
    from ecommerce_agent.service import AgentService
    svc = AgentService(settings)
    svc.operations.recommendations.create(
        "tenant-test", _rec(), actor="admin-test"
    )
    svc.close()

    app = create_app(settings)
    with TestClient(app) as client:
        r_submit = client.post(
            "/v1/products/recommendations/rec-1/transition?store_id=store-a",
            headers=ADMIN_HEADERS,
            json={"action": "submit"},
        )
        assert r_submit.status_code == 200
        r_approve = client.post(
            "/v1/products/recommendations/rec-1/transition?store_id=store-a",
            headers=ADMIN_HEADERS,
            json={"action": "approve"},
        )
    assert r_approve.status_code == 200, r_approve.text
    data = r_approve.json()
    assert data["recommendation"]["state"] == RecommendationState.APPROVED.value
    assert data["audit"]["action"] == "approve", (
        f"approve 响应返回了 {data['audit']['action']} 审计（应为 approve）"
    )
    assert data["audit"]["from_state"] == "awaiting_review"
    assert data["audit"]["to_state"] == "approved"
    assert data["write_status"] == "applied"


def test_endpoints_require_admin(tmp_path) -> None:
    """无凭据 → 401（鉴权门）。"""
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get(
            "/v1/products/store-a/item-a/sku-a/read-model",
        )
    assert response.status_code in (401, 403)


class _WorkbenchConsoleStructure(HTMLParser):
    """解析 /admin 页面，收集 M9-R 工作台视图的 id / 导航 / API 引用。"""

    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.nav_views: set[str] = set()

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "button" and values.get("data-view"):
            self.nav_views.add(values["data-view"])


def test_admin_console_has_m9r_workbench_view(tmp_path) -> None:
    """P1-3 反例：/admin 页面含「商品经营」工作台视图（真实页面非 dict 冒充）。

    复验指出「WP4 尚无可验收页面，浏览器验收脚本是假绿」——本测试锁定
    admin 页面实际渲染 M9-R 工作台：导航项 + 输入框 + 查询按钮 + loader
    JS + 消费的 JSON API 路径全部存在。
    """
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        page = client.get("/admin")
    assert page.status_code == 200
    structure = _WorkbenchConsoleStructure()
    structure.feed(page.text)
    # 导航含「商品经营」
    assert "m9r-workbench" in structure.nav_views
    # 视图内关键控件
    assert {
        "m9rStore",
        "m9rItem",
        "m9rSku",
        "m9rLoadWorkbench",
        "m9rKpis",
        "m9rMetricRows",
        "m9rGates",
        "m9rRecRows",
    } <= structure.ids
    # loader 与 API 路径真实存在（非 dict 断言冒充浏览器）
    assert "loadM9rWorkbench" in page.text
    assert "loadM9rWorkbench" in page.text and ".addEventListener('click'" in page.text
    assert "/v1/products/${encodeURIComponent(storeId)}" in page.text or "/workbench" in page.text
