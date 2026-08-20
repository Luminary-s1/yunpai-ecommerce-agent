"""M9-R WP2 复审反证：门禁生产消费者 + freshness 修正 + 污染反推。

覆盖（WP5 复审计划批次 2）：
- evidence-gates 路由可达（门禁不再空转）
- latest_revision_view 无 revision → 显式 missing + all_passed=False
- experiment freshness 用 analysis_input_freshness（非恒 None）
- 污染旗标从 quality_gate.issues 自动反推
- conclusion_allowed 缺门禁 → False（fail-closed）
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.business.service import OperationsService
from ecommerce_agent.database import Database
from ecommerce_agent.product_diagnosis.diagnosis import build_diagnosis_facts
from ecommerce_agent.readonly_data.contracts import EvidenceState
from ecommerce_agent.service import AgentService

from conftest import make_settings

ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}


def _seed_revision(db: Database, *, connector_id: str = "taobao_official") -> None:
    """种 asset + revision + day bucket（真实 operational 数据）。"""
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO creative_assets(
                asset_id, tenant_id, sha256, mime_type, width, height, storage_ref,
                source_ref, feature_schema_version, payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "asset-1", "tenant-a", "e" * 64, "image/png", 1200, 1200,
                "objects/a.png", "fixture://a", "image-v1", "f" * 64,
                "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO listing_revisions(
                id, tenant_id, connector_id, store_id, item_id, sku_id, revision_no,
                title, main_image_asset_id, sale_price, attributes_json, active_from,
                active_to, source_updated_at, payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rev-1", "tenant-a", connector_id, "store-a", "item-a", "sku-a", 1,
                "测试商品", "asset-1", "109.00", '{"stock_status":"in_stock"}',
                "2026-08-01T00:00:00+00:00", "2026-08-30T00:00:00+00:00",
                "2026-08-10T00:00:00+00:00", "a" * 64,
                "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO traffic_metric_buckets(
                id, tenant_id, listing_revision_id, metric_start, metric_end,
                bucket_granularity, traffic_source, impressions, clicks, visitors,
                favorites, cart_adds, orders, sales_amount, ad_spend,
                search_impressions, recommend_impressions, data_as_of, source_id,
                payload_hash, quality_flags_json, version, created_at, updated_at,
                connector_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "bucket-1", "tenant-a", "rev-1", "2026-08-10T00:00:00+00:00",
                "2026-08-10T23:59:59+00:00", "day", "recommend", 1000, 80, 75,
                8, 5, 2, "218.00", "0", 100, 900, "2026-08-10T12:00:00+00:00",
                "src-1", "b" * 64, "[]", 1,
                "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00",
                connector_id,
            ),
        )


def _ops(tmp_path: Path) -> OperationsService:
    db = Database(tmp_path / "gates.sqlite3")
    db.initialize()
    _seed_revision(db)
    return OperationsService(db)


def _route_client(tmp_path: Path):
    """用 AgentService 种数据 + close 释放锁 + create_app 复用同一目录。"""
    settings = make_settings(tmp_path)
    svc = AgentService(settings)
    _seed_revision(svc.db)  # 种数据到 AgentService 的 db
    svc.close()
    return create_app(settings)


def test_evidence_gates_route_reachable(tmp_path) -> None:
    """evidence-gates 路由可达：有真实 operational 数据。"""
    app = _route_client(tmp_path)
    with TestClient(app) as client:
        response = client.get(
            "/v1/products/store-a/item-a/sku-a/evidence-gates",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 200
    data = response.json()
    assert data["sku_id"] == "sku-a"
    assert "gates" in data
    assert isinstance(data["all_passed"], bool)


def test_evidence_gates_missing_revision_fail_closed(tmp_path) -> None:
    """无 revision → 显式 missing + all_passed=False（不强给结论）。"""
    app = _route_client(tmp_path)
    with TestClient(app) as client:
        response = client.get(
            "/v1/products/store-a/item-a/no-such-sku/evidence-gates",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 200
    data = response.json()
    assert data["evidence_state"] == EvidenceState.MISSING.value
    assert data["all_passed"] is False


def test_latest_revision_view_returns_missing_when_no_revision(tmp_path) -> None:
    """latest_revision_view 无 revision → 显式 missing 视图。"""
    ops = _ops(tmp_path)
    view = ops.evidence_bridge.latest_revision_view(
        "tenant-a", store_id="store-a", sku_id="no-such-sku"
    )
    assert view["evidence_state"] == EvidenceState.MISSING.value


def test_latest_revision_view_has_quality_gate_key(tmp_path) -> None:
    """revision 视图含 quality_gate 键（B3：无引用实验 → None，不编造）。"""
    ops = _ops(tmp_path)
    view = ops.evidence_bridge.get_revision_view("tenant-a", "rev-1")
    assert "quality_gate" in view
    # 无引用该 revision 的 analysis run → None（门禁拒绝，不编造）
    assert view["quality_gate"] is None


def test_pollution_autodetected_from_quality_gate_issues() -> None:
    """B6：quality_gate.issues 含 stock_not_available → 自动标 stockout+degraded。"""
    facts = build_diagnosis_facts(
        "sku-x",
        {
            "evidence_state": "actual",
            "quality_gate": {
                "status": "blocked",
                "issues": ["stock_not_available"],
            },
        },
    )
    assert facts.stockout is True
    assert facts.degraded is True
    assert facts.conclusion_allowed() is False


def test_pollution_autodetected_ad_spend() -> None:
    """B6：ad_spend_not_controlled → 自动标 ad_change 污染。"""
    facts = build_diagnosis_facts(
        "sku-x",
        {
            "evidence_state": "actual",
            "quality_gate": {
                "status": "blocked",
                "issues": ["ad_spend_not_controlled"],
            },
        },
    )
    assert facts.pollution == "ad_change"
    assert facts.degraded is True


def test_conclusion_denied_without_quality_gate() -> None:
    """B4：quality_gate 缺失（None）→ conclusion_allowed=False（fail-closed）。"""
    facts = build_diagnosis_facts(
        "sku-x",
        {
            "evidence_state": "actual",
            "exposures": 1000,
            "clicks": 100,
            "quality_gate": None,
        },
    )
    assert facts.conclusion_allowed() is False


def test_conclusion_denied_when_freshness_stale() -> None:
    """B4：freshness stale → conclusion_allowed=False。"""
    facts = build_diagnosis_facts(
        "sku-x",
        {
            "evidence_state": "actual",
            "exposures": 1000,
            "clicks": 100,
            "quality_gate": {"status": "passed", "issues": []},
            "freshness": {"status": "stale", "usable_as_current": False, "reason_codes": ["x"]},
        },
    )
    assert facts.conclusion_allowed() is False


def test_run_gates_with_model_output_forbidden(tmp_path) -> None:
    """B5：run_gates 传 model_output 含 effect → 越权 Gate 失败。"""
    ops = _ops(tmp_path)
    view = ops.evidence_bridge.get_revision_view("tenant-a", "rev-1")
    all_passed, gates = ops.evidence_bridge.run_gates(
        view, model_output={"interpretation": "点击不足", "effect": 0.5}
    )
    assert all_passed is False
    assert any(g.name == "output_scope" and not g.passed for g in gates)
