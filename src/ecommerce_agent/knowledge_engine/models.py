"""knowledge_engine 数据模型：gbrain 融合的三层边界 + 编译真相/时间线。

设计原则（低耦合、可复用）：
- 本模块只定义"数据结构契约"，不依赖任何运行时、数据库、外部服务。
- 任何调用方（RAG、Wiki、图谱导入、梦循环）都通过这里的模型读写知识。
- scope 对应 Amazon-GBrain 的三层知识边界；compiled_truth/timeline
  对应 garrytan/gbrain 的双段页面模型。

scope 语义：
- general  跨租户通用：行业规则、平台法规、通用话术（人人可查）
- seller   单店私有：商品、SKU、售后政策、FAQ（按 store 隔离）
- memory   长期记忆：用户偏好、历史决策（默认隔离，显式查询才进）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class KnowledgeScope(str, Enum):
    """三层知识边界（对应 Amazon-GBrain 的三层 Source）。"""

    GENERAL = "general"
    SELLER = "seller"
    MEMORY = "memory"


class KnowledgeKind(str, Enum):
    """六类实体 + 扩展，对应任务6的 dictionary_schema.json。"""

    CATEGORY = "category"
    PRODUCT = "product"
    SKU = "sku"
    ATTRIBUTE = "attribute"
    POLICY = "policy"
    SCRIPT = "script"
    FAQ = "faq"
    RULE = "rule"


def utc_now_iso() -> str:
    """UTC ISO 时间戳，供时间线等字段使用。"""
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class TimelineEntry:
    """时间线单条记录：证据轨迹，只追加，永不删除。

    对应 gbrain 的 timeline 段（append-only evidence trail）。
    """

    at: str  # ISO 时间
    action: str  # created / revised / consolidated / retired ...
    note: str = ""  # 变更说明
    source: str = ""  # 证据来源

    def to_dict(self) -> dict[str, str]:
        return {"at": self.at, "action": self.action, "note": self.note, "source": self.source}


@dataclass(slots=True)
class KnowledgeItem:
    """单条知识（图谱节点 / Wiki 词条 的统一数据模型）。

    结构：
    - id/kind：唯一标识 + 实体类型
    - scope：三层边界（general / seller / memory）
    - compiled_truth：当前最佳结论（可更新，对应 gbrain 上半段）
    - timeline：演化历史（只追加，对应 gbrain 下半段）
    - attributes：实体自身的字段（随 kind 变化）
    - scope_key：scope=seller 时的店铺维度；scope=memory 时的记忆维度
    """

    id: str
    kind: KnowledgeKind
    scope: KnowledgeScope
    compiled_truth: str
    timeline: list[TimelineEntry] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    scope_key: str = "all"  # seller 时为 store_id，general 时为 "all"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "scope": self.scope.value,
            "scope_key": self.scope_key,
            "compiled_truth": self.compiled_truth,
            "timeline": [e.to_dict() for e in self.timeline],
            "attributes": self.attributes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnowledgeItem":
        return cls(
            id=data["id"],
            kind=KnowledgeKind(data["kind"]),
            scope=KnowledgeScope(data["scope"]),
            scope_key=data.get("scope_key", "all"),
            compiled_truth=data["compiled_truth"],
            timeline=[
                TimelineEntry(**e) for e in data.get("timeline", [])
            ],
            attributes=data.get("attributes", {}),
        )


# 供 from_dict 等场景反序列化时容错：未知 kind/scope 不崩溃
def coerce_kind(value: str) -> KnowledgeKind:
    """kind 是知识主键性质，未知值必须暴露（fail-fast，负责人二次 review #9.1）。

    脏数据静默落 PRODUCT 会把"不是商品的错误条目"伪装成商品，排查极难。
    加载层调用此函数时未知 kind 会抛 ValueError——错误信息含原始值，
    数据层可在调用方统一转 quarantine（隔离）而非吞掉。
    """
    try:
        return KnowledgeKind(value)
    except ValueError:
        raise ValueError(
            f"未知知识类型: {value!r}（合法值: {[k.value for k in KnowledgeKind]}）"
        ) from None
