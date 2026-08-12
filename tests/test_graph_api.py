"""图谱检索 API 测试：实体查询、关系遍历、多跳推理、检索、统计。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.config import Settings


def _make_settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        model_provider="glm",
        model_base_url="http://127.0.0.1:9/v1",
        model_name="test-14b",
        model_api_key="",
        model_timeout_seconds=0.2,
        model_max_output_tokens=200,
        model_temperature=0.0,
        model_thinking_enabled=False,
        model_streaming=False,
        model_retry_attempts=0,
        model_enabled=False,
        model_mock_mode=True,
        model_context_limit_tokens=128000,
        context_budget_ratio=0.7,
        rag_top_k=5,
        rag_min_score=0.08,
        rag_direct_approved_answer=True,
        rag_direct_approved_min_score=0.6,
        max_input_chars=2000,
        session_history_limit=6,
        admin_api_key="test-admin-key-123456",
        admin_auth_required=True,
        bootstrap_admin_id="admin-test",
        auth_required=True,
        bootstrap_tenant_id="tenant-test",
        bootstrap_client_id="client-test",
        bootstrap_client_key="test-client-key-12345",
        bootstrap_client_can_supply_order_context=False,
        subject_hash_key="test-subject-hash-key-12345",
        session_idle_timeout_minutes=120,
        message_retention_days=30,
        audit_retention_days=180,
        max_request_body_bytes=2048,
        rate_limit_requests_per_minute=100,
        min_free_disk_mb=1,
    )


ADMIN_HEADERS = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}


pytestmark = pytest.mark.usefixtures("mock_neo4j_query")


@pytest.fixture(scope="module")
def client(tmp_path_factory) -> TestClient:
    data_dir = tmp_path_factory.mktemp("graph-api")
    app = create_app(_make_settings(data_dir))
    with TestClient(app) as test_client:
        yield test_client


def test_graph_entity_api(client: TestClient) -> None:
    """实体查询：找到 SKU。"""
    resp = client.get("/v1/graph/entity/SKU/sku_id/QC-AF5-WHITE", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "QC-AF5-WHITE"
    assert "title" in data["properties"]


def test_graph_entity_not_found(client: TestClient) -> None:
    """实体查询：不存在返回 404。"""
    resp = client.get("/v1/graph/entity/SKU/sku_id/NO-SUCH", headers=ADMIN_HEADERS)
    assert resp.status_code == 404


def test_graph_relations_api(client: TestClient) -> None:
    """关系遍历：SKU 的 BELONGS_TO。"""
    resp = client.get("/v1/graph/relations/QC-AF5-WHITE?rel_type=BELONGS_TO", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    rels = resp.json()
    assert len(rels) > 0
    assert rels[0]["target"] == "air_fryer"


def test_graph_multi_hop_api(client: TestClient) -> None:
    """多跳推理：SKU -> 品类 <- 政策（APPLIES_TO 反向）。"""
    resp = client.post(
        "/v1/graph/multi-hop",
        headers=ADMIN_HEADERS,
        json={"start_id": "QC-AF5-WHITE", "rel_types": ["BELONGS_TO", "APPLIES_TO-"]},
    )
    assert resp.status_code == 200
    paths = resp.json()
    assert len(paths) > 0
    assert any("七天" in str(p) for p in paths)


def test_graph_search_api(client: TestClient) -> None:
    """关键词检索。"""
    resp = client.get("/v1/graph/search?q=保修", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) > 0


def test_graph_stats_api(client: TestClient) -> None:
    """图谱统计。"""
    resp = client.get("/v1/graph/stats", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    stats = resp.json()
    assert stats["total_nodes"] >= 200
    assert stats["total_rels"] >= 200


def test_graph_requires_admin(client: TestClient) -> None:
    """无鉴权返回 401。"""
    resp = client.get("/v1/graph/stats")
    assert resp.status_code in (401, 503)  # admin 未配置时 503，否则 401
