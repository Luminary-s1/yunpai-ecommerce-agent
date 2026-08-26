from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import Settings
from .policy import (
    business_action_completion_claim_is_authorized,
    delivery_time_claim_confidence,
    delivery_time_claim_segments,
    delivery_time_claim_requires_support,
    has_business_action_completion_claim,
)
from .text_utils import normalize_text


CUSTOMER_SERVICE_SUGGESTION_VERSION = "customer-service-suggestion-v1"
CUSTOMER_SERVICE_FACT_TOOLS = frozenset(
    {
        "get_customer_sales_facts",
        "get_customer_after_sales_facts",
    }
)
_EXPLICIT_INVENTORY_QUANTITY_REQUEST = re.compile(
    r"(多少|几)\s*(件|个|台)|库存\s*(量|数量)|具体\s*(库存|数量)|"
    r"(?:库存|可售|现货|剩余).{0,6}(?:多少|几)|还剩.{0,6}(?:多少|几)"
)
_NON_ACTIONABLE_INVENTORY_QUANTITY_CONTEXT = re.compile(
    r"(?:不是|不想|无需|不用|不要|不需要|不必|别).{0,14}"
    r"(?:库存.{0,4}(?:量|数量)|(?:多少|几).{0,3}(?:件|个|台)|还剩)|"
    r"(?:库存.{0,4}(?:量|数量)|(?:多少|几).{0,3}(?:件|个|台)|还剩)"
    r".{0,14}(?:无需|不用|不要|不需要|不必|别说)|"
    r"(?:如果|假如|假设).{0,24}(?:库存.{0,4}(?:量|数量)|"
    r"(?:多少|几).{0,3}(?:件|个|台)|还剩)"
)
_STALE_CURRENT_CLAIM = re.compile(
    r"(目前|当前|现在).{0,8}(有货|现货|库存充足|可下单|可以下单)|"
    r"(可以|可).{0,4}(立即|马上).{0,4}(下单|购买|发货)|"
    r"(目前|当前|现在|刚刚|刚才).{0,12}"
    r"((已|已经)(发货|签收|送达|完成)|运输中|派送中|审核中|退款中|退款成功)"
)
_MISSING_INVENTORY_ZERO_CLAIM = re.compile(
    r"(库存|可售|现货|剩余).{0,8}(为|是|有|剩)?\s*(0|零)\s*(件|个|台)?|"
    r"(0|零)\s*(件|个|台).{0,6}(库存|可售|现货|剩余)"
)
_INVENTORY_AVAILABILITY_CLAIM = re.compile(
    r"(?:商品|宝贝|这款|该款|库存|现货).{0,12}"
    r"(?:有货|无货|缺货|售罄|库存充足|现货充足|可下单|可以下单)|"
    r"(?:^|[，。；、\s])(?:有货|无货|缺货|售罄)(?:[，。；、\s]|$)"
)
_INVENTORY_QUANTITY_CLAIM = re.compile(
    r"(?:库存|可售|现货|剩余).{0,8}(?:为|是|有|剩)?\s*"
    r"(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百千]+)\s*"
    r"(?:件|台|个(?!工作日))|"
    r"(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百千]+)\s*"
    r"(?:件|台|个(?!工作日)).{0,6}(?:库存|可售|现货|剩余)"
)
_INBOUND_INVENTORY_CLAIM = re.compile(r"在途|待入库|调拨中")
_WAREHOUSE_DETAIL_CLAIM = re.compile(
    r"仓库|仓号|库位|仓位|发货仓|备货仓|"
    r"[\u4e00-\u9fff]{1,8}仓(?=[，。；、\s]|$|(?:有货|库存|备货|发出|发货|出库))"
)
_LOGISTICS_STATE_CLAIM = re.compile(
    r"(?:物流|快递|包裹).{0,14}"
    r"(?:(?:已|已经)(?:发货|揽收|揽件|签收|送达|到达|出库)|"
    r"(?:发货|揽收|揽件|签收|送达|到达|出库|到货|到)了|"
    r"运输中|运送中|正在运输|正在运送|派送中|配送中|正在派送|正在配送)"
)
_REFUND_DESTINATION = (
    r"(?:(?:您|你)的?)?(?:原)?(?:支付)?"
    r"(?:账户|账号|银行卡|原卡|余额|支付方式|支付账户|渠道)"
)
_REFUND_TRANSFER = (
    rf"(?:(?:原路)?(?:退|返)(?:回|还)|"
    rf"(?:退|返)(?:至|到|入).{{0,4}}{_REFUND_DESTINATION})"
)
_AFTER_SALES_STATE_CLAIM = re.compile(
    rf"(?:(?:退款|退货|换货|售后).{{0,14}}"
    rf"(?:审核中|处理中|正在审核|正在处理|还在审核|"
    rf"(?:已|已经)(?:提交|受理|通过|完成|拒绝|关闭|退款|到账|"
    rf"{_REFUND_TRANSFER})|"
    rf"审核通过了|通过审核了|通过了|完成了|拒绝了|被拒了|关闭了|"
    rf"到账了|退回了|退回去了|退还了|返回了|返还了|退款成功)|"
    rf"(?:款项|退款款项|钱|金额).{{0,14}}"
    rf"(?:(?:已|已经)(?:到账|{_REFUND_TRANSFER})|"
    rf"到账了|退回了|退还了|返回了|返还了))"
)
_ORDER_STATE_CLAIM = re.compile(
    r"订单.{0,14}(?:已创建|待付款|备货中|待发货|"
    r"(?:尚未|未|还没)(?:付款|发货)|"
    r"(?:已|已经)(?:发货|出库|签收|送达|完成|关闭|取消|撤销|作废)|"
    r"(?:发货|出库|签收|送达|完成|关闭|取消|撤销|作废|关)了)"
)
_PAYMENT_STATE_CLAIM = re.compile(
    r"(?:款项|付款|支付|订单).{0,14}(?:(?:尚未|未|待|还没)(?:付款|支付)|"
    r"(?:已|已经)(?:付款|支付|退款|关闭)|"
    r"(?:付款|支付)(?:成功|了)|部分退款)"
)
_UNSCOPED_FULFILLMENT_STATE_CLAIM = re.compile(
    r"(?:(?:已|已经)(?:发货|出库|揽收|签收|送达)|"
    r"(?:尚未|未)(?:发货|出库|揽收|签收|送达))"
)
_UNSCOPED_FULFILLMENT_STATUS_CLAIMS = (
    (
        re.compile(r"(?:^|[，。；、\s])(?:已|已经)(?:发货|出库)(?:了)?"),
        {"shipped", "in_transit", "out_for_delivery", "delivered"},
    ),
    (
        re.compile(r"(?:^|[，。；、\s])(?:已|已经)(?:揽收|揽件)(?:了)?"),
        {"in_transit", "out_for_delivery", "delivered"},
    ),
    (
        re.compile(
            r"(?:^|[，。；、\s])(?:已|已经)(?:签收|送达|到达|到货)(?:了)?"
        ),
        {"delivered"},
    ),
    (
        re.compile(r"(?:^|[，。；、\s])(?:尚未|未|还没)(?:发货|出库)"),
        {"created", "fulfilling"},
    ),
)
_OPERATIONAL_FACT_UNCERTAINTY = re.compile(
    r"(?:无法|不能|暂时无法|尚不能|不确定|无法核实|未能核实|待|需要|需)"
    r".{0,12}(?:确认|核对|核实|查询|查明)|"
    r"(?:确认|核对|核实|查询|查明).{0,8}(?:后|为准)"
)
_OPERATIONAL_FACT_HYPOTHETICAL = re.compile(r"(?:如果|假如|假设|若|倘若)")
_OPERATIONAL_FACT_QUESTION = re.compile(
    r"(?:是否|能否|可否|有没有|是不是)|(?:吗|么|呢)$"
)
_OPERATIONAL_CLAUSE_SPLIT = re.compile(
    r"[，。；！？、,;!?]|但是|不过|而是|但|却|同时|并且|而且|另外|然后"
)
_OPERATIONAL_CLAUSE_TOKENIZER = re.compile(
    r"([，；,;]|[。！？.!?]|但是|不过|而是|但|却|同时|并且|而且|另外|然后)"
)
_ORDER_UNSCOPED_STATE = re.compile(
    r"(?:已|已经)(?:创建|完成|关闭|取消|撤销|作废|付款|支付)|"
    r"(?:完成|关闭|取消|撤销|作废|关|付款|支付)了|"
    r"待付款|待发货|备货中|"
    r"(?:尚未|未|还没)(?:付款|发货)"
)
_AFTER_SALES_UNSCOPED_STATE = re.compile(
    rf"(?:已|已经)(?:提交|受理|通过|完成|拒绝|关闭|退款|到账|"
    rf"{_REFUND_TRANSFER})|"
    rf"审核中|处理中|正在审核|正在处理|还在审核|退款成功|"
    rf"通过了|完成了|拒绝了|被拒了|关闭了|到账了|退回了|退回去了|"
    rf"退还了|返回了|返还了"
)
_UNSCOPED_REFUND_TRANSFER_STATE_CLAIM = re.compile(
    rf"(?:已|已经){_REFUND_TRANSFER}(?:了)?"
)
_LOGISTICS_UNSCOPED_STATE = re.compile(
    r"(?:已|已经)(?:发货|出库|揽收|揽件|签收|送达|到达|到货)|"
    r"(?:发货|出库|揽收|揽件|签收|送达|到达|到货|到)了|"
    r"运输中|运送中|正在运输|正在运送|派送中|配送中|正在派送|正在配送|"
    r"(?:尚未|未|还没)(?:发货|出库|揽收|揽件|签收|送达|到达|到货)"
)
_GENERIC_ACTION_COMPLETION = re.compile(
    r"(?:操作|处理)(?:已|已经)(?:完成|成功)|"
    r"(?:已|已经)(?:完成|成功)(?:此次|本次)?(?:操作|处理)"
)
_ORDER_STATUS_CLAIMS = (
    (re.compile(r"订单.{0,14}(?:已创建|待付款)"), {"created"}),
    (
        re.compile(r"订单.{0,14}(?:备货中|待发货|(?:尚未|未|还没)发货)"),
        {"fulfilling"},
    ),
    (
        re.compile(r"订单.{0,14}(?:(?:已|已经)(?:发货|出库)|(?:发货|出库)了)"),
        {"shipped"},
    ),
    (
        re.compile(
            r"订单.{0,14}(?:(?:已|已经)(?:签收|送达|完成)|"
            r"(?:签收|送达|完成)了)"
        ),
        {"delivered"},
    ),
    (
        re.compile(r"订单.{0,14}(?:(?:已|已经)关闭|(?:关闭|关)了)"),
        {"closed"},
    ),
    (
        re.compile(
            r"订单.{0,14}(?:(?:已|已经)(?:取消|撤销|作废)|"
            r"(?:取消|撤销|作废)了)"
        ),
        {"canceled", "cancelled", "closed"},
    ),
)
_PAYMENT_STATUS_CLAIMS = (
    (
        re.compile(
            r"(?:款项|付款|支付|订单).{0,14}"
            r"(?:尚未|未|待|还没)(?:付款|支付)"
        ),
        {"unpaid"},
    ),
    (
        re.compile(
            r"(?:款项|付款|支付|订单).{0,14}"
            r"(?:(?:已|已经)(?:付款|支付)|(?:付款|支付)(?:成功|了))"
        ),
        {"paid"},
    ),
    (re.compile(r"(?:款项|付款|支付).{0,14}部分退款"), {"partially_refunded"}),
    (re.compile(r"(?:款项|付款|支付).{0,14}(?:已|已经)退款"), {"refunded"}),
    (re.compile(r"(?:款项|付款|支付).{0,14}(?:已|已经)关闭"), {"closed"}),
)
_LOGISTICS_STATUS_CLAIMS = (
    (
        re.compile(
            r"(?:物流|快递|包裹).{0,14}"
            r"(?:运输中|在途|运送中|正在运输|正在运送|(?:已|已经)揽件)"
        ),
        {"in_transit"},
    ),
    (
        re.compile(
            r"(?:物流|快递|包裹).{0,14}"
            r"(?:派送中|配送中|正在派送|正在配送)"
        ),
        {"out_for_delivery"},
    ),
    (
        re.compile(
            r"(?:物流|快递|包裹).{0,14}(?:(?:已|已经)(?:签收|送达|到货)|"
            r"(?:签收|送达|到货|到)了|送到了)"
        ),
        {"delivered"},
    ),
)
_AFTER_SALES_STATUS_CLAIMS = (
    (
        re.compile(
            r"(?:退款|退货|换货|售后).{0,14}"
            r"(?:审核中|处理中|正在审核|正在处理|还在审核|"
            r"(?:已|已经)(?:提交|受理))"
        ),
        {"submitted", "accepted", "reviewing"},
    ),
    (
        re.compile(
            r"(?:退款|退货|换货|售后).{0,14}"
            r"(?:(?:已|已经)(?:通过|批准)|审核通过了|通过审核了|通过了)"
        ),
        {"approved"},
    ),
    (
        re.compile(
            r"(?:退款|退货|换货|售后).{0,14}"
            r"(?:(?:已|已经)拒绝|未通过|拒绝了|被拒了)"
        ),
        {"rejected"},
    ),
    (
        re.compile(
            rf"(?:(?:退款|退货|换货|售后).{{0,14}}"
            rf"(?:(?:已|已经)(?:完成|退款|到账|{_REFUND_TRANSFER})|"
            rf"完成了|到账了|退回了|退回去了|退还了|返回了|返还了|"
            rf"退款成功)|"
            rf"(?:款项|退款款项|钱|金额).{{0,14}}"
            rf"(?:(?:已|已经)(?:到账|{_REFUND_TRANSFER})|"
            rf"到账了|退回了|退还了|返回了|返还了)|"
            rf"(?:已|已经){_REFUND_TRANSFER}(?:了)?)"
        ),
        {"completed", "refunded"},
    ),
    (
        re.compile(r"(?:退款|退货|换货|售后).{0,14}(?:已|已经)关闭"),
        {"closed", "cancelled", "canceled"},
    ),
)
_PRODUCT_STATUS_CLAIMS = (
    (
        re.compile(r"(?:商品|宝贝|这款|该款).{0,12}(?:在售|已上架|正常销售|可购买)"),
        {"active"},
    ),
    (
        re.compile(r"(?:商品|宝贝|这款|该款).{0,12}(?:已下架|下架|停售|不可购买)"),
        {"inactive"},
    ),
    (
        re.compile(r"(?:商品|宝贝|这款|该款).{0,12}(?:已删除|删除)"),
        {"deleted"},
    ),
)


def _numeric_pattern(value: Any) -> str | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    normalized = format(number.normalize(), "f")
    if number == number.to_integral_value():
        integer = format(number.quantize(Decimal("1")), "f")
        return rf"{re.escape(integer)}(?:\.0+)?"
    return re.escape(normalized)


def _mentions_inventory_quantity(draft: str, value: Any, *, label: str) -> bool:
    number = _numeric_pattern(value)
    if number is None:
        return False
    if label == "inbound":
        patterns = (
            rf"在途.{{0,8}}{number}",
            rf"{number}\s*(件|个|台)?.{{0,6}}在途",
        )
    else:
        patterns = (
            rf"(库存|可售|现货|剩余).{{0,8}}{number}",
            rf"{number}\s*(?:件|台|个(?!工作日)).{{0,6}}(库存|可售|现货|剩余)?",
        )
    return any(re.search(pattern, draft) for pattern in patterns)


def _explicit_inventory_quantity_requested(question: str) -> bool:
    return bool(
        _EXPLICIT_INVENTORY_QUANTITY_REQUEST.search(question)
        and not _NON_ACTIONABLE_INVENTORY_QUANTITY_CONTEXT.search(question)
    )


def _is_approved_exact_answer(draft: str, approved_answers: list[str]) -> bool:
    normalized_draft = normalize_text(draft)
    return bool(normalized_draft) and any(
        normalize_text(answer) == normalized_draft for answer in approved_answers
    )


def _delivery_claim_supported_by_facts(
    draft: str,
    output: dict[str, Any],
    *,
    question: str | None = None,
) -> bool:
    logistics = ((output.get("facts") or {}).get("logistics") or {})
    fact_segments: list[tuple[str, int]] = []
    for key in ("last_event", "status"):
        for clause in _OPERATIONAL_CLAUSE_SPLIT.split(
            str(logistics.get(key) or "")
        ):
            clause = clause.strip()
            if not clause:
                continue
            matches = delivery_time_claim_segments(clause)
            fact_segments.extend(
                (segment, delivery_time_claim_confidence(clause))
                for segment in (matches or [clause])
            )

    claim_segments: list[tuple[str, int]] = []
    for clause in _OPERATIONAL_CLAUSE_SPLIT.split(draft):
        clause = clause.strip()
        if not clause or not delivery_time_claim_requires_support(
            clause,
            question=question,
        ):
            continue
        matches = delivery_time_claim_segments(clause)
        claim_segments.extend(
            (segment, delivery_time_claim_confidence(clause))
            for segment in (matches or [clause])
        )
    if not claim_segments:
        return False
    for claim_segment, claim_confidence in claim_segments:
        normalized_claim = normalize_text(claim_segment)
        if not any(
            len(normalized_fact) >= 4
            and (
                normalized_fact in normalized_claim
                or normalized_claim in normalized_fact
            )
            and fact_confidence >= claim_confidence
            for fact_segment, fact_confidence in fact_segments
            if (normalized_fact := normalize_text(fact_segment))
        ):
            return False
    return True


def customer_service_delivery_claim_authorized(
    draft: str,
    tool_result: dict[str, Any],
    *,
    approved_answers: list[str] | None = None,
    question: str | None = None,
) -> bool:
    if _is_approved_exact_answer(draft, approved_answers or []):
        return True
    tool_name = str(tool_result.get("tool_name") or "")
    if (
        tool_name not in CUSTOMER_SERVICE_FACT_TOOLS
        or tool_result.get("status") != "success"
        or tool_result.get("postcondition_met") is not True
    ):
        return False
    return _delivery_claim_supported_by_facts(
        draft,
        tool_result.get("output") or {},
        question=question,
    )


def verified_customer_service_business_action(
    tool_result: dict[str, Any],
) -> str | None:
    if (
        tool_result.get("tool_kind") != "write"
        or tool_result.get("status") != "success"
        or tool_result.get("postcondition_met") is not True
    ):
        return None
    tool_name = str(tool_result.get("tool_name") or "").strip().lower()
    return tool_name or None


def _claim_clause_is_nonassertive(
    clause: str,
    match: re.Match[str],
) -> bool:
    if _OPERATIONAL_FACT_UNCERTAINTY.search(clause):
        return True
    if _OPERATIONAL_FACT_HYPOTHETICAL.search(clause[: match.start()]):
        return True
    local_start = max(0, match.start() - 8)
    local_end = min(len(clause), match.end() + 4)
    return _OPERATIONAL_FACT_QUESTION.search(clause[local_start:local_end]) is not None


def _has_asserted_claim(draft: str, pattern: re.Pattern[str]) -> bool:
    return any(
        not _claim_clause_is_nonassertive(clause, match)
        for clause in _OPERATIONAL_CLAUSE_SPLIT.split(draft)
        for match in pattern.finditer(clause)
    )


def _status_claim_mismatch(
    draft: str,
    actual_statuses: set[str],
    claims: tuple[tuple[re.Pattern[str], set[str]], ...],
) -> bool:
    normalized_statuses = {status.strip().lower() for status in actual_statuses if status}
    return any(
        _has_asserted_claim(draft, pattern)
        and not normalized_statuses.intersection(allowed)
        for pattern, allowed in claims
    )


def _operational_subject(text: str) -> str | None:
    if re.search(r"订单", text):
        return "订单"
    if re.search(r"退款|退货|换货|售后|款项", text):
        return "退款"
    if re.search(r"物流|快递|包裹", text):
        return "物流"
    return None


def _expand_operational_subject_carryover(
    draft: str,
    *,
    question: str | None = None,
) -> str:
    subject = _operational_subject(question or "")
    carry_subject = False
    expanded: list[str] = []
    for token in _OPERATIONAL_CLAUSE_TOKENIZER.split(draft):
        if not token:
            continue
        if _OPERATIONAL_CLAUSE_TOKENIZER.fullmatch(token):
            carry_subject = token in {"，", ",", "；", ";", "但是", "不过", "而是", "但", "却"}
            if not carry_subject:
                subject = None
            continue
        clause = token.strip()
        explicit_subject = _operational_subject(clause)
        if explicit_subject:
            subject = explicit_subject
        elif subject == "订单" and (
            carry_subject or not expanded
        ) and _ORDER_UNSCOPED_STATE.search(clause) and not _GENERIC_ACTION_COMPLETION.search(
            clause
        ):
            clause = f"订单{clause}"
        elif (
            (carry_subject or not expanded)
            and subject == "退款"
            and _AFTER_SALES_UNSCOPED_STATE.search(clause)
            and not _GENERIC_ACTION_COMPLETION.search(clause)
        ):
            clause = f"退款{clause}"
        elif (
            (carry_subject or not expanded)
            and subject == "物流"
            and _LOGISTICS_UNSCOPED_STATE.search(clause)
        ):
            clause = f"物流{clause}"
        expanded.append(clause)
        carry_subject = False
    return "。".join(expanded)


def _claims_sales_operational_fact(draft: str) -> bool:
    return any(
        _has_asserted_claim(draft, pattern)
        for pattern in (
            _INVENTORY_AVAILABILITY_CLAIM,
            _INVENTORY_QUANTITY_CLAIM,
            *(pattern for pattern, _ in _PRODUCT_STATUS_CLAIMS),
        )
    )


def _claims_after_sales_operational_fact(draft: str) -> bool:
    return any(
        _has_asserted_claim(draft, pattern)
        for pattern in (
            _ORDER_STATE_CLAIM,
            _PAYMENT_STATE_CLAIM,
            _LOGISTICS_STATE_CLAIM,
            _AFTER_SALES_STATE_CLAIM,
            _UNSCOPED_REFUND_TRANSFER_STATE_CLAIM,
            _UNSCOPED_FULFILLMENT_STATE_CLAIM,
        )
    )


def _operational_write_claims_are_authorized(
    draft: str,
    verified_business_action: str | None,
    *,
    question: str | None = None,
) -> bool:
    if not verified_business_action:
        return False
    operational_clauses = [
        clause
        for clause in _OPERATIONAL_CLAUSE_SPLIT.split(draft)
        if _claims_sales_operational_fact(clause)
        or _claims_after_sales_operational_fact(clause)
    ]
    return bool(operational_clauses) and all(
        has_business_action_completion_claim(clause, question=question)
        and business_action_completion_claim_is_authorized(
            clause,
            verified_business_action,
            question=question,
        )
        for clause in operational_clauses
    )


def build_customer_service_response_policy(
    tool_name: str,
    output: dict[str, Any],
) -> dict[str, Any]:
    """Translate trusted facts into customer-facing disclosure constraints."""

    if tool_name not in CUSTOMER_SERVICE_FACT_TOOLS:
        return {}
    freshness = output.get("freshness") or {}
    data_as_of = output.get("data_as_of")
    state = str(output.get("state") or "missing")
    current = freshness.get("usable_as_current") is True
    return {
        "policy_version": "customer-service-response-policy-v1",
        "fact_domain": output.get("domain"),
        "fact_state": state,
        "facts_usable_for_response": state not in {"blocked", "missing"},
        "current_claims_allowed": current,
        "must_display_data_as_of": bool(data_as_of and not current),
        "data_as_of": data_as_of,
        "freshness_status": freshness.get("status"),
        "missing": list(output.get("missing") or []),
        "inventory": {
            "default_customer_view": "availability_status_only",
            "exact_available_quantity": (
                "explicit_customer_request_and_current_fact_only"
            ),
            "inbound_quantity": "internal_only",
            "warehouse_detail": "never_disclose",
        },
        "commitments": {
            "delivery_time": "approved_policy_or_verified_fact_only",
            "refund_or_order_action": "verified_write_postcondition_only",
            "stale_or_missing_fact": "clarify_or_handoff_without_guessing",
        },
    }


def enrich_customer_service_tool_result(
    tool_result: dict[str, Any],
) -> dict[str, Any]:
    tool_name = str(tool_result.get("tool_name") or "")
    if tool_name not in CUSTOMER_SERVICE_FACT_TOOLS:
        return tool_result
    output = dict(tool_result.get("output") or {})
    output["response_policy"] = build_customer_service_response_policy(
        tool_name,
        output,
    )
    return {**tool_result, "output": output}


def validate_customer_service_draft(
    draft: str,
    tool_result: dict[str, Any],
    *,
    question: str | None = None,
    approved_answers: list[str] | None = None,
) -> tuple[bool, str]:
    """Validate execution-fact disclosure without reinterpreting user intent."""

    tool_name = str(tool_result.get("tool_name") or "")
    verified_business_action = verified_customer_service_business_action(tool_result)
    if not business_action_completion_claim_is_authorized(
        draft,
        verified_business_action,
        question=question,
    ):
        return False, "customer_service_unverified_business_action_claim"
    if delivery_time_claim_requires_support(
        draft,
        question=question,
    ) and not customer_service_delivery_claim_authorized(
        draft,
        tool_result,
        approved_answers=approved_answers,
        question=question,
    ):
        return False, "customer_service_unverified_delivery_commitment"
    operational_draft = _expand_operational_subject_carryover(
        draft,
        question=question,
    )
    sales_claim = _claims_sales_operational_fact(operational_draft)
    after_sales_claim = _claims_after_sales_operational_fact(operational_draft)
    if tool_name not in CUSTOMER_SERVICE_FACT_TOOLS:
        if sales_claim or after_sales_claim:
            if _operational_write_claims_are_authorized(
                operational_draft,
                verified_business_action,
                question=question,
            ):
                return True, "customer_service_output_policy_not_applicable"
            return False, "customer_service_unverified_operational_status_claim"
        return True, "customer_service_output_policy_not_applicable"
    if (
        tool_name == "get_customer_sales_facts" and after_sales_claim
    ) or (
        tool_name == "get_customer_after_sales_facts" and sales_claim
    ):
        return False, "customer_service_unverified_operational_status_claim"
    if (
        tool_result.get("status") != "success"
        or tool_result.get("postcondition_met") is not True
    ):
        return False, "customer_service_fact_not_verified"
    output = tool_result.get("output") or {}
    policy = output.get("response_policy") or build_customer_service_response_policy(
        tool_name,
        output,
    )
    if policy.get("facts_usable_for_response") is False:
        return False, "customer_service_fact_blocked"
    if policy.get("must_display_data_as_of"):
        data_as_of = str(policy.get("data_as_of") or "")
        date = data_as_of.partition("T")[0]
        normalized = draft.replace(" ", "")
        if data_as_of not in draft and (not date or date not in normalized):
            return False, "customer_service_data_as_of_required"
    if (
        policy.get("current_claims_allowed") is False
        and not policy.get("data_as_of")
        and not _has_asserted_claim(draft, _STALE_CURRENT_CLAIM)
    ):
        return False, "customer_service_data_as_of_required"
    if policy.get("current_claims_allowed") is False and _has_asserted_claim(
        draft,
        _STALE_CURRENT_CLAIM,
    ):
        return False, "customer_service_stale_current_claim"
    if tool_name == "get_customer_sales_facts":
        facts = output.get("facts") or {}
        product = facts.get("product") or {}
        inventory = facts.get("inventory") or {}
        if _status_claim_mismatch(
            draft,
            {str(product.get("status") or "")},
            _PRODUCT_STATUS_CLAIMS,
        ):
            return False, "customer_service_product_status_mismatch"
        if inventory.get("state") == "missing" and _MISSING_INVENTORY_ZERO_CLAIM.search(
            draft
        ):
            return False, "customer_service_missing_inventory_fabricated"
        if _INBOUND_INVENTORY_CLAIM.search(draft):
            return False, "customer_service_inbound_inventory_internal"
        if _WAREHOUSE_DETAIL_CLAIM.search(draft):
            return False, "customer_service_warehouse_detail_internal"
        if _mentions_inventory_quantity(
            draft,
            inventory.get("available_quantity"),
            label="available",
        ) and not _explicit_inventory_quantity_requested(question or ""):
            return False, "customer_service_exact_inventory_not_requested"
    if tool_name == "get_customer_after_sales_facts":
        facts = output.get("facts") or {}
        order = facts.get("order") or {}
        logistics = facts.get("logistics") or {}
        if order.get("state") == "missing" and (
            _has_asserted_claim(operational_draft, _ORDER_STATE_CLAIM)
            or _has_asserted_claim(operational_draft, _PAYMENT_STATE_CLAIM)
        ):
            return False, "customer_service_missing_order_fabricated"
        if logistics.get("state") == "missing" and _has_asserted_claim(
            operational_draft,
            _LOGISTICS_STATE_CLAIM,
        ):
            return False, "customer_service_missing_logistics_fabricated"
        if not facts.get("after_sales") and _has_asserted_claim(
            operational_draft,
            _AFTER_SALES_STATE_CLAIM,
        ):
            return False, "customer_service_missing_after_sales_fabricated"
        if _status_claim_mismatch(
            operational_draft,
            {str(order.get("order_status") or "")},
            _ORDER_STATUS_CLAIMS,
        ):
            return False, "customer_service_order_status_mismatch"
        if _status_claim_mismatch(
            operational_draft,
            {str(order.get("payment_status") or "")},
            _PAYMENT_STATUS_CLAIMS,
        ):
            return False, "customer_service_payment_status_mismatch"
        if _status_claim_mismatch(
            operational_draft,
            {str(logistics.get("status") or "")},
            _LOGISTICS_STATUS_CLAIMS,
        ):
            return False, "customer_service_logistics_status_mismatch"
        if _status_claim_mismatch(
            operational_draft,
            {
                str(item.get("status") or "")
                for item in facts.get("after_sales") or []
            },
            _AFTER_SALES_STATUS_CLAIMS,
        ):
            return False, "customer_service_after_sales_status_mismatch"
        if _status_claim_mismatch(
            operational_draft,
            {
                str(order.get("order_status") or ""),
                str(logistics.get("status") or ""),
            },
            _UNSCOPED_FULFILLMENT_STATUS_CLAIMS,
        ):
            return False, "customer_service_fulfillment_status_mismatch"
    return True, "customer_service_output_policy_passed"


def build_customer_service_suggestion(
    state: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    tool_result = state.get("tool_result") or {}
    output = tool_result.get("output") or {}
    provenance = output.get("source_provenance") or {}
    freshness = output.get("freshness") or {}
    fact_evidence = output.get("evidence") or []
    customer_content = (state.get("context_bundle") or {}).get(
        "customer_service_content"
    ) or {}
    scripts = customer_content.get("scripts") or []
    signals = customer_content.get("keyword_signals") or []
    execution_mode = str(state.get("execution_mode") or "live")
    requires_human = state.get("requires_human") is True
    handoff_id = state.get("handoff_id")
    human_task = None
    if requires_human:
        human_task = {
            "required": True,
            "task_id": handoff_id,
            "status": state.get("handoff_status"),
            "persisted": bool(handoff_id),
            "shadow_observation_only": execution_mode == "shadow" and not handoff_id,
        }
    degradation = None
    if requires_human or state.get("model_fallback"):
        degradation = {
            "reason": state.get("route_reason"),
            "requires_human": requires_human,
            "model_fallback": state.get("model_fallback") is True,
        }
    return {
        "contract_version": CUSTOMER_SERVICE_SUGGESTION_VERSION,
        "execution_mode": execution_mode,
        "delivery_status": (
            "suggestion_not_sent" if execution_mode == "shadow" else "runtime_response"
        ),
        "decision": {
            "mode": state.get("decision_mode"),
            "intent": state.get("intent"),
            "risk_level": state.get("risk_level"),
            "reason": state.get("route_reason"),
        },
        "knowledge": {
            "source_ids": [item.get("id") for item in state.get("retrieved", [])],
            "approved_script_ids": [item.get("id") for item in scripts],
            "keyword_signal_ids": [item.get("knowledge_id") for item in signals],
            "keyword_authority": customer_content.get("keyword_authority"),
        },
        "facts": {
            "tool_name": tool_result.get("tool_name"),
            "evidence_ids": [
                item.get("evidence_id")
                for item in fact_evidence
                if item.get("evidence_id")
            ],
            "data_as_of": output.get("data_as_of"),
            "freshness_status": freshness.get("status"),
            "source_type": provenance.get("source_type"),
            "response_policy": output.get("response_policy"),
        },
        "model": {
            "provider": settings.model_provider,
            "name": settings.model_name,
            "enabled": settings.model_enabled,
            "mock_mode": settings.model_mock_mode,
            "fallback": state.get("model_fallback") is True,
        },
        "context_snapshot_id": state.get("context_snapshot_id"),
        "context_evidence_ids": list(state.get("context_evidence_ids") or []),
        "degradation": degradation,
        "human_task": human_task,
    }


def customer_service_content_for_model(
    content: dict[str, Any],
    retrieved: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep governance and matched signals; RAG remains the answer-content path."""

    retrieved_ids = {str(item.get("id")) for item in retrieved}
    scripts = [
        {
            key: item.get(key)
            for key in (
                "id",
                "source",
                "version",
                "intent",
                "risk_level",
                "store_id",
                "sku_id",
                "approved_by",
                "effective_from",
                "effective_to",
            )
        }
        for item in content.get("scripts", [])
        if str(item.get("id")) in retrieved_ids
    ]
    signals = [
        {
            key: item.get(key)
            for key in (
                "knowledge_id",
                "keyword",
                "scenario",
                "risk_level",
                "authority",
                "source",
                "version",
            )
        }
        for item in content.get("keyword_signals", [])
    ]
    return {
        "scripts": scripts,
        "keyword_signals": signals,
        "fast_path_eligible": content.get("fast_path_eligible") is True,
        "fast_path_rule": content.get("fast_path_rule"),
        "keyword_authority": content.get("keyword_authority") or "advisory_only",
        "exclusions": dict(content.get("exclusions") or {}),
    }
