from __future__ import annotations

import json
from typing import Any, Literal, Protocol

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


class IntentModel(Protocol):
    settings: object

    def generate_json(
        self,
        messages: list[dict[str, str]],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


_RULES: tuple[tuple[CustomerIntent, tuple[str, ...]], ...] = (
    ("complaint", ("投诉", "差评", "举报", "曝光")),
    ("after_sales", ("退货", "退款", "换货", "保修", "物流")),
    (
        "product_inquiry",
        ("多少钱", "规格", "参数", "尺寸", "材质", "对比", "推荐"),
    ),
)

_MODEL_SYSTEM_PROMPT = (
    "你是客服消息意图分类器。只能选择 product_inquiry、after_sales、complaint、"
    "chitchat，并只返回包含 intent 与 0 到 1 confidence 的 JSON 对象。"
)
_FEW_SHOT_EXAMPLES = (
    {"message": "这款还有哪些颜色", "intent": "product_inquiry"},
    {"message": "收到后怎么换货", "intent": "after_sales"},
    {"message": "客服态度太差了", "intent": "complaint"},
    {"message": "你好呀", "intent": "chitchat"},
)


def classify(message: str, *, model: IntentModel | None) -> IntentResult:
    normalized = message.strip()
    if not normalized or not any(character.isalnum() for character in normalized):
        return _default_result()
    for intent, keywords in _RULES:
        if any(keyword in normalized for keyword in keywords):
            return IntentResult(intent=intent, confidence=0.95, method="rule")
    if model is None:
        return _default_result()
    timeout_seconds = _model_timeout(model)
    messages = [
        {"role": "system", "content": _MODEL_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task_type": "intent_classification",
                    "examples": _FEW_SHOT_EXAMPLES,
                    "message": normalized[:4000],
                },
                ensure_ascii=False,
            ),
        },
    ]
    try:
        payload = model.generate_json(messages, timeout_seconds=timeout_seconds)
        return IntentResult(
            intent=payload["intent"],
            confidence=payload["confidence"],
            method="model",
        )
    except Exception:
        return _default_result()


def _model_timeout(model: IntentModel) -> float:
    settings = getattr(model, "settings", None)
    value = getattr(settings, "intent_classify_timeout_seconds", 2.0)
    try:
        return max(0.001, float(value))
    except (TypeError, ValueError):
        return 2.0


def _default_result() -> IntentResult:
    return IntentResult(intent="chitchat", confidence=0.0, method="default")
