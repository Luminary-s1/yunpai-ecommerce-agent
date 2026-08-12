"""knowledge_engine 梦循环（Dream Cycle）：让知识库自动维护。

对应 garrytan/gbrain 的 24/7 后台作业，落地为三个轻量作业：

作业① 增量摄取（ingest）：把新增知识源增量进库，不重复处理已见内容
作业② 一致性校验（consistency）：扫图谱孤立节点、悬空引用（删除/改名后残留）
作业③ 合并记忆（consolidate）：相似事实聚类→归纳→取置信度最高→不删原文

设计原则（低耦合、可复用）：
- 三个作业都是纯函数，输入 KnowledgeItem 列表，输出报告 dict。
- 不依赖数据库、外部服务，只对内存中的知识做运算。
- 下游可任意封装为 cron / worker / 一次性脚本。

合并记忆规则（对齐 gbrain src/core/cycle/phases/consolidate.ts）：
- 同实体事实 ≥ 3 条才合并（minFactsPerBucket）
- 最老事实 ≥ 24h（minOldestAgeMs，避免刚写入就合并）
- 相似度 ≥ 0.85 归簇（cosine 阈值）
- 每簇取置信度最高那条作结论
- 永不删除原文，只标记 consolidated
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Iterable

from .models import KnowledgeItem, KnowledgeScope, KnowledgeKind, TimelineEntry, utc_now_iso


# ---------- 作业① 增量摄取 ----------

@dataclass(slots=True)
class IngestReport:
    seen_ids: set[str] = field(default_factory=set)
    new_items: list[KnowledgeItem] = field(default_factory=list)
    duplicates: int = 0

    @property
    def total(self) -> int:
        return len(self.new_items) + self.duplicates


def ingest(incoming: Iterable[KnowledgeItem], existing_ids: Iterable[str]) -> IngestReport:
    """增量摄取：只吸收未见过的知识，跳过已见（按 id 去重）。

    参数：
        incoming:     新知识流
        existing_ids: 已入库的知识 id（可从存储层读）

    返回：IngestReport（新增列表 + 重复数 + 已见集合）
    """
    seen = set(existing_ids)
    report = IngestReport(seen_ids=set(seen))
    for item in incoming:
        if item.id in seen:
            report.duplicates += 1
            continue
        seen.add(item.id)
        report.new_items.append(item)
    report.seen_ids = seen
    return report


# ---------- 作业② 一致性校验 ----------

@dataclass(slots=True)
class ConsistencyReport:
    dangling_references: list[dict] = field(default_factory=list)
    orphan_nodes: list[str] = field(default_factory=list)
    issues: int = 0

    def add_dangling(self, *, source_id: str, ref_kind: str, ref_id: str, reason: str) -> None:
        """记录一条悬空引用（知识引用了不存在的目标）。"""
        self.dangling_references.append(
            {"source_id": source_id, "ref_kind": ref_kind, "ref_id": ref_id, "reason": reason}
        )
        self.issues += 1

    def add_orphan(self, node_id: str) -> None:
        """记录一个孤立节点（没有被任何知识引用，也无出边）。"""
        self.orphan_nodes.append(node_id)
        self.issues += 1


def _extract_refs(item: KnowledgeItem) -> list[tuple[str, str]]:
    """从一条知识里提取它引用的其他知识（kind, id）。

    引用关系来自 attributes 里的外键字段：
    - faq.ref_script_id → script
    - faq.sku_id → sku
    - sku.item_id → product
    - policy.scope_key → category（当 scope=Category 时）
    """
    refs: list[tuple[str, str]] = []
    attrs = item.attributes
    if item.kind is KnowledgeKind.FAQ:
        if attrs.get("ref_script_id"):
            refs.append(("script", attrs["ref_script_id"]))
        if attrs.get("sku_id"):
            refs.append(("sku", attrs["sku_id"]))
    if item.kind is KnowledgeKind.SKU and attrs.get("item_id"):
        refs.append(("product", attrs["item_id"]))
    if (
        item.kind is KnowledgeKind.POLICY
        and attrs.get("scope") == "Category"
        and attrs.get("scope_key")
        and attrs["scope_key"] != "all"
    ):
        refs.append(("category", attrs["scope_key"]))
    return refs


def consistency_check(items: Iterable[KnowledgeItem]) -> ConsistencyReport:
    """一致性校验：找悬空引用 + 孤立节点。

    参数：
        items: 全部知识

    返回：ConsistencyReport（悬空引用列表 + 孤立节点列表）
    """
    report = ConsistencyReport()
    id_set = {i.id for i in items}
    referenced = set()
    emitting = set()
    for item in items:
        refs = _extract_refs(item)
        emitting.add(item.id) if refs else None
        for ref_kind, ref_id in refs:
            referenced.add(ref_id)
            if ref_id not in id_set:
                report.add_dangling(
                    source_id=item.id,
                    ref_kind=ref_kind,
                    ref_id=ref_id,
                    reason=f"{item.kind.value} 引用了不存在的 {ref_kind}:{ref_id}",
                )
    # 孤立节点：既无出边（不引用别人）、也无入边（不被引用）
    # 品类例外：品类是树根，允许无父被引用，不判孤岛
    for item in items:
        if item.id not in referenced and item.id not in emitting:
            if item.kind is not KnowledgeKind.CATEGORY:
                report.add_orphan(item.id)
    return report


def auto_repair(report: ConsistencyReport, items: list[KnowledgeItem], *, now: str | None = None) -> dict:
    """自动修复一致性校验发现的问题。

    策略（低耦合、只读、可溯源）：
    - 悬空引用：把源知识标记为 `dangling=true` 并 append timeline 说明，
      而不是删除或修改任务6产物（数据仍是只读资产）。
    - 孤立节点：同样标记 `orphan=true`，不删除（可能是有意保留的独立知识）。

    返回修复报告：{"marked_dangling": n, "marked_orphan": n, "repaired_items": [ids]}
    """
    now_iso = now or utc_now_iso()
    marked_dangling = 0
    marked_orphan = 0
    repaired: list[str] = []

    # 悬空引用的源知识 → 标记 dangling
    dangling_sources = {d["source_id"] for d in report.dangling_references}
    for item in items:
        if item.id in dangling_sources and not item.attributes.get("dangling"):
            item.attributes["dangling"] = True
            item.timeline.append(
                TimelineEntry(
                    at=now_iso,
                    action="marked_dangling",
                    note=f"发现悬空引用，已标记失效（修复由梦循环自动执行）",
                )
            )
            marked_dangling += 1
            repaired.append(item.id)

    # 孤立节点 → 标记 orphan
    orphan_ids = set(report.orphan_nodes)
    for item in items:
        if item.id in orphan_ids and not item.attributes.get("orphan"):
            item.attributes["orphan"] = True
            item.timeline.append(
                TimelineEntry(
                    at=now_iso,
                    action="marked_orphan",
                    note=f"检测为孤立节点，已标记（保留原文，可人工复核）",
                )
            )
            marked_orphan += 1
            repaired.append(item.id)

    return {
        "marked_dangling": marked_dangling,
        "marked_orphan": marked_orphan,
        "repaired_items": repaired,
    }


# ---------- 作业③ 合并记忆 ----------

@dataclass(slots=True)
class ConsolidateReport:
    clusters: list[list[str]] = field(default_factory=list)
    consolidated: list[dict] = field(default_factory=list)
    skipped: int = 0


def _cosine_similarity(a: str, b: str) -> float:
    """字符级余弦相似度（不依赖向量服务，纯标准库）。

    实现：把字符串按字符 + 双字符切分，用字符袋计算 Jaccard 系数作为相似度近似。
    这是轻量版——gbrain 用 embedding 余弦，这里用 n-gram 字符袋近似，保证可复用、
    零依赖。真实场景可用 text_utils.cosine_similarity 替换。
    """
    if a == b:
        return 1.0
    bag_a = set(a) | {a[i:i+2] for i in range(len(a) - 1)}
    bag_b = set(b) | {b[i:i+2] for i in range(len(b) - 1)}
    inter = bag_a & bag_b
    union = bag_a | bag_b
    if not union:
        return 0.0
    return len(inter) / len(union)


def _confidence(item: KnowledgeItem) -> float:
    """取一条知识的置信度。优先看 attributes.confidence，缺省 0.8。"""
    try:
        return float(item.attributes.get("confidence", 0.8))
    except (TypeError, ValueError):
        return 0.8


def consolidate(
    items: Iterable[KnowledgeItem],
    *,
    min_facts: int = 3,
    min_age_hours: float = 24.0,
    threshold: float = 0.85,
    now: str | None = None,
) -> ConsolidateReport:
    """合并记忆：把相似事实聚成簇，每簇归纳为一条结论，不删原文。

    对齐 gbrain consolidate.ts 的规则：
    - 同一 scope 下 ≥ min_facts 条才处理
    - 最老事实 ≥ min_age_hours
    - 相似度 ≥ threshold 归簇
    - 每簇取置信度最高那条的 compiled_truth 作为结论
    - 原知识标记 consolidated（append timeline），永不删除

    参数：
        items:         全部知识（或某个 scope 子集）
        min_facts:     最少条数才合并（默认3，gbrain 默认）
        min_age_hours: 最老事实的最小年龄（默认24h，gbrain 默认）
        threshold:     相似度阈值（默认0.85，gbrain 默认）
        now:           当前时间（可注入，便于测试）

    返回：ConsolidateReport
    """
    now_iso = now or utc_now_iso()
    now_dt = datetime.fromisoformat(now_iso)
    report = ConsolidateReport()

    # 1. 按 (scope, kind) 分组，避免跨类型误合并
    buckets: dict[tuple[str, str], list[KnowledgeItem]] = {}
    for item in items:
        key = (item.scope.value, item.kind.value)
        buckets.setdefault(key, []).append(item)

    for (scope, kind), group in buckets.items():
        if len(group) < min_facts:
            report.skipped += len(group)
            continue

        # 2. 最老事实年龄检查
        created_times = []
        for item in group:
            for e in item.timeline:
                if e.action == "created":
                    try:
                        created_times.append(datetime.fromisoformat(e.at))
                    except ValueError:
                        continue
        if created_times:
            oldest = min(created_times)
            # 时区一致性：now_dt 带时区，oldest 可能是 naive（如 "2026-08-03"），
            # 统一转成 UTC-aware 再相减
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            age_hours = (now_dt - oldest).total_seconds() / 3600
            if age_hours < min_age_hours:
                report.skipped += len(group)
                continue

        # 3. 相似度聚类（贪心）
        unassigned = list(group)
        while unassigned:
            seed = unassigned[0]
            cluster = [seed]
            unassigned = unassigned[1:]
            rest = []
            for other in unassigned:
                sim = _cosine_similarity(seed.compiled_truth, other.compiled_truth)
                if sim >= threshold:
                    cluster.append(other)
                else:
                    rest.append(other)
            unassigned = rest

            if len(cluster) >= 2:
                report.clusters.append([i.id for i in cluster])
                # 4. 取置信度最高那条作结论
                best = max(cluster, key=_confidence)
                report.consolidated.append(
                    {
                        "scope": scope,
                        "kind": kind,
                        "cluster_ids": [i.id for i in cluster],
                        "conclusion": best.compiled_truth,
                        "conclusion_source_id": best.id,
                        "confidence": _confidence(best),
                    }
                )
                # 5. 标记已并入（不删原文）
                for i in cluster:
                    i.timeline.append(
                        TimelineEntry(
                            at=now_iso,
                            action="consolidated",
                            note=f"已并入结论（来源 {best.id}）",
                        )
                    )
            else:
                report.skipped += 1

    return report


# ---------- 便捷封装 ----------

def apply_consolidation(
    report: ConsolidateReport,
    knowledge_base,
    *,
    tenant_id: str | None = None,
    default_store_id: str = "default",
) -> dict[str, int]:
    """把合并记忆结论落库（P1-1：梦循环自动维护闭环）。

    对 consolidate 报告里每条结论：
    - 以 id="kg-consolidated-{conclusion_source_id}" 写入运行时 knowledge 表
      （category 按 kind 映射，question 用结论文本，answer 用结论内容）
    - 幂等：已存在则跳过（不重复写）
    - 永不删除原文（原知识由 consolidate 标记 consolidated，此处只写结论行）

    参数：
        report: consolidate() 返回的 ConsolidateReport
        knowledge_base: 运行时 KnowledgeBase 实例

    返回：{"written": 写入条数, "skipped_existing": 跳过条数}
    """
    if not report.consolidated:
        return {"written": 0, "skipped_existing": 0}
    kind_to_category = {
        "faq": "常见问答", "script": "客服话术",
        "policy": "售后政策", "rule": "行业规则",
        "product": "商品", "category": "品类",
        "sku": "SKU", "attribute": "属性",
    }
    written = 0
    skipped = 0
    for item in report.consolidated:
        conclusion_id = f"kg-consolidated-{item['conclusion_source_id']}"
        try:
            existing = None
            with knowledge_base.db.connect() as conn:
                row = conn.execute(
                    "SELECT knowledge_key FROM knowledge WHERE knowledge_key=?",
                    (conclusion_id,),
                ).fetchone()
                existing = row
            if existing:
                skipped += 1
                continue
            knowledge_base.add_document(
                category=kind_to_category.get(item["kind"], "常见问答"),
                intent=f"consolidated-{item['kind']}",
                question=str(item["conclusion"])[:500],
                answer=str(item["conclusion"]),
                keywords="",
                risk_level="low",
                source=f"dream-cycle:consolidated:{item['conclusion_source_id']}",
                status="active",
                approved_by="dream-cycle",
                tenant_id=tenant_id,
                knowledge_key=conclusion_id,
                layer="evolution",
                store_id=default_store_id if item["scope"] == "seller" else None,
            )
            written += 1
        except Exception:
            skipped += 1
    return {"written": written, "skipped_existing": skipped}


def run_dream_cycle(
    items: list[KnowledgeItem],
    *,
    existing_ids: Iterable[str] = (),
    min_facts: int = 3,
    threshold: float = 0.85,
) -> dict:
    """一次完整的梦循环（三个作业依次跑），返回汇总报告。

    这是"一键跑梦循环"的入口，供 cron / CLI / 测试调用。
    """
    ingest_report = ingest(items, existing_ids)
    consistency = consistency_check(items)
    consolidate_report = consolidate(items, min_facts=min_facts, threshold=threshold)

    return {
        "ingest": {
            "new": len(ingest_report.new_items),
            "duplicates": ingest_report.duplicates,
        },
        "consistency": {
            "dangling_references": len(consistency.dangling_references),
            "orphan_nodes": len(consistency.orphan_nodes),
        },
        "consolidate": {
            "clusters": len(consolidate_report.clusters),
            "consolidated": len(consolidate_report.consolidated),
            "skipped": consolidate_report.skipped,
        },
    }
