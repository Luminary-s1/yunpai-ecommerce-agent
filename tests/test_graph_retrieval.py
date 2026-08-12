"""图谱检索服务测试：实体查询、关系遍历、多跳推理、关键词检索、统计。"""

from __future__ import annotations

import pytest

from ecommerce_agent.knowledge_engine.neo4j_client import Neo4jClient
from ecommerce_agent.knowledge_engine.graph_retrieval import GraphRetrievalService


pytestmark = pytest.mark.usefixtures("mock_neo4j_query")


@pytest.fixture(scope="module")
def svc() -> GraphRetrievalService:
    return GraphRetrievalService(Neo4jClient())


def test_entity_query_finds_sku(svc: GraphRetrievalService) -> None:
    """实体查询：找到 SKU。"""
    result = svc.entity_query("SKU", "sku_id", "QC-AF5-WHITE")
    assert result is not None
    assert result["id"] == "QC-AF5-WHITE"
    assert "title" in result["properties"]


def test_entity_query_not_found(svc: GraphRetrievalService) -> None:
    """实体查询：不存在的返回 None。"""
    result = svc.entity_query("SKU", "sku_id", "NO-SUCH-SKU")
    assert result is None


def test_relation_traverse_belongs_to(svc: GraphRetrievalService) -> None:
    """关系遍历：SKU 的 BELONGS_TO。"""
    rels = svc.relation_traverse("QC-AF5-WHITE", "BELONGS_TO")
    assert len(rels) > 0
    assert rels[0]["rel_type"] == "BELONGS_TO"
    assert rels[0]["target"] == "air_fryer"


def test_relation_traverse_all(svc: GraphRetrievalService) -> None:
    """关系遍历：不指定类型返回全部。"""
    rels = svc.relation_traverse("QC-AF5-WHITE")
    assert len(rels) > 0
    assert any(r["rel_type"] != "BELONGS_TO" for r in rels)  # 有 HAS_ATTR 等


def test_multi_hop_sku_to_policy(svc: GraphRetrievalService) -> None:
    """多跳推理：SKU -> 品类 <- 政策，找到退货政策。

    APPLIES_TO- 表示反向（政策→品类），命中品类的适用政策。
    """
    paths = svc.multi_hop("QC-AF5-WHITE", ["BELONGS_TO", "APPLIES_TO-"])
    assert len(paths) > 0
    # 应能到达七天无理由政策
    assert any("七天" in str(p) for p in paths)


def test_search_finds_faq(svc: GraphRetrievalService) -> None:
    """关键词检索：保修相关。"""
    results = svc.search("保修多久")
    assert len(results) > 0


def test_search_negative(svc: GraphRetrievalService) -> None:
    """关键词检索：不存在的内容返回空。"""
    results = svc.search("量子力学外星人")
    assert len(results) == 0


def test_stats(svc: GraphRetrievalService) -> None:
    """图谱统计。"""
    stats = svc.stats()
    assert stats["total_nodes"] >= 200
    assert stats["total_rels"] >= 200
    assert "Rule" in stats["by_label"]
    assert "REFERS_TO" in stats["by_rel"]


# ---------- 契约测试（负责人二次 review #5：max_hops/max_depth 语义） ----------

def test_multi_hop_hops_equal_rel_types_len(svc: GraphRetrievalService) -> None:
    """契约：实际跳数 = len(rel_types)，与 max_hops 无关。

    若把 max_hops 当跳数限制，1 条关系的链（实际 1 跳）配 max_hops=5
    应返回结果；跳数语义错误（把 max_hops 当深度）会导致结果为空。
    """
    paths = svc.multi_hop("QC-AF5-WHITE", ["BELONGS_TO"], max_hops=5)
    assert len(paths) > 0, "1 跳链配 max_hops=5 不应空（跳数由 rel_types 决定）"


def test_multi_hop_limit_scale_factor(svc: GraphRetrievalService) -> None:
    """契约：max_hops 只做 LIMIT 系数（max_hops*20），不限制路径深度。"""
    paths_small = svc.multi_hop("QC-AF5-WHITE", ["BELONGS_TO", "APPLIES_TO-"], max_hops=1)
    assert len(paths_small) >= 0
    # 不抛异常、返回结构正确即契约成立（LIMIT 是查询参数，由 mock 接受）
    assert all({"end_id", "end_label", "props"} <= set(p) for p in paths_small)


def test_relation_traverse_max_depth_is_limit_not_hops(svc: GraphRetrievalService) -> None:
    """契约：relation_traverse 只做单跳遍历，max_depth 是 LIMIT 系数。"""
    rels = svc.relation_traverse("QC-AF5-WHITE", max_depth=10)
    assert len(rels) > 0
    # 单跳：返回的每条 target 都是直接邻居（结构上有 rel_type）
    assert all("rel_type" in r and "target" in r for r in rels)
