from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


DecisionMode = Literal[
    "answer",
    "clarify",
    "observe",
    "act",
    "handoff",
    "refuse",
    "finish",
]


class AgentDecision(BaseModel):
    """A model-selected semantic step. It never carries execution authority."""

    intent: str = Field(default="general", min_length=1, max_length=64)
    mode: DecisionMode
    tool_name: str | None = Field(default=None, min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list, max_length=16)
    expected_outcome: str | None = Field(default=None, max_length=300)
    response: str | None = Field(default=None, max_length=1200)
    reason: str = Field(min_length=1, max_length=300)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("arguments", mode="before")
    @classmethod
    def normalize_null_arguments(cls, value: Any) -> Any:
        # Some OpenAI-compatible models serialize an empty object as null.
        return {} if value is None else value

    @field_validator("missing_fields", mode="before")
    @classmethod
    def normalize_null_missing_fields(cls, value: Any) -> Any:
        return [] if value is None else value

    @model_validator(mode="after")
    def validate_step_shape(self) -> "AgentDecision":
        if self.mode in {"observe", "act"} and not self.tool_name:
            raise ValueError(f"{self.mode} decisions require tool_name")
        if self.mode not in {"observe", "act"} and self.tool_name:
            raise ValueError(f"{self.mode} decisions cannot select a tool")
        if self.mode == "clarify" and not self.missing_fields:
            raise ValueError("clarify decisions require missing_fields")
        return self


def extract_json_object(content: str) -> dict[str, Any]:
    """Parse a single JSON object, tolerating a surrounding Markdown fence."""

    value = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced.group(1).strip()
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("model decision is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("model decision must be a JSON object")
    return data
