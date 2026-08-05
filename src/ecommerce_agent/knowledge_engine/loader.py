"""knowledge_engine 加载器：把任务6交付的 02_clean/ JSON 加载为统一知识模型。

设计原则（低耦合、可复用）：
- 只读任务6交付物（02_clean/*.json），绝不修改它们。
- 输出是 models.KnowledgeItem 列表，下游（图谱导入/Wiki/RAG/梦循环）都可消费。
- 加载逻辑与存储、检索解耦：本模块只负责"读 + 转 + 标 scope"。

scope 自动判定规则（对齐三层知识边界）：
- rule（行业规则）：general，跨租户通用
- script 中 layer=platform/industry 的通用话术：general
- policy 中 scope_key=all 的跨品类政策：general
- 其余（商品/SKU/政策单店/FAQ/话术店级）：seller
- memory 默认不自动标（需显式传入，或由梦循环写入）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .models import (
    KnowledgeItem,
    KnowledgeKind,
    KnowledgeScope,
    TimelineEntry,
    coerce_kind,
    utc_now_iso,
)


# 每个 kind 对应的 JSON 文件名（任务6交付物的命名约定）
_KIND_TO_FILENAME: dict[KnowledgeKind, str] = {
    KnowledgeKind.CATEGORY: "category.json",
    KnowledgeKind.PRODUCT: "product.json",
    KnowledgeKind.SKU: "sku.json",
    KnowledgeKind.ATTRIBUTE: "attribute.json",
    KnowledgeKind.POLICY: "policy.json",
    KnowledgeKind.SCRIPT: "script.json",
    KnowledgeKind.FAQ: "faq.json",
    KnowledgeKind.RULE: "rule.json",
}

# 可选的扩展数据文件（同 kind，追加加载，如规则扩充）
_EXTRA_FILES: dict[KnowledgeKind, list[str]] = {
    KnowledgeKind.RULE: ["rule_extended.json"],
}


def _to_truth(kind: KnowledgeKind, record: dict) -> str:
    """从一条记录生成 compiled_truth（当前最佳结论）。

    不同实体取最能代表"它是什么"的字段拼成一句话。
    """
    kind = coerce_kind(kind.value)
    if kind is KnowledgeKind.PRODUCT:
        return record.get("title", record.get("item_id", ""))
    if kind is KnowledgeKind.SKU:
        return record.get("title", record.get("sku_id", ""))
    if kind is KnowledgeKind.CATEGORY:
        return record.get("category_name", record.get("category_code", ""))
    if kind is KnowledgeKind.POLICY:
        # 当前结论 = 政策名 + 具体内容（让 Wiki 词条有信息量）
        name = record.get("policy_name", "")
        content = record.get("content", "")
        return f"{name}：{content}" if name and content else (name or content)
    if kind is KnowledgeKind.SCRIPT:
        return record.get("canonical_answer", record.get("intent", ""))
    if kind is KnowledgeKind.FAQ:
        return record.get("answer", record.get("question", ""))
    if kind is KnowledgeKind.ATTRIBUTE:
        return f"{record.get('attr_key', '')}={record.get('attr_value', '')}"
    if kind is KnowledgeKind.RULE:
        # 当前结论 = 规则名 + 内容摘要
        title = record.get("rule_title", "")
        summary = record.get("content_summary", "")
        return f"{title}：{summary}" if title and summary else (title or summary)
    return str(record.get("id", ""))


def _to_id(kind: KnowledgeKind, record: dict) -> str:
    """取实体的唯一键作为 KnowledgeItem.id。"""
    key_map: dict[KnowledgeKind, list[str]] = {
        KnowledgeKind.CATEGORY: ["category_code"],
        KnowledgeKind.PRODUCT: ["item_id"],
        KnowledgeKind.SKU: ["sku_id"],
        KnowledgeKind.ATTRIBUTE: ["spec_key"],
        KnowledgeKind.POLICY: ["policy_code"],
        KnowledgeKind.SCRIPT: ["script_id"],
        KnowledgeKind.FAQ: ["faq_id"],
        KnowledgeKind.RULE: ["rule_code"],
    }
    for key in key_map.get(coerce_kind(kind.value), ["id"]):
        if key in record and record[key]:
            return str(record[key])
    return str(record.get("id", ""))


def _is_general(kind: KnowledgeKind, record: dict) -> bool:
    """判断一条记录是否属于 general（跨租户通用）。

    规则：
    - rule（行业规则）→ general
    - policy 且 scope_key=all → general（跨品类政策）
    - script 且 layer 是 platform/industry → general（平台/行业通用话术）
    - 其余 → seller
    """
    kind = coerce_kind(kind.value)
    if kind is KnowledgeKind.RULE:
        return True
    if kind is KnowledgeKind.POLICY and record.get("scope_key") == "all":
        return True
    if kind is KnowledgeKind.SCRIPT and record.get("layer") in ("platform", "industry"):
        return True
    return False


def infer_scope(kind: KnowledgeKind, record: dict) -> KnowledgeScope:
    """自动判定记录的三层边界 scope。"""
    if _is_general(kind, record):
        return KnowledgeScope.GENERAL
    return KnowledgeScope.SELLER


def load_record(
    kind: KnowledgeKind, record: dict, *, scope: KnowledgeScope | None = None
) -> KnowledgeItem:
    """把一条任务6记录转成 KnowledgeItem。

    scope 不传时自动判定；compiled_truth 由记录生成；timeline 初始为 created。
    """
    kind = coerce_kind(kind.value)
    resolved_scope = scope or infer_scope(kind, record)
    item = KnowledgeItem(
        id=_to_id(kind, record),
        kind=kind,
        scope=resolved_scope,
        compiled_truth=_to_truth(kind, record),
        attributes=dict(record),
        scope_key="all" if resolved_scope is KnowledgeScope.GENERAL else "all",
    )
    item.timeline.append(
        TimelineEntry(
            # 优先用记录自带的真实采集/更新时间作为 created 时间戳；
            # 否则用当前时间。这保证历史数据（任务6产物）能参与合并记忆，
            # 而不是所有知识都是"刚刚创建"永远不满 24h。
            at=record.get("captured_at") or record.get("updated_at") or utc_now_iso(),
            action="created",
            note=f"加载自任务6交付物：{_KIND_TO_FILENAME[kind]}",
            source=record.get("source", ""),
        )
    )
    return item


def load_clean_dir(clean_dir: str | Path) -> list[KnowledgeItem]:
    """加载任务6交付的 02_clean/ 目录下的所有 JSON。

    参数：
        clean_dir: 02_clean 目录路径（如 knowledge_graph_output/02_clean）

    返回：
        全部 KnowledgeItem 列表（六类实体 + 规则）。
    """
    base = Path(clean_dir)
    if not base.is_dir():
        raise FileNotFoundError(f"02_clean 目录不存在：{base}")

    items: list[KnowledgeItem] = []
    for kind, filename in _KIND_TO_FILENAME.items():
        paths = [filename] + _EXTRA_FILES.get(kind, [])
        for fname in paths:
            path = base / fname
            if not path.exists():
                continue  # 某个实体类型缺失不阻塞，尽量加载
            with path.open("r", encoding="utf-8") as fh:
                records = json.load(fh)
            for record in records:
                items.append(load_record(kind, record))
    return items


def stats(items: Iterable[KnowledgeItem]) -> dict[str, int]:
    """统计知识分布：按 scope 和 kind。"""
    by_scope: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for item in items:
        by_scope[item.scope.value] = by_scope.get(item.scope.value, 0) + 1
        by_kind[item.kind.value] = by_kind.get(item.kind.value, 0) + 1
    return {"by_scope": by_scope, "by_kind": by_kind, "total": len(list(items))}
