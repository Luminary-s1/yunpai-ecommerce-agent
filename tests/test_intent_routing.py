from __future__ import annotations

import pytest

from ecommerce_agent.intent import classify


class ChitchatModel:
    def generate_json(self, _messages, **_kwargs):
        return {"intent": "chitchat", "confidence": 0.82}


class UnexpectedModel:
    def generate_json(self, _messages, **_kwargs):
        raise AssertionError("rule and invalid-input paths must not call the model")


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
