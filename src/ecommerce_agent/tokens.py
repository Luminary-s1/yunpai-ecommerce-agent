"""Conservative token estimates: CJK characters count as 1, all others as ceil(len / 4)."""

from __future__ import annotations

import math
from typing import Any


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def count_tokens(text: str) -> int:
    cjk_count = sum(1 for character in text if _is_cjk(character))
    other_count = len(text) - cjk_count
    return cjk_count + math.ceil(other_count / 4)


def count_messages(messages: list[dict[str, Any]]) -> int:
    return sum(count_tokens(str(message.get("content", ""))) for message in messages)


def truncate_history(
    history: list[dict[str, Any]],
    *,
    budget_tokens: int,
) -> tuple[list[dict[str, Any]], dict[str, int | bool]]:
    budget = max(0, budget_tokens)
    if not history:
        return [], {
            "kept": 0,
            "dropped": 0,
            "tokens": 0,
            "budget": budget,
            "over_budget": False,
        }

    rounds: list[list[dict[str, Any]]] = []
    current_round: list[dict[str, Any]] = []
    for message in history:
        if message.get("role") == "user" and current_round:
            rounds.append(current_round)
            current_round = []
        current_round.append(message)
    if current_round:
        rounds.append(current_round)

    kept_rounds_reversed = [rounds[-1]]
    tokens = count_messages(rounds[-1])
    for round_messages in reversed(rounds[:-1]):
        round_tokens = count_messages(round_messages)
        if tokens + round_tokens > budget:
            break
        kept_rounds_reversed.append(round_messages)
        tokens += round_tokens

    kept = [
        message
        for round_messages in reversed(kept_rounds_reversed)
        for message in round_messages
    ]
    return kept, {
        "kept": len(kept),
        "dropped": len(history) - len(kept),
        "tokens": tokens,
        "budget": budget,
        "over_budget": tokens > budget,
    }
