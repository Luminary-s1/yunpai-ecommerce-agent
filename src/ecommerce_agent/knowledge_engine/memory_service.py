"""知识库 memory 层写入服务（P1-2：让长期记忆真正可用）。

设计（对齐 KnowledgeScope.MEMORY 语义"默认隔离，显式查询才进"）：
- memory 知识 = 店铺级长期记忆（售后高频问题归纳、买家偏好、历史决策结论）
- 写入：layer='evolution' + store_id=店铺 + source='memory://...'
  （复用运行时 layer=evolution 的隔离语义，检索默认不命中）
- 读取：显式传 memory=True 才进（对齐"默认隔离"）
- 幂等：同 store 同内容不重复写（近似去重）

与 evolution.py 的区别：
- evolution 是"反馈→候选→门禁→批准"的治理链路（需审批）
- 本服务是"运营/系统直接记录长期记忆"（低风险事实，直接写 active）
  适合：高频问题归纳、买家偏好（已脱敏）、运营决策结论
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from ..rag import KnowledgeBase

logger = logging.getLogger("knowledge_engine.memory")

# memory 知识的 layer（复用运行时 layer=evolution 隔离语义）
MEMORY_LAYER = "evolution"
# 记忆类别（业务语义）
MEMORY_CATEGORIES = {
    "buyer_preference": "买家偏好",
    "frequent_issue": "高频问题",
    "decision_note": "决策记录",
}


class KnowledgeMemoryService:
    """店铺级长期记忆写入/查询服务。"""

    def __init__(self, knowledge: KnowledgeBase) -> None:
        self.knowledge = knowledge

    def record(
        self,
        store_id: str,
        *,
        fact: str,
        category: str = "frequent_issue",
        source: str = "",
        tenant_id: str | None = None,
    ) -> str:
        """记录一条店铺级长期记忆（写 layer=evolution，检索默认隔离）。

        参数：
            store_id: 店铺 id（记忆按店铺隔离）
            fact: 记忆内容（如"本店退货高峰在周三"）
            category: 记忆类别（buyer_preference/frequent_issue/decision_note）
            source: 证据来源（如"feedback://..."、"chat://..."）
            tenant_id: 租户

        返回：knowledge 行 id。
        """
        if not store_id or not fact.strip():
            raise ValueError("store_id 和 fact 必填")
        # 幂等去重（防呆）：同店铺同内容不重复写，返回已有 id
        with self.knowledge.db.connect() as conn:
            existing_row = conn.execute(
                "SELECT knowledge_key FROM knowledge "
                "WHERE layer=? AND store_id=? AND answer=? AND status='active' "
                "LIMIT 1",
                (MEMORY_LAYER, store_id, fact.strip()),
            ).fetchone()
            if existing_row:
                return str(existing_row["knowledge_key"])
        category_label = MEMORY_CATEGORIES.get(category, category)
        memory_id = f"kg-memory-{uuid.uuid4().hex[:12]}"
        self.knowledge.add_document(
            category=category_label,
            intent=f"memory-{category}",
            question=f"[记忆·{store_id}] {fact[:100]}",
            answer=fact,
            keywords=f"memory {store_id} {category}",
            risk_level="low",
            source=source or "memory://manual",
            status="active",
            approved_by="memory-service",
            tenant_id=tenant_id,
            knowledge_key=memory_id,
            layer=MEMORY_LAYER,
            store_id=store_id,
        )
        return memory_id

    def recall(
        self,
        store_id: str,
        *,
        query: str = "",
        limit: int = 10,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """显式召回店铺记忆（默认隔离：普通检索不命中 memory）。

        参数：
            store_id: 店铺 id
            query: 关键词过滤（空=全部）
            limit: 条数

        返回：记忆行列表。
        """
        params: list[Any] = [MEMORY_LAYER, store_id]
        sql = (
            "SELECT id, knowledge_key, category, intent, question, answer, keywords, "
            "source, store_id, layer, created_at FROM knowledge "
            "WHERE layer=? AND store_id=? AND status='active'"
        )
        if tenant_id:
            sql += " AND (tenant_id IS NULL OR tenant_id=?)"
            params.append(tenant_id)
        if query:
            sql += " AND (question LIKE ? OR answer LIKE ? OR keywords LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like, like])
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.knowledge.db.connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def forget(self, memory_id: str, *, tenant_id: str | None = None) -> bool:
        """删除一条记忆（或停用）。

        参数：
            memory_id: memory 的 knowledge_key（如 kg-memory-xxx）或行 id

        返回：是否删除。
        """
        where = "(id=? OR knowledge_key=?)"
        params: list[Any] = [memory_id, memory_id]
        if tenant_id:
            where += " AND (tenant_id IS NULL OR tenant_id=?)"
            params.append(tenant_id)
        with self.knowledge.db.connect() as conn:
            cur = conn.execute(f"DELETE FROM knowledge WHERE {where}", tuple(params))
            return cur.rowcount > 0
