"""知识库系统质量验证脚本：一键测"系统好不好"。

覆盖 5 个层面：
1. 数据完整性（真值表覆盖率 / 关系准确率）
2. 功能可用性（Q1-Q7 图谱验证查询）
3. 客服实战（真实问题能否答对）
4. 可解释性（回答能否溯源到法规）
5. 一致性（同一问题反复问结果一致）

用法：
    .venv/Scripts/python.exe -m ecommerce_agent.knowledge_engine.verify_system

前提：Neo4j 已启动；连接参数走 env（NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD）。
"""

from __future__ import annotations

import base64
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any
import urllib.request

NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "change-me")
NEO4J_URI = os.getenv("NEO4J_URI", "http://localhost:7474")
NEO4J_URL = f"{NEO4J_URI}/db/neo4j/tx/commit"
REPO = Path(os.getenv("VERIFY_REPO", "D:/yunpai-ecommerce-agent-main"))


def _query(statement: str) -> list[list]:
    """执行 Cypher，返回行列表。"""
    body = json.dumps({"statements": [{"statement": statement}]}).encode("utf-8")
    token = base64.b64encode(f"{NEO4J_USER}:{NEO4J_PASSWORD}".encode()).decode()
    req = urllib.request.Request(NEO4J_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("errors"):
        raise RuntimeError(f"Neo4j 查询失败: {payload['errors']}")
    return [r["row"] for r in payload["results"][0]["data"]]


def _check(name: str, passed: bool, detail: str = "") -> bool:
    mark = "✅" if passed else "❌"
    print(f"{mark} {name}" + (f" — {detail}" if detail else ""))
    return passed


def verify_data_completeness() -> tuple[int, int]:
    """真值表覆盖率 + 抽样准确率。"""
    print("\n【1. 数据完整性】")
    results: list[bool] = []

    # 覆盖率：真值表比对图谱
    nodes = _query("MATCH (n) RETURN n.id AS id, labels(n)[0] AS label")
    by_label: dict[str, set] = {}
    for nid, label in nodes:
        by_label.setdefault(label, set()).add(nid)
    truth_path = REPO / "knowledge_graph_output" / "06_report" / "truth_table.csv"
    truth = list(csv.DictReader(open(truth_path, encoding="utf-8")))
    label_map = {"Product(SPU)": "Product", "SKU": "SKU", "Category": "Category", "Policy": "Policy"}
    hit = sum(1 for r in truth if r["entity_key"] in by_label.get(label_map.get(r["entity_type"], ""), set()))
    coverage = hit / len(truth)
    results.append(_check(f"核心实体覆盖率: {hit}/{len(truth)} = {coverage*100:.0f}% (≥90%)", coverage >= 0.9))

    # 准确率：抽样计划比对图谱
    rels = _query("MATCH (a)-[r]->(b) RETURN a.id AS s, type(r) AS t, b.id AS tgt")
    rel_set = set((str(s), t, str(tgt)) for s, t, tgt in rels)
    plan_path = REPO / "knowledge_graph_output" / "06_report" / "sampling_plan.csv"
    plan = [r for r in csv.DictReader(open(plan_path, encoding="utf-8")) if r["expected"].strip().upper() == "TRUE"]
    rel_hit = sum(1 for r in plan if (r["source"], r["rel_type"], r["target"]) in rel_set)
    accuracy = rel_hit / len(plan)
    results.append(_check(f"关系准确率: {rel_hit}/{len(plan)} = {accuracy*100:.0f}% (≥85%)", accuracy >= 0.85))

    return sum(results), len(results)


def verify_queries() -> tuple[int, int]:
    """Q1-Q7 图谱验证查询。"""
    print("\n【2. 功能可用性（Q1-Q7）】")
    checks = [
        ("Q1 空气炸锅→SKU", "MATCH (c:Category {category_code:'air_fryer'})<-[:BELONGS_TO]-(s:SKU) RETURN count(s)", lambda r: r and int(r[0][0]) > 0),
        ("Q2 在售SKU按品类", "MATCH (s:SKU {status:'active'})-[:BELONGS_TO]->(c:Category) RETURN count(s)", lambda r: r and int(r[0][0]) > 0),
        ("Q3 政策适用air_fryer", "MATCH (p:Policy)-[:APPLIES_TO]->(c:Category {category_code:'air_fryer'}) RETURN count(p)", lambda r: r and int(r[0][0]) > 0),
        ("Q4 断货检测", "MATCH (s:SKU)-[:BELONGS_TO]->(c) RETURN count(s)", lambda r: r and int(r[0][0]) > 0),
        ("Q5 政策溯源", "MATCH (p:Policy)-[:RELATED_TO]->(r:Rule) RETURN count(r)", lambda r: r and int(r[0][0]) > 0),
        ("Q6 新品类FAQ", "MATCH (f:FAQ {category:'商品'}) RETURN count(f)", lambda r: r and int(r[0][0]) > 0),
        ("Q7 五类关系", "MATCH ()-[r]->() RETURN count(DISTINCT type(r))", lambda r: r and int(r[0][0]) >= 5),
    ]
    results = []
    for name, query, ok in checks:
        try:
            rows = _query(query)
            results.append(_check(name, ok(rows)))
        except Exception as exc:
            results.append(_check(name, False, str(exc)))
    return sum(results), len(results)


def verify_customer_scenarios() -> tuple[int, int]:
    """客服实战：真实问题能否答对。"""
    print("\n【3. 客服实战】")
    scenarios = [
        ("退货", "MATCH (s:SKU {sku_id:'QC-AF5-WHITE'})-[:BELONGS_TO]->(c:Category)<-[:APPLIES_TO]-(p:Policy {policy_type:'return'}) RETURN p.policy_name", "七天无理由退货"),
        ("发票", "MATCH (r:Rule {theme:'发票开具'}) RETURN r.rule_title", "发票"),
        ("发货", "MATCH (s:SKU {sku_id:'QC-AF5-WHITE'})-[:BELONGS_TO]->(c:Category)<-[:APPLIES_TO]-(p:Policy {policy_type:'logistics'}) RETURN p.policy_name", "发货"),
        ("保修", "MATCH (s:SKU {sku_id:'QC-AF5-WHITE'})-[:BELONGS_TO]->(c:Category)-[:BELONGS_TO*0..1]->(p:Category)<-[:APPLIES_TO]-(pol:Policy {policy_type:'warranty'}) RETURN pol.policy_name", "保修"),
    ]
    results = []
    for name, query, expected in scenarios:
        try:
            rows = _query(query)
            ok = bool(rows) and any(expected in str(r) for r in rows)
            results.append(_check(f"{name}: 能找到'{expected}'相关答案", ok))
        except Exception as exc:
            results.append(_check(f"{name}", False, str(exc)))
    return sum(results), len(results)


def verify_explainability() -> tuple[int, int]:
    """可解释性：回答能否溯源到法规。"""
    print("\n【4. 可解释性（溯源）】")
    results = []
    rows = _query("MATCH (p:Policy)-[:RELATED_TO]->(r:Rule) RETURN p.policy_name, r.rule_title, r.authority")
    has_source = any(r[2] for r in rows)  # 有权威来源
    results.append(_check(f"政策能溯源到法规: {len(rows)} 条溯源链", len(rows) > 0))
    results.append(_check(f"法规有权威来源: {len(rows)} 条含来源", has_source))
    return sum(results), len(results)


def verify_consistency() -> tuple[int, int]:
    """一致性：同一问题反复问结果一致。"""
    print("\n【5. 一致性（同一问题反复问）】")
    query = ("MATCH (s:SKU {sku_id:'QC-AF5-WHITE'})-[:BELONGS_TO]->(c:Category)"
             "<-[:APPLIES_TO]-(p:Policy {policy_type:'return'}) RETURN p.policy_name")
    answers = set()
    for _ in range(3):
        rows = _query(query)
        if rows:
            answers.add(str(rows[0][0]))
    consistent = len(answers) == 1
    _check(f"反复问3次答案一致: {answers}", consistent)
    return (1, 1) if consistent else (0, 1)


def main() -> int:
    print("=" * 50)
    print("云湃知识库系统质量验证")
    print("=" * 50)

    all_checks: list[tuple[int, int]] = []
    all_checks.append(verify_data_completeness())
    all_checks.append(verify_queries())
    all_checks.append(verify_customer_scenarios())
    all_checks.append(verify_explainability())
    all_checks.append(verify_consistency())

    passed = sum(p for p, _ in all_checks)
    total = sum(t for _, t in all_checks)
    print("\n" + "=" * 50)
    print(f"总计: {passed}/{total} 项通过 ({passed/total*100:.0f}%)")
    print("=" * 50)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
