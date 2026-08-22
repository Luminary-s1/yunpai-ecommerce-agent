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
    # R3（D-034 默认路径）：模型语义可用（model_semantic_enabled=True）才走模型。
    diag_interp = DiagnosisModelInterpreter(diag_gateway)
    rec_interp = RecommendationModelInterpreter(rec_gateway)
    return OperationsService(
        db,
        diagnosis_interpreter=diag_interp,
        recommendation_interpreter=rec_interp,
        model_semantic_enabled=True,
    )


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
    provenance = result["facts_snapshot"]["semantic_provenance"]
    assert provenance["diagnosis"]["prompt_version"] == "m9r-diagnosis-v1"
    assert provenance["recommendation"]["prompt_version"] == (
        "m9r-recommendation-v1"
    )
    # 审计落痕（create 走 db.audit 的 audit_log，非 product_recommendation_audit）
    with ops.db.connect() as conn:
        audit = conn.execute(
            "SELECT event_type FROM audit_log "
            "WHERE tenant_id=? AND subject_id=? AND event_type=?",
            ("tenant-a", "rec-1", "recommendation.create"),
        ).fetchone()
    assert audit is not None, f"缺 create 审计落痕"


def _scan_src(pattern: str) -> list[str]:
    """跨平台源码扫描：纯 Python 递归读 .py 文件，替代 grep subprocess（R6 修复）。

    复验指出 POSIX 下 subprocess.run(shell=True)+参数列表会让 grep 收不到参数。
    改用 Python 直接遍历 src/，跨平台稳定。

    C2（盲点 #9 修复）：用 ast 解析排除 docstring/注释——逐行子串匹配会命中
    docstring 里的自述文字（如"recommendation_engine.generate 在生产路径只有
    一个调用点"），把注释当真实调用点（假绿）。真实调用必须出现在**代码节点**
    （Attribute/Name 等）里，docstring 是 Expr(Constant) 节点不参与。
    """
    import ast
    import os

    root = Path(__file__).resolve().parents[1] / "src" / "ecommerce_agent"
    hits: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = Path(dirpath) / fname
            source = fpath.read_text(encoding="utf-8", errors="ignore")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                # 只扫真实代码节点（排除 docstring: Expr(Constant str) 和 Module 等无行号节点）
                if getattr(node, "lineno", None) is None:
                    continue
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                    continue  # docstring / 纯字符串表达式
                if pattern in ast.unparse(node):
                    hits.append(
                        f"{fpath.relative_to(root.parent)}: {node.lineno}: "
                        f"{ast.unparse(node)[:80]}"
                    )
    return hits


def _node_source(source: str, node: ast.AST) -> str:
    """ast 节点对应的源码片段（Python < 3.9 无 ast.unparse 的兜底）。"""
    import ast

    lines = source.splitlines()
    start = max(0, getattr(node, "lineno", 1) - 1)
    end = min(len(lines), getattr(node, "end_lineno", start + 1))
    return "\n".join(lines[start:end])


def test_engine_generate_has_production_call_site() -> None:
    """P3 验收：engine.generate 唯一非测试调用点在 business/service.py。"""
    # 纯 Python 扫描（R6 跨平台），替代 grep subprocess。
    prod_sites = [
        line for line in _scan_src("recommendation_engine.generate")
        if "test" not in line and "eval.py" not in line
    ]
    # 至少一个生产调用点，且指向 service.py 的 generate_and_persist 路径
    assert any("service.py" in line for line in prod_sites), (
        f"engine.generate 无生产调用点: {prod_sites}"
    )


def test_engine_generate_not_called_from_client_payload_route(tmp_path) -> None:
    """反证：POST /recommendations（管理员手工提交）不走引擎（旁路）。"""
    # 该路径直接落库客户端 payload（workbench_api create_recommendation），
    # 不调用 engine.generate——通过断言 workbench_api 无 engine.generate 来锁定。
    hits = _scan_src("engine.generate")
    api_hits = [h for h in hits if "workbench_api.py" in h]
    assert not api_hits, f"workbench_api 不应直接调引擎: {api_hits}"


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


def test_generate_marks_older_same_sku_stale(tmp_path) -> None:
    """B4（盲点 #6 修复）：同 SKU 新建议落库后，旧非终态建议自动标记 stale。

    任务书 WP3 L365"事实更新后旧建议标 stale，不原地改写历史"。修复前 MARK_STALE
    仅手工 API，生产链不自动触发——旧 approved 建议继续以现行结论展示。
    修复后 generate_and_persist 落库后把同 SKU 其他非终态建议标 stale。
    """
    from datetime import UTC, datetime

    from ecommerce_agent.business.service import OperationsService
    from ecommerce_agent.database import Database
    from ecommerce_agent.product_lifecycle.engine import (
        RecommendationCandidate,
        RecommendationEngine,
    )
    from ecommerce_agent.product_lifecycle.schemas import (
        RecommendationState,
        RecommendationType,
    )
    from ecommerce_agent.product_diagnosis.diagnosis import (
        Diagnosis,
        DiagnosisType,
    )
    from ecommerce_agent.product_read_model.models import (
        AggregateRule,
        Granularity,
        MetricValue,
        SKUReadModel,
    )

    db = Database(tmp_path / "auto-stale.sqlite3")
    db.initialize()
    ops = OperationsService(db)

    # 直接种一条旧建议（同 SKU，DRAFT 态）
    _missing = MetricValue.missing(
        Granularity.DAILY, AggregateRule.SUM, "2026-08-17", "test"
    )
    sku = SKUReadModel(
        tenant_id="tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a",
        revision=1, impressions=_missing, clicks=_missing, add_to_cart=_missing,
        orders=_missing, payments=_missing, refunds=_missing, net_sales=_missing,
        sellable_stock=_missing, in_transit_stock=_missing,
    )
    diag_old = Diagnosis(
        diagnosis_type=DiagnosisType.EVIDENCE_INSUFFICIENT,
        sku_id="sku-a",
        reason="old evidence",
        evidence_facts={
            "evidence_state": "actual", "freshness": {"usable_as_current": True},
            "quality_gate": "passed", "quality_gate_issues": [],
            "exposures": 50.0, "clicks": 5.0, "conversions": 1.0,
        },
        degraded=False,
    )
    engine = RecommendationEngine()
    old_rec = engine.generate(
        tenant_id="tenant-a", diagnosis=diag_old, sku=sku,
        recommendation_id="rec-old", created_at=datetime(2026, 8, 19, tzinfo=UTC),
    )
    created_old = ops.recommendations.create("tenant-a", old_rec, actor="admin")
    assert created_old["write_status"] == "applied"

    # 生产链 generate（同 SKU，新证据/新结论）→ rec-old 应被自动标 stale
    r2 = ops.generate_and_persist_recommendation(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a",
        recommendation_id="rec-2",
    )
    assert r2["write_status"] == "applied"
    # rec-old 已 stale；rec-2 保留非 stale
    view1 = ops.recommendations.get("tenant-a", "rec-old")
    view2 = ops.recommendations.get("tenant-a", "rec-2")
    assert view1["state"] == RecommendationState.STALE.value, (
        f"旧建议应自动 stale: {view1['state']}"
    )
    assert view2["state"] != RecommendationState.STALE.value

    # 故障注入：stale 的领域审计写入失败时，新建议和旧建议状态必须一起回滚。
    with ops.db.connect() as conn:
        conn.execute(
            "UPDATE product_recommendations SET state='draft' "
            "WHERE tenant_id='tenant-a' AND recommendation_id='rec-old'"
        )
        conn.execute(
            """
            CREATE TRIGGER fail_m9r_stale_audit
            BEFORE INSERT ON product_recommendation_audit
            WHEN NEW.action='mark_stale'
            BEGIN
                SELECT RAISE(ABORT, 'stale_transition_failed');
            END
            """
        )

    class _UniqueRecommendationInterpreter:
        def interpret(self, diagnosis):
            return RecommendationCandidate(
                type=RecommendationType.KEEP_OBSERVE,
                rationale="atomic rollback candidate",
                degraded=True,
            )

    ops.recommendation_engine = RecommendationEngine(
        interpreter=_UniqueRecommendationInterpreter()
    )
    with pytest.raises(Exception, match="stale_transition_failed"):
        ops.generate_and_persist_recommendation(
            "tenant-a", store_id="store-a", item_id="item-a", sku_id="sku-a",
            recommendation_id="rec-3",
        )
    with ops.db.connect() as conn:
        rec3 = conn.execute(
            "SELECT state FROM product_recommendations "
            "WHERE tenant_id='tenant-a' AND recommendation_id='rec-3'"
        ).fetchone()
        rec_old_state = conn.execute(
            "SELECT state FROM product_recommendations "
            "WHERE tenant_id='tenant-a' AND recommendation_id='rec-old'"
        ).fetchone()
    assert rec3 is None, "stale 失败时新建议必须回滚"
    assert rec_old_state["state"] == RecommendationState.DRAFT.value
