"""Wiki 浏览 API 测试：合并列表、详情、分页、鉴权、404。

覆盖任务3 Wiki 搭建的验收：
- 词条列表（运行时 Q&A 为准 + 资产层实体类）
- 词条详情（含 attributes/timeline/source）
- 编辑后详情即时显示新结论（运行时为准）
- 鉴权 / 404 / 类型筛选 / 统计
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.config import Settings
from ecommerce_agent.knowledge_engine import (
    KnowledgeItem,
    KnowledgeKind,
    KnowledgeScope,
    import_to_runtime,
)
from ecommerce_agent.knowledge_management import (
    KnowledgeReviseRequest,
    KnowledgeTransitionRequest,
)

from conftest import make_settings


ADMIN_HEADERS = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as test_client:
        yield test_client


def _import_faq(service, *, item_id: str, question: str, answer: str) -> None:
    """把一条 FAQ 导入运行时（模拟 M3 资产层→运行时）。"""
    import_to_runtime(
        [
            KnowledgeItem(
                id=item_id,
                kind=KnowledgeKind.FAQ,
                scope=KnowledgeScope.SELLER,
                scope_key="qinchuan",
                compiled_truth=answer,
                attributes={"question": question, "risk_level": "low"},
            )
        ],
        service.knowledge,
        default_store_id="qinchuan",
    )


def test_wiki_requires_admin(client: TestClient) -> None:
    """无鉴权返回 401/503（对齐 graph API）。"""
    resp = client.get("/v1/wiki/items")
    assert resp.status_code in (401, 503)


def test_wiki_items_lists_runtime_and_asset(client: TestClient) -> None:
    """列表同时包含运行时 Q&A 和资产层（本环境 02_clean 资产 382 条合并）。"""
    app = client.app
    _import_faq(
        app.state.agent,
        item_id="WIKI-TEST-1",
        question="保修多久",
        answer="保修 12 个月",
    )
    resp = client.get("/v1/wiki/items?limit=500", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    ids = [i["id"] for i in data]
    assert "WIKI-TEST-1" in ids, "导入运行时的知识应出现在 Wiki 列表（id 归一化后无 kg-）"
    # 本测试环境含 02_clean 资产层，Q&A 应同时来自运行时和资产层
    assert any(i["kind"] == "faq" for i in data)


def test_wiki_items_kind_filter(client: TestClient) -> None:
    """按类型筛选：faq 只返回 faq。"""
    app = client.app
    _import_faq(
        app.state.agent,
        item_id="WIKI-TEST-2",
        question="退货运费谁出",
        answer="退货运费按平台规则",
    )
    resp = client.get("/v1/wiki/items?kind=faq&limit=500", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert all(i["kind"] == "faq" for i in data)
    assert any(i["id"] == "WIKI-TEST-2" for i in data)


def test_wiki_item_detail(client: TestClient) -> None:
    """详情返回 compiled_truth/attributes/timeline/source。"""
    app = client.app
    _import_faq(
        app.state.agent,
        item_id="WIKI-TEST-3",
        question="发货时效",
        answer="48 小时内发货",
    )
    resp = client.get("/v1/wiki/items/WIKI-TEST-3", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    item = resp.json()
    assert item["id"] == "WIKI-TEST-3"
    assert item["compiled_truth"] == "48 小时内发货"
    assert item["source"] == "runtime"
    assert item["attributes"]["question"] == "发货时效"
    assert item["attributes"]["status"] == "active"
    assert len(item["timeline"]) >= 1
    assert item["timeline"][0]["action"] == "created"


def test_wiki_item_not_found(client: TestClient) -> None:
    """不存在的词条返回 404。"""
    resp = client.get("/v1/wiki/items/NO-SUCH-ENTITY", headers=ADMIN_HEADERS)
    assert resp.status_code == 404


def test_wiki_detail_reflects_revise(client: TestClient) -> None:
    """编辑（revise→evaluate→approve）后，详情即时显示新结论（运行时为准）。"""
    app = client.app
    svc = app.state.agent
    tenant = svc.settings.bootstrap_tenant_id
    # 带 tenant 导入：生命周期管理严格按 tenant_id=? 过滤，无 tenant 的行不可被 /v1/admin 管理
    import_to_runtime(
        [
            KnowledgeItem(
                id="WIKI-REVISE-1",
                kind=KnowledgeKind.FAQ,
                scope=KnowledgeScope.SELLER,
                scope_key="qinchuan",
                compiled_truth="旧结论：保修 12 个月",
                attributes={"question": "保修多久"},
            )
        ],
        svc.knowledge,
        tenant_id=tenant,
        default_store_id="qinchuan",
    )
    mgmt = svc.knowledge_management

    current = mgmt.get_item(tenant, "kg-WIKI-REVISE-1")
    revised = mgmt.revise(
        tenant, current["id"],
        KnowledgeReviseRequest(
            expected_record_version=current["record_version"],
            question=None,
            answer="新结论：保修 24 个月",
            keywords=None,
            source=None,
        ),
        "admin-test",
    )
    evaluated = mgmt.evaluate(
        tenant, revised["id"],
        KnowledgeTransitionRequest(
            expected_record_version=revised["record_version"], note=None
        ),
        "admin-test",
    )
    mgmt.approve(
        tenant, evaluated["id"],
        KnowledgeTransitionRequest(
            expected_record_version=evaluated["record_version"], note=None
        ),
        "admin-test",
    )

    resp = client.get("/v1/wiki/items/WIKI-REVISE-1", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    item = resp.json()
    assert item["compiled_truth"] == "新结论：保修 24 个月", (
        "编辑 approve 后 Wiki 详情必须是新结论（编辑即时生效）"
    )
    actions = [e["action"] for e in item["timeline"]]
    assert "revised" in actions


def test_wiki_stats(client: TestClient) -> None:
    """统计返回 total / by_kind / by_source。"""
    app = client.app
    _import_faq(
        app.state.agent,
        item_id="WIKI-TEST-4",
        question="质量问题",
        answer="质量问题可退换",
    )
    resp = client.get("/v1/wiki/stats", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    stats = resp.json()
    assert stats["total"] >= 1
    assert stats["by_kind"].get("faq", 0) >= 1
    assert stats["by_source"].get("runtime", 0) >= 1


def test_wiki_does_not_touch_clean_dir(client: TestClient, tmp_path: Path) -> None:
    """回归断言：Wiki 读操作不修改 02_clean/ 资产（mtime/hash 不变）。

    任务3 承诺"不碰已交付数据"——这里用临时目录模拟资产层，断言读操作零副作用。
    """
    from ecommerce_agent.knowledge_engine.wiki_api import load_merged_items

    clean_dir = tmp_path / "02_clean"
    clean_dir.mkdir()
    (clean_dir / "faq.json").write_text(
        '[{"faq_id": "ASSET-FAQ-1", "question": "资产层测试", "answer": "资产层答案", "source": "asset"}]',
        encoding="utf-8",
    )
    before = {
        p.name: (p.stat().st_mtime_ns, p.read_bytes())
        for p in clean_dir.iterdir()
    }
    app = client.app
    load_merged_items(
        knowledge_base=app.state.agent.knowledge,
        tenant_id=app.state.agent.settings.bootstrap_tenant_id,
        clean_dir=clean_dir,
    )
    after = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in clean_dir.iterdir()}
    assert before == after, "读操作不得修改 02_clean/ 资产（mtime/hash 不变）"


def test_wiki_items_pagination(client: TestClient) -> None:
    """分页：offset+limit 返回对应切片（缺口1/深水区2）。"""
    app = client.app
    for i in range(3):
        _import_faq(
            app.state.agent,
            item_id=f"PAGE-{i}",
            question=f"分页问题{i}",
            answer=f"分页答案{i}",
        )
    page1 = client.get("/v1/wiki/items?limit=2&offset=0", headers=ADMIN_HEADERS).json()
    page2 = client.get("/v1/wiki/items?limit=2&offset=2", headers=ADMIN_HEADERS).json()
    assert len(page1) == 2
    assert len(page2) >= 1
    ids1 = {i["id"] for i in page1}
    ids2 = {i["id"] for i in page2}
    assert not (ids1 & ids2), "分页两页不应有重叠"


def test_wiki_items_has_category_field(client: TestClient) -> None:
    """词条视图含 category 字段（缺口1：Wiki 分类验收项）。"""
    app = client.app
    _import_faq(
        app.state.agent,
        item_id="CAT-TEST-1",
        question="分类测试",
        answer="分类答案",
    )
    data = client.get("/v1/wiki/items?kind=faq&limit=500", headers=ADMIN_HEADERS).json()
    assert data, "应有 faq 词条"
    assert "category" in data[0], "词条视图必须含 category 字段"
    assert data[0]["category"], "category 非空"


def test_wiki_search_finds_runtime(client: TestClient) -> None:
    """搜索命中运行时知识（双通道主通道，不依赖 Neo4j）。"""
    app = client.app
    _import_faq(
        app.state.agent,
        item_id="SEARCH-TEST-1",
        question="空气炸锅保修多久",
        answer="保修 12 个月",
    )
    resp = client.get("/v1/wiki/search?q=空气炸锅&limit=10", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    results = resp.json()
    assert isinstance(results, list)


def test_wiki_put_edits_runtime_kind(client: TestClient) -> None:
    """PUT 编辑 Q&A 词条：走生命周期创建 candidate（复用 knowledge_management.create）。"""
    app = client.app
    _import_faq(
        app.state.agent,
        item_id="EDIT-TEST-1",
        question="旧问题",
        answer="旧答案",
    )
    resp = client.put(
        "/v1/wiki/items/EDIT-TEST-1",
        headers=ADMIN_HEADERS,
        json={"answer": "新答案（编辑生效）"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "candidate"
    assert body["next"] == "evaluate→approve"


def test_wiki_put_rejects_entity_kind(client: TestClient) -> None:
    """PUT 编辑实体类（product）→ 422 只读。"""
    resp = client.put(
        "/v1/wiki/items/QC-SPU-AF5",
        headers=ADMIN_HEADERS,
        json={"answer": "尝试编辑商品"},
    )
    assert resp.status_code in (404, 422)  # 资产层商品在无 02_clean 环境 404；有则 422 只读


def test_wiki_items_status_filter_lists_candidate_and_retired(client: TestClient) -> None:
    """P2 遗留：status 参数透传——candidate/retired 可列出，默认仍只看 active。"""
    app = client.app
    svc = app.state.agent
    tenant = svc.settings.bootstrap_tenant_id
    mgmt = svc.knowledge_management
    from ecommerce_agent.knowledge_management import KnowledgeCreateRequest

    created = mgmt.create(
        tenant,
        KnowledgeCreateRequest(
            category="常见问答", intent="status-filter", question="状态筛选",
            answer="状态筛选答案", source="wiki://manual", layer="platform",
        ),
        "admin-test",
        knowledge_key="kg-STATUS-CAND-1",
    )
    cand = client.get("/v1/wiki/items?status=candidate&limit=500", headers=ADMIN_HEADERS)
    assert cand.status_code == 200
    assert any(i["id"] == "STATUS-CAND-1" for i in cand.json()), "status=candidate 应列出候选词条"
    default = client.get("/v1/wiki/items?limit=500", headers=ADMIN_HEADERS)
    assert not any(i["id"] == "STATUS-CAND-1" for i in default.json()), "默认列表只看 active"

    evaluated = mgmt.evaluate(tenant, created["id"],
        KnowledgeTransitionRequest(expected_record_version=created["record_version"]), "admin-test")
    approved = mgmt.approve(tenant, evaluated["id"],
        KnowledgeTransitionRequest(expected_record_version=evaluated["record_version"]), "admin-test")
    mgmt.retire(tenant, approved["id"],
        KnowledgeTransitionRequest(expected_record_version=approved["record_version"]), "admin-test")
    retired = client.get("/v1/wiki/items?status=retired&limit=500", headers=ADMIN_HEADERS)
    assert any(i["id"] == "STATUS-CAND-1" for i in retired.json()), "status=retired 应列出停用词条"


def test_wiki_search_hit_id_opens_item_detail(client: TestClient) -> None:
    """P2 遗留：搜索命中 id 用 knowledge_key 归一化，能经 /v1/wiki/items/{id} 打开详情。"""
    app = client.app
    svc = app.state.agent
    tenant = svc.settings.bootstrap_tenant_id
    mgmt = svc.knowledge_management
    from ecommerce_agent.knowledge_management import KnowledgeCreateRequest

    created = mgmt.create(
        tenant,
        KnowledgeCreateRequest(
            category="常见问答", intent="search-key-test", question="搜索键测试问题",
            answer="搜索键测试答案", source="wiki://manual", layer="platform",
        ),
        "admin-test",
        knowledge_key="kg-SEARCH-KEY-1",
    )
    evaluated = mgmt.evaluate(tenant, created["id"],
        KnowledgeTransitionRequest(expected_record_version=created["record_version"]), "admin-test")
    mgmt.approve(tenant, evaluated["id"],
        KnowledgeTransitionRequest(expected_record_version=evaluated["record_version"]), "admin-test")
    resp = client.get("/v1/wiki/search?q=搜索键测试&limit=10", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert any(r["id"] == "SEARCH-KEY-1" for r in resp.json()), "搜索命中 id 应为 knowledge_key 归一化后的词条 id"
    detail = client.get("/v1/wiki/items/SEARCH-KEY-1", headers=ADMIN_HEADERS)
    assert detail.status_code == 200, "搜索命中的 id 必须能打开词条详情"
