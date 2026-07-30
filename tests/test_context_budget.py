from __future__ import annotations

import json

import pytest

from ecommerce_agent.config import Settings
from ecommerce_agent.prompts import build_decision_messages, build_messages
from ecommerce_agent.service import AgentService
from ecommerce_agent.tokens import count_messages, count_tokens, truncate_history

from conftest import make_settings, principal_for


def test_count_tokens_uses_deterministic_estimate() -> None:
    assert count_tokens("") == 0
    assert count_tokens("中文abcde") == 4
    assert count_messages([{"content": "售后"}, {"content": "hello"}]) == 4


def test_history_budget_truncates_200_messages_and_keeps_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONTEXT_BUDGET_RATIO", raising=False)
    settings = Settings.from_env()
    history = [
        {"role": "user", "content": f"{index:04d}" + "客" * 96}
        for index in range(200)
    ]

    kept, meta = truncate_history(
        history,
        budget_tokens=int(1000 * settings.context_budget_ratio),
    )

    assert meta == {
        "kept": 7,
        "dropped": 193,
        "tokens": 679,
        "budget": 700,
        "over_budget": False,
    }
    assert count_messages(kept) <= meta["budget"]
    assert kept[-1] is history[-1]


def test_context_budget_settings_default_and_clamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_CONTEXT_LIMIT_TOKENS", "64000")
    monkeypatch.setenv("CONTEXT_BUDGET_RATIO", "0.99")
    high = Settings.from_env()
    assert high.model_context_limit_tokens == 64000
    assert high.context_budget_ratio == 0.9

    monkeypatch.setenv("CONTEXT_BUDGET_RATIO", "0.01")
    assert Settings.from_env().context_budget_ratio == 0.1


def test_prompts_keep_upstream_history_and_at_least_highest_score_document() -> None:
    documents = [
        {
            "id": "low",
            "category": "faq",
            "intent": "general",
            "question": "low question",
            "answer": "low answer",
            "source": "test",
            "version": 1,
            "score": 0.1,
            "layer": "global",
            "store_id": None,
            "sku_id": None,
        },
        {
            "id": "high",
            "category": "faq",
            "intent": "general",
            "question": "high question",
            "answer": "high answer",
            "source": "test",
            "version": 1,
            "score": 0.9,
            "layer": "global",
            "store_id": None,
            "sku_id": None,
        },
    ]
    history = [
        {"role": "user", "content": f"history-{index}"}
        for index in range(8)
    ]

    generation = build_messages(
        question="question",
        documents=documents,
        context={},
        history=history,
        knowledge_budget_tokens=1,
    )
    assert "[high]" in generation[1]["content"]
    assert "[low]" not in generation[1]["content"]
    assert "history-0" in generation[1]["content"]

    decision = build_decision_messages(
        question="question",
        documents=documents,
        context={},
        history=history,
        tool_catalog=[],
        observation=None,
        step_count=0,
        max_steps=4,
        knowledge_budget_tokens=1,
    )
    payload = json.loads(decision[1]["content"])
    assert [item["id"] for item in payload["knowledge_evidence"]] == ["high"]
    assert payload["recent_history"] == history


def test_chat_snapshot_records_history_window_evidence(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        response = service.chat(
            principal_for(service),
            "budget-evidence",
            "尺码怎么选",
        )
        with service.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT bundle_json, evidence_json
                FROM context_snapshots
                WHERE tenant_id=? AND trace_id=?
                """,
                ("tenant-test", response.trace_id),
            ).fetchall()

        assert rows
        for row in rows:
            bundle = json.loads(row["bundle_json"])
            evidence = json.loads(row["evidence_json"])
            assert bundle["recent_history_meta"]["budget"] > 0
            assert any(item["type"] == "history_window" for item in evidence)
    finally:
        service.close()
