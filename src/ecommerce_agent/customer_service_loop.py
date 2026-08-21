from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import Settings


CUSTOMER_SERVICE_SUGGESTION_VERSION = "customer-service-suggestion-v1"
CUSTOMER_SERVICE_FACT_TOOLS = frozenset(
    {
        "get_customer_sales_facts",
        "get_customer_after_sales_facts",
    }
)
_EXPLICIT_INVENTORY_QUANTITY_REQUEST = re.compile(
    r"(多少|几)\s*(件|个|台)?|库存\s*(量|数量)|具体\s*(库存|数量)|"
    r"还剩|剩余\s*(多少|几)"
)
_STALE_CURRENT_CLAIM = re.compile(
    r"(目前|当前|现在).{0,8}(有货|现货|库存充足|可下单|可以下单)|"
    r"(可以|可).{0,4}(立即|马上).{0,4}(下单|购买|发货)"
)
_MISSING_INVENTORY_ZERO_CLAIM = re.compile(
    r"(库存|可售|现货|剩余).{0,8}(为|是|有|剩)?\s*(0|零)\s*(件|个|台)?|"
    r"(0|零)\s*(件|个|台).{0,6}(库存|可售|现货|剩余)"
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
            rf"{number}\s*(件|个|台).{{0,6}}(库存|可售|现货|剩余)?",
        )
    return any(re.search(pattern, draft) for pattern in patterns)


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
) -> tuple[bool, str]:
    """Validate execution-fact disclosure without reinterpreting user intent."""

    tool_name = str(tool_result.get("tool_name") or "")
    if tool_name not in CUSTOMER_SERVICE_FACT_TOOLS:
        return True, "customer_service_output_policy_not_applicable"
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
        if _STALE_CURRENT_CLAIM.search(draft):
            return False, "customer_service_stale_current_claim"
    if tool_name == "get_customer_sales_facts":
        inventory = ((output.get("facts") or {}).get("inventory") or {})
        if inventory.get("state") == "missing" and _MISSING_INVENTORY_ZERO_CLAIM.search(
            draft
        ):
            return False, "customer_service_missing_inventory_fabricated"
        if _mentions_inventory_quantity(
            draft,
            inventory.get("inbound_quantity"),
            label="inbound",
        ):
            return False, "customer_service_inbound_inventory_internal"
        if _mentions_inventory_quantity(
            draft,
            inventory.get("available_quantity"),
            label="available",
        ) and not _EXPLICIT_INVENTORY_QUANTITY_REQUEST.search(question or ""):
            return False, "customer_service_exact_inventory_not_requested"
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
