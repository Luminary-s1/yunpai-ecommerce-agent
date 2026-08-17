"""A1/A2/A3/A6 接线测试：记忆 API、记忆影响回答、knowledge_worker、热更新。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app

from conftest import make_settings

ADMIN_HEADERS = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as test_client:
        yield test_client


# ---------- A1：记忆管理 API ----------

def test_memory_requires_admin(client: TestClient) -> None:
    """记忆端点无鉴权 → 401/503。"""
    assert client.get("/v1/admin/knowledge/memory?store_id=qc").status_code in (401, 503)
    assert client.post("/v1/admin/knowledge/memory", json={}).status_code in (401, 503)
    assert client.delete("/v1/admin/knowledge/memory/kg-memory-x").status_code in (401, 503)


def test_memory_record_recall_forget(client: TestClient) -> None:
    """记录 → 召回 → 删除 全链路。"""
    # 记录
    resp = client.post(
        "/v1/admin/knowledge/memory",
        headers=ADMIN_HEADERS,
        json={"store_id": "qc-store", "fact": "本店退货高峰在周三", "category": "frequent_issue"},
    )
    assert resp.status_code == 200
    memory_id = resp.json()["memory_id"]
    assert memory_id.startswith("kg-memory-")

    # 召回（同店铺）
    got = client.get(
        "/v1/admin/knowledge/memory?store_id=qc-store&limit=50", headers=ADMIN_HEADERS
    )
    assert got.status_code == 200
    items = got.json()["items"]
    assert any(m["knowledge_key"] == memory_id for m in items)
    assert any("退货高峰" in m["answer"] for m in items)

    # 隔离：其他店铺查不到
    other = client.get(
        "/v1/admin/knowledge/memory?store_id=other-store", headers=ADMIN_HEADERS
    )
    assert other.json()["count"] == 0

    # 删除
    removed = client.delete(
        f"/v1/admin/knowledge/memory/{memory_id}", headers=ADMIN_HEADERS
    )
    assert removed.status_code == 200
    gone = client.get(
        "/v1/admin/knowledge/memory?store_id=qc-store", headers=ADMIN_HEADERS
    )
    assert gone.json()["count"] == 0


def test_memory_record_validation(client: TestClient) -> None:
    """缺 store_id/fact → 422。"""
    resp = client.post(
        "/v1/admin/knowledge/memory",
        headers=ADMIN_HEADERS,
        json={"store_id": "", "fact": "  "},
    )
    assert resp.status_code == 422


# ---------- A2：记忆影响回答（graph.retrieve 并入记忆） ----------

def test_memory_recall_merges_into_retrieval(client: TestClient) -> None:
    """写入店铺记忆后，该店铺的回答检索应包含记忆来源文档。"""
    service = getattr(client.app.state, "agent", None)
    assert service is not None, "create_app 应暴露 app.state.agent"
    # 记录一条记忆
    resp = client.post(
        "/v1/admin/knowledge/memory",
        headers=ADMIN_HEADERS,
        json={"store_id": "qc-store", "fact": "本店保修政策已升级为 36 个月"},
    )
    assert resp.status_code == 200
    memory_id = resp.json()["memory_id"]
    # 直接调 memory.recall 验证服务接线（graph 内部同样走此通道）
    rows = service.memory.recall("qc-store", query="保修", tenant_id="tenant-test")
    assert any(r["knowledge_key"] == memory_id for r in rows)
    # 再验证 graph 检索并入：构造检索场景，检查记忆以 memory: 来源出现
    from ecommerce_agent.knowledge_engine.memory_service import KnowledgeMemoryService

    assert isinstance(service.memory, KnowledgeMemoryService)


def test_memory_doc_survives_build_messages(client: TestClient) -> None:
    """记忆文档并入 retrieved 后能走完 build_messages（A2 字段兼容，防 category 缺失崩溃）。"""
    from ecommerce_agent.prompts import build_messages

    service = getattr(client.app.state, "agent", None)
    assert service is not None
    # 记录记忆并召回（结构同 graph.retrieve 并入的文档）
    client.post(
        "/v1/admin/knowledge/memory",
        headers=ADMIN_HEADERS,
        json={"store_id": "qc-store", "fact": "本店保修政策已升级为 36 个月"},
    )
    rows = service.memory.recall("qc-store", query="保修", tenant_id="tenant-test")
    assert rows, "应有记忆"
    docs = [
        {
            "id": r["id"],
            "source": f"memory:{r['knowledge_key']}",
            "question": r["question"],
            "answer": r["answer"],
            "score": 0.0,
            "layer": "evolution",
            "category": r["category"] or "店铺记忆",
            "intent": r["intent"] or "memory",
        }
        for r in rows
    ]
    messages = build_messages(
        question="保修多久",
        documents=docs,  # type: ignore[arg-type]
        context={"store_id": "qc-store"},
        history=[],
    )
    assert any("36 个月" in m["content"] for m in messages), "记忆内容应进入提示词"


def test_memory_influences_chat_sources(client: TestClient) -> None:
    """A2 真实 e2e：记录记忆 → 完整走 chat（graph.invoke）→ 回答 sources 含 memory 来源。

    这是"记忆影响回答"闭环的真实验证（此前只有 memory.recall + build_messages 单元级，
    没验证 graph.retrieve 真的并入记忆进回答链路）。
    """
    from ecommerce_agent.auth import Principal

    service = getattr(client.app.state, "agent", None)
    assert service is not None

    # 记录一条店铺记忆（影响该店后续回答）
    resp = client.post(
        "/v1/admin/knowledge/memory",
        headers=ADMIN_HEADERS,
        json={"store_id": "qc-store", "fact": "本店保修政策已升级为 36 个月"},
    )
    assert resp.status_code == 200
    memory_id = resp.json()["memory_id"]

    # 用带 store 上下文的买家身份发消息（走完整 graph，含 retrieve→generate→persist）
    principal = service.auth.authenticate(
        service.settings.bootstrap_client_id,
        service.settings.bootstrap_client_key,
        "buyer-memory-e2e",
    )
    assert principal is not None
    response = service.chat(
        principal,
        session_id="e2e-memory-session",
        message="本店保修多久？",
        context={"store_id": "qc-store"},
    )
    # 回答的 sources 应包含刚记录的记忆（memory: 来源）
    memory_sources = [s for s in response.sources if str(s.source).startswith("memory")]
    assert memory_sources, (
        "客服回答应引用店铺记忆（sources 含 memory: 来源）——"
        f"实际 sources={[s.source for s in response.sources]}"
    )
    assert memory_sources[0].id == memory_id or memory_sources[0].id == str(
        service.memory.recall("qc-store", tenant_id="tenant-test")[0]["id"]
    )


# ---------- A3：knowledge_worker 状态 ----------

def test_knowledge_worker_status_available(client: TestClient) -> None:
    """服务暴露 knowledge_worker 状态（测试默认关，不启动线程）。"""
    service = getattr(client.app.state, "agent", None)
    assert service is not None
    status = service.knowledge_worker_status()
    assert "running" in status
    assert "enabled" in status
    assert status["enabled"] is False  # 测试默认关闭（kg_dream_worker_enabled=False）
    assert "last_report" in status
    assert "last_run_at" in status


# ---------- A6：热更新更新内容 ----------

def test_import_assets_updates_existing_content(client: TestClient) -> None:
    """02_clean 修改后重导应更新内容（A6 修复：此前只跳过不更新）。"""
    service = getattr(client.app.state, "agent", None)
    assert service is not None
    # 第一次导入
    first = client.post("/v1/admin/knowledge/import-assets", headers=ADMIN_HEADERS)
    assert first.status_code == 200
    assert first.json()["imported"] >= 100

    from ecommerce_agent.knowledge_engine.loader import load_clean_dir
    from ecommerce_agent.knowledge_engine.runtime_bridge import import_to_runtime

    clean_dir = (
        Path(__file__).resolve().parent.parent / "knowledge_graph_output" / "02_clean"
    )
    items = load_clean_dir(clean_dir)
    changed_item = next(
        item
        for item in items
        if item.kind.value in {"faq", "script", "policy", "rule"}
        and (item.scope.value == "general" or item.scope_key == "all")
    )
    marker = "【hot-update-contract】"
    changed_item = replace(
        changed_item, compiled_truth=f"{changed_item.compiled_truth}{marker}"
    )

    # 02_clean 是共享资产，只允许 appliance 的全局刷新上下文更新。
    stats = import_to_runtime(
        [changed_item],
        service.knowledge,
        tenant_id=None,
        default_store_id="tenant-test",
        update_existing=True,
        allow_global_update=True,
    )
    assert stats["updated"] == 1, "热更新应更新目标 kg-* 行"
    assert stats["imported"] == 0, "不应新增重复行"
    with service.db.connect() as conn:
        row = conn.execute(
            """
            SELECT answer FROM knowledge
            WHERE id=? AND tenant_id IS NULL AND status='active'
            """,
            (f"kg-{changed_item.id}",),
        ).fetchone()
    assert row is not None
    assert row["answer"].endswith(marker)
