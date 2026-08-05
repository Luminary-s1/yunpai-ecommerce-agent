"""图谱检索服务：实体查询、关系遍历、多跳推理、关键词检索、统计。

供图谱检索 API（graph_api.py）和四套 Prompt 调用，是 M4/M5 消费的入口。
所有外部值走参数化（$name），标签/类型走白名单校验，防 Cypher 注入。
"""

from __future__ import annotations

from typing import Any

from .neo4j_client import Neo4jClient

# 允许的实体标签（白名单，防注入）
_ALLOWED_LABELS = {
    "Category", "Product", "SKU", "Attribute", "Policy", "Script", "FAQ", "Rule",
}
# 允许的关系类型（白名单）
_ALLOWED_REL_TYPES = {
    "BELONGS_TO", "HAS_ATTR", "APPLIES_TO", "REFERS_TO", "RELATED_TO",
}
# 允许的查询键（防止任意属性名注入）
_ALLOWED_KEYS = {
    "id", "item_id", "sku_id", "category_code", "policy_code", "faq_id",
    "script_id", "rule_code", "spec_key",
}


# 品类 code → 中文名映射（让用户搜"数码/家电"等能命中商品）
_CATEGORY_CN: dict[str, str] = {
    "home_appliance": "小家电",
    "air_fryer": "空气炸锅",
    "cordless_vacuum": "无线吸尘器",
    "humidifier": "加湿器",
    "electric_kettle": "电热水壶",
    "air_circulation_fan": "循环风扇",
    "digital": "数码",
    "digital_audio": "数码音频",
    "digital_power": "数码电源",
    "apparel": "服饰",
}


def _validate_label(label: str) -> str:
    if label not in _ALLOWED_LABELS:
        raise ValueError(f"非法实体标签: {label}")
    return label


def _validate_rel_type(rel_type: str | None) -> str | None:
    if rel_type is not None and rel_type not in _ALLOWED_REL_TYPES:
        raise ValueError(f"非法关系类型: {rel_type}")
    return rel_type


def _validate_key(key: str) -> str:
    if key not in _ALLOWED_KEYS:
        raise ValueError(f"非法查询键: {key}")
    return key


class GraphRetrievalService:
    """图谱检索服务，封装 Neo4j 图谱的常见查询。"""

    def __init__(self, client: Neo4jClient) -> None:
        self.client = client

    def entity_query(self, label: str, key: str, value: str) -> dict | None:
        """实体查询：按唯一键找单个实体，返回节点属性。"""
        label = _validate_label(label)
        key = _validate_key(key)
        rows = self.client.query(
            f"MATCH (n:{label} {{{key}: $value}}) "
            f"RETURN n.id AS id, labels(n)[0] AS label, properties(n) AS props LIMIT 1",
            params={"value": value},
        )
        if not rows:
            return None
        row = rows[0]
        return {"id": row[0], "label": row[1], "properties": row[2]}

    def relation_traverse(
        self, entity_id: str, rel_type: str | None = None, *, max_depth: int = 3
    ) -> list[dict]:
        """关系遍历：从实体出发，沿关系（可指定类型）找邻居。"""
        rel_type = _validate_rel_type(rel_type)
        rel_clause = f"-[r:{rel_type}]->" if rel_type else "-[r]->"
        rows = self.client.query(
            f"MATCH (n {{id: $eid}}){rel_clause}(m) "
            f"RETURN n.id AS source, type(r) AS rel_type, m.id AS target, "
            f"labels(m)[0] AS target_label LIMIT $limit",
            params={"eid": entity_id, "limit": max_depth * 20},
        )
        return [
            {"source": r[0], "rel_type": r[1], "target": r[2], "target_label": r[3]}
            for r in rows
        ]

    def multi_hop(
        self, start_id: str, rel_types: list[str], *, max_hops: int = 3
    ) -> list[dict]:
        """多跳推理：沿指定关系序列找可达路径。

        关系类型支持方向：默认正向，追加 `-` 表示反向。
        例：["BELONGS_TO", "APPLIES_TO-"] 表示 SKU-[:BELONGS_TO]->()<-[:APPLIES_TO]-(end)
        （APPLIES_TO 反向命中"政策→品类"，即找品类的适用政策）
        """
        validated: list[str] = []
        for i, raw in enumerate(rel_types):
            rel_name = raw[:-1] if raw.endswith("-") else raw
            if rel_name not in _ALLOWED_REL_TYPES:
                raise ValueError(f"非法关系类型: {rel_name}")
            is_reverse = raw.endswith("-")
            is_last = i == len(rel_types) - 1
            if is_reverse:
                # 反向：<-[:TYPE]-
                validated.append(f"<-[:{rel_name}]-" + ("" if is_last else "()"))
            else:
                # 正向：-[:TYPE]->，最后一段不带匿名节点
                validated.append(f"-[:{rel_name}]->" + ("" if is_last else "()"))

        rel_chain = "".join(validated)
        rows = self.client.query(
            f"MATCH (start {{id: $sid}}){rel_chain}(end) "
            f"RETURN end.id AS end_id, labels(end)[0] AS end_label, "
            f"properties(end) AS props LIMIT $limit",
            params={"sid": start_id, "limit": max_hops * 20},
        )
        return [
            {"end_id": r[0], "end_label": r[1], "props": r[2]}
            for r in rows
        ]

    def search(self, query_text: str, limit: int = 10) -> list[dict]:
        """关键词检索：在 FAQ/政策/规则/商品的 question/title/content 等字段中搜索。

        覆盖字段（防止内容散落在不同属性导致漏检）：
        - FAQ: question / answer
        - 商品: title
        - 政策: policy_name / content
        - 规则: rule_title / content_summary
        - 话术: canonical_answer / keywords

        查询词支持空格分词：多个词用 AND（全部命中才算）。
        连续中文短语不切分（如"保修多久"整体匹配）。
        """
        terms = query_text.strip().split()
        if not terms:
            return []
        # 中文品类名 → 匹配对应 category code（如"数码"→digital_audio/digital_power）
        # 命中的 code 作为 OR 条件加入（任一命中即可，与文本字段并行）
        code_terms: list[str] = []
        for term in terms:
            for code, cn in _CATEGORY_CN.items():
                if cn == term or term in cn:
                    code_terms.append(code)
        # 构造 AND 条件：每个文本词都要命中至少一个字段
        field_list = [
            "n.question", "n.answer", "n.title", "n.policy_name",
            "n.content", "n.rule_title", "n.content_summary",
            "n.canonical_answer", "n.keywords",
            "n.category", "n.category_name",  # 品类名（支持按品类搜商品）
        ]
        and_clauses = []
        params = {}
        for i, term in enumerate(terms):
            # 每个词一个参数 $t{i}，所有字段都引用它（OR）
            or_clause = " OR ".join(f"{fld} CONTAINS $t{i}" for fld in field_list)
            and_clauses.append(f"({or_clause})")
            params[f"t{i}"] = term
        # 若命中了品类 code，把 code 匹配并入主 OR（而非额外 AND）
        if code_terms:
            code_param = " OR ".join(f"n.category CONTAINS $c{i}" for i in range(len(code_terms)))
            and_clauses[0] = f"({and_clauses[0][1:-1]} OR {code_param})"
            for j, code in enumerate(code_terms):
                params[f"c{j}"] = code
        where = " AND ".join(and_clauses)
        params["limit"] = limit
        rows = self.client.query(
            f"MATCH (n) WHERE {where} "
            f"RETURN n.id AS id, labels(n)[0] AS label, "
            f"coalesce(n.question, n.title, n.policy_name, n.rule_title, n.id) AS title "
            f"LIMIT $limit",
            params=params,
        )
        return [
            {"id": r[0], "label": r[1], "title": r[2]}
            for r in rows
        ]

    def stats(self) -> dict:
        """图谱统计：各实体/关系数量。"""
        node_rows = self.client.query(
            "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS cnt ORDER BY cnt DESC"
        )
        rel_rows = self.client.query(
            "MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS cnt ORDER BY cnt DESC"
        )
        total_nodes = sum(int(r[1]) for r in node_rows)
        total_rels = sum(int(r[1]) for r in rel_rows)
        return {
            "total_nodes": total_nodes,
            "total_rels": total_rels,
            "by_label": {r[0]: int(r[1]) for r in node_rows},
            "by_rel": {r[0]: int(r[1]) for r in rel_rows},
        }
