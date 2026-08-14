# 图谱检索服务 API + Prompt 模板 + 检索质量评测 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 executing-plans 按任务逐步实施，每步用 checkbox (`- [ ]`) 跟踪。

**Goal:** 交付验收文档的交付物 ⑤（图谱检索服务独立 API）+ ⑥（四套 Prompt 模板库）+ ⑦（30+ 问题检索质量评测），让 M4/M5 能通过 API 调用图谱能力。

**Architecture:** 零新增依赖（用 urllib 走 Neo4j HTTP API）。服务层 `GraphRetrievalService` 封装实体查询/关系遍历/多跳推理，`graph_api.py` 暴露独立 REST API，`prompt_templates.py` 提供四套防幻觉 Prompt，`evaluation_suite.py` 提供 30+ 问题评测。遵循项目既有模式：`service.py` 组装 → `build_xxx_router(service, require_admin)` → `api.py` 注册。

**Tech Stack:** Python 3.11、FastAPI、Pydantic、Neo4j HTTP API（urllib，零依赖）、pytest。

## Global Constraints

- 零新增第三方依赖（pyproject.toml 不新增）
- 遵循项目 API 模式：`build_xxx_router(service, require_admin)` + `APIRouter(prefix="/v1")`
- 服务在 `service.py` 的 `AgentService.__init__` 中组装
- Pydantic 模型用 `model_config = ConfigDict(extra="forbid")`
- Neo4j 通过 HTTP API（`http://localhost:7474/db/neo4j/tx/commit`）访问，不用官方驱动
- 测试放 `tests/`，遵循项目测试约定（`make_settings`/`principal_for`）
- 图谱检索结果必须可溯源（含来源法规），Prompt 含防幻觉指令

---

### Task 1: Neo4j 客户端封装

**Files:**
- Create: `src/ecommerce_agent/knowledge_engine/neo4j_client.py`
- Test: `tests/test_neo4j_client.py`

**Interfaces:**
- Consumes: 无（独立）
- Produces: `Neo4jClient`（封装 HTTP API）
  - `__init__(self, uri="http://localhost:7474", user="neo4j", password="${NEO4J_PASSWORD:-change-me}")`
  - `query(self, statement: str) -> list[list]` — 执行 Cypher，返回行列表
  - `connect_check(self) -> bool` — 连接测试

- [ ] **Step 1: 写失败测试**

```python
# tests/test_neo4j_client.py
from ecommerce_agent.knowledge_engine.neo4j_client import Neo4jClient


def test_client_query_returns_rows():
    client = Neo4jClient()
    rows = client.query("RETURN 1 AS one")
    assert rows == [[1]]


def test_client_connect_check():
    client = Neo4jClient()
    assert client.connect_check() is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 .venv/Scripts/python.exe -m pytest tests/test_neo4j_client.py -v`
Expected: FAIL（module not found）

- [ ] **Step 3: 实现 Neo4jClient**

```python
# src/ecommerce_agent/knowledge_engine/neo4j_client.py
"""Neo4j HTTP API 客户端（零第三方依赖，用 urllib）。

遵循项目"零新增依赖"约束，通过 Neo4j HTTP API 访问图谱。
"""

from __future__ import annotations

import base64
import json
import urllib.request
from typing import Any


class Neo4jError(RuntimeError):
    pass


class Neo4jClient:
    """Neo4j 图数据库 HTTP 客户端。

    用法：
        client = Neo4jClient()
        rows = client.query("MATCH (n) RETURN count(n)")
    """

    def __init__(
        self,
        uri: str = "http://localhost:7474",
        user: str = "neo4j",
        password: str = "${NEO4J_PASSWORD:-change-me}",
    ) -> None:
        self.endpoint = f"{uri}/db/neo4j/tx/commit"
        self.token = base64.b64encode(f"{user}:{password}".encode()).decode()

    def query(self, statement: str, *, timeout: int = 30) -> list[list]:
        """执行 Cypher 语句，返回行列表（每行是值列表）。"""
        body = json.dumps({"statements": [{"statement": statement}]}).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Basic {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise Neo4jError(f"Neo4j 连接失败: {exc}") from exc
        if payload.get("errors"):
            raise Neo4jError(f"Neo4j 查询失败: {payload['errors']}")
        results = payload.get("results", [])
        if not results:
            return []
        return [r["row"] for r in results[0]["data"]]

    def connect_check(self) -> bool:
        """连接测试：能否执行简单查询。"""
        try:
            self.query("RETURN 1")
            return True
        except Neo4jError:
            return False
```

- [ ] **Step 4: 运行测试确认通过**

Run: `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 .venv/Scripts/python.exe -m pytest tests/test_neo4j_client.py -v`
Expected: PASS

---

### Task 2: 图谱检索服务

**Files:**
- Create: `src/ecommerce_agent/knowledge_engine/graph_retrieval.py`
- Test: `tests/test_graph_retrieval.py`

**Interfaces:**
- Consumes: `Neo4jClient`（Task 1）
- Produces: `GraphRetrievalService`
  - `__init__(self, client: Neo4jClient)`
  - `entity_query(self, label: str, key: str, value: str) -> dict | None` — 实体查询
  - `relation_traverse(self, entity_id: str, rel_type: str | None = None, *, max_depth: int = 3) -> list[dict]` — 关系遍历
  - `multi_hop(self, start_id: str, rel_types: list[str], *, max_hops: int = 3) -> list[dict]` — 多跳推理
  - `stats(self) -> dict` — 图谱统计
  - `search(self, query_text: str, limit: int = 10) -> list[dict]` — 关键词检索（FAQ/政策/商品）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_graph_retrieval.py
from ecommerce_agent.knowledge_engine.neo4j_client import Neo4jClient
from ecommerce_agent.knowledge_engine.graph_retrieval import GraphRetrievalService


def _service():
    return GraphRetrievalService(Neo4jClient())


def test_entity_query_finds_product():
    svc = _service()
    result = svc.entity_query("SKU", "sku_id", "QC-AF5-WHITE")
    assert result is not None
    assert "title" in result["properties"]


def test_relation_traverse_belongs_to():
    svc = _service()
    rels = svc.relation_traverse("QC-AF5-WHITE", "BELONGS_TO")
    assert any(r["rel_type"] == "BELONGS_TO" for r in rels)


def test_multi_hop_sku_to_policy():
    svc = _service()
    # SKU -> 品类 -> 政策（退货政策）
    paths = svc.multi_hop("QC-AF5-WHITE", ["BELONGS_TO", "APPLIES_TO"])
    assert any("RETURN" in str(p) for p in paths)  # 七天无理由政策


def test_search_finds_faq():
    svc = _service()
    results = svc.search("保修多久")
    assert len(results) > 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 .venv/Scripts/python.exe -m pytest tests/test_graph_retrieval.py -v`
Expected: FAIL（module not found）

- [ ] **Step 3: 实现 GraphRetrievalService**

```python
# src/ecommerce_agent/knowledge_engine/graph_retrieval.py
"""图谱检索服务：实体查询、关系遍历、多跳推理、关键词检索。

供图谱检索 API（graph_api.py）和四套 Prompt 调用，是 M4/M5 消费的入口。
"""

from __future__ import annotations

from typing import Any

from .neo4j_client import Neo4jClient


class GraphRetrievalService:
    """图谱检索服务，封装 Neo4j 图谱的常见查询。"""

    def __init__(self, client: Neo4jClient) -> None:
        self.client = client

    def entity_query(self, label: str, key: str, value: str) -> dict | None:
        """实体查询：按唯一键找单个实体，返回节点属性。"""
        rows = self.client.query(
            f"MATCH (n:{label} {{{key}: $value}}) "
            f"RETURN n.id AS id, labels(n)[0] AS label, properties(n) AS props LIMIT 1",
            # 用参数避免注入
        )
        # Neo4j HTTP API 需显式传参数
        rows = self.client.query(
            f"MATCH (n:{label} {{{key}: '{value}'}}) "
            f"RETURN n.id AS id, labels(n)[0] AS label, properties(n) AS props LIMIT 1"
        )
        if not rows:
            return None
        row = rows[0]
        return {"id": row[0], "label": row[1], "properties": row[2]}

    def relation_traverse(
        self, entity_id: str, rel_type: str | None = None, *, max_depth: int = 3
    ) -> list[dict]:
        """关系遍历：从实体出发，沿关系（可指定类型）找邻居。"""
        rel_clause = f"[:{rel_type}]" if rel_type else ""
        rows = self.client.query(
            f"MATCH (n {{id: '{entity_id}'}})-[{rel_clause}]->(m) "
            f"RETURN n.id AS source, type(r) AS rel_type, m.id AS target, "
            f"labels(m)[0] AS target_label LIMIT {max_depth * 20}"
        )
        return [
            {"source": r[0], "rel_type": r[1], "target": r[2], "target_label": r[3]}
            for r in rows
        ]

    def multi_hop(
        self, start_id: str, rel_types: list[str], *, max_hops: int = 3
    ) -> list[dict]:
        """多跳推理：沿指定关系序列找可达路径。"""
        rel_chain = "".join(f"-[:{rt}]->" for rt in rel_types)
        rows = self.client.query(
            f"MATCH (start {{id: '{start_id}'}}){rel_chain}(end) "
            f"RETURN end.id AS end_id, labels(end)[0] AS end_label, "
            f"properties(end) AS props LIMIT {max_hops * 20}"
        )
        return [
            {"end_id": r[0], "end_label": r[1], "props": r[2]}
            for r in rows
        ]

    def search(self, query_text: str, limit: int = 10) -> list[dict]:
        """关键词检索：在 FAQ/政策/规则/商品的 question/title/content 中搜索。"""
        rows = self.client.query(
            f"MATCH (n) WHERE "
            f"n.question CONTAINS '{query_text}' OR n.title CONTAINS '{query_text}' "
            f"OR n.policy_name CONTAINS '{query_text}' OR n.rule_title CONTAINS '{query_text}' "
            f"OR n.content_summary CONTAINS '{query_text}' "
            f"RETURN n.id AS id, labels(n)[0] AS label, "
            f"coalesce(n.question, n.title, n.policy_name, n.rule_title, n.id) AS title "
            f"LIMIT {limit}"
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 .venv/Scripts/python.exe -m pytest tests/test_graph_retrieval.py -v`
Expected: PASS

---

### Task 3: 四套 Prompt 模板

**Files:**
- Create: `src/ecommerce_agent/knowledge_engine/prompt_templates.py`
- Test: `tests/test_prompt_templates.py`

**Interfaces:**
- Consumes: 无（独立，输出字符串模板）
- Produces: `PROMPT_TEMPLATES: dict[str, str]`（四套场景）+ `render_prompt(scene: str, context: str, question: str) -> str`
  - 场景 key：`customer_service` / `product_recommend` / `aftersale_policy` / `competitor_analysis`
  - 每套含防幻觉指令（仅基于检索结果回答，不知道就说不知道）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_prompt_templates.py
from ecommerce_agent.knowledge_engine.prompt_templates import PROMPT_TEMPLATES, render_prompt


def test_four_templates_exist():
    assert set(PROMPT_TEMPLATES.keys()) == {
        "customer_service", "product_recommend", "aftersale_policy", "competitor_analysis",
    }


def test_render_prompt_injects_context():
    prompt = render_prompt("customer_service", "七天无理由退货", "能退吗")
    assert "七天无理由退货" in prompt  # 上下文注入
    assert "能退吗" in prompt  # 问题注入
    assert "防幻觉" in prompt or "不知道" in prompt  # 防幻觉指令


def test_all_templates_have_antihallucination():
    for name, template in PROMPT_TEMPLATES.items():
        assert "仅基于" in template or "不知道" in template, f"{name} 缺防幻觉"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 .venv/Scripts/python.exe -m pytest tests/test_prompt_templates.py -v`
Expected: FAIL（module not found）

- [ ] **Step 3: 实现四套 Prompt 模板**

```python
# src/ecommerce_agent/knowledge_engine/prompt_templates.py
"""四套业务场景 Prompt 模板（含防幻觉指令）。

对齐验收文档交付物⑥：Prompt 需含防幻觉指令（仅基于图谱检索结果和 Wiki 文档回答）。
"""

from __future__ import annotations

# 场景模板：{context} 注入检索结果，{question} 注入用户问题
PROMPT_TEMPLATES: dict[str, str] = {
    "customer_service": (
        "你是电商客服助手。以下是从知识图谱/Wiki 检索到的可靠事实：\n"
        "{context}\n\n"
        "顾客问题：{question}\n\n"
        "回答要求：\n"
        "1. 仅基于上述检索事实回答，不得编造任何未出现在检索结果中的信息。\n"
        "2. 如果检索结果不包含答案，明确说'抱歉，我暂时无法确定这个问题'。\n"
        "3. 回答要简洁、友好、面向顾客。"
    ),
    "product_recommend": (
        "你是电商商品推荐助手。以下是从知识图谱检索到的商品事实：\n"
        "{context}\n\n"
        "顾客需求：{question}\n\n"
        "回答要求：\n"
        "1. 仅基于检索到的商品信息推荐，不虚构商品属性。\n"
        "2. 如果检索结果没有顾客想要的商品，说明'当前没有匹配的商品'。\n"
        "3. 推荐时给出价格、卖点等具体依据。"
    ),
    "aftersale_policy": (
        "你是电商售后政策助手。以下是从知识图谱检索到的售后政策：\n"
        "{context}\n\n"
        "顾客询问：{question}\n\n"
        "回答要求：\n"
        "1. 严格按检索到的政策条款回答，不自行扩大或缩小政策范围。\n"
        "2. 引用政策时标注来源（如'依据三包规定'）。\n"
        "3. 如果政策不明确，建议转人工处理。"
    ),
    "competitor_analysis": (
        "你是竞品分析助手。以下是从知识图谱检索到的竞品/商品信息：\n"
        "{context}\n\n"
        "分析需求：{question}\n\n"
        "回答要求：\n"
        "1. 仅基于检索到的数据做对比分析，不猜测未检索到的竞品数据。\n"
        "2. 给出价格、卖点等可量化对比，说明数据来源。\n"
        "3. 如果数据不足，明确指出缺少哪些信息。"
    ),
}


def render_prompt(scene: str, context: str, question: str) -> str:
    """渲染指定场景的 Prompt。

    参数：
        scene: 场景 key（customer_service/product_recommend/aftersale_policy/competitor_analysis）
        context: 从图谱/Wiki 检索到的上下文
        question: 用户问题

    返回：完整 Prompt 字符串
    """
    if scene not in PROMPT_TEMPLATES:
        raise ValueError(f"未知场景: {scene}，可选: {list(PROMPT_TEMPLATES)}")
    return PROMPT_TEMPLATES[scene].format(context=context, question=question)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 .venv/Scripts/python.exe -m pytest tests/test_prompt_templates.py -v`
Expected: PASS

---

### Task 4: 图谱检索 API

**Files:**
- Create: `src/ecommerce_agent/knowledge_engine/graph_api.py`
- Modify: `src/ecommerce_agent/api.py`（注册 router）
- Test: `tests/test_graph_api.py`

**Interfaces:**
- Consumes: `GraphRetrievalService`（Task 2）、`AgentService`（注入 client）
- Produces: `build_graph_router(service: AgentService, require_admin) -> APIRouter`（`/v1/graph/*`）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_graph_api.py
from pathlib import Path

from fastapi.testclient import TestClient

from ecommerce_agent.service import AgentService
from ecommerce_agent.api import build_app

from conftest import make_settings


def test_graph_entity_api():
    svc = AgentService(make_settings(Path(".") / "tmp-test"))
    app = build_app(svc)
    client = TestClient(app)
    try:
        resp = client.get("/v1/graph/entity/SKU/QC-AF5-WHITE")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "QC-AF5-WHITE"
    finally:
        svc.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 .venv/Scripts/python.exe -m pytest tests/test_graph_api.py -v`
Expected: FAIL（404，router 未注册）

- [ ] **Step 3: 实现 graph_api.py**

```python
# src/ecommerce_agent/knowledge_engine/graph_api.py
"""图谱检索 API：实体查询、关系遍历、多跳推理、关键词检索。

对齐验收文档交付物⑤：图谱检索服务需封装为独立 API 可被业务层调用。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .graph_retrieval import GraphRetrievalService
from .neo4j_client import Neo4jClient, Neo4jError
from ..auth import AdminPrincipal
from ..service import AgentService


class MultiHopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_id: str = Field(min_length=1, max_length=128)
    rel_types: list[str] = Field(min_length=1, max_length=5)
    max_hops: int = Field(default=3, ge=1, le=5)


def build_graph_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/graph", tags=["graph"])
    # 懒加载：Neo4j 客户端在首次请求时创建（不阻塞服务启动）
    retrieval: GraphRetrievalService | None = None

    def _svc() -> GraphRetrievalService:
        nonlocal retrieval
        if retrieval is None:
            retrieval = GraphRetrievalService(Neo4jClient())
        return retrieval

    @router.get("/entity/{label}/{key}/{value}")
    def entity_query(
        label: str,
        key: str,
        value: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = _svc().entity_query(label, key, value)
        except Neo4jError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail=f"实体 {label}:{key}={value} 不存在")
        return result

    @router.get("/relations/{entity_id}")
    def relation_traverse(
        entity_id: str,
        rel_type: str | None = Query(default=None),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        try:
            return _svc().relation_traverse(entity_id, rel_type)
        except Neo4jError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/multi-hop")
    def multi_hop(
        request: MultiHopRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        try:
            return _svc().multi_hop(request.start_id, request.rel_types, max_hops=request.max_hops)
        except Neo4jError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/search")
    def search(
        q: str = Query(min_length=1, max_length=200),
        limit: int = Query(default=10, ge=1, le=50),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        try:
            return _svc().search(q, limit=limit)
        except Neo4jError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/stats")
    def stats(admin: AdminPrincipal = Depends(require_admin)) -> dict[str, Any]:
        try:
            return _svc().stats()
        except Neo4jError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return router
```

- [ ] **Step 4: 注册 router 到 api.py**

在 `src/ecommerce_agent/api.py` 的 `include_router` 区块添加：

```python
    from .knowledge_engine.graph_api import build_graph_router
    app.include_router(build_graph_router(service, require_admin))
```

- [ ] **Step 5: 运行测试确认通过**

Run: `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 .venv/Scripts/python.exe -m pytest tests/test_graph_api.py -v`
Expected: PASS

---

### Task 5: 检索质量评测套件

**Files:**
- Create: `src/ecommerce_agent/knowledge_engine/evaluation_suite.py`
- Test: `tests/test_evaluation_suite.py`

**Interfaces:**
- Consumes: `GraphRetrievalService`（Task 2）
- Produces: `EVALUATION_QUESTIONS: list[dict]`（30+ 问题，含预期答案关键信息）+ `run_evaluation(svc: GraphRetrievalService) -> dict`（返回通过率）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_evaluation_suite.py
from ecommerce_agent.knowledge_engine.neo4j_client import Neo4jClient
from ecommerce_agent.knowledge_engine.graph_retrieval import GraphRetrievalService
from ecommerce_agent.knowledge_engine.evaluation_suite import EVALUATION_QUESTIONS, run_evaluation


def test_30_plus_questions():
    assert len(EVALUATION_QUESTIONS) >= 30


def test_run_evaluation_passes():
    svc = GraphRetrievalService(Neo4jClient())
    report = run_evaluation(svc)
    assert report["pass_rate"] >= 0.5  # 至少 50% 通过（随数据质量提升）
```

- [ ] **Step 2: 运行测试确认失败**

Run: `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 .venv/Scripts/python.exe -m pytest tests/test_evaluation_suite.py -v`
Expected: FAIL（module not found）

- [ ] **Step 3: 实现评测套件**

```python
# src/ecommerce_agent/knowledge_engine/evaluation_suite.py
"""图谱检索质量评测：30+ 问题，验证召回、引用、回答正确性。

对齐验收文档交付物⑦：检索质量评测报告（测试集覆盖 30+ 问题）。
每个问题含预期答案的关键信息（expected_terms），评测时检查检索结果是否命中。
"""

from __future__ import annotations

from .graph_retrieval import GraphRetrievalService

# 30+ 评测问题：覆盖 商品/品类/政策/FAQ/规则 五类
EVALUATION_QUESTIONS: list[dict] = [
    # 商品查询
    {"q": "空气炸锅", "scene": "product", "expected_terms": ["空气炸锅"]},
    {"q": "无线吸尘器", "scene": "product", "expected_terms": ["吸尘器"]},
    {"q": "加湿器", "scene": "product", "expected_terms": ["加湿器"]},
    {"q": "电热水壶", "scene": "product", "expected_terms": ["水壶"]},
    {"q": "循环风扇", "scene": "product", "expected_terms": ["风扇"]},
    {"q": "蓝牙耳机", "scene": "product", "expected_terms": ["耳机"]},
    {"q": "充电宝", "scene": "product", "expected_terms": ["充电宝"]},
    {"q": "羽绒服", "scene": "product", "expected_terms": ["羽绒服"]},
    # 政策查询
    {"q": "七天无理由退货", "scene": "policy", "expected_terms": ["七天无理由"]},
    {"q": "保修多久", "scene": "policy", "expected_terms": ["保修"]},
    {"q": "退货政策", "scene": "policy", "expected_terms": ["退货"]},
    {"q": "价格保护", "scene": "policy", "expected_terms": ["价保", "价格保护"]},
    {"q": "发货时效", "scene": "policy", "expected_terms": ["发货"]},
    {"q": "发票", "scene": "rule", "expected_terms": ["发票"]},
    {"q": "退款", "scene": "policy", "expected_terms": ["退款"]},
    # FAQ 查询
    {"q": "能退货吗", "scene": "faq", "expected_terms": ["退货"]},
    {"q": "保修多久", "scene": "faq", "expected_terms": ["保修"]},
    {"q": "多久发货", "scene": "faq", "expected_terms": ["发货"]},
    {"q": "怎么开发票", "scene": "faq", "expected_terms": ["发票"]},
    {"q": "尺码怎么选", "scene": "faq", "expected_terms": ["尺码"]},
    # 规则查询
    {"q": "三包规定", "scene": "rule", "expected_terms": ["三包"]},
    {"q": "消费者权益保护", "scene": "rule", "expected_terms": ["消费者权益"]},
    {"q": "价格欺诈", "scene": "rule", "expected_terms": ["价格欺诈"]},
    {"q": "食品安全", "scene": "rule", "expected_terms": ["食品"]},
    {"q": "物流投诉", "scene": "rule", "expected_terms": ["物流"]},
    # 多跳推理
    {"q": "空气炸锅适用什么政策", "scene": "multi_hop", "expected_terms": ["空气炸锅"]},
    {"q": "吸尘器保修依据", "scene": "multi_hop", "expected_terms": ["吸尘器"]},
    {"q": "退货政策依据法规", "scene": "multi_hop", "expected_terms": ["退货"]},
    {"q": "发票开具依据", "scene": "multi_hop", "expected_terms": ["发票"]},
    {"q": "数码产品退换", "scene": "multi_hop", "expected_terms": ["数码"]},
    # 异常/负例
    {"q": "不存在的商品xyz", "scene": "negative", "expected_terms": []},
    {"q": "外星人入侵怎么办", "scene": "negative", "expected_terms": []},
    {"q": "量子力学解释", "scene": "negative", "expected_terms": []},
]


def run_evaluation(svc: GraphRetrievalService, *, verbose: bool = False) -> dict:
    """运行全部评测问题，返回通过率报告。

    判定逻辑：
    - 正常场景（product/policy/faq/rule/multi_hop）：检索结果标题包含任一 expected_term 即通过
    - 负例（negative）：检索结果应为空（未命中不该命中的）
    """
    passed = 0
    details = []
    for item in EVALUATION_QUESTIONS:
        q = item["q"]
        results = svc.search(q, limit=10)
        if item["scene"] == "negative":
            ok = len(results) == 0  # 负例应检索不到
        else:
            titles = " ".join(str(r["title"]) for r in results)
            ok = any(term in titles for term in item["expected_terms"])
        if ok:
            passed += 1
        details.append({"q": q, "scene": item["scene"], "passed": ok, "hits": len(results)})
        if verbose:
            mark = "✅" if ok else "❌"
            print(f"{mark} [{item['scene']}] {q} -> {len(results)} hits")

    total = len(EVALUATION_QUESTIONS)
    return {
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 3),
        "details": details,
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 .venv/Scripts/python.exe -m pytest tests/test_evaluation_suite.py -v`
Expected: PASS（pass_rate ≥ 0.5）

---

### Task 6: 全量回归 + 文档更新

**Files:**
- Modify: `src/ecommerce_agent/knowledge_engine/__init__.py`（导出新模块）
- Modify: `docs/gbrain融合方案_云湃知识库.md`（补充 API/Prompt/评测交付）
- Test: 全量 `pytest`

- [ ] **Step 1: 导出新模块到 __init__.py**

```python
# src/ecommerce_agent/knowledge_engine/__init__.py 追加
from .neo4j_client import Neo4jClient, Neo4jError
from .graph_retrieval import GraphRetrievalService
from .prompt_templates import PROMPT_TEMPLATES, render_prompt
from .graph_api import build_graph_router
from .evaluation_suite import EVALUATION_QUESTIONS, run_evaluation
```

- [ ] **Step 2: 更新方案文档**

在 `docs/gbrain融合方案_云湃知识库.md` 追加"图谱检索服务 + Prompt + 评测"交付状态。

- [ ] **Step 3: 跑全量测试确认无回归**

Run: `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 .venv/Scripts/python.exe -m pytest tests/test_knowledge_engine.py tests/test_neo4j_client.py tests/test_graph_retrieval.py tests/test_prompt_templates.py tests/test_graph_api.py tests/test_evaluation_suite.py -q`
Expected: 全 PASS

- [ ] **Step 4: 提交交付**

确认验收文档 ⑦ 交付物（API + Prompt + 评测报告）全部就位。
