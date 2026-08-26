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
    assert data["metrics"]["impressions"]["evidence_state"] == "missing"
    assert "authoritative_service" in data["metrics"]["impressions"]
    assert "import_manifest_id" in data["metrics"]["impressions"]
    assert data["identity"] == {
        "material_code": None,
        "title": None,
        "merchant_code": None,
        "evidence": None,
    }
    assert data["revision"] is None


def test_workbench_uses_one_explicit_revision_for_metrics_and_gates(tmp_path) -> None:
    """The page must not combine revision-N metrics with latest-revision gates."""
    settings = make_settings(tmp_path)
    from ecommerce_agent.service import AgentService

    svc = AgentService(settings)
    with svc.db.connect() as conn:
        conn.execute(
            """
            INSERT INTO creative_assets(
                asset_id, tenant_id, sha256, mime_type, width, height,
                storage_ref, source_ref, feature_schema_version, payload_hash,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "asset-m9r", "tenant-test", "e" * 64, "image/png", 1200, 1200,
                "objects/m9r.png", "fixture://m9r", "image-v1", "f" * 64,
                "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00",
            ),
        )
        for revision, revision_id, impressions, day in (
            (1, "rev-m9r-1", 101, "10"),
            (2, "rev-m9r-2", 202, "11"),
        ):
            source_time = f"2026-08-{day}T00:00:00+00:00"
            conn.execute(
                """
                INSERT INTO listing_revisions(
                    id, tenant_id, connector_id, store_id, item_id, sku_id,
                    revision_no, title, main_image_asset_id, sale_price,
                    attributes_json, active_from, active_to, source_updated_at,
                    payload_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id, "tenant-test", "virtual_taobao", "store-a",
                    "item-a", "sku-a", revision, f"商品 rev {revision}",
                    "asset-m9r", "109.00", "{}", source_time,
                    f"2026-08-{day}T23:59:59+00:00", source_time,
                    str(revision) * 64, source_time, source_time,
                ),
            )
            conn.execute(
                """
                INSERT INTO traffic_metric_buckets(
                    id, tenant_id, listing_revision_id, metric_start, metric_end,
                    bucket_granularity, traffic_source, impressions, clicks,
                    visitors, favorites, cart_adds, orders, sales_amount,
                    ad_spend, search_impressions, recommend_impressions,
                    data_as_of, source_id, payload_hash, quality_flags_json,
                    version, created_at, updated_at, connector_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"bucket-m9r-{revision}", "tenant-test", revision_id,
                    source_time, f"2026-08-{day}T23:59:59+00:00", "day",
                    "recommend", impressions, 10, 10, 0, 1, 1, "100", "0",
                    10, impressions - 10, source_time, f"source-m9r-{revision}",
                    "a" * 64, "[]", 1, source_time, source_time,
                    "virtual_taobao",
                ),
            )
    svc.close()

    app = create_app(settings)
    with TestClient(app) as client:
        revision_1 = client.get(
            "/v1/products/store-a/item-a/sku-a/workbench?revision=1",
            headers=ADMIN_HEADERS,
        ).json()
        revision_2 = client.get(
            "/v1/products/store-a/item-a/sku-a/workbench?revision=2",
            headers=ADMIN_HEADERS,
        ).json()
        analysis_runs = client.get(
            "/v1/products/store-a/item-a/sku-a/analysis-runs?revision=1",
            headers=ADMIN_HEADERS,
        ).json()
        diagnosis = client.get(
            "/v1/products/store-a/item-a/sku-a/diagnosis?revision=2",
            headers=ADMIN_HEADERS,
        ).json()
        generated = client.post(
            "/v1/products/store-a/item-a/sku-a/recommendation/generate",
            headers=ADMIN_HEADERS,
            json={"recommendation_id": "rec-revision-2", "revision": 2},
        )

    assert revision_1["metrics"]["impressions"]["value"] == 101
    assert revision_1["revision"]["revision_id"] == "rev-m9r-1"
    assert revision_1["evidence_gates"]["revision_id"] == "rev-m9r-1"
    assert revision_2["metrics"]["impressions"]["value"] == 202
    assert revision_2["revision"]["revision_id"] == "rev-m9r-2"
    assert revision_2["evidence_gates"]["revision_id"] == "rev-m9r-2"
    assert analysis_runs["revision"]["revision_id"] == "rev-m9r-1"
    assert analysis_runs["experiments"] == []
    assert analysis_runs["reason"] is None
    assert diagnosis["revision"]["revision_id"] == "rev-m9r-2"
    assert generated.status_code == 200, generated.text
    assert (
        generated.json()["facts_snapshot"]["evidence_references"]
        ["listing_revision"]["revision_id"]
        == "rev-m9r-2"
    )


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
    """客户端不得绕过模型语义链直接提交建议类型和事实。"""
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
    assert response.status_code == 409
    assert response.json()["detail"] == "manual_recommendation_creation_not_allowed"


def test_persistence_service_redacts_pii_for_all_callers(tmp_path) -> None:
    """脱敏必须位于持久化边界，不能只保护已关闭的手工 HTTP 旁路。"""
    settings = make_settings(tmp_path)
    from ecommerce_agent.service import AgentService
    svc = AgentService(settings)
    rec = Recommendation(
        recommendation_id="rec-pii",
        type=RecommendationType.KEEP_OBSERVE,
        target=TargetObject(store_id="store-a"),
        facts_snapshot={"contact": {"phone": "13800138000"}},
        rationale="联系客户 13800138000",
        missing_evidence=["缺 13800138000 的确认"],
        alternatives=[RecommendationType.EXPERIMENT],
        created_at=datetime(2026, 8, 18, tzinfo=UTC),
        updated_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    persisted = svc.operations.recommendations.create(
        "tenant-test", rec, actor="admin-test"
    )
    svc.close()
    body = str(persisted)
    assert "13800138000" not in body
    assert "138****8000" in body


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
