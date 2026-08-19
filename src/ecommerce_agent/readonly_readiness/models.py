from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


READONLY_DEMO_FIXTURE_ID = "m7r-readonly-demo-v1"


class ReadonlyDemoLoadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: Literal["m7r-readonly-demo-v1"]
    store_id: str = Field(min_length=1, max_length=128)
    confirm_demo: Literal[True]

    @field_validator("store_id")
    @classmethod
    def validate_store_id(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("invalid_readonly_demo_store")
        return value


__all__ = ["READONLY_DEMO_FIXTURE_ID", "ReadonlyDemoLoadRequest"]
