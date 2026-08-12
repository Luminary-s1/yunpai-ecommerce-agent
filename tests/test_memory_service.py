"""知识库 memory 层测试（P1-2）：per-store 长期记忆写入/召回/隔离/删除。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ecommerce_agent.knowledge_engine import KnowledgeMemoryService
from ecommerce_agent.service import AgentService

from conftest import make_settings


@pytest.fixture()
def service(tmp_path: Path) -> AgentService:
    svc = AgentService(make_settings(tmp_path))
    yield svc
    svc.close()


def test_record_and_recall(service: AgentService) -> None:
    """写入 + 显式召回（per-store）。"""
    mem = KnowledgeMemoryService(service.knowledge)
    mem.record(
        "store-a", fact="A店退货高峰在周三",
        category="frequent_issue", source="chat://test",
        tenant_id=service.settings.bootstrap_tenant_id,
    )
    rows = mem.recall("store-a", tenant_id=service.settings.bootstrap_tenant_id)
    assert len(rows) == 1
    assert rows[0]["answer"] == "A店退货高峰在周三"
    assert rows[0]["layer"] == "evolution"


def test_store_isolation(service: AgentService) -> None:
    """隔离：B 店查不到 A 店记忆。"""
    mem = KnowledgeMemoryService(service.knowledge)
    mem.record("store-a", fact="A店秘密", tenant_id=service.settings.bootstrap_tenant_id)
    mem.record("store-b", fact="B店公开", tenant_id=service.settings.bootstrap_tenant_id)
    rows_a = mem.recall("store-a", tenant_id=service.settings.bootstrap_tenant_id)
    rows_b = mem.recall("store-b", tenant_id=service.settings.bootstrap_tenant_id)
    assert [r["answer"] for r in rows_a] == ["A店秘密"]
    assert [r["answer"] for r in rows_b] == ["B店公开"]


def test_memory_not_in_default_retrieval(service: AgentService) -> None:
    """默认隔离：普通 RAG 检索不命中 memory（显式召回才进）。"""
    mem = KnowledgeMemoryService(service.knowledge)
    mem.record(
        "store-a", fact="非常特殊的记忆内容XYZ123",
        tenant_id=service.settings.bootstrap_tenant_id,
    )
    hits = service.knowledge.retrieve(
        "非常特殊的记忆内容XYZ123", top_k=3, min_score=0.01,
        tenant_id=service.settings.bootstrap_tenant_id,
    )
    assert not any(
        str(h.get("source", "")).startswith("memory")
        for h in hits
    ), "普通检索不应命中 memory 层"


def test_recall_keyword_filter(service: AgentService) -> None:
    """关键词过滤召回。"""
    mem = KnowledgeMemoryService(service.knowledge)
    mem.record("store-a", fact="退货流程说明", tenant_id=service.settings.bootstrap_tenant_id)
    mem.record("store-a", fact="新品上市预告", tenant_id=service.settings.bootstrap_tenant_id)
    rows = mem.recall("store-a", query="退货", tenant_id=service.settings.bootstrap_tenant_id)
    assert len(rows) == 1
    assert rows[0]["answer"] == "退货流程说明"


def test_forget(service: AgentService) -> None:
    """删除记忆。"""
    mem = KnowledgeMemoryService(service.knowledge)
    kid = mem.record("store-a", fact="要删除的记忆", tenant_id=service.settings.bootstrap_tenant_id)
    assert mem.forget(kid) is True
    rows = mem.recall("store-a", tenant_id=service.settings.bootstrap_tenant_id)
    assert rows == []


def test_record_idempotent(service: AgentService) -> None:
    """防呆：同店铺同内容重复写只存一条，返回同一 id。"""
    mem = KnowledgeMemoryService(service.knowledge)
    first = mem.record(
        "store-a", fact="重复内容", tenant_id=service.settings.bootstrap_tenant_id
    )
    second = mem.record(
        "store-a", fact="重复内容", tenant_id=service.settings.bootstrap_tenant_id
    )
    assert first == second
    rows = mem.recall("store-a", tenant_id=service.settings.bootstrap_tenant_id)
    assert len(rows) == 1
