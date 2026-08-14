"""Neo4j 客户端测试：连接、查询、参数化防注入。"""

from __future__ import annotations

import pytest

from ecommerce_agent.knowledge_engine.neo4j_client import Neo4jClient, Neo4jError


pytestmark = pytest.mark.usefixtures("mock_neo4j_transport")


def test_client_query_returns_rows() -> None:
    """query 返回行列表。"""
    client = Neo4jClient()
    rows = client.query("RETURN 1 AS one")
    assert rows == [[1]]


def test_client_connect_check() -> None:
    """连接测试。"""
    client = Neo4jClient()
    assert client.connect_check() is True


def test_client_params_prevents_injection() -> None:
    """参数化查询：注入尝试不产生额外结果，且不报错。"""
    client = Neo4jClient()
    # 用参数化：value 是注入串，应只当字面量匹配
    rows = client.query(
        "MATCH (n) WHERE n.id = $value RETURN count(n)",
        params={"value": "'; DETACH DELETE n; //"},
    )
    # 不崩溃，返回一个数字
    assert len(rows) == 1
    assert isinstance(rows[0][0], int)


def test_client_bad_connection_raises() -> None:
    """连接失败抛 Neo4jError。"""
    client = Neo4jClient(password="wrong-password")
    with pytest.raises(Neo4jError):
        client.query("RETURN 1")
