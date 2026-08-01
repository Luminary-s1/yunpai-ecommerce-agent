from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ecommerce_agent.config import Settings
from ecommerce_agent.intent import classify
from ecommerce_agent.llm import ModelGateway
from ecommerce_agent.prompts import DECISION_SYSTEM_PROMPT

from conftest import make_settings


class ChitchatModel:
    def generate_json(self, _messages, **_kwargs):
        return {"intent": "chitchat", "confidence": 0.82}


class UnexpectedModel:
    def generate_json(self, _messages, **_kwargs):
        raise AssertionError("rule and invalid-input paths must not call the model")


class CapturingModel:
    def __init__(self, payload=None, *, timeout_seconds: float = 0.05):
        self.settings = SimpleNamespace(
            intent_classify_timeout_seconds=timeout_seconds
        )
        self.payload = payload or {"intent": "product_inquiry", "confidence": 0.84}
        self.calls = []

    def generate_json(self, messages, *, timeout_seconds):
        self.calls.append((messages, timeout_seconds))
        return self.payload


SAMPLES = [
    pytest.param("这款水杯多少钱", "product_inquiry", id="product-price"),
    pytest.param("请问有哪些规格", "product_inquiry", id="product-spec"),
    pytest.param("能介绍一下产品参数吗", "product_inquiry", id="product-parameters"),
    pytest.param("这个背包尺寸多大", "product_inquiry", id="product-size"),
    pytest.param("两款商品帮我对比推荐一下", "product_inquiry", id="product-compare"),
    pytest.param("这个订单怎么退货", "after_sales", id="after-sales-return"),
    pytest.param("我想申请退款", "after_sales", id="after-sales-refund"),
    pytest.param("收到后可以换货吗", "after_sales", id="after-sales-exchange"),
    pytest.param("产品保修多久", "after_sales", id="after-sales-warranty"),
    pytest.param("物流到哪里了", "after_sales", id="after-sales-logistics"),
    pytest.param("我要投诉客服", "complaint", id="complaint-direct"),
    pytest.param("准备给你们差评", "complaint", id="complaint-review"),
    pytest.param("我要举报这个商家", "complaint", id="complaint-report"),
    pytest.param("我会曝光这次服务", "complaint", id="complaint-expose"),
    pytest.param("投诉退款一直没人处理", "complaint", id="complaint-refund"),
    pytest.param("你好", "chitchat", id="chitchat-greeting"),
    pytest.param("今天天气不错", "chitchat", id="chitchat-weather"),
    pytest.param("谢谢你的帮助", "chitchat", id="chitchat-thanks"),
    pytest.param("再见", "chitchat", id="chitchat-goodbye"),
    pytest.param("你是谁", "chitchat", id="chitchat-identity"),
]


@pytest.mark.parametrize(("message", "expected"), SAMPLES)
def test_customer_intent_samples(message: str, expected: str) -> None:
    assert classify(message, model=ChitchatModel()).intent == expected


def test_rule_result_is_high_confidence_without_model_call() -> None:
    result = classify("请推荐一款保温杯", model=UnexpectedModel())

    assert result.intent == "product_inquiry"
    assert result.confidence == 0.95
    assert result.method == "rule"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("我要投诉你们的退款流程", "complaint"),
        ("举报这个商品参数造假", "complaint"),
        ("退款的商品多少钱", "after_sales"),
    ],
)
def test_rule_priority(message: str, expected: str) -> None:
    assert classify(message, model=UnexpectedModel()).intent == expected


@pytest.mark.parametrize("message", ["", "   ", "！？……---"])
def test_empty_or_symbol_only_message_uses_safe_default(message: str) -> None:
    result = classify(message, model=UnexpectedModel())

    assert result.intent == "chitchat"
    assert result.confidence == 0.0
    assert result.method == "default"


def test_very_long_message_is_classified_without_model_call() -> None:
    result = classify("投诉" + "服务体验很差" * 2000, model=UnexpectedModel())

    assert result.intent == "complaint"
    assert result.method == "rule"


def test_rule_miss_uses_bounded_short_few_shot_model_prompt() -> None:
    model = CapturingModel()

    result = classify("我想看看有哪些颜色", model=model)

    assert result.intent == "product_inquiry"
    assert result.confidence == 0.84
    assert result.method == "model"
    assert len(model.calls) == 1
    messages, timeout_seconds = model.calls[0]
    assert timeout_seconds == 0.05
    assert all(item["content"] != DECISION_SYSTEM_PROMPT for item in messages)
    serialized = json.dumps(messages, ensure_ascii=False)
    assert "intent_classification" in serialized
    assert all(
        intent in serialized
        for intent in ("product_inquiry", "after_sales", "complaint", "chitchat")
    )
    assert len(serialized) < 1200


def test_model_exception_uses_safe_default_without_raising() -> None:
    class FailingModel(CapturingModel):
        def generate_json(self, messages, *, timeout_seconds):
            self.calls.append((messages, timeout_seconds))
            raise RuntimeError("upstream failed")

    model = FailingModel()

    result = classify("能陪我聊聊吗", model=model)

    assert len(model.calls) == 1
    assert result.intent == "chitchat"
    assert result.confidence == 0.0
    assert result.method == "default"


def test_model_timeout_degrades_within_configured_latency() -> None:
    class TimingOutModel(CapturingModel):
        def generate_json(self, messages, *, timeout_seconds):
            self.calls.append((messages, timeout_seconds))
            time.sleep(timeout_seconds)
            raise TimeoutError("classification deadline exceeded")

    model = TimingOutModel(timeout_seconds=0.02)

    started = time.perf_counter()
    result = classify("随便聊点什么", model=model)
    elapsed = time.perf_counter() - started

    assert model.calls[0][1] == 0.02
    assert result.method == "default"
    assert elapsed < 0.5


def test_invalid_model_result_uses_safe_default() -> None:
    model = CapturingModel({"intent": "unknown", "confidence": 4})

    result = classify("介绍一下你们店", model=model)

    assert result.intent == "chitchat"
    assert result.confidence == 0.0
    assert result.method == "default"


def test_model_disabled_never_makes_an_external_request(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        model_enabled=False,
        model_mock_mode=False,
    )
    gateway = ModelGateway(settings)
    calls = 0

    def unexpected_post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("disabled model must not make an external request")

    gateway._client.post = unexpected_post  # type: ignore[method-assign]
    try:
        result = classify("能陪我聊聊吗", model=gateway)
    finally:
        gateway.close()

    assert result.method == "default"
    assert calls == 0


def test_mock_gateway_classifies_a_rule_miss_via_model(tmp_path) -> None:
    gateway = ModelGateway(make_settings(tmp_path))
    try:
        result = classify("我想看看有哪些颜色", model=gateway)
    finally:
        gateway.close()

    assert result.intent == "product_inquiry"
    assert result.confidence == 0.82
    assert result.method == "model"


def test_intent_classify_timeout_defaults_to_two_seconds(monkeypatch) -> None:
    monkeypatch.delenv("INTENT_CLASSIFY_TIMEOUT_SECONDS", raising=False)
    assert Settings.from_env().intent_classify_timeout_seconds == 2.0

    monkeypatch.setenv("INTENT_CLASSIFY_TIMEOUT_SECONDS", "0.25")
    assert Settings.from_env().intent_classify_timeout_seconds == 0.25
