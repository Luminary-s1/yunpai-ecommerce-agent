"""Neo4j HTTP API 客户端（零第三方依赖，用 urllib）。

遵循项目"零新增依赖"约束，通过 Neo4j HTTP API 访问图谱。
所有查询支持参数化（$param），防止 Cypher 注入。
"""

from __future__ import annotations

import base64
import json
import urllib.request
from typing import Any


class Neo4jError(RuntimeError):
    """Neo4j 连接或查询错误。"""


class Neo4jClient:
    """Neo4j 图数据库 HTTP 客户端。

    用法：
        client = Neo4jClient()
        rows = client.query("MATCH (n) RETURN count(n)")

    所有外部输入必须通过 params 传递（$name 占位），禁止字符串拼接。
    """

    def __init__(
        self,
        uri: str = "http://localhost:7474",
        user: str = "neo4j",
        password: str = "yunpai123",
    ) -> None:
        self.endpoint = f"{uri}/db/neo4j/tx/commit"
        self.token = base64.b64encode(f"{user}:{password}".encode()).decode()

    def query(
        self,
        statement: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> list[list]:
        """执行 Cypher 语句，返回行列表（每行是值列表）。

        参数：
            statement: Cypher 语句（外部值必须用 $name 占位）
            params: 参数绑定（防注入）

        返回：每行是一个列表，元素按 RETURN 顺序。
        """
        payload = {"statements": [{"statement": statement}]}
        if params:
            payload["statements"][0]["parameters"] = params
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Basic {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                response = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise Neo4jError(f"Neo4j 连接失败: {exc}") from exc
        if response.get("errors"):
            raise Neo4jError(f"Neo4j 查询失败: {response['errors']}")
        results = response.get("results", [])
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
