"""knowledge_engine 单元测试：模型层 + 加载器 + 梦循环。

覆盖：
- 模型层：KnowledgeItem.revise 追加时间线、不删原文；scope 枚举
- 加载器：读 02_clean JSON、标 scope、编译真相生成
- 梦循环：增量摄取去重 / 一致性校验悬空引用与孤立节点 / 合并记忆聚类
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ecommerce_agent.knowledge_engine import (
    KnowledgeItem,
    KnowledgeKind,
    KnowledgeScope,
    TimelineEntry,
    load_clean_dir,
    load_record,
    infer_scope,
    stats,
    ingest,
    consistency_check,
    consolidate,
    run_dream_cycle,
)


# ---------- 模型层 ----------


def test_knowledge_item_to_from_dict_roundtrip() -> None:
    """KnowledgeItem 可序列化/反序列化，保证可存储可传输。"""
    item = KnowledgeItem(
        id="FAQ-1",
        kind=KnowledgeKind.FAQ,
        scope=KnowledgeScope.GENERAL,
        compiled_truth="七天无理由退货",
        attributes={"question": "能退货吗", "answer": "七天无理由"},
    )
    restored = KnowledgeItem.from_dict(item.to_dict())
    assert restored.id == item.id
    assert restored.scope == item.scope
    assert restored.compiled_truth == item.compiled_truth
    assert restored.attributes == item.attributes


def test_scope_enum_values() -> None:
    """三层边界枚举值正确。"""
    assert KnowledgeScope.GENERAL.value == "general"
    assert KnowledgeScope.SELLER.value == "seller"
    assert KnowledgeScope.MEMORY.value == "memory"


# ---------- 加载器 ----------

def test_load_record_policy_general() -> None:
    """政策 scope_key=all → general（跨品类政策）。"""
    item = load_record(
        KnowledgeKind.POLICY,
        {
            "policy_code": "RETURN-1",
            "policy_name": "七天无理由退货",
            "content": "消费者自签收之日起七天内可退货",
            "scope": "Category",
            "scope_key": "all",
            "source": "network",
        },
    )
    assert item.scope is KnowledgeScope.GENERAL
    assert item.id == "RETURN-1"
    assert "七天无理由" in item.compiled_truth


def test_load_record_policy_seller() -> None:
    """政策 scope_key 指向具体品类 → seller（单店政策）。"""
    item = load_record(
        KnowledgeKind.POLICY,
        {
            "policy_code": "WARR-2",
            "policy_name": "吸尘器保修",
            "content": "整机保修 24 个月",
            "scope": "SKU",
            "scope_key": "QC-VC-A1",
            "source": "fixture",
        },
    )
    assert item.scope is KnowledgeScope.SELLER


def test_load_record_rule_general() -> None:
    """行业规则 → general（跨租户通用）。"""
    item = load_record(
        KnowledgeKind.RULE,
        {
            "rule_code": "RULE-1",
            "rule_title": "三包规定",
            "content_summary": "国家三包规定",
        },
    )
    assert item.scope is KnowledgeScope.GENERAL


def test_infer_scope() -> None:
    """infer_scope 自动判定。"""
    assert infer_scope(KnowledgeKind.RULE, {}) is KnowledgeScope.GENERAL
    assert infer_scope(KnowledgeKind.PRODUCT, {"item_id": "X"}) is KnowledgeScope.SELLER
    assert (
        infer_scope(KnowledgeKind.POLICY, {"scope_key": "all"})
        is KnowledgeScope.GENERAL
    )
    assert (
        infer_scope(KnowledgeKind.POLICY, {"scope_key": "air_fryer"})
        is KnowledgeScope.SELLER
    )


def test_load_clean_dir_real_data(tmp_path: Path) -> None:
    """用真实任务6产物集成测试：02_clean/ 加载 + 标 scope + 统计。"""
    # 定位真实交付物（若 CI 无此目录则跳过）
    # __file__ = tests/test_knowledge_engine.py → parent= tests/ → parent.parent = 项目根
    repo_root = Path(__file__).resolve().parent.parent
    clean_dir = repo_root / "knowledge_graph_output" / "02_clean"
    if not clean_dir.is_dir():
        pytest.skip("真实 02_clean 目录不存在，跳过集成测试")

    items = load_clean_dir(clean_dir)
    assert len(items) > 0

    # 六类实体 + 规则都能加载
    kinds = {item.kind for item in items}
    assert KnowledgeKind.CATEGORY in kinds
    assert KnowledgeKind.PRODUCT in kinds
    assert KnowledgeKind.SKU in kinds
    assert KnowledgeKind.POLICY in kinds
    assert KnowledgeKind.SCRIPT in kinds
    assert KnowledgeKind.FAQ in kinds
    assert KnowledgeKind.RULE in kinds

    # SKU 必须从 sku.json 加载（曾因缺失被静默跳过，导致 SKU 无测试守护）
    sku_items = [i for i in items if i.kind is KnowledgeKind.SKU]
    assert len(sku_items) >= 12, f"SKU 应 ≥12 条（从 sku.json 派生），实际 {len(sku_items)}"
    assert all(i.scope_key for i in sku_items)

    # 每个 item 都有 id、compiled_truth、scope、timeline
    for item in items:
        assert item.id
        assert item.compiled_truth
        assert item.scope in (KnowledgeScope.GENERAL, KnowledgeScope.SELLER)
        assert item.timeline, "每个知识必须有初始 created 时间线"

    # 统计有 scope 分布
    s = stats(items)
    assert s["total"] == len(items)
    assert set(s["by_scope"].keys()) <= {"general", "seller", "memory"}


# ---------- 梦循环：增量摄取 ----------

def _mk_item(iid: str, kind=KnowledgeKind.PRODUCT, scope=KnowledgeScope.SELLER) -> KnowledgeItem:
    return KnowledgeItem(
        id=iid, kind=kind, scope=scope, compiled_truth=f"知识{iid}"
    )


def test_ingest_dedup() -> None:
    """增量摄取：重复 id 跳过，新 id 吸收。"""
    incoming = [_mk_item("A"), _mk_item("B"), _mk_item("A")]  # A 出现两次
    report = ingest(incoming, existing_ids=["A"])
    # A 出现在 incoming 里两次：第一次已见（同 existing），第二次是重复
    assert report.duplicates == 2
    assert [i.id for i in report.new_items] == ["B"]
    assert report.total == 3  # B 新增 + 2 重复


def test_ingest_all_new() -> None:
    """增量摄取：全新知识全部吸收。"""
    report = ingest([_mk_item("X"), _mk_item("Y")], existing_ids=[])
    assert len(report.new_items) == 2
    assert report.duplicates == 0


# ---------- 梦循环：一致性校验 ----------

def test_consistency_finds_dangling_and_orphan() -> None:
    """一致性校验：悬空引用 + 孤立节点都能发现。"""
    # FAQ 引用不存在的 script，SKU 引用不存在的 product
    faq = KnowledgeItem(
        id="FAQ-1", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.SELLER,
        compiled_truth="保修多久",
        attributes={"ref_script_id": "SCRIPT-MISSING", "sku_id": "SKU-MISSING"},
    )
    sku = KnowledgeItem(
        id="SKU-1", kind=KnowledgeKind.SKU, scope=KnowledgeScope.SELLER,
        compiled_truth="吸尘器", attributes={"item_id": "PROD-MISSING"},
    )
    report = consistency_check([faq, sku])
    assert len(report.dangling_references) >= 2  # SCRIPT-MISSING + SKU-MISSING + PROD-MISSING
    assert report.issues >= 2
    # 悬空引用记录含源和目标
    assert all("source_id" in d for d in report.dangling_references)


def test_consistency_clean() -> None:
    """一致性校验：无悬空引用、无孤立节点。"""
    prod = _mk_item("PROD-1")
    sku = KnowledgeItem(
        id="SKU-1", kind=KnowledgeKind.SKU, scope=KnowledgeScope.SELLER,
        compiled_truth="吸尘器", attributes={"item_id": "PROD-1"},
    )
    report = consistency_check([prod, sku])
    assert report.issues == 0
    assert not report.dangling_references


# ---------- 梦循环：合并记忆 ----------

def test_consolidate_requires_min_facts() -> None:
    """合并记忆：不足 3 条不合并。"""
    items = [_mk_item("A"), _mk_item("B")]  # 只有 2 条
    report = consolidate(items)
    assert report.clusters == []
    assert report.consolidated == []
    assert report.skipped > 0


def test_consolidate_groups_similar() -> None:
    """合并记忆：相似知识聚成簇，取置信度最高作结论，不删原文。"""
    items = [
        KnowledgeItem(
            id="F1", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.GENERAL,
            compiled_truth="七天无理由退货规则：自签收起7天内可退货",
            attributes={"confidence": 0.9},
        ),
        KnowledgeItem(
            id="F2", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.GENERAL,
            compiled_truth="七天无理由退货规则：7天内可申请退货",
            attributes={"confidence": 0.95},
        ),
        KnowledgeItem(
            id="F3", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.GENERAL,
            compiled_truth="七天无理由退货规则：签收七天内退货",
            attributes={"confidence": 0.6},
        ),
        # 完全不同的一条，不应并入
        KnowledgeItem(
            id="F4", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.GENERAL,
            compiled_truth="快递发货时效：付款后48小时内发货",
            attributes={"confidence": 0.8},
        ),
    ]
    report = consolidate(items, min_facts=3, min_age_hours=0, threshold=0.5)
    # F1/F2/F3 相似聚成一簇，F4 独立
    assert len(report.clusters) >= 1
    # 取置信度最高（F2=0.95）作结论
    best = max(report.consolidated, key=lambda c: c["confidence"])
    assert best["conclusion_source_id"] == "F2"
    # 被合并的知识标记了 consolidated，但没被删除
    for i in items:
        actions = [e.action for e in i.timeline]
        if i.id in best["cluster_ids"]:
            assert "consolidated" in actions  # 标了已并入
        assert i.id in [x.id for x in items]  # 原文仍在
    # 完全不同的 F4 未被合并
    f4_ids = [cid for cl in report.clusters for cid in cl]
    assert "F4" not in f4_ids


def test_run_dream_cycle_end_to_end() -> None:
    """梦循环一键跑：三个作业都返回汇总。"""
    items = [_mk_item("A"), _mk_item("B"), _mk_item("A")]  # A 出现两次
    report = run_dream_cycle(items, existing_ids=["A"])
    assert report["ingest"]["duplicates"] == 2  # incoming 里 A 出现两次都算重复
    assert "consistency" in report
    assert "consolidate" in report


# ---------- Wiki 渲染 ----------

def test_render_item_has_sections() -> None:
    """词条页包含 当前结论/属性/演化历史 三段，且编译真相在上。"""
    from ecommerce_agent.knowledge_engine import render_item
    item = KnowledgeItem(
        id="POL-1", kind=KnowledgeKind.POLICY, scope=KnowledgeScope.SELLER,
        compiled_truth="保修 12 个月",
        attributes={"policy_name": "整机保修", "risk_level": "low"},
        timeline=[TimelineEntry(at="2026-01-01T00:00:00+00:00", action="created")],
    )
    md = render_item(item)
    assert "## 当前结论" in md
    assert "保修 12 个月" in md
    assert "## 演化历史" in md
    assert "created" in md
    # 当前结论出现在演化历史之前
    assert md.index("## 当前结论") < md.index("## 演化历史")


def test_render_wiki_creates_pages(tmp_path: Path) -> None:
    """render_wiki 生成词条页 + index，文件名安全处理。"""
    from ecommerce_agent.knowledge_engine import render_wiki
    items = [
        KnowledgeItem(
            id="QC-SPU-AF5|brand", kind=KnowledgeKind.ATTRIBUTE, scope=KnowledgeScope.SELLER,
            compiled_truth="晴川", attributes={"attr_key": "brand"},
        ),
        KnowledgeItem(
            id="FAQ-1", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.GENERAL,
            compiled_truth="七天无理由", attributes={"question": "能退货吗"},
        ),
    ]
    result = render_wiki(items, tmp_path)
    assert result["rendered"] == 2
    assert (tmp_path / "index.md").exists()
    # | 被替换为 _，Windows 安全文件名
    assert (tmp_path / "attribute" / "QC-SPU-AF5_brand.md").exists()
    assert (tmp_path / "faq" / "FAQ-1.md").exists()


# ---------- 梦循环调度器 ----------

def test_run_dream_cycle_once_returns_report(tmp_path: Path) -> None:
    """调度器一次性运行返回完整报告（三作业都有结果）。"""
    from ecommerce_agent.knowledge_engine import run_dream_cycle_once
    # 造一个临时 02_clean 结构
    import json as _json
    clean_dir = tmp_path
    (clean_dir / "faq.json").write_text(
        _json.dumps([
            {"faq_id": "FAQ-1", "question": "保修多久", "answer": "保修12个月", "source": "fixture"},
            {"faq_id": "FAQ-2", "question": "能退货吗", "answer": "七天无理由", "source": "fixture"},
        ]),
        encoding="utf-8",
    )
    report = run_dream_cycle_once(clean_dir)
    assert report["total_items"] == 2
    assert "ingest" in report and "new" in report["ingest"]
    assert "consistency" in report
    assert "consolidate" in report


# ---------- 合并记忆真实时间戳 ----------

def test_consolidate_with_naive_timestamp() -> None:
    """无时区时间戳（如 2026-08-03）不报错，能正常算年龄。"""
    item = KnowledgeItem(
        id="R-1", kind=KnowledgeKind.RULE, scope=KnowledgeScope.GENERAL,
        compiled_truth="七天无理由退货规则",
        timeline=[TimelineEntry(at="2026-08-03", action="created")],
    )
    # min_age=0 允许立即合并；重点是不抛 TypeError
    report = consolidate([item], min_facts=1, min_age_hours=0)
    assert report is not None


# ---------- 梦循环自动修复 ----------

def test_auto_repair_marks_dangling_and_orphan() -> None:
    """auto_repair 把悬空引用和孤立节点标记为失效，不删除数据。"""
    from ecommerce_agent.knowledge_engine import auto_repair, consistency_check
    # 构造：FAQ 引用不存在的 script（悬空），孤立 Product（无出边无入边）
    faq = KnowledgeItem(
        id="FAQ-X", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.SELLER,
        compiled_truth="保修多久", attributes={"ref_script_id": "SCRIPT-MISSING"},
    )
    prod = KnowledgeItem(
        id="PROD-ALONE", kind=KnowledgeKind.PRODUCT, scope=KnowledgeScope.SELLER,
        compiled_truth="孤立商品",
    )
    items = [faq, prod]
    report = consistency_check(items)
    assert report.issues > 0  # 确实发现了问题

    repair = auto_repair(report, items)
    assert repair["marked_dangling"] >= 1  # FAQ-X 被标记悬空
    assert repair["marked_orphan"] >= 1    # PROD-ALONE 被标记孤立
    # 数据没被删除，只是标记
    assert faq.attributes.get("dangling") is True
    assert prod.attributes.get("orphan") is True
    # timeline 有修复记录（可溯源）
    assert any(e.action == "marked_dangling" for e in faq.timeline)


def test_auto_repair_idempotent() -> None:
    """已标记的不重复标记（幂等）。"""
    from ecommerce_agent.knowledge_engine import auto_repair, consistency_check
    faq = KnowledgeItem(
        id="FAQ-X", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.SELLER,
        compiled_truth="保修多久", attributes={"ref_script_id": "SCRIPT-MISSING"},
    )
    items = [faq]
    report = consistency_check(items)
    r1 = auto_repair(report, items)
    r2 = auto_repair(report, items)
    assert r2["marked_dangling"] == 0  # 第二次不重复标记
    assert r1["marked_dangling"] >= 1


# ---------- P1-1：合并记忆落库 ----------

def test_apply_consolidation_persists(tmp_path: Path) -> None:
    """合并结论落库（apply_consolidation）：写入 + 幂等。"""
    from ecommerce_agent.knowledge_engine.dream_cycle import consolidate, apply_consolidation
    from ecommerce_agent.rag import KnowledgeBase
    from ecommerce_agent.database import Database

    items = []
    for i, txt in enumerate(["七天无理由退货", "七天无理由退货政策", "七天无理由退货规定"]):
        items.append(KnowledgeItem(
            id=f"C-{i}", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.SELLER,
            compiled_truth=txt, scope_key="qinchuan",
            timeline=[TimelineEntry(at="2026-08-01T00:00:00+00:00", action="created")],
            attributes={"confidence": 0.9},
        ))
    rep = consolidate(items, min_facts=3, min_age_hours=1, threshold=0.7)
    assert len(rep.clusters) >= 1, "高相似知识应触发合并"

    db = Database(tmp_path / "agent.sqlite3")
    db.initialize()
    kb = KnowledgeBase(db)
    first = apply_consolidation(rep, kb)
    assert first["written"] >= 1
    second = apply_consolidation(rep, kb)
    assert second["written"] == 0
    assert second["skipped_existing"] >= 1, "幂等：重复调用应跳过"
    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM knowledge WHERE knowledge_key LIKE 'kg-consolidated-%'"
        ).fetchone()[0]
    assert count == first["written"], f"DB 应只有 {first['written']} 条结论行"


def test_load_clean_dir_logs_missing_asset_files(tmp_path: Path, caplog) -> None:
    """P3：02_clean 缺某个实体文件必须打 warning，不再静默跳过。"""
    import logging

    clean_dir = tmp_path / "02_clean"
    clean_dir.mkdir()
    (clean_dir / "faq.json").write_text("[]", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.loader"):
        items = load_clean_dir(clean_dir)
    assert items == []
    assert any("缺失" in rec.message for rec in caplog.records), "缺文件必须打 warning"


def test_coerce_scope_removed_from_public_api() -> None:
    """死导出守卫：coerce_scope 全仓零调用，必须已删除。"""
    import ecommerce_agent.knowledge_engine as ke
    import ecommerce_agent.knowledge_engine.models as km

    assert not hasattr(ke, "coerce_scope"), "coerce_scope 不应再从包级导出"
    assert not hasattr(km, "coerce_scope"), "coerce_scope 函数应已删除"


def test_knowledge_item_has_no_revise_method() -> None:
    """死方法守卫：KnowledgeItem.revise 生产零调用（时间线语义由 runtime_bridge 拼接），必须已删除。"""
    item = KnowledgeItem(
        id="X-1", kind=KnowledgeKind.FAQ, scope=KnowledgeScope.GENERAL, compiled_truth="t"
    )
    assert not hasattr(item, "revise"), "KnowledgeItem.revise 死方法应已删除"
