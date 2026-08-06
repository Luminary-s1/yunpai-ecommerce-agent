from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .text_utils import normalize_text, redact_sensitive


PROMPT_INJECTION_PATTERNS = (
    r"忽略.{0,8}(之前|以上|系统).{0,8}(指令|规则)",
    r"(system prompt|系统提示词|开发者消息|隐藏指令)",
    r"(越权|绕过).{0,8}(限制|权限|审核|平台)",
)

HIGH_RISK_ACTION_PATTERNS = (
    r"(帮我|给我|立即|马上|现在).{0,6}(退款|退钱|赔付|赔偿|补偿)",
    r"(申请|执行|操作).{0,5}(退款|退货|换货|赔付)",
    r"(改|修改|换).{0,5}(价格|价钱|地址|手机号|收货人|发票抬头)",
    r"(价格|价钱|地址|手机号|收货人|发票抬头).{0,8}(改|修改|换)",
    r"(取消|关闭).{0,4}订单",
    r"(补发|重新发|拦截快递|召回包裹)",
)

UNAUTHORIZED_DATA_PATTERNS = (
    r"(别家|竞品|其他店铺).{0,8}(真实销量|库存|订单|买家)",
    r"(其他|别的).{0,5}(买家|客户).{0,5}(电话|地址|数据|信息)",
)

FORBIDDEN_OUTPUT_PATTERNS = (
    r"(已经|已为您|现已).{0,8}(退款|退钱|改价|改地址|取消订单|补发|赔付|开票)",
    r"(保证|承诺|一定|百分之百).{0,12}(到货|发货|有效|成功|退款)",
    r"(请提供|发送).{0,6}(密码|验证码|完整身份证|银行卡密码)",
    r"(加我微信|转到私人账户|站外支付)",
)

# Internal identifiers a shopper cannot be expected to know. The agent must resolve
# them from the wording the customer already used instead of asking for them.
INTERNAL_IDENTIFIER_FIELDS = {
    "sku",
    "sku_id",
    "skuid",
    "sku_code",
    "item_id",
    "itemid",
    "num_iid",
    "product_id",
    "productid",
    "product_code",
    "spu",
    "spu_id",
    "spuid",
    "goods_id",
    "catalog_id",
    "catalog_item_id",
}

INTERNAL_IDENTIFIER_LABEL = "商品名称或商品链接"

INTERNAL_IDENTIFIER_REQUEST_PATTERNS = (
    r"sku",
    r"(item|product|spu|goods)[\s_-]*id",
    r"(商品|宝贝|货品)\s*(id|编号|编码|货号|代码)",
)

ALLOWED_CONTEXT_FIELDS = {
    "authorized",
    "platform",
    "store_id",
    "shop_id",
    "product_name",
    "sku_id",
    "sku",
    "order_id",
    "order_status",
    "logistics_status",
    "carrier",
    "tracking_last_event",
    "shop_policy",
}


@dataclass(frozen=True, slots=True)
class PrecheckDecision:
    route: str
    reason: str


def sanitize_context(context: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in context.items():
        if key not in ALLOWED_CONTEXT_FIELDS:
            continue
        if isinstance(value, bool):
            sanitized[key] = value
        elif isinstance(value, (str, int, float)):
            normalized = normalize_text(str(value))[:500]
            sanitized[key] = redact_sensitive(normalized)[0]
    return sanitized


def precheck_request(message: str, context: dict[str, Any]) -> PrecheckDecision:
    """Enforce trust boundaries without deciding normal business intent."""

    for pattern in PROMPT_INJECTION_PATTERNS:
        if re.search(pattern, message, re.IGNORECASE):
            return PrecheckDecision("refuse", "prompt_injection_detected")
    for pattern in UNAUTHORIZED_DATA_PATTERNS:
        if re.search(pattern, message, re.IGNORECASE):
            return PrecheckDecision("refuse", "unauthorized_data_request")
    return PrecheckDecision("deliberate", "llm_deliberation_allowed")


def is_business_action_request(message: str) -> bool:
    """Detect actions that require verified execution or a human handoff."""

    return any(re.search(pattern, message) for pattern in HIGH_RISK_ACTION_PATTERNS)


def asks_for_internal_identifier(text: str) -> bool:
    """Detect a reply that demands SKU/item ids a shopper does not have."""

    return any(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in INTERNAL_IDENTIFIER_REQUEST_PATTERNS
    )


def customer_facing_missing_fields(fields: list[str]) -> list[str]:
    """Replace internal identifier field names with what a shopper can provide."""

    described: list[str] = []
    for field in fields:
        label = field.strip()
        if label.lower().replace("-", "_") in INTERNAL_IDENTIFIER_FIELDS:
            label = INTERNAL_IDENTIFIER_LABEL
        if label and label not in described:
            described.append(label)
    return described


def review_output(answer: str, evidence: str) -> tuple[bool, str]:
    if not answer.strip():
        return False, "empty_model_output"
    verified_business_result = bool(
        re.search(r'"postcondition_met"\s*:\s*true', evidence, re.IGNORECASE)
    )
    for pattern in FORBIDDEN_OUTPUT_PATTERNS:
        if re.search(pattern, answer) and not verified_business_result:
            return False, "forbidden_commitment_in_output"
    # Treat 499 and 499.00 as equal; keep percentages distinct.
    unsupported_numbers = _normalized_numbers(answer) - _normalized_numbers(evidence)
    if unsupported_numbers:
        return False, "numeric_claim_without_evidence"
    return True, "output_policy_passed"


def _normalized_numbers(text: str) -> set[str]:
    values: set[str] = set()
    for raw in re.findall(r"\d+(?:\.\d+)?%?", text):
        percent = raw.endswith("%")
        number_text = raw[:-1] if percent else raw
        try:
            number = Decimal(number_text)
        except InvalidOperation:
            continue
        values.add(("%" if percent else "") + format(number.normalize(), "f"))
    return values
