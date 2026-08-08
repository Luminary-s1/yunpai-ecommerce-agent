from __future__ import annotations

import json
import queue
import re
import threading
from pathlib import Path
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
    # 降级原因。method="default" 时必然非空，让「模型判定为闲聊」与「模型链路挂了」
    # 在数据上可区分——否则两者的返回值完全一样，线上无从发现后者。
    error: str | None = None


class IntentModel(Protocol):
    settings: object

    def generate_json(
        self,
        messages: list[dict[str, str]],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


_RULE_PRIORITY: tuple[CustomerIntent, ...] = (
    "complaint",
    "after_sales",
    "product_inquiry",
)
_RULE_KEYWORDS: dict[CustomerIntent, tuple[str, ...]] = {
    "complaint": ("投诉", "差评", "举报", "曝光"),
    "after_sales": ("退货", "退款", "换货", "保修", "物流"),
    "product_inquiry": ("多少钱", "规格", "参数", "尺寸", "材质", "对比", "推荐"),
}
_RULE_CONFIDENCE = 0.95
_NEGATION_PREFIX_MARKERS = ("不", "别", "无需", "无须", "取消", "停止")
_NEGATION_SUFFIX_MARKERS = ("不用", "不要", "取消", "算了", "停止", "作罢")
_NEGATION_WINDOW = 6
_RULE_CLAUSE_BOUNDARY = re.compile(r"[,，。.!！？?；;\n]")
_PROCESS_CONTEXT_PATTERN = re.compile(
    r"(客服|售后|商家|店家|平台|工单|处理|答复|回复|承诺|安排|补送|补寄|"
    r"发货|寄送|配送|仓库|核对|检查|检验|品控|质控|预约|跟进|转接|"
    r"服务|包装|收到|收货|货品|赠品|出库|装箱|复核|核验|履约|返修|人工)"
)
_PROCESS_FAILURE_PATTERN = re.compile(
    r"(无故|无人|没人|未能|没有|没能|还没|没发|不给|关闭|拒绝|坏|破|"
    r"寄错|发错|漏发|少发|延期|延误|拖延|拖了|中断|失效|无效|"
    r"不处理|不回复|没进展|没解决)"
)
_PROCESS_ACCOUNTABILITY_PATTERN = re.compile(
    r"(谁.{0,6}(负责|说明|解释)|(?:为什么|为何|凭什么).{0,12}|"
    r"给.{0,6}(说法|解释)|(?:怎么|如何).{0,8}(负责|解释|核对|检查|处理)|"
    r"(?:怎么|如何).{0,8}的|到底.{0,6}(谁|怎么|为何|为什么|负责|说明|解释)|"
    r"也敢|(?:申请|准备|要|会).{0,8}(反映|申诉|追究)|"
    r"(?:结果|却|反而).{0,12}|这种.{0,12}也(?:能|敢).{0,6}[吗？?])"
)
_PROCESS_RECURRENCE_PATTERN = re.compile(
    r"(一再|反复|多次|屡次|来回|再次|又|仍|始终|至今|"
    r"第[二三四五六七八九十\d]+(?:次|回)|"
    r"[两三四五六七八九十\d]+(?:次|回))"
)
_PROCESS_INCIDENT_PATTERN = re.compile(
    r"(已经|曾经|之前|收到|收货|到货|签收|送来|寄来|发来|开箱|拆|被|"
    r"之后|以后|过去)"
)
_TERSE_RULE_PREFIXES = (
    "麻烦",
    "帮我",
    "给我",
    "替我",
    "我要",
    "我想",
    "想要",
    "请",
)
_TERSE_RULE_SUFFIXES = (
    "怎么办",
    "怎么弄",
    "一下",
    "你们",
    "呢",
    "吧",
    "啊",
    "呀",
    "嘛",
    "了",
    "呗",
    "下",
    "你",
)
_RULE_BUSINESS_EVIDENCE: dict[str, tuple[tuple[str, ...], ...]] = {
    "曝光": (
        ("我要", "我会", "准备", "否则", "再不", "就去", "投诉", "举报", "维权"),
        (
            "客服",
            "商家",
            "卖家",
            "店铺",
            "平台",
            "订单",
            "服务",
            "售后",
            "假货",
        ),
    ),
    "推荐": (
        ("这款", "哪款", "一款", "两款", "型号", "预算", "价位", "选购"),
    ),
    "物流": (
        (
            "订单",
            "包裹",
            "快递",
            "发货",
            "收货",
            "签收",
            "单号",
            "查询",
            "查一下",
            "跟踪",
            "到哪",
            "进度",
        ),
    ),
    "退款": (
        (
            "订单",
            "商品",
            "产品",
            "下单",
            "购买",
            "买了",
            "收货",
            "收到",
            "签收",
            "申请",
            "办理",
            "到账",
            "售后",
            "进度",
            "处理",
        ),
    ),
    "退货": (
        (
            "订单",
            "商品",
            "产品",
            "下单",
            "购买",
            "买了",
            "收货",
            "收到",
            "签收",
            "申请",
            "办理",
            "售后",
            "进度",
            "处理",
        ),
    ),
    "保修": (
        (
            "订单",
            "商品",
            "产品",
            "下单",
            "购买",
            "买了",
            "收货",
            "收到",
            "签收",
            "申请",
            "办理",
            "售后",
            "进度",
            "处理",
        ),
    ),
}

_INTENTS: frozenset[str] = frozenset(
    ("product_inquiry", "after_sales", "complaint", "chitchat")
)

_ROUTING_FIELDS = frozenset({"knowledge_intent", "prompt_variant", "sop_intent"})
_ROUTING_CONFIG_PATH = Path(__file__).with_name("intent_routing.json")


def load_intent_routing(path: str | Path = _ROUTING_CONFIG_PATH) -> dict[str, dict[str, str]]:
    """Load and validate the controlled-intent routing contract."""
    with Path(path).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or set(payload) != set(_INTENTS):
        raise ValueError("intent routing must declare exactly the controlled intents")
    routing: dict[str, dict[str, str]] = {}
    for intent in _INTENTS:
        entry = payload[intent]
        if not isinstance(entry, dict) or set(entry) != _ROUTING_FIELDS:
            raise ValueError(f"invalid routing entry for {intent}")
        if any(not isinstance(value, str) or not value.strip() for value in entry.values()):
            raise ValueError(f"routing values for {intent} must be non-empty strings")
        routing[intent] = {key: entry[key].strip() for key in _ROUTING_FIELDS}
    return routing


INTENT_ROUTING = load_intent_routing()


def routing_for_intent(intent: str) -> dict[str, str]:
    """Return a copy so callers cannot mutate the process-wide routing contract."""
    return dict(INTENT_ROUTING.get(intent, INTENT_ROUTING["chitchat"]))

# 已裁定的标注口径，与 evals/intent/README.md「标注口径」一节保持一致。
# 传达的是判据本身而非具体样例——把基准里的争议样例写成 few-shot 就成了对基准
# 过拟合，那样分数会涨而能力不会。
_LABELLING_POLICY = (
    "分类时先判断主要诉求，再判断是否存在已经发生、需要处理的具体商品或履约问题。"
    "商品故障、破损、缺件等消息，如果主要诉求是办理退换修或查询进度，归 after_sales，"
    "即使消息同时抱怨质量或表达失望；这类故障陈述在客服语境中本身就表示待处理问题，"
    "不要求用户明确说出退款或换货。"
    "当主要诉求是要求商家对处理流程本身追责，例如反复无进展、承诺未兑现、"
    "服务处理失当或要求解释责任时，才归 complaint；具体商品或履约事件只作为投诉背景，"
    "不把这种流程追责降成 after_sales。"
    "发票开具、抬头变更或重开属于订单服务，归 after_sales。"
    "尚未得到结果的审核、发货、物流、退款或售后进度询问，默认归 after_sales；"
    "只有同时出现反复推诿、承诺未履行、要求追责或翻旧账等流程责任信号时，才归 complaint。"
    "售前询问退换货政策、保修条款、发货时效属 product_inquiry；"
    "after_sales 要求已存在一笔交易和一个待处理的问题。"
)

# 用自然语言描述期望字段是不够的：examples 演示的是「怎么标注」，从未演示过
# 「输出长什么样」，模型于是合法地把结果套进了信封。这里直接印出目标对象。
_MODEL_SYSTEM_PROMPT = (
    "你是客服消息意图分类器。intent 只能取 product_inquiry、after_sales、"
    "complaint、chitchat 之一，confidence 取 0 到 1 的小数。"
    + _LABELLING_POLICY
    + "输入中的 advisory_signals 只是召回提示，可能误报；必须按完整消息独立判断，"
    "不得把规则候选直接当作结论。"
    + "严格返回下面这一个 JSON 对象，不要嵌套、不要包装、不要额外字段："
    '{"intent": "chitchat", "confidence": 0.5}'
)
_RULE_REVIEW_PROMPT = (
    "关键词可能处于否定或非电商语境，必须按整句真实诉求判断。"
    "已明确取消或否定某个诉求且没有新诉求时，判为 chitchat。"
)
_FEW_SHOT_EXAMPLES = (
    {"message": "这款还有哪些颜色", "intent": "product_inquiry"},
    {"message": "收到后怎么换货", "intent": "after_sales"},
    {"message": "刚收货的耳机就没声音，做工真让人失望", "intent": "after_sales"},
    {
        "message": "安装预约改了三次仍没人上门，之前的处理为什么一直无效",
        "intent": "complaint",
    },
    {"message": "你好呀", "intent": "chitchat"},
)
_RULE_REVIEW_EXAMPLES = (
    {"message": "不用办理退货了，多谢", "intent": "chitchat"},
)


def classify(message: str, *, model: IntentModel | None) -> IntentResult:
    normalized = message.strip()
    if not normalized or not any(character.isalnum() for character in normalized):
        return _default_result()
    process_review = _matches_process_accountability(normalized)
    review_rule_match = False
    rule_match = _match_rule(normalized)
    rule_intent: CustomerIntent | None = None
    rule_keywords: tuple[str, ...] = ()
    if rule_match is not None:
        rule_intent, rule_keywords = rule_match
        review_rule_match = _requires_model_review(normalized, rule_keywords) or (
            process_review and rule_intent != "complaint"
        )
        if not review_rule_match:
            return IntentResult(
                intent=rule_intent,
                confidence=_RULE_CONFIDENCE,
                method="rule",
            )
    if model is None:
        return _default_result("model_not_configured")
    timeout_seconds = _model_timeout(model)
    system_prompt = _MODEL_SYSTEM_PROMPT
    examples = _FEW_SHOT_EXAMPLES
    if review_rule_match:
        system_prompt += _RULE_REVIEW_PROMPT
        examples += _RULE_REVIEW_EXAMPLES
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task_type": "intent_classification",
                    "examples": examples,
                    "message": normalized[:4000],
                    "advisory_signals": {
                        "semantic_authority": False,
                        **(
                            {
                                "rule_candidate": rule_intent,
                                "matched_keywords": list(rule_keywords),
                                "process_accountability": process_review,
                            }
                            if review_rule_match
                            else {}
                        ),
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]
    outcome: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def invoke_model() -> None:
        try:
            payload = model.generate_json(
                messages,
                timeout_seconds=timeout_seconds,
            )
            outcome.put(
                (
                    "result",
                    payload,
                )
            )
        except Exception as exc:
            outcome.put(("error", exc))

    worker = threading.Thread(
        target=invoke_model,
        name="intent-classifier-deadline",
        daemon=True,
    )
    worker.start()
    # Leave a small scheduling margin so returning the fallback itself remains
    # inside the configured wall-clock budget, not merely the socket wait.
    deadline_margin = min(0.02, timeout_seconds * 0.05)
    worker.join(max(0.001, timeout_seconds - deadline_margin))
    if worker.is_alive():
        return _default_result("model_deadline_exceeded")
    outcome_kind, outcome_value = outcome.get_nowait()
    try:
        if outcome_kind == "error":
            raise outcome_value
        payload = outcome_value
    except Exception as exc:
        return _default_result(f"model_call_failed:{type(exc).__name__}")
    result = _coerce_model_payload(payload)
    if result is None:
        return _default_result(f"model_payload_rejected:{_payload_shape(payload)}")
    return result


def _match_rule(
    message: str,
) -> tuple[CustomerIntent, tuple[str, ...]] | None:
    for intent in _RULE_PRIORITY:
        matches = tuple(
            keyword for keyword in _RULE_KEYWORDS[intent] if keyword in message
        )
        if matches:
            return intent, matches
    return None


def _matches_process_accountability(message: str) -> bool:
    """Recognize a failed service process plus an accountability complaint.

    Each signal is insufficient on its own: ordinary service questions have a
    process noun, routine after-sales messages have a failure, and emphatic
    questions may contain an accountability phrase. Requiring their composition
    keeps normal remedies on the model path while making repeated process failure
    a deterministic high-priority signal.
    """

    process_context = _PROCESS_CONTEXT_PATTERN.search(message)
    failure = _PROCESS_FAILURE_PATTERN.search(message)
    accountability = _PROCESS_ACCOUNTABILITY_PATTERN.search(message)
    recurrence = _PROCESS_RECURRENCE_PATTERN.search(message)
    incident = _PROCESS_INCIDENT_PATTERN.search(message)
    generic_process_verb = bool(
        process_context and process_context.group(0) == "处理"
    )
    return bool(
        process_context
        and (
            (failure and (accountability or recurrence))
            or (accountability and incident and not generic_process_verb)
        )
    )


def _requires_model_review(message: str, keywords: tuple[str, ...]) -> bool:
    for keyword in keywords:
        if _keyword_is_negated(message, keyword):
            return True
        evidence_groups = _RULE_BUSINESS_EVIDENCE.get(keyword)
        if evidence_groups is not None and not (
            _is_terse_rule_request(message, keyword)
            or _has_business_evidence(message, keyword, evidence_groups)
        ):
            return True
    return False


def _is_terse_rule_request(message: str, keyword: str) -> bool:
    folded_keyword = keyword.casefold()
    for clause in _RULE_CLAUSE_BOUNDARY.split(message.casefold()):
        compact = "".join(character for character in clause if character.isalnum())
        start = 0
        while (index := compact.find(folded_keyword, start)) >= 0:
            prefix = compact[:index]
            suffix_start = index + len(folded_keyword)
            suffix = compact[suffix_start:]
            if _contains_only_markers(
                prefix, _TERSE_RULE_PREFIXES
            ) and _contains_only_markers(suffix, _TERSE_RULE_SUFFIXES):
                return True
            start = suffix_start
    return False


def _contains_only_markers(value: str, markers: tuple[str, ...]) -> bool:
    while value:
        for marker in markers:
            if value.startswith(marker):
                value = value[len(marker) :]
                break
        else:
            return False
    return True


def _has_business_evidence(
    message: str,
    keyword: str,
    evidence_groups: tuple[tuple[str, ...], ...],
) -> bool:
    folded_keyword = keyword.casefold()
    for clause in _RULE_CLAUSE_BOUNDARY.split(message.casefold()):
        if folded_keyword not in clause:
            continue
        if all(
            any(marker in clause for marker in group)
            for group in evidence_groups
        ):
            return True
    return False


def _keyword_is_negated(message: str, keyword: str) -> bool:
    start = 0
    while (index := message.find(keyword, start)) >= 0:
        prefix = message[max(0, index - _NEGATION_WINDOW) : index]
        suffix_start = index + len(keyword)
        suffix = message[suffix_start : suffix_start + _NEGATION_WINDOW]
        if any(marker in prefix for marker in _NEGATION_PREFIX_MARKERS):
            return True
        if any(marker in suffix for marker in _NEGATION_SUFFIX_MARKERS):
            return True
        start = suffix_start
    return False


def _coerce_model_payload(payload: Any) -> IntentResult | None:
    """把模型返回的实际形状归一成 IntentResult，无法归一时返回 None。

    提示词只能提高目标形状的概率，保证不了它。真实的 glm-4.7-flash 稳定把结果
    包成 {"answer": {...}}，直接下标取值会整条丢弃一个本来正确的答案。
    """
    payload = _unwrap_envelope(payload)
    if not isinstance(payload, dict):
        return None
    intent = payload.get("intent")
    if isinstance(intent, str):
        intent = intent.strip().lower()
    if intent not in _INTENTS:
        return None
    try:
        confidence = float(payload.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    # 越界只截断不否决：intent 才是有效载荷，confidence 超范围是格式毛病，
    # 为此丢掉一个正确的分类结果不划算。
    return IntentResult(
        intent=intent,
        confidence=min(1.0, max(0.0, confidence)),
        method="model",
    )


def _unwrap_envelope(payload: Any) -> Any:
    """逐层拆掉 {"answer": {...}} / {"result": {...}} 这类单键信封。

    限定单键且内层仍是 dict，所以 {"intent": "chitchat"} 不会被误拆；限 3 层，
    防畸形输出把这里变成深递归。
    """
    for _ in range(3):
        if not isinstance(payload, dict) or len(payload) != 1:
            break
        inner = next(iter(payload.values()))
        if not isinstance(inner, dict):
            break
        payload = inner
    return payload


def _payload_shape(payload: Any) -> str:
    """只描述形状不带内容——诊断够用，且不会把用户消息带进日志。"""
    if isinstance(payload, dict):
        return "{" + ",".join(sorted(str(key) for key in payload)[:5]) + "}"
    return type(payload).__name__


def _model_timeout(model: IntentModel) -> float:
    settings = getattr(model, "settings", None)
    value = getattr(settings, "intent_classify_timeout_seconds", 2.0)
    try:
        return max(0.001, float(value))
    except (TypeError, ValueError):
        return 2.0


def _default_result(error: str = "unclassifiable_input") -> IntentResult:
    return IntentResult(
        intent="chitchat",
        confidence=0.0,
        method="default",
        error=error,
    )
