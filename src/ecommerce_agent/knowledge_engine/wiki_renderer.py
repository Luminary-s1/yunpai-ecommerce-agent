"""knowledge_engine Wiki 渲染器：把 KnowledgeItem 渲染成可互链的词条页。

设计（低耦合、可复用、对齐 gbrain 双段页面）：
- 每个 KnowledgeItem 渲染成一篇 Markdown 词条页
- 每页结构 = 编译真相（上，当前结论）+ 时间线（下，演化历史）
- 页间按引用关系生成互链（[[wikilink]]），形成知识网络
- 纯标准库，输出是 markdown 文本，可落盘 / 可被任何下游消费

gbrain 对应：
- "compiled truth" 上半段 → 本页的"当前结论"
- "timeline" 下半段 → 本页的"演化历史"
- "wikilink" 互链 → 本页的"相关词条"
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .models import KnowledgeItem, KnowledgeKind, KnowledgeScope


def _safe_filename(item_id: str) -> str:
    """把实体 id 转成安全的文件名（Windows 不允许 | 等字符）。"""
    return item_id.replace("|", "_").replace("/", "_").replace("\\", "_").replace(":", "_")


def _title(item: KnowledgeItem) -> str:
    """词条页标题：优先用 attributes 里的名称字段。"""
    attrs = item.attributes
    for key in ("title", "policy_name", "rule_title", "category_name", "question"):
        if attrs.get(key):
            return str(attrs[key])
    return item.compiled_truth


def _ref_links(item: KnowledgeItem) -> list[tuple[str, str]]:
    """提取本词条引用的其他实体 → (目标标题, 目标 kind)。"""
    refs: list[tuple[str, str]] = []
    attrs = item.attributes
    # FAQ 引用话术 / 商品
    if item.kind is KnowledgeKind.FAQ:
        if attrs.get("ref_script_id"):
            refs.append((str(attrs["ref_script_id"]), "Script"))
        if attrs.get("sku_id"):
            refs.append((str(attrs["sku_id"]), "SKU"))
    # SKU 引用商品
    if item.kind is KnowledgeKind.SKU and attrs.get("item_id"):
        refs.append((str(attrs["item_id"]), "Product"))
    # 商品引用品类
    if item.kind is KnowledgeKind.PRODUCT and attrs.get("category"):
        refs.append((str(attrs["category"]), "Category"))
    # 政策引用品类
    if (
        item.kind is KnowledgeKind.POLICY
        and attrs.get("scope") == "Category"
        and attrs.get("scope_key")
        and attrs["scope_key"] != "all"
    ):
        refs.append((str(attrs["scope_key"]), "Category"))
    return refs


def _attributes_table(item: KnowledgeItem) -> str:
    """渲染 attributes 为 markdown 表格（排除长文本和已展示字段）。"""
    skip = {"title", "policy_name", "rule_title", "question", "answer",
            "content", "canonical_answer", "content_summary"}
    rows = []
    for k, v in item.attributes.items():
        if k in skip or v in ("", None):
            continue
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        rows.append(f"| {k} | {v} |")
    if not rows:
        return ""
    return "## 属性\n\n| 字段 | 值 |\n|---|---|\n" + "\n".join(rows) + "\n"


def _timeline_section(item: KnowledgeItem) -> str:
    """渲染时间线（只追加的证据轨迹）。"""
    if not item.timeline:
        return ""
    lines = []
    for e in item.timeline:
        note = f" — {e.note}" if e.note else ""
        src = f"（来源：{e.source}）" if e.source else ""
        lines.append(f"- `{e.at}` **{e.action}**{note}{src}")
    return "## 演化历史\n\n" + "\n".join(lines) + "\n"


def _related_section(item: KnowledgeItem) -> str:
    """渲染相关词条（wikilink 互链）。"""
    refs = _ref_links(item)
    if not refs:
        return ""
    lines = [f"- [[{ref_id}]]（{ref_kind}）" for ref_id, ref_kind in refs]
    return "## 相关词条\n\n" + "\n".join(lines) + "\n"


def render_item(item: KnowledgeItem) -> str:
    """把单个 KnowledgeItem 渲染成一篇 Markdown 词条页。"""
    lines = [
        f"# {_title(item)}",
        "",
        f"> **实体 ID**：`{item.id}` ｜ **类型**：`{item.kind.value}` ｜ "
        f"**层级**：`{item.scope.value}`",
        "",
        "## 当前结论",
        "",
        item.compiled_truth,
        "",
    ]
    attrs_section = _attributes_table(item)
    if attrs_section:
        lines.append(attrs_section)
        lines.append("")
    related = _related_section(item)
    if related:
        lines.append(related)
        lines.append("")
    timeline = _timeline_section(item)
    if timeline:
        lines.append(timeline)
    return "\n".join(lines).rstrip() + "\n"


def render_wiki(items: Iterable[KnowledgeItem], out_dir: str | Path) -> dict[str, int]:
    """渲染整个知识库为 Wiki 词条页，每实体一页，写入 out_dir。

    文件命名：{kind}/{id}.md（按类型分目录，便于浏览）。
    另生成 index.md 总览页。

    返回：{"rendered": 渲染页数, "index": 是否生成}
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    count = 0
    index_links: list[str] = []
    for item in items:
        kind_dir = out / item.kind.value
        kind_dir.mkdir(parents=True, exist_ok=True)
        page = render_item(item)
        (kind_dir / f"{_safe_filename(item.id)}.md").write_text(page, encoding="utf-8")
        index_links.append(f"- [[{item.id}]]（{item.kind.value} · {item.scope.value}）")
        count += 1

    # 生成 index 总览页
    index_content = [
        "# 云湃知识库 Wiki",
        "",
        f"> 共 {count} 个词条，由 knowledge_engine 渲染生成。",
        "",
        "## 词条索引",
        "",
        *sorted(index_links),
        "",
    ]
    (out / "index.md").write_text("\n".join(index_content), encoding="utf-8")

    return {"rendered": count, "index": 1}
