from __future__ import annotations

from ecommerce_agent.tokens import count_messages, truncate_history


def test_truncate_history_keeps_complete_latest_round() -> None:
    history = [
        {"role": role, "content": "客" * 100}
        for _ in range(200)
        for role in ("user", "assistant")
    ]

    kept, meta = truncate_history(history, budget_tokens=1000)

    assert meta == {
        "kept": 10,
        "dropped": 390,
        "tokens": 1000,
        "budget": 1000,
        "over_budget": False,
    }
    assert count_messages(kept) <= 1000
    assert kept[-2:] == history[-2:]
    assert kept[-2] is history[-2]
    assert kept[-1] is history[-1]


def test_truncate_history_marks_unavoidable_latest_round_over_budget() -> None:
    history = [
        {"role": "user", "content": "最近问题"},
        {"role": "assistant", "content": "最近回答"},
    ]

    kept, meta = truncate_history(history, budget_tokens=1)

    assert kept == history
    assert meta["kept"] == 2
    assert meta["over_budget"] is True
