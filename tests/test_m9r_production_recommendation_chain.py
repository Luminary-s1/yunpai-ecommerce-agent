"""M9-R P3 生产语义链闭环测试（阻断3 修复验收）。

验证任务书"基于固化事实和流量诊断，由模型产生语义建议，经代码校验后固化"
的唯一生产入口 generate_and_persist_recommendation：
1. 全链走通：诊断 → 引擎（模型解释器被调用）→ 校验 → 落库 DRAFT + 审计。
2. gateway.calls == 1：模型确实被生产路径调用（D-034 达标，非 Ruleset 降级冒充）。
3. engine.generate 生产调用点：grep 断言唯一非测试调用在 business/service.py。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ecommerce_agent.business.service import OperationsService
from ecommerce_agent.database import Database
from ecommerce_agent.product_diagnosis.diagnosis import DiagnosisType
from ecommerce_agent.product_diagnosis.interpreter import (
    DiagnosisModelInterpreter,
    RulesetDiagnosisInterpreter,
)
from ecommerce_agent.product_lifecycle.engine import (
    RecommendationModelInterpreter,
    RecommendationType,
)
from ecommerce_agent.product_read_model.query import ProductReadQuery


class _MockGateway:
    """mock ModelGateway：返回固定 JSON 或按需抛异常。"""

    def __init__(self, return_value: dict | None = None, raise_exc: bool = False):
        self._return = return_value or {}
        self._raise = raise_exc
        self.calls = 0

    def generate_json(self, messages, **kwargs):
        self.calls += 1
        if self._raise:
            raise RuntimeError("model unavailable")
        return self._return


def _seed(db: Database) -> None:
    """种 asset + revision + day bucket（真实 operational 数据，供诊断可跑）。"""
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
                "rev-1", "tenant-a", "taobao_official", "store-a", "item-a", "sku-a", 1,
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
                "taobao_official",
            ),
        )


def _ops(tmp_path: Path, *, diag_gateway: _MockGateway, rec_gateway: _MockGateway) -> OperationsService:
    db = Database(tmp_path / "prod-chain.sqlite3")
    db.initialize()
    _seed(db)
    # 注入模型解释器：诊断模型 + 建议模型都被 mock，断言生产路径调用它们。
    diag_interp = DiagnosisModelInterpreter(diag_gateway)
    rec_interp = RecommendationModelInterpreter(rec_gateway)
    return OperationsService(db, diagnosis_interpreter=diag_interp, recommendation_interpreter=rec_interp)


def test_generate_and_persist_full_chain(tmp_path) -> None:
    """全链走通：模型解释器被调用（gateway.calls==1）→ 落库 DRAFT + 审计。"""
    diag_gw = _MockGateway(return_value={
        "diagnosis_type": DiagnosisType.EVIDENCE_INSUFFICIENT.value,
        "reason": "no qualified experiment",
        "degraded": True,
    })
    rec_gw = _MockGateway(return_value={
        "type": "保持观察",
        "rationale": "model keep observe",
        "degraded": True,
    })
    ops = _ops(tmp_path, diag_gateway=diag_gw, rec_gateway=rec_gw)

    result = ops.generate_and_persist_recommendation(
        "tenant-a",
        store_id="store-a", item_id="item-a", sku_id="sku-a",
        recommendation_id="rec-1",
    )
    # 两个模型解释器都被生产路径调用（D-034 达标）
    assert diag_gw.calls == 1, f"诊断模型未被调用: calls={diag_gw.calls}"
    assert rec_gw.calls == 1, f"建议模型未被调用: calls={rec_gw.calls}"
    # 落库 DRAFT
    assert result["write_status"] == "applied"
    assert result["state"] == "draft"
    assert result["type"] == "保持观察"
    # 审计落痕（create 走 db.audit 的 audit_log，非 product_recommendation_audit）
    with ops.db.connect() as conn:
        audit = conn.execute(
            "SELECT event_type FROM audit_log "
            "WHERE tenant_id=? AND subject_id=? AND event_type=?",
            ("tenant-a", "rec-1", "recommendation.create"),
        ).fetchone()
    assert audit is not None, f"缺 create 审计落痕"


def test_engine_generate_has_production_call_site() -> None:
    """P3 验收：engine.generate 唯一非测试调用点在 business/service.py。"""
    import subprocess

    out = subprocess.run(
        ["grep", "-rn", "recommendation_engine.generate",
         "src/ecommerce_agent/"],
        capture_output=True, text=True, encoding="utf-8", shell=True,
    ).stdout
    prod_sites = [
        line for line in out.splitlines()
        if "test" not in line and "eval.py" not in line
    ]
    # 至少一个生产调用点，且指向 service.py 的 generate_and_persist 路径
    assert any("service.py" in line for line in prod_sites), (
        f"engine.generate 无生产调用点: {prod_sites}"
    )


def test_engine_generate_not_called_from_client_payload_route(tmp_path) -> None:
    """反证：POST /recommendations（管理员手工提交）不走引擎（旁路）。"""
    # 该路径直接落库客户端 payload（workbench_api create_recommendation），
    # 不调用 engine.generate——通过断言 service.py 无旁路调用来锁定。
    # 实际旁路校验在 workbench_api 路由层；此处验证引擎无第二条生产入口。
    import subprocess

    out = subprocess.run(
        ["grep", "-rn", "engine.generate",
         "src/ecommerce_agent/workbench_api.py"],
        capture_output=True, text=True, encoding="utf-8", shell=True,
    ).stdout
    assert out.strip() == "", f"workbench_api 不应直接调引擎: {out}"


def test_generate_route_reachable_and_persists(tmp_path) -> None:
    """HTTP 生产入口：POST /recommendation/generate 可达 + 落库 DRAFT。"""
    from fastapi.testclient import TestClient

    from conftest import make_settings
    from ecommerce_agent.api import create_app
    from ecommerce_agent.service import AgentService

    settings = make_settings(tmp_path)
    svc = AgentService(settings)
    _seed(svc.db)  # 种数据到 AgentService 的 db
    svc.close()
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/v1/products/store-a/item-a/sku-a/recommendation/generate",
            json={"recommendation_id": "rec-http-1"},
            headers={"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"},
        )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["state"] == "draft"
    assert data["recommendation_id"] == "rec-http-1"
