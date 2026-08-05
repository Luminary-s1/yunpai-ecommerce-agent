"""knowledge_engine → 运行时 集成测试（B1b：layer 即 scope）。

证明：双引擎资产层 → 导入运行时 knowledge 表 → RAG 检索隔离 全链路可用。

覆盖：
- scope→layer 映射（B1b：general→platform / seller→store）
- 隔离：seller 知识只能被本店检索到，general 全局可见
- 反证：去掉 layer 隔离后，跨店检索必须能拿到不该拿的数据（证明隔离真实生效）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ecommerce_agent.knowledge_engine import (
    KnowledgeItem,
    KnowledgeKind,
    KnowledgeScope,
    to_knowledge_row,
    import_to_runtime,
    load_from_runtime,
)
from ecommerce_agent.knowledge_management import (
    KnowledgeReviseRequest,
    KnowledgeTransitionRequest,
)
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


# ---------- scope → layer 映射 ----------

def test_scope_to_layer_mapping() -> None:
    """B1b：scope 映射到运行时 layer，不加新列。"""
    # general → platform（跨租户通用话术/规则）
    general = KnowledgeItem(
        id="R-1", kind=KnowledgeKind.RULE, scope=KnowledgeScope.GENERAL,
        compiled_truth="七天无理由退货规则",
    )
    row = to_knowledge_row(general)
    assert row is not None
    assert row["layer"] == "platform"
    assert row["store_id"] is None  # general 无店铺维度

    # seller → store，store_id 取自 scope_key
    seller = KnowledgeItem(
        id="F-1", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.SELLER,
        compiled_truth="空气炸锅保修多久", scope_key="qinchuan",
        attributes={"question": "保修多久"},
    )
    row = to_knowledge_row(seller)
    assert row["layer"] == "store"
    assert row["store_id"] == "qinchuan"


def test_entity_kinds_skipped() -> None:
    """实体类（Product/SKU/Category/Attribute）留在图谱资产层，不进 RAG 表。"""
    prod = KnowledgeItem(
        id="P-1", kind=KnowledgeKind.PRODUCT, scope=KnowledgeScope.SELLER,
        compiled_truth="空气炸锅 AF5",
    )
    assert to_knowledge_row(prod) is None  # 不导入运行时


def test_seller_without_scope_key_defaults_store() -> None:
    """seller 无 scope_key 时兜底到 default_store_id，保证隔离不失效。"""
    seller = KnowledgeItem(
        id="F-2", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.SELLER,
        compiled_truth="测试知识",
    )
    row = to_knowledge_row(seller, default_store_id="fallback-store")
    assert row["store_id"] == "fallback-store"


# ---------- 导入运行时 + RAG 检索隔离（端到端） ----------

@pytest.fixture()
def service(tmp_path: Path):
    svc = AgentService(make_settings(tmp_path))
    yield svc
    svc.close()


def test_import_idempotent(service) -> None:
    """幂等（D-014）：重复导入不报错、不产生重复行。"""
    items = [
        KnowledgeItem(
            id="FAQ-IDEMPOTENT", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.SELLER,
            compiled_truth="幂等测试知识", scope_key="store-a",
            attributes={"question": "幂等测试"},
        )
    ]
    r1 = import_to_runtime(items, service.knowledge, default_store_id="store-a")
    r2 = import_to_runtime(items, service.knowledge, default_store_id="store-a")
    assert r1["imported"] == 1
    assert r2["imported"] == 0  # 第二次全跳过
    assert r2["skipped_existing"] == 1
    with service.db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM knowledge WHERE id='kg-FAQ-IDEMPOTENT'").fetchone()[0]
    assert n == 1  # 只有一行，无重复


def test_import_then_rag_retrieval(service) -> None:
    """导入后，RAG 能检索到 imported 知识（端到端打通）。"""
    items = [
        KnowledgeItem(
            id="FAQ-TEST-1", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.SELLER,
            compiled_truth="空气炸锅保修 12 个月",
            scope_key="qinchuan",
            attributes={"question": "空气炸锅保修多久", "risk_level": "low"},
        )
    ]
    import_to_runtime(items, service.knowledge, default_store_id="qinchuan")

    principal = principal_for(service)
    results = service.knowledge.retrieve(
        "空气炸锅保修多久",
        top_k=5,
        min_score=0.05,
        intent=None,
        tenant_id=principal.tenant_id,
        store_id="qinchuan",
    )
    assert any(r["source"] == "kg:FAQ-TEST-1" for r in results), "导入知识应能被 RAG 检索到"


def test_seller_isolation_across_stores(service) -> None:
    """隔离：A 店知识不能被 B 店检索到（B1b 的核心价值）。"""
    items = [
        KnowledgeItem(
            id="FAQ-STORE-A", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.SELLER,
            compiled_truth="A店专属：晴川空气炸锅特惠",
            scope_key="store-a",
            attributes={"question": "A店特惠是什么"},
        )
    ]
    import_to_runtime(items, service.knowledge, default_store_id="store-a")

    principal = principal_for(service)
    # A 店能检索到
    in_a = service.knowledge.retrieve(
        "A店特惠是什么", top_k=5, min_score=0.05, tenant_id=principal.tenant_id, store_id="store-a"
    )
    assert any(r["source"] == "kg:FAQ-STORE-A" for r in in_a)
    # B 店检索不到（隔离）
    in_b = service.knowledge.retrieve(
        "A店特惠是什么", top_k=5, min_score=0.05, tenant_id=principal.tenant_id, store_id="store-b"
    )
    assert all(r["source"] != "kg:FAQ-STORE-A" for r in in_b), "A店知识不应泄漏到B店"


def test_general_visible_to_all_stores(service) -> None:
    """general 知识对所有店铺可见（跨店通用）。"""
    items = [
        KnowledgeItem(
            id="RULE-GENERAL", kind=KnowledgeKind.RULE, scope=KnowledgeScope.GENERAL,
            compiled_truth="国家三包规定：七天无理由退货",
        )
    ]
    import_to_runtime(items, service.knowledge)

    principal = principal_for(service)
    for store_id in ("store-a", "store-b"):
        results = service.knowledge.retrieve(
            "三包规定", top_k=5, min_score=0.05, tenant_id=principal.tenant_id, store_id=store_id
        )
        assert any(r["source"] == "kg:RULE-GENERAL" for r in results), f"{store_id} 应能检索到 general"


def test_counterexample_layer_isolation(service) -> None:
    """反证：破坏 layer 隔离（把 seller 知识 store_id 置空）后，跨店检索必须能拿到。

    项目要求"每项能力做反证"——证明隔离测试真的在测东西。
    """
    items = [
        KnowledgeItem(
            id="FAQ-LEAK", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.SELLER,
            compiled_truth="机密：仅A店可见的促销",
            scope_key="store-a",
            attributes={"question": "A店机密"},
        )
    ]
    import_to_runtime(items, service.knowledge, default_store_id="store-a")

    # 正常情况：B 店检索不到（隔离生效）
    principal = principal_for(service)
    normal_b = service.knowledge.retrieve(
        "A店机密", top_k=5, min_score=0.05, tenant_id=principal.tenant_id, store_id="store-b"
    )
    assert all(r["source"] != "kg:FAQ-LEAK" for r in normal_b), "隔离应挡住跨店泄漏"

    # 反证：如果把该知识的 store_id 改成 NULL（模拟隔离被破坏），B 店必须能检索到
    with service.db.connect() as conn:
        conn.execute("UPDATE knowledge SET store_id=NULL WHERE id='kg-FAQ-LEAK'")
    broken_b = service.knowledge.retrieve(
        "A店机密", top_k=5, min_score=0.05, tenant_id=principal.tenant_id, store_id="store-b"
    )
    assert any(r["source"] == "kg:FAQ-LEAK" for r in broken_b), (
        "反证：隔离被破坏后跨店必须能检索到，证明隔离测试真实有效"
    )


# ---------- 反向加载器 load_from_runtime（任务3 Wiki：编辑即时生效的闭环） ----------

def test_load_from_runtime_roundtrip(service) -> None:
    """导入 → 读回：id 归一化（kg- 剥离）、compiled_truth/answer 一致、scope 反推。"""
    items = [
        KnowledgeItem(
            id="FAQ-ROUNDTRIP", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.SELLER,
            compiled_truth="空气炸锅保修 12 个月",
            scope_key="qinchuan",
            attributes={"question": "空气炸锅保修多久", "risk_level": "low"},
        )
    ]
    import_to_runtime(items, service.knowledge, default_store_id="qinchuan")

    loaded = load_from_runtime(service.knowledge)
    by_id = {item.id: item for item in loaded}
    assert "FAQ-ROUNDTRIP" in by_id, "读回后 id 应剥离 kg- 前缀，与资产层同名"
    item = by_id["FAQ-ROUNDTRIP"]
    assert item.compiled_truth == "空气炸锅保修 12 个月"
    assert item.kind is KnowledgeKind.FAQ
    assert item.scope is KnowledgeScope.SELLER
    assert item.scope_key == "qinchuan"
    assert item.attributes["store_id"] == "qinchuan"
    assert any(e.action == "created" for e in item.timeline)


def test_load_from_runtime_sees_revise(service) -> None:
    """编辑后读回新结论（任务3 的核心：编辑 → Wiki 即时生效）。"""
    items = [
        KnowledgeItem(
            id="FAQ-REVISE", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.SELLER,
            compiled_truth="旧结论：保修 12 个月",
            scope_key="qinchuan",
            attributes={"question": "保修多久"},
        )
    ]
    # 带 tenant 导入：生命周期管理严格按 tenant_id=? 过滤，无 tenant 的行不可被 /v1/admin 管理
    tenant = service.settings.bootstrap_tenant_id
    import_to_runtime(
        items, service.knowledge, tenant_id=tenant, default_store_id="qinchuan"
    )

    # 管理员走生命周期 revise → evaluate → approve（真实路径）
    mgmt = service.knowledge_management
    current = mgmt.get_item(tenant, "kg-FAQ-REVISE")
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

    loaded = load_from_runtime(service.knowledge)
    by_id = {item.id: item for item in loaded}
    assert "FAQ-REVISE" in by_id
    assert by_id["FAQ-REVISE"].compiled_truth == "新结论：保修 24 个月", (
        "编辑 approve 后，读回必须是新结论（运行时为准，即时生效）"
    )
    actions = [e.action for e in by_id["FAQ-REVISE"].timeline]
    assert "revised" in actions, "演化历史应包含修订记录"


def test_load_from_runtime_industry_is_rule(service) -> None:
    """layer=industry 的运行时行读回为 rule（general）。"""
    items = [
        KnowledgeItem(
            id="RULE-READBACK", kind=KnowledgeKind.RULE, scope=KnowledgeScope.GENERAL,
            compiled_truth="国家三包规定",
        )
    ]
    import_to_runtime(items, service.knowledge)

    loaded = load_from_runtime(service.knowledge)
    by_id = {item.id: item for item in loaded}
    assert "RULE-READBACK" in by_id
    assert by_id["RULE-READBACK"].kind is KnowledgeKind.RULE
    assert by_id["RULE-READBACK"].scope is KnowledgeScope.GENERAL
