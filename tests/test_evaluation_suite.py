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


# ---------- P2-2 评测阈值门禁（RAG_EVAL_THRESHOLD 环境变量） ----------

def _patch_eval(pass_rate: float):
    """mock run_evaluation 返回指定通过率的报告，避免真连 Neo4j。

    scheduler.run_evaluation_once 内是函数级导入，需 patch 源模块名字：
      neo4j_client.Neo4jClient（可实例化） + evaluation_suite.run_evaluation（假报告）。
    """
    import unittest.mock as mock

    fake_report = {
        "total": 35,
        "passed": int(pass_rate * 35),
        "pass_rate": pass_rate,
        "details": [{"passed": True}] * 35,
    }
    patch_eval = mock.patch(
        "ecommerce_agent.knowledge_engine.evaluation_suite.run_evaluation",
        return_value=fake_report,
    )
    patch_client = mock.patch(
        "ecommerce_agent.knowledge_engine.neo4j_client.Neo4jClient",
        return_value=object(),
    )
    return patch_eval, patch_client


def test_eval_below_threshold_returns_gate_error(monkeypatch) -> None:
    """P2-2：通过率低于 RAG_EVAL_THRESHOLD → status=below_threshold + error。"""
    from ecommerce_agent.knowledge_engine import scheduler

    monkeypatch.setenv("RAG_EVAL_THRESHOLD", "0.9")
    patch_eval, patch_svc = _patch_eval(pass_rate=0.7)
    with patch_svc, patch_eval:
        report = scheduler.run_evaluation_once()
    assert report["status"] == "below_threshold"
    assert report["error"]
    assert "0.9" in report["error"]
    assert report["eval_threshold"] == 0.9


def test_eval_above_threshold_returns_ok(monkeypatch) -> None:
    """P2-2：通过率达到阈值 → status=ok，无 error。"""
    from ecommerce_agent.knowledge_engine import scheduler

    monkeypatch.setenv("RAG_EVAL_THRESHOLD", "0.9")
    patch_eval, patch_svc = _patch_eval(pass_rate=0.95)
    with patch_svc, patch_eval:
        report = scheduler.run_evaluation_once()
    assert report["status"] == "ok"
    assert "error" not in report
    assert report["eval_threshold"] == 0.9
