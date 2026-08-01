from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


CustomerIntent = Literal[
    "product_inquiry",
    "after_sales",
    "complaint",
    "chitchat",
]
IntentMethod = Literal["rule", "model", "default"]


class IntentResult(BaseModel):
    intent: CustomerIntent
    confidence: float = Field(ge=0.0, le=1.0)
    method: IntentMethod


_RULES: tuple[tuple[CustomerIntent, tuple[str, ...]], ...] = (
    ("complaint", ("投诉", "差评", "举报", "曝光")),
    ("after_sales", ("退货", "退款", "换货", "保修", "物流")),
    (
        "product_inquiry",
        ("多少钱", "规格", "参数", "尺寸", "材质", "对比", "推荐"),
    ),
)


def classify(message: str, *, model: object | None) -> IntentResult:
    normalized = message.strip()
    if not normalized or not any(character.isalnum() for character in normalized):
        return _default_result()
    for intent, keywords in _RULES:
        if any(keyword in normalized for keyword in keywords):
            return IntentResult(intent=intent, confidence=0.95, method="rule")
    return _default_result()


def _default_result() -> IntentResult:
    return IntentResult(intent="chitchat", confidence=0.0, method="default")
