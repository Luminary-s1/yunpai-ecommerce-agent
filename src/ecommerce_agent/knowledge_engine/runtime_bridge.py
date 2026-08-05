"""knowledge_engine → 运行时 knowledge 表的导入桥（B1b：layer 即 scope）。

设计（低耦合、可复用、零 schema 改动）：
- 双引擎（资产层）持有完整数据：scope / compiled_truth / timeline / 全属性
- 运行时（knowledge 表）只承载 RAG 可消费的 Q&A 类知识
- 本桥负责把资产层的 KnowledgeItem 翻译成 knowledge 表的插入行，
  并把 scope 映射到运行时已有的 layer 字段（B1b，不加新列）

scope → layer 映射（B1b 核心）：
    general → platform（跨租户通用话术） / industry（行业规则）
    seller  → store（单店，store_id 取自 scope_key 或调用方默认）
    memory  → evolution（记忆，默认隔离）

哪些知识进 RAG 表：
    FAQ / Script / Policy / Rule（有 Q&A 语义，RAG 可检索）
    实体类（Category/Product/SKU/Attribute）留在图谱资产层，
    供 Wiki 人读 + 将来 Neo4j 导入，不进 Q&A 表

关键约束：
    seller 知识必须有非空 store_id，否则会被所有店铺检索到（隔离漏洞）
"""

from __future__ import annotations

from typing import Any

from .models import (
    KnowledgeItem,
    KnowledgeKind,
    KnowledgeScope,
    TimelineEntry,
    utc_now_iso,
)


# scope → layer 映射（B1b：复用运行时已有 layer，不加列）
SCOPE_TO_LAYER: dict[KnowledgeScope, str] = {
    KnowledgeScope.GENERAL: "platform",
    KnowledgeScope.SELLER: "store",
    KnowledgeScope.MEMORY: "evolution",
}

# 运行时 layer → scope（SCOPE_TO_LAYER 的反向映射；industry/platform 均属 general）
RUNTIME_LAYER_TO_SCOPE: dict[str, KnowledgeScope] = {
    "platform": KnowledgeScope.GENERAL,
    "industry": KnowledgeScope.GENERAL,
    "store": KnowledgeScope.SELLER,
    "product": KnowledgeScope.SELLER,
    "evolution": KnowledgeScope.MEMORY,
}

# kind → knowledge 表 category 字段（RAG 检索的类别标签）
KIND_TO_CATEGORY: dict[KnowledgeKind, str] = {
    KnowledgeKind.FAQ: "常见问答",
    KnowledgeKind.SCRIPT: "客服话术",
    KnowledgeKind.POLICY: "售后政策",
    KnowledgeKind.RULE: "行业规则",
}

# 只导入 Q&A 类（RAG 可消费）；实体类留在图谱资产层
RAG_IMPORTABLE: set[KnowledgeKind] = {
    KnowledgeKind.FAQ,
    KnowledgeKind.SCRIPT,
    KnowledgeKind.POLICY,
    KnowledgeKind.RULE,
}

# 任务6 risk_level 有 critical，运行时 knowledge 表只有 low/medium/high，需映射
_RISK_MAP = {"critical": "high", "high": "high", "medium": "medium", "low": "low"}


def _to_question(item: KnowledgeItem) -> str:
    """取一条知识的"问题"侧（RAG 检索时的 query 命中对象）。"""
    attrs = item.attributes
    if item.kind is KnowledgeKind.FAQ:
        return attrs.get("question") or item.compiled_truth
    if item.kind is KnowledgeKind.SCRIPT:
        return attrs.get("intent") or attrs.get("canonical_question") or item.compiled_truth
    if item.kind is KnowledgeKind.POLICY:
        return attrs.get("policy_name") or item.compiled_truth
    if item.kind is KnowledgeKind.RULE:
        return attrs.get("rule_title") or item.compiled_truth
    return item.compiled_truth


def _to_keywords(item: KnowledgeItem) -> str:
    """取关键词：优先记录自带，其次拼上意图/别名。"""
    attrs = item.attributes
    kw = attrs.get("keywords", "") or ""
    extras = []
    for k in ("intent", "aliases"):
        v = attrs.get(k)
        if isinstance(v, str) and v:
            extras.append(v)
        elif isinstance(v, list):
            extras.extend(str(x) for x in v)
    return " ".join([kw, *extras]).strip()


def to_knowledge_row(item: KnowledgeItem, *, default_store_id: str = "default") -> dict[str, Any] | None:
    """把一个 KnowledgeItem 翻译成 knowledge 表插入行。

    返回 None 表示该知识不应进 RAG 表（实体类，留在图谱）。
    参数：
        item:            双引擎资产层的知识
        default_store_id: seller 知识没有 scope_key 时的兜底店铺 id
    """
    if item.kind not in RAG_IMPORTABLE:
        return None  # 实体类留在图谱资产层

    # scope → layer（B1b）；已有 layer 优先保留
    layer = item.attributes.get("layer") or SCOPE_TO_LAYER[item.scope]

    # seller 知识必须有非空 store_id（否则检索隔离失效）
    store_id: str | None = None
    if item.scope is KnowledgeScope.SELLER:
        store_id = item.scope_key if item.scope_key and item.scope_key != "all" else default_store_id

    risk = _RISK_MAP.get(str(item.attributes.get("risk_level", "low")), "low")

    return {
        "id": f"kg-{item.id}",
        "category": KIND_TO_CATEGORY[item.kind],
        "intent": item.attributes.get("intent") or item.id,
        "question": _to_question(item),
        "answer": item.compiled_truth,
        "keywords": _to_keywords(item),
        "risk_level": risk,
        # source 用 kg:{item.id} 唯一标识，保证检索结果可区分是哪条知识
        "source": f"kg:{item.id}",
        "version": 1,
        "status": "active",
        "approved_by": "builtin",
        "layer": layer,
        "store_id": store_id,
        "sku_id": item.attributes.get("sku_id"),
        "review_status": "approved",
    }


def import_to_runtime(
    items: list[KnowledgeItem],
    knowledge_base,
    *,
    tenant_id: str | None = None,
    default_store_id: str = "default",
) -> dict[str, int]:
    """把双引擎资产层知识导入运行时 knowledge 表。

    参数：
        items:            双引擎的 KnowledgeItem 列表（loader.load_clean_dir 产出）
        knowledge_base:   运行时 KnowledgeBase 实例（service.knowledge）
        tenant_id:        租户；general 传 None（全局），seller 传店铺租户
        default_store_id: seller 知识无 scope_key 时的兜底店铺 id

    返回：
        {"imported": 导入条数, "skipped_entity": 留在图谱的实体条数}

    幂等：以 id="kg-{item.id}" 导入，重复调用会产生重复行。
    调用方应先查已有 id 再做增量（可配合 ingest 去重）。
    """
    imported = 0
    skipped_entity = 0
    skipped_existing = 0
    # 幂等：先查已存在的 kg-* id，重复导入跳过（D-014 语义，不报错不重复）
    # 不修改运行时 KnowledgeBase，直接用其 db 连接查询
    existing: set[str] = set()
    try:
        with knowledge_base.db.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM knowledge WHERE id LIKE 'kg-%'"
            ).fetchall()
            existing = {r["id"] for r in rows}
    except Exception:
        existing = set()
    for item in items:
        row = to_knowledge_row(item, default_store_id=default_store_id)
        if row is None:
            skipped_entity += 1
            continue
        target_id = f"kg-{item.id}"
        if target_id in existing:
            skipped_existing += 1
            continue
        # add_document 不接受 None 的 sku_id 之外的可空字段，这里显式剔除
        row = {k: v for k, v in row.items() if v is not None}
        knowledge_base.add_document(
            **row,
            tenant_id=tenant_id,
            knowledge_key=f"kg-{item.id}",
        )
        existing.add(target_id)
        imported += 1
    return {
        "imported": imported,
        "skipped_entity": skipped_entity,
        "skipped_existing": skipped_existing,
    }


def load_from_runtime(
    knowledge_base,
    *,
    tenant_id: str | None = None,
    statuses: tuple[str, ...] = ("active",),
) -> list[KnowledgeItem]:
    """反向加载器：从运行时 knowledge 表读 Q&A 类知识 → KnowledgeItem 列表。

    与 import_to_runtime 互逆，构成"资产层 → 运行时 → 资产层"闭环
    （任务3 Wiki 搭建的"编辑即时生效"依赖此桥）。

    读取规则（对齐 import_to_runtime 的写入口径）：
    - 只读 Q&A 类行（layer ∈ platform/industry/store/product/evolution 且非实体类），
      实体类（Category/Product/SKU/Attribute）留在资产层，不读
    - id 归一化：运行时 `kg-{id}` → KnowledgeItem.id = `{id}`（与资产层同名）
    - 演化历史：由该 knowledge_key 的**全部版本行**（含 retired/candidate）按 version
      序拼接 —— 每次版本创建/激活/停用都构成一条可溯源的时间线
    - scope 由运行时 layer 反推（B1b 反向映射：platform/industry→general，
      store/product→seller，evolution→memory）

    参数：
        knowledge_base: 运行时 KnowledgeBase 实例（service.knowledge）
        tenant_id:      租户过滤；None 表示全局（含 tenant_id IS NULL）
        statuses:       只读哪些状态的行（默认只读 active）

    返回：
        Q&A 类 KnowledgeItem 列表（无资产层对应的纯运行时知识也会返回）。
    """
    placeholders = ",".join("?" for _ in statuses)
    params: list[Any] = [*statuses]
    tenant_clause = ""
    if tenant_id is not None:
        tenant_clause = "AND (tenant_id IS NULL OR tenant_id=?)"
        params.append(tenant_id)
    with knowledge_base.db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, knowledge_key, category, intent, question, answer, keywords,
                   risk_level, source, version, status, review_status, layer,
                   store_id, sku_id, created_at, updated_at, effective_from,
                   effective_to, approved_by, tenant_id
            FROM knowledge
            WHERE status IN ({placeholders}) {tenant_clause}
            ORDER BY knowledge_key, version ASC
            """,
            tuple(params),
        ).fetchall()

    # 按 knowledge_key 归组（同一条知识的多版本）
    by_key: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_key.setdefault(row["knowledge_key"], []).append(dict(row))

    items: list[KnowledgeItem] = []
    for key, versions in by_key.items():
        current = versions[-1]  # version ASC，最后一行即最新版本
        layer = current["layer"] or "industry"
        scope = RUNTIME_LAYER_TO_SCOPE.get(layer, KnowledgeScope.GENERAL)

        # id 归一化：运行时 knowledge_key 保留 `kg-{id}` 约定（import_to_runtime 写入时
        # knowledge_key=f"kg-{item.id}"），且跨版本不变；行 id 随版本递增会变（新 uuid），
        # 不能作为词条稳定 id。剥 kg- 前缀对齐资产层同名。
        item_id = key
        if item_id.startswith("kg-"):
            item_id = item_id[len("kg-") :]

        # 编译真相（当前最佳结论）
        compiled_truth = current["answer"] or current["question"]

        # 演化历史：逐版本拼接（created/revised/activated/retired）
        timeline: list[TimelineEntry] = []
        for v in versions:
            when = v.get("updated_at") or v.get("effective_from") or utc_now_iso()
            if v["version"] <= 1:
                action, note = "created", f"导入运行时：{key}"
            elif v["status"] == "active":
                action, note = "revised", f"版本 {v['version']} 生效"
            elif v["status"] == "retired":
                action, note = "retired", f"版本 {v['version']} 停用"
            else:
                action, note = "revised", f"版本 {v['version']} 候选"
            timeline.append(
                TimelineEntry(
                    at=when,
                    action=action,
                    note=note,
                    source=v.get("source", "") or "runtime",
                )
            )

        attributes: dict[str, Any] = {
            "category": current["category"],
            "intent": current["intent"],
            "question": current["question"],
            "answer": current["answer"],
            "keywords": current["keywords"] or "",
            "risk_level": current["risk_level"] or "low",
            "source": current["source"],
            "version": current["version"],
            "status": current["status"],
            "review_status": current["review_status"] or "",
            "layer": current["layer"],
            "store_id": current["store_id"],
            "sku_id": current["sku_id"],
            "knowledge_key": key,
            "approved_by": current["approved_by"],
            "effective_from": current["effective_from"],
        }

        # kind：layer=industry/platform → rule（行业规则）；否则按 category 反推
        kind = _infer_kind_from_layer_and_category(layer, current["category"])
        scope_key = current["store_id"] or "all"

        items.append(
            KnowledgeItem(
                id=item_id,
                kind=kind,
                scope=scope,
                scope_key=scope_key,
                compiled_truth=compiled_truth,
                timeline=timeline,
                attributes=attributes,
            )
        )
    return items


def _infer_kind_from_layer_and_category(
    layer: str, category: str | None
) -> KnowledgeKind:
    """从运行时 layer + category 反推 KnowledgeKind（尽力而为，可被合并层覆盖）。

    layer=industry 的行必是行业规则（rule）；其余按 category 关键词归类，
    未命中回退 policy（售后政策）。
    """
    if layer == "industry":
        return KnowledgeKind.RULE
    text = category or ""
    if "常见问答" in text or "FAQ" in text.upper():
        return KnowledgeKind.FAQ
    if "客服话术" in text or "SOP" in text.upper():
        return KnowledgeKind.SCRIPT
    if "行业规则" in text or "法规" in text:
        return KnowledgeKind.RULE
    return KnowledgeKind.POLICY
