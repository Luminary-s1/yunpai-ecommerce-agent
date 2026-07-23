from __future__ import annotations

import pytest
from pydantic import ValidationError

from ecommerce_agent.decision import AgentDecision, extract_json_object


def test_action_decision_requires_a_tool() -> None:
    with pytest.raises(ValidationError, match="tool_name"):
        AgentDecision(mode="act", reason="execute requested")


def test_clarification_decision_requires_missing_fields() -> None:
    with pytest.raises(ValidationError, match="missing_fields"):
        AgentDecision(mode="clarify", reason="more input needed")


def test_null_empty_containers_from_a_model_are_normalized() -> None:
    decision = AgentDecision(
        mode="answer",
        reason="knowledge answer",
        arguments=None,
        missing_fields=None,
    )
    assert decision.arguments == {}
    assert decision.missing_fields == []


def test_non_container_arguments_remain_invalid() -> None:
    with pytest.raises(ValidationError, match="arguments"):
        AgentDecision(mode="answer", reason="knowledge answer", arguments="not-an-object")


def test_fenced_json_decision_is_parsed_without_free_text() -> None:
    assert extract_json_object('```json\n{"mode":"answer"}\n```') == {
        "mode": "answer"
    }
