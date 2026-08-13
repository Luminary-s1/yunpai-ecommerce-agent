"""M3 多租户隔离测试（专项审查 P1×2 + P2×1 + P3×6 修复的回归锁）。

核心语义：NULL 租户行 = 全局知识，只可被 appliance 自身（启动导入）写；
租户 API 永远只能写本租户行。租户影子编辑：租户编辑全局词条 → 生成该租户
私有新版本，其他租户仍见全局版。

覆盖（对应修复计划 v2）：
- P1-1 租户 approve/rollback/complete_rollout 不得退休全局行
- 影子编辑：租户 A 编辑全局词条 approve 后，A 优先见影子版、B 仍见全局版
- P1-2 租户 import-assets 热更新不得改写全局行
- P2-1 general/无店铺 seller 资产导入后 tenant_id IS NULL
- P3-9 跨租户同 store 同 fact 记忆各自落库
- P3-5 forget 精确租户（租户删不掉全局记忆）
- P3-4 load_from_runtime(None) 只返回全局行
"""

from __future__ import annotations

import pytest

from ecommerce_agent.knowledge_engine import (
    KnowledgeItem,
    KnowledgeKind,
    KnowledgeScope,
    import_to_runtime,
    load_from_runtime,
)
from ecommerce_agent.knowledge_management import (
    KnowledgeCreateRequest,
    KnowledgeTransitionRequest,
)
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


# ---------- P1-1：租户不得退休全局行 ----------

def _create_global_active(service: AgentService, key: str, answer: str = "全局规则答案") -> str:
    """建一条全局（tenant_id IS NULL）active 行，模拟 02_clean 全局资产。"""
    return service.knowledge.add_document(
        category="行业规则", intent="rule", question="全局规则问题",
        answer=answer, keywords="", risk_level="low", source="kg:test",
        version=1, status="active", review_status="approved",
        tenant_id=None, knowledge_key=f"kg-{key}", layer="platform",
        store_id=None, sku_id=None,
    )


def test_tenant_approve_does_not_retire_global_row(tmp_path) -> None:
    """P1-1：租户 A 影子编辑全局词条 approve 后，全局 active 行必须仍在。"""
    service = AgentService(make_settings(tmp_path))
    mgmt = service.knowledge_management
    tenant = "tenant-a"

    _create_global_active(service, "GLOBAL-X")

    # 租户 A 影子编辑：同 knowledge_key 建私有 candidate（layer=platform 无 store）
    created = mgmt.create(
        tenant,
        KnowledgeCreateRequest(
            category="行业规则", intent="rule", question="全局规则问题",
            answer="租户A定制答案", source="wiki://manual", layer="platform",
        ),
        "admin-a",
        knowledge_key="kg-GLOBAL-X",
    )
    evaluated = mgmt.evaluate(
        tenant, created["id"], KnowledgeTransitionRequest(expected_record_version=1), "reviewer-a"
    )
    mgmt.approve(
        tenant, created["id"],
        KnowledgeTransitionRequest(expected_record_version=evaluated["record_version"]),
        "reviewer-a",
    )

    # 全局行必须仍为 active（不被租户 A 退休）
    with service.db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM knowledge WHERE knowledge_key='kg-GLOBAL-X' AND tenant_id IS NULL"
        ).fetchone()
    assert row is not None and row["status"] == "active", "租户 approve 不得退休全局行"


def test_shadow_edit_tenant_a_preferred_but_tenant_b_sees_global(tmp_path) -> None:
    """影子编辑：A 检索优先命中私有版；B 检索仍命中全局版。"""
    service = AgentService(make_settings(tmp_path))
    mgmt = service.knowledge_management
    tenant_a = "tenant-a"
    tenant_b = "tenant-b"

    _create_global_active(service, "GLOBAL-Y")

    created = mgmt.create(
        tenant_a,
        KnowledgeCreateRequest(
            category="行业规则", intent="rule", question="全局规则问题",
            answer="租户A定制答案", source="wiki://manual", layer="platform",
        ),
        "admin-a",
        knowledge_key="kg-GLOBAL-Y",
    )
    evaluated = mgmt.evaluate(
        tenant_a, created["id"], KnowledgeTransitionRequest(expected_record_version=1), "reviewer-a"
    )
    mgmt.approve(
        tenant_a, created["id"],
        KnowledgeTransitionRequest(expected_record_version=evaluated["record_version"]),
        "reviewer-a",
    )

    hits_a = service.knowledge.retrieve("全局规则", top_k=3, min_score=0.05, tenant_id=tenant_a)
    assert any(h["answer"] == "租户A定制答案" for h in hits_a), "租户 A 应优先命中影子版"

    hits_b = service.knowledge.retrieve("全局规则", top_k=3, min_score=0.05, tenant_id=tenant_b)
    assert any(h["answer"] == "全局规则答案" for h in hits_b), "租户 B 应仍见全局版"
    assert all(h["answer"] != "租户A定制答案" for h in hits_b), "影子版不得泄漏给租户 B"


# ---------- P1-2：租户 import-assets 不得改写全局行 ----------

def test_tenant_import_hot_update_cannot_rewrite_global_row(tmp_path) -> None:
    """P1-2：租户 admin 调 import_to_runtime(update_existing=True) 全局行内容不变。"""
    service = AgentService(make_settings(tmp_path))
    tenant = "tenant-a"

    # 全局资产（导入时 tenant_id=None）
    import_to_runtime(
        [KnowledgeItem(
            id="GLOBAL-Z", kind=KnowledgeKind.RULE, scope=KnowledgeScope.GENERAL,
            compiled_truth="全局原始答案", attributes={"question": "全局规则问题"},
        )],
        service.knowledge,
        tenant_id=None,
    )

    # 租户 A 用同内容资产热更新（模拟 import-assets?update=true 传 admin.tenant_id）
    stats = import_to_runtime(
        [KnowledgeItem(
            id="GLOBAL-Z", kind=KnowledgeKind.RULE, scope=KnowledgeScope.GENERAL,
            compiled_truth="被租户改写的答案", attributes={"question": "全局规则问题"},
        )],
        service.knowledge,
        tenant_id=tenant,
        update_existing=True,
    )

    # 全局行内容必须不变；改写行必须不落库为 NULL（应被 foreign/skip 拦截）
    hits = service.knowledge.retrieve("全局规则", top_k=3, min_score=0.05, tenant_id=tenant)
    assert any(h["answer"] == "全局原始答案" for h in hits), "全局行内容不得被租户热更新改写"
    assert all(h["answer"] != "被租户改写的答案" for h in hits), "租户改写内容不得出现"
    assert stats["update_failed"] == 0 and stats["skipped_foreign"] == 0, "general 项应走全局路径而非越权改写"


# ---------- P2-1 + ⑤：general/无店铺 seller 资产全局化 ----------

def test_general_and_storeless_seller_assets_import_as_global(tmp_path) -> None:
    """P2-1+⑤：general 与 scope_key=all 的 seller 资产落 tenant_id IS NULL。"""
    service = AgentService(make_settings(tmp_path))
    tenant = "tenant-a"

    import_to_runtime(
        [
            KnowledgeItem(
                id="R-GEN", kind=KnowledgeKind.RULE, scope=KnowledgeScope.GENERAL,
                compiled_truth="通用规则答案", attributes={"question": "规则问题"},
            ),
            KnowledgeItem(
                id="F-ALL", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.SELLER,
                scope_key="all", compiled_truth="无店铺FAQ答案",
                attributes={"question": "FAQ问题"},
            ),
            KnowledgeItem(
                id="F-STORE", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.SELLER,
                scope_key="store-a", compiled_truth="店铺私有答案",
                attributes={"question": "私有问题"},
            ),
        ],
        service.knowledge,
        tenant_id=tenant,
        default_store_id=tenant,
    )

    with service.db.connect() as conn:
        gen = conn.execute("SELECT tenant_id, store_id FROM knowledge WHERE id='kg-R-GEN'").fetchone()
        all_ = conn.execute("SELECT tenant_id, store_id FROM knowledge WHERE id='kg-F-ALL'").fetchone()
        store = conn.execute("SELECT tenant_id, store_id FROM knowledge WHERE id='kg-F-STORE'").fetchone()
    assert gen["tenant_id"] is None and gen["store_id"] is None, "general 必须全局 NULL"
    assert all_["tenant_id"] is None and all_["store_id"] is None, "无店铺 seller 必须全局 NULL"
    assert store["tenant_id"] == tenant and store["store_id"] == "store-a", "有店铺 seller 保留租户与店铺"

    # 另一租户能检索到全局知识
    hits = service.knowledge.retrieve("FAQ问题", top_k=3, min_score=0.05, tenant_id="tenant-b")
    assert any(h["source"] == "kg:F-ALL" for h in hits), "无店铺 FAQ 应对其他租户可见"


# ---------- P3-9：memory dedup 租户条件 ----------

def test_memory_dedup_is_tenant_scoped(tmp_path) -> None:
    """P3-9：跨租户同 store 同 fact 记忆各自落库（不被去重吞掉）。"""
    service = AgentService(make_settings(tmp_path))
    memory = service.memory

    first = memory.record("store-x", fact="同一事实", tenant_id="tenant-a")
    second = memory.record("store-x", fact="同一事实", tenant_id="tenant-b")
    assert first != second, "跨租户同内容记忆不得被去重吞掉"

    a = memory.recall("store-x", tenant_id="tenant-a")
    b = memory.recall("store-x", tenant_id="tenant-b")
    assert any(r["knowledge_key"] == first for r in a), "A 的记忆应可 recall"
    assert any(r["knowledge_key"] == second for r in b), "B 的记忆应可 recall"


# ---------- P3-5：forget 精确租户 ----------

def test_tenant_cannot_forget_global_memory(tmp_path) -> None:
    """P3-5：租户 admin 删不掉全局记忆；全局（None）可删。"""
    service = AgentService(make_settings(tmp_path))
    memory = service.memory

    global_mem = memory.record("store-g", fact="全局记忆", tenant_id=None)
    assert not memory.forget(global_mem, tenant_id="tenant-a"), "租户不得删除全局记忆"
    assert memory.forget(global_mem, tenant_id=None), "全局（None）应可删除全局记忆"


# ---------- P3-4：load_from_runtime None 语义 ----------

def test_load_from_runtime_none_returns_only_global(tmp_path) -> None:
    """P3-4：tenant_id=None 只返回全局行；传租户返回本租户 + 全局。"""
    service = AgentService(make_settings(tmp_path))
    import_to_runtime(
        [
            KnowledgeItem(
                id="R-NONE", kind=KnowledgeKind.RULE, scope=KnowledgeScope.GENERAL,
                compiled_truth="全局规则", attributes={"question": "规则"},
            ),
            KnowledgeItem(
                id="F-NONE", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.SELLER,
                scope_key="store-a", compiled_truth="店铺私有",
                attributes={"question": "私有"},
            ),
        ],
        service.knowledge,
        tenant_id="tenant-a",
        default_store_id="store-a",
    )

    globals_only = load_from_runtime(service.knowledge, tenant_id=None)
    assert all(item.attributes.get("tenant_id") is None for item in globals_only), (
        "None 应只返回全局行"
    )
    assert all(item.id != "F-NONE" for item in globals_only), "私有行不得出现在 None 视图"

    tenant_view = load_from_runtime(service.knowledge, tenant_id="tenant-a")
    ids = {item.id for item in tenant_view}
    assert "R-NONE" in ids and "F-NONE" in ids, "租户视图应含本租户 + 全局"
