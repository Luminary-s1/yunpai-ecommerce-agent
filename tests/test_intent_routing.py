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

CROSS_DOMAIN_HOLDOUT_CASES = [
    pytest.param("帮我推荐一家医院", id="recommend-hospital"),
    pytest.param("推荐点好玩的地方", id="recommend-attraction"),
    pytest.param("曝光度调高一点", id="exposure-level"),
    pytest.param("退款这词你懂吗", id="refund-meta"),
    pytest.param("这张照片曝光过度了", id="overexposed-photo"),
    pytest.param("我在物流行业干了十年", id="logistics-career"),
]

CROSS_CLAUSE_CONTAMINATION_CASES = [
    pytest.param(
        "这个商品没问题，帮我推荐一家医院", id="product-then-hospital"
    ),
    pytest.param(
        "我的订单已经到了，物流专业学什么", id="order-then-major"
    ),
    pytest.param(
        "客服已经回复我了，这张照片曝光怎么调", id="service-then-photo"
    ),
    pytest.param(
        "我申请了会员，退款这个词怎么读", id="application-then-refund-meta"
    ),
]

AMBIGUOUS_KEYWORD_BUSINESS_CASES = [
    pytest.param(
        "请推荐一款通勤背包", "product_inquiry", id="recommend-product"
    ),
    pytest.param(
        "两款商品帮我对比推荐一下",
        "product_inquiry",
        id="recommend-catalogue",
    ),
    pytest.param("我会曝光这次服务", "complaint", id="expose-service"),
    pytest.param("我要曝光这个卖假货的商家", "complaint", id="expose-merchant"),
    pytest.param("帮我查一下订单物流", "after_sales", id="track-order"),
    pytest.param("物流到哪里了", "after_sales", id="track-shipment"),
    pytest.param("我想申请退款", "after_sales", id="request-refund"),
    pytest.param("退款什么时候到账", "after_sales", id="refund-arrival"),
]

TERSE_BUSINESS_CASES = [
    pytest.param("我要退款", "after_sales", id="terse-request-refund"),
    pytest.param("退款", "after_sales", id="terse-refund"),
    pytest.param("物流呢", "after_sales", id="terse-logistics"),
    pytest.param("推荐一下", "product_inquiry", id="terse-recommend"),
    pytest.param("曝光你们", "complaint", id="terse-expose"),
]

SHORT_CROSS_DOMAIN_GUARDS = [
    pytest.param("相机曝光", id="short-camera-exposure"),
    pytest.param("推荐信", id="short-reference-letter"),
    pytest.param("物流公司", id="short-logistics-company"),
    pytest.param("退款一词", id="short-refund-meta"),
]

PROCESS_ACCOUNTABILITY_CASES = [
    pytest.param(
        "返修工单无故被关闭两回，谁能说明处理依据",
        id="closed-repair-ticket",
    ),
    pytest.param(
        "补送安排一再延期，为什么始终没有进展",
        id="repeated-reship-delay",
    ),
    pytest.param(
        "客服来回转接却无人跟进，给个明确说法",
        id="repeated-transfer",
    ),
    pytest.param(
        "第三回把型号寄错了，仓库核对环节到底谁负责",
        id="repeated-wrong-fulfilment",
    ),
    pytest.param(
        "箱子一拆商品就碎了，包装检查环节是怎么放行的",
        id="quality-process-accountability-regression",
    ),
    pytest.param(
        "鞋盒被压扁还沾水，这种包装也能出库吗",
        id="packaging-accountability-regression",
    ),
    pytest.param(
        "第二回把颜色发错，出库环节到底怎么核验",
        id="warehouse-process-regression",
    ),
    pytest.param(
        "送来的餐具缺了两件，装箱环节为何没有发现",
        id="packing-process-regression",
    ),
    pytest.param(
        "换新的机器开机又报错，之前处理根本没解决",
        id="unresolved-repair-regression",
    ),
    pytest.param(
        "配送员把包裹放错楼栋，反馈后仍无人联系",
        id="unresolved-delivery-regression",
    ),
]

PROCESS_ACCOUNTABILITY_NEGATIVES = [
    pytest.param("杯盖裂了想换新", "after_sales", id="defect-remedy"),
    pytest.param("退回的货多久能收到款", "after_sales", id="refund-status"),
    pytest.param("快递今天停在站点了", "after_sales", id="shipment-status"),
    pytest.param("这款鞋有几个型号", "product_inquiry", id="product-options"),
    pytest.param("客服几点下班", "chitchat", id="service-hours"),
    pytest.param("这种包装也能回收吗", "product_inquiry", id="packaging-policy"),
    pytest.param("收到商品后如何申请换货", "after_sales", id="received-remedy"),
]

TERSE_POLICY_QUESTIONS = [
    pytest.param("可以退款吗", id="can-refund"),
    pytest.param("请问退款吗", id="ask-refund"),
    pytest.param("退款可以吗", id="refund-allowed"),
]


@pytest.mark.parametrize(("message", "expected"), SAMPLES)
def test_customer_intent_samples(message: str, expected: str) -> None:
    model = CapturingModel({"intent": expected, "confidence": 0.84})

    result = classify(message, model=model)

    assert result.intent == expected
    assert result.method == "model"
    assert len(model.calls) == 1


def test_rule_result_is_high_confidence_when_model_is_not_configured() -> None:
    result = classify("请推荐一款保温杯", model=None)

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
    assert classify(message, model=None).intent == expected


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

    result = classify("我要投诉退款商品多少钱", model=None)

    assert result.intent == "complaint"


@pytest.mark.parametrize("message", CROSS_DOMAIN_HOLDOUT_CASES)
def test_cross_domain_holdout_is_deferred_to_model(message: str) -> None:
    model = CapturingModel({"intent": "chitchat", "confidence": 0.73})

    result = classify(message, model=model)

    assert len(model.calls) == 1
    assert result.method == "model"
    assert result.error is None


@pytest.mark.parametrize("message", CROSS_CLAUSE_CONTAMINATION_CASES)
def test_business_evidence_from_another_clause_does_not_short_circuit(
    message: str,
) -> None:
    model = CapturingModel({"intent": "chitchat", "confidence": 0.73})

    result = classify(message, model=model)

    assert len(model.calls) == 1
    assert result.method == "model"
    assert result.error is None


@pytest.mark.parametrize(
    ("message", "expected"), AMBIGUOUS_KEYWORD_BUSINESS_CASES
)
def test_business_evidence_keeps_ambiguous_keyword_on_rule_fast_path(
    message: str, expected: str
) -> None:
    result = classify(message, model=None)

    assert result.intent == expected
    assert result.confidence == intent_module._RULE_CONFIDENCE
    assert result.method == "rule"
    assert result.error is None


@pytest.mark.parametrize(("message", "expected"), TERSE_BUSINESS_CASES)
def test_terse_business_message_stays_on_rule_fast_path(
    message: str, expected: str
) -> None:
    result = classify(message, model=None)

    assert result.intent == expected
    assert result.confidence == intent_module._RULE_CONFIDENCE
    assert result.method == "rule"
    assert result.error is None


@pytest.mark.parametrize("message", SHORT_CROSS_DOMAIN_GUARDS)
def test_short_cross_domain_context_is_still_deferred(message: str) -> None:
    model = CapturingModel({"intent": "chitchat", "confidence": 0.73})

    result = classify(message, model=model)

    assert len(model.calls) == 1
    assert result.method == "model"
    assert result.error is None


@pytest.mark.parametrize("message", PROCESS_ACCOUNTABILITY_CASES)
def test_process_accountability_structure_requires_model_confirmation(
    message: str,
) -> None:
    model = CapturingModel({"intent": "complaint", "confidence": 0.87})

    result = classify(message, model=model)

    assert intent_module._matches_process_accountability(message)
    assert len(model.calls) == 1
    assert result.intent == "complaint"
    assert result.method == "model"


@pytest.mark.parametrize(
    ("message", "expected"), PROCESS_ACCOUNTABILITY_NEGATIVES
)
def test_process_accountability_structure_does_not_absorb_normal_business(
    message: str,
    expected: str,
) -> None:
    model = CapturingModel({"intent": expected, "confidence": 0.86})

    result = classify(message, model=model)

    assert not intent_module._matches_process_accountability(message)
    assert result.intent == expected


def test_product_care_question_is_not_a_process_accountability_complaint() -> None:
    result = classify("可拆卸椅套沾了污渍该怎么处理", model=None)

    assert result.intent != "complaint"


@pytest.mark.parametrize(
    "message",
    (
        "售后审核为什么还没有通过",
        "为什么我的订单还没有发货提醒",
    ),
)
def test_neutral_progress_question_requires_model_arbitration(message: str) -> None:
    model = CapturingModel({"intent": "after_sales", "confidence": 0.88})

    result = classify(message, model=model)

    assert len(model.calls) == 1
    assert result.intent == "after_sales"
    assert result.method == "model"


@pytest.mark.parametrize(
    "message",
    (
        "售后审核为什么还没有通过",
        "为什么我的订单还没有发货提醒",
    ),
)
def test_neutral_progress_question_without_model_abstains(message: str) -> None:
    result = classify(message, model=None)

    assert result.intent == "chitchat"
    assert result.method == "default"
    assert result.error == "model_not_configured"


@pytest.mark.parametrize("message", TERSE_POLICY_QUESTIONS)
def test_terse_policy_question_is_deferred_to_model(message: str) -> None:
    model = CapturingModel({"intent": "product_inquiry", "confidence": 0.73})

    result = classify(message, model=model)

    assert len(model.calls) == 1
    assert result.method == "model"
    assert result.error is None


def test_cross_domain_gate_declares_positive_business_evidence() -> None:
    assert not hasattr(intent_module, "_RULE_REVIEW_CONTEXTS")
    assert set(intent_module._RULE_BUSINESS_EVIDENCE) == {
        "曝光",
        "推荐",
        "物流",
        "退款",
    }
    assert all(intent_module._RULE_BUSINESS_EVIDENCE.values())

    serialized = json.dumps(
        intent_module._RULE_BUSINESS_EVIDENCE, ensure_ascii=False
    )
    assert all(
        domain_term not in serialized
        for domain_term in ("医院", "景点", "照片", "摄影", "行业", "公司")
    )


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


def test_very_long_message_is_classified_without_model_when_disabled() -> None:
    result = classify("投诉" + "服务体验很差" * 2000, model=None)

    assert result.intent == "complaint"
    assert result.method == "rule"


@pytest.mark.parametrize(
    ("message", "model_intent"),
    (
        ("还没下单，想先了解保修条件", "product_inquiry"),
        ("尚未购买，想先看看退货条件", "product_inquiry"),
        ("不用换货了，只想确认维修进度", "after_sales"),
        ("东西有划痕，但我现在只是咨询清洁方法", "product_inquiry"),
    ),
)
def test_rule_signal_never_overrides_model_semantics(
    message: str,
    model_intent: str,
) -> None:
    model = CapturingModel({"intent": model_intent, "confidence": 0.86})

    result = classify(message, model=model)

    assert result.intent == model_intent
    assert result.method == "model"
    assert len(model.calls) == 1


def test_rule_and_process_matches_are_advisory_signals_in_model_request() -> None:
    model = CapturingModel({"intent": "after_sales", "confidence": 0.88})

    classify("返修进度一直没更新，但先别投诉", model=model)

    task = json.loads(model.calls[0][0][1]["content"])
    assert task["advisory_signals"]["rule_candidate"] == "complaint"
    assert task["advisory_signals"]["matched_keywords"] == ["投诉"]
    assert task["advisory_signals"]["semantic_authority"] is False


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


def test_model_prompt_explicitly_requests_json() -> None:
    model = CapturingModel()

    classify("我想看看有哪些颜色", model=model)

    system_prompt = model.calls[0][0][0]["content"]
    assert "json" in system_prompt.casefold()


def test_adjudicated_labelling_policy_reaches_the_model() -> None:
    """守的是产品决策，不是实现细节。

    「诉求优先于语气」「售前咨询归商品咨询」两条口径由人裁定并写进
    evals/intent/README.md。它们若只留在文档和语料里，模型无从知晓，基准会持续
    在这两处扣分而看不出原因。
    """
    model = CapturingModel()

    classify("我想看看有哪些颜色", model=model)

    system_prompt = model.calls[0][0][0]["content"]
    assert intent_module._LABELLING_POLICY in system_prompt
    assert "具体商品或履约问题" in system_prompt
    assert "不要求用户明确说出退款或换货" in system_prompt
    assert "才归 complaint" in system_prompt
    # 口径必须以判据形式传达；写成具体样例即是对基准过拟合
    assert all(
        sample not in system_prompt
        for sample in ("我这东西坏了", "支持七天无理由吗")
    )


def test_labelling_policy_distinguishes_remedy_from_process_accountability() -> None:
    model = CapturingModel()

    classify("我想看看有哪些颜色", model=model)

    system_prompt = model.calls[0][0][0]["content"]
    assert "主要诉求" in system_prompt
    assert "处理流程本身" in system_prompt
    assert "追责" in system_prompt
    assert "办理退换修或查询进度" in system_prompt


def test_labelling_policy_keeps_order_invoice_service_in_after_sales() -> None:
    model = CapturingModel()

    classify("我想看看有哪些颜色", model=model)

    system_prompt = model.calls[0][0][0]["content"]
    assert "发票开具" in system_prompt
    assert "抬头变更" in system_prompt
    assert "订单服务" in system_prompt


def test_mixed_after_sales_few_shot_is_paraphrased_in_model_request() -> None:
    model = CapturingModel()

    classify("我这东西坏了，质量也太差了吧", model=model)

    assert len(model.calls) == 1
    task = json.loads(model.calls[0][0][1]["content"])
    expected_example = {
        "message": "刚收货的耳机就没声音，做工真让人失望",
        "intent": "after_sales",
    }
    assert expected_example in task["examples"]
    examples = json.dumps(task["examples"], ensure_ascii=False)
    assert "我这东西坏了" not in examples
    assert "质量也太差了吧" not in examples


def test_process_accountability_complaint_few_shot_uses_an_unseen_scenario() -> None:
    model = CapturingModel()

    classify("我想看看有哪些颜色", model=model)

    task = json.loads(model.calls[0][0][1]["content"])
    assert {
        "message": "安装预约改了三次仍没人上门，之前的处理为什么一直无效",
        "intent": "complaint",
    } in task["examples"]


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
    assert classify("我要投诉", model=None).error is None
    assert classify("我想看看有哪些颜色", model=CapturingModel()).error is None


def test_intent_classify_timeout_defaults_to_two_seconds(monkeypatch) -> None:
    monkeypatch.delenv("INTENT_CLASSIFY_TIMEOUT_SECONDS", raising=False)
    assert Settings.from_env().intent_classify_timeout_seconds == 2.0

    monkeypatch.setenv("INTENT_CLASSIFY_TIMEOUT_SECONDS", "0.25")
    assert Settings.from_env().intent_classify_timeout_seconds == 0.25
