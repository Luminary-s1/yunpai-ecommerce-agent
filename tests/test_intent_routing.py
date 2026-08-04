from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from ecommerce_agent import intent as intent_module
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


_UNSET = object()


class CapturingModel:
    def __init__(self, payload=_UNSET, *, timeout_seconds: float = 0.05):
        self.settings = SimpleNamespace(
            intent_classify_timeout_seconds=timeout_seconds
        )
        # 用哨兵而非 `payload or ...`：`None` 和 `[]` 是要测的真实返回值，
        # 不能被默认值悄悄顶掉。
        self.payload = (
            {"intent": "product_inquiry", "confidence": 0.84}
            if payload is _UNSET
            else payload
        )
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

AMBIGUOUS_RULE_CASES = [
    pytest.param("你这个推荐算法真烂", "complaint", id="recommendation-algorithm"),
    pytest.param("这个相机曝光怎么调", "product_inquiry", id="camera-exposure"),
    pytest.param("给我推荐个电影", "chitchat", id="movie-recommendation"),
    pytest.param("我朋友在物流公司上班", "chitchat", id="logistics-employment"),
    pytest.param("不需要退款了，谢谢", "chitchat", id="negated-refund"),
]


@pytest.mark.parametrize(("message", "expected"), SAMPLES)
def test_customer_intent_samples(message: str, expected: str) -> None:
    assert classify(message, model=ChitchatModel()).intent == expected


def test_rule_result_is_high_confidence_without_model_call() -> None:
    result = classify("请推荐一款保温杯", model=UnexpectedModel())

    assert result.intent == "product_inquiry"
    assert result.confidence == intent_module._RULE_CONFIDENCE == 0.95
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


def test_rule_priority_is_explicit_and_mapping_order_independent(monkeypatch) -> None:
    assert intent_module._RULE_PRIORITY == (
        "complaint",
        "after_sales",
        "product_inquiry",
    )
    assert set(intent_module._RULE_PRIORITY) == set(intent_module._RULE_KEYWORDS)
    reordered = {
        intent: intent_module._RULE_KEYWORDS[intent]
        for intent in reversed(intent_module._RULE_PRIORITY)
    }
    monkeypatch.setattr(intent_module, "_RULE_KEYWORDS", reordered)

    result = classify("我要投诉退款商品多少钱", model=UnexpectedModel())

    assert result.intent == "complaint"


@pytest.mark.parametrize(("message", "expected"), AMBIGUOUS_RULE_CASES)
def test_ambiguous_rule_hit_is_deferred_to_model(
    message: str, expected: str
) -> None:
    model = CapturingModel({"intent": expected, "confidence": 0.88})

    result = classify(message, model=model)

    assert len(model.calls) == 1
    assert "不用办理退货了" in json.dumps(model.calls[0][0], ensure_ascii=False)
    assert result.intent == expected
    assert result.confidence == 0.88
    assert result.method == "model"
    assert result.error is None


@pytest.mark.parametrize(("message", "_expected"), AMBIGUOUS_RULE_CASES)
def test_ambiguous_rule_hit_without_model_is_observable_default(
    message: str, _expected: str
) -> None:
    result = classify(message, model=None)

    assert result.intent == "chitchat"
    assert result.confidence == 0.0
    assert result.method == "default"
    assert result.error == "model_not_configured"


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
    assert "不用办理退货了" not in serialized
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
    configured_seconds = 0.02
    default_seconds = 2.0

    class TimingOutModel(CapturingModel):
        def generate_json(self, messages, *, timeout_seconds):
            self.calls.append((messages, timeout_seconds))
            time.sleep(timeout_seconds)
            raise TimeoutError("classification deadline exceeded")

    model = TimingOutModel(timeout_seconds=configured_seconds)

    started = time.perf_counter()
    result = classify("随便聊点什么", model=model)
    elapsed = time.perf_counter() - started

    assert len(model.calls) == 1
    assert model.calls[0][1] == configured_seconds
    assert result.method == "default"
    # Degrading must honour the configured budget rather than falling back to the default
    # one. A quarter of the default stays well clear of it and of scheduling noise.
    assert elapsed < default_seconds / 4


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


@pytest.mark.parametrize(
    ("payload", "expected_intent", "expected_confidence"),
    [
        # 2026-08-04 从 glm-4.7-flash 实测抓到的真实形状。
        ({"answer": {"intent": "after_sales", "confidence": 0.95}}, "after_sales", 0.95),
        ({"result": {"intent": "complaint", "confidence": 0.9}}, "complaint", 0.9),
        # 双层信封
        (
            {"data": {"answer": {"intent": "chitchat", "confidence": 0.7}}},
            "chitchat",
            0.7,
        ),
        # 目标形状本身必须原样通过
        ({"intent": "product_inquiry", "confidence": 0.6}, "product_inquiry", 0.6),
        # 单键但值不是 dict，不能被误当成信封拆掉
        ({"intent": "complaint"}, "complaint", 0.5),
        # 大小写与空白
        ({"intent": " After_Sales ", "confidence": 0.8}, "after_sales", 0.8),
        # confidence 越界只截断，不因此丢掉正确的 intent
        ({"intent": "complaint", "confidence": 4}, "complaint", 1.0),
        ({"intent": "complaint", "confidence": -1}, "complaint", 0.0),
        # confidence 缺失或不可解析时取中性值
        ({"intent": "chitchat"}, "chitchat", 0.5),
        ({"intent": "chitchat", "confidence": "高"}, "chitchat", 0.5),
    ],
)
def test_model_payload_shapes_are_normalized(
    payload: dict, expected_intent: str, expected_confidence: float
) -> None:
    result = classify("我想看看有哪些颜色", model=CapturingModel(payload))

    assert result.method == "model"
    assert result.intent == expected_intent
    assert result.confidence == pytest.approx(expected_confidence)
    assert result.error is None


@pytest.mark.parametrize(
    "payload",
    [
        {"intent": "unknown", "confidence": 0.9},
        {"answer": {"intent": "unknown"}},
        {"answer": {}},
        {"foo": "bar"},
        [],
        "chitchat",
        None,
    ],
)
def test_unusable_model_payload_falls_back_with_a_reason(payload) -> None:
    result = classify("我想看看有哪些颜色", model=CapturingModel(payload))

    assert result.method == "default"
    assert result.intent == "chitchat"
    assert result.confidence == 0.0
    assert result.error is not None
    assert result.error.startswith("model_payload_rejected:")


def test_real_gateway_unwraps_the_observed_glm_envelope(tmp_path) -> None:
    """端到端复现 2026-08-04 的线上形状：模型答对了，旧代码把答案丢了。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"answer": {"intent": "after_sales", '
                            '"confidence": 0.95}}'
                        }
                    }
                ]
            },
        )

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_streaming=False,
        model_api_key="test-model-key",
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        result = classify("包裹三天没动静了", model=gateway)
    finally:
        gateway.close()

    assert result.intent == "after_sales"
    assert result.confidence == 0.95
    assert result.method == "model"


@pytest.mark.parametrize(
    ("message", "model", "expected_error"),
    [
        ("", UnexpectedModel(), "unclassifiable_input"),
        ("😀😀", UnexpectedModel(), "unclassifiable_input"),
        ("能陪我聊聊吗", None, "model_not_configured"),
    ],
)
def test_degradation_reasons_are_distinguishable(message, model, expected_error) -> None:
    assert classify(message, model=model).error == expected_error


def test_model_call_failure_records_the_exception_type() -> None:
    class FailingModel(CapturingModel):
        def generate_json(self, messages, *, timeout_seconds):
            raise TimeoutError("classification deadline exceeded")

    result = classify("能陪我聊聊吗", model=FailingModel())

    # 超时与「返回值解析不了」必须留下不同的痕迹，否则线上只能看到一堆 chitchat。
    assert result.error == "model_call_failed:TimeoutError"


def test_rule_and_model_hits_carry_no_error() -> None:
    assert classify("我要投诉", model=UnexpectedModel()).error is None
    assert classify("我想看看有哪些颜色", model=CapturingModel()).error is None


def test_intent_classify_timeout_defaults_to_two_seconds(monkeypatch) -> None:
    monkeypatch.delenv("INTENT_CLASSIFY_TIMEOUT_SECONDS", raising=False)
    assert Settings.from_env().intent_classify_timeout_seconds == 2.0

    monkeypatch.setenv("INTENT_CLASSIFY_TIMEOUT_SECONDS", "0.25")
    assert Settings.from_env().intent_classify_timeout_seconds == 0.25
