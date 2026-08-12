"""检索质量评测套件测试：30+ 问题，通过率验证。"""

from __future__ import annotations

import pytest

from ecommerce_agent.knowledge_engine.neo4j_client import Neo4jClient
from ecommerce_agent.knowledge_engine.graph_retrieval import GraphRetrievalService
from ecommerce_agent.knowledge_engine.evaluation_suite import (
    EVALUATION_QUESTIONS,
    run_evaluation,
)


pytestmark = pytest.mark.usefixtures("mock_neo4j_query")


@pytest.fixture(scope="module")
def svc() -> GraphRetrievalService:
    return GraphRetrievalService(Neo4jClient())


def test_30_plus_questions() -> None:
    """评测问题集 ≥ 30 个（验收文档要求 30+）。"""
    assert len(EVALUATION_QUESTIONS) >= 30


def test_questions_have_required_fields() -> None:
    """每个问题都有 q/scene/expected_terms。"""
    for item in EVALUATION_QUESTIONS:
        assert "q" in item and item["q"]
        assert "scene" in item
        assert "expected_terms" in item


def test_run_evaluation_returns_report(svc: GraphRetrievalService) -> None:
    """评测返回报告，通过率 ≥ 0.9（对齐 scheduler 门禁，不再 0.5 松闸）。"""
    report = run_evaluation(svc)
    assert report["total"] >= 30
    assert report["pass_rate"] >= 0.9, f"通过率 {report['pass_rate']} 过低"


def test_evaluation_has_negative_cases() -> None:
    """评测含负例（异常场景应检索不到）。"""
    scenes = {item["scene"] for item in EVALUATION_QUESTIONS}
    assert "negative" in scenes
