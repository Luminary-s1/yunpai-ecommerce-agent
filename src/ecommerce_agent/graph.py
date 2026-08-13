from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from .config import Settings
from .context_builder import ContextBuilder
from .database import Database, utc_now
from .decision import AgentDecision
from .handoff import HandoffService
from .intent import classify, routing_for_intent
from .llm import ModelError, ModelGateway, ModelUnavailableError
from .policy import (
    asks_for_internal_identifier,
    customer_facing_missing_fields,
    is_business_action_request,
    precheck_request,
    review_output,
    sanitize_context,
)
from .prompts import (
    DECISION_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_decision_messages,
    build_messages,
)
from .rag import KnowledgeBase
from .schemas import AgentState
from .sops import SopService
from .text_utils import normalize_text, redact_sensitive
from .tokens import count_tokens, truncate_history
from .tools import ToolExecutionContext, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from .knowledge_engine.memory_service import KnowledgeMemoryService


MODEL_UNAVAILABLE_HANDOFF_ANSWER = (
    "当前无法可靠完成自动处理，我会把现有信息和执行记录转给人工客服。"
)


def _map_scene(intent: str) -> str:
    """按意图映射生成阶段场景（M3 四套 Prompt 接入）。

    return/refund/return_exchange/after_sales → aftersale_policy
    product → product_recommend
    competitor → competitor_analysis
    其余 → customer_service
    """
    intent = (intent or "").lower()
    if intent in {"return", "refund", "return_exchange", "after_sales", "invoice"}:
        return "aftersale_policy"
    if intent in {"product", "inventory", "price_promo"}:
        return "product_recommend"
    if "competitor" in intent:
        return "competitor_analysis"
    return "customer_service"


# 连续低质回复判定集合（对齐 origin/main：连续 2 次低质 → 强制转人工）
LOW_QUALITY_ROUTE_REASONS = frozenset(
    {"model_unavailable", "low_confidence_handoff", "no_evidence"}
)


def _bounded_product_context_ready(state: AgentState) -> bool:
    """唯一候选商品 + 产品咨询场景：决策收敛为单步（对齐 origin/main，merge 时丢失）。

    满足时 deliberate 用空工具目录、max_steps=1、planning_constraint=bounded_product_answer，
    且模型不得进入 observe/act 循环（否则唯一候选商品的答案计划会失控）。
    """
    if state.get("react_step") != 0:
        return False
    if state.get("customer_intent") != "product_inquiry":
        return False
    if state.get("context_readiness") != "ready" or not state.get("retrieved"):
        return False
    advisor = (state.get("context_bundle") or {}).get("product_advisor") or {}
    candidates = advisor.get("candidates") or []
    return isinstance(candidates, list) and len(candidates) == 1 and bool(candidates[0])


def verify_response(state: AgentState) -> dict[str, Any]:
    evidence = " ".join(document["answer"] for document in state["retrieved"])
    evidence += " " + json.dumps(state["context_bundle"], ensure_ascii=False)
    passed, reason = review_output(state["draft"], evidence)
    verified_result = state.get("tool_result", {}).get("postcondition_met") is True
    if state.get("model_retry_advised") and not verified_result:
        return {
            "review_route": "retry_later",
            "trace": [*state["trace"], "verify:model_temporarily_unavailable"],
        }
    if (state["model_fallback"] and not verified_result) or not passed:
        return {
            "answer": (
                state["draft"]
                if state["model_fallback"]
                else "为避免给出未经核实的承诺，我会将这个问题转给人工客服。"
            ),
            "requires_human": True,
            "review_route": "handoff",
            "route_reason": (
                "model_unavailable" if state["model_fallback"] else reason
            ),
            "trace": [
                *state["trace"],
                f"verify:{reason}",
                "postcondition:handoff",
            ],
        }
    return {
        "answer": state["draft"],
        "review_route": "pass",
        "trace": [*state["trace"], "verify:passed", "postcondition:answer"],
    }


def persist_response(
    state: AgentState,
    *,
    db: Database,
    sops: SopService,
) -> dict[str, Any]:
    user_message_id = state.get("user_message_id") or f"msg-{uuid.uuid4().hex}"
    now = utc_now()
    sources = [
        {key: document[key] for key in ("id", "category", "source", "version", "score")}
        for document in state.get("retrieved", [])
    ]
    safe_user, persist_redacted = redact_sensitive(state["normalized_input"])
    user_redacted = bool(state.get("input_redacted")) or persist_redacted
    safe_answer, answer_redacted = redact_sensitive(state["answer"])
    # 护栏脱敏标志并入：检索文档被 inspect_item 脱敏时，answer 已是脱敏文本，
    # redact_sensitive 判定 False，但内容确实被脱敏过 → redacted 应置 1
    if any(doc.get("_guard_sanitized") for doc in state.get("retrieved", [])):
        answer_redacted = True
    with db._write_lock, db.connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO messages(
                id, trace_id, session_id, role, content, intent, risk_level,
                route_reason, sources_json, model_fallback, created_at,
                tenant_id, client_id, redacted, context_snapshot_id,
                customer_intent, intent_confidence, intent_method
            ) VALUES (?, ?, ?, 'user', ?, NULL, NULL, NULL, '[]', 0, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                user_message_id,
                state["trace_id"],
                state["session_id"],
                safe_user,
                now,
                state["tenant_id"],
                state["client_id"],
                int(user_redacted),
                state.get("customer_intent"),
                state.get("intent_confidence"),
                state.get("intent_method"),
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO messages(
                id, trace_id, session_id, role, content, intent, risk_level,
                route_reason, sources_json, model_fallback, created_at,
                tenant_id, client_id, redacted, context_snapshot_id,
                customer_intent, intent_confidence, intent_method
            ) VALUES (?, ?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state["message_id"],
                state["trace_id"],
                state["session_id"],
                safe_answer,
                state["intent"],
                state["risk_level"],
                state["route_reason"],
                json.dumps(sources, ensure_ascii=False),
                int(state["model_fallback"]),
                utc_now(),
                state["tenant_id"],
                state["client_id"],
                int(answer_redacted),
                state.get("context_snapshot_id"),
                state.get("customer_intent"),
                state.get("intent_confidence"),
                state.get("intent_method"),
            ),
        )
        invocation_id = state.get("invocation_id")
        if invocation_id:
            response = {
                "message_id": state["message_id"],
                "trace_id": state["trace_id"],
                "session_id": state.get("external_session_id") or state["session_id"],
                "answer": safe_answer,
                "intent": state["intent"],
                "risk_level": state["risk_level"],
                "requires_human": state["requires_human"],
                "reason": state["route_reason"],
                "sources": sources,
                "model_fallback": state["model_fallback"],
                "handoff_id": state.get("handoff_id"),
                "handoff_status": state.get("handoff_status"),
                "sop_id": (state.get("active_sop") or {}).get("id"),
                "sop_version": (state.get("active_sop") or {}).get("version"),
                "context_snapshot_id": state.get("context_snapshot_id"),
                "context_readiness": state.get("context_readiness"),
                "evidence_ids": state.get("context_evidence_ids", []),
            }
            cursor = conn.execute(
                """
                UPDATE agent_invocations
                SET status='completed', response_json=?, last_error=NULL,
                    updated_at=?, completed_at=?
                WHERE id=? AND tenant_id=? AND status='running'
                """,
                (
                    json.dumps(response, ensure_ascii=False),
                    utc_now(),
                    utc_now(),
                    invocation_id,
                    state["tenant_id"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("agent invocation completion was not persisted")
    db.audit(
        "chat.completed",
        "agent",
        state["message_id"],
        {
            "trace_id": state["trace_id"],
            "intent": state["intent"],
            "decision_mode": state.get("decision_mode"),
            "selected_tool": state.get("selected_tool"),
            "risk_level": state["risk_level"],
            "requires_human": state["requires_human"],
            "handoff_id": state.get("handoff_id"),
            "react_step": state.get("react_step", 0),
            "tool_status": state.get("tool_result", {}).get("status"),
            "sop_id": (state.get("active_sop") or {}).get("id"),
            "sop_version": (state.get("active_sop") or {}).get("version"),
            "context_snapshot_id": state.get("context_snapshot_id"),
            "context_readiness": state.get("context_readiness"),
            "evidence_ids": state.get("context_evidence_ids", []),
            "trace": state["trace"],
        },
        state["tenant_id"],
    )
    active_sop = state.get("active_sop") or {}
    if (
        state.get("execution_mode") != "shadow"
        and active_sop.get("run_id")
        and state["requires_human"]
    ):
        sops.mark_handoff(str(active_sop["run_id"]), state["route_reason"])
    return {"trace": [*state["trace"], "persist"]}


def build_graph(
    *,
    settings: Settings,
    db: Database,
    knowledge: KnowledgeBase,
    model: ModelGateway,
    handoffs: HandoffService,
    tools: ToolRegistry,
    sops: SopService,
    contexts: ContextBuilder,
    memory: KnowledgeMemoryService | None = None,
) -> StateGraph:
    builder = StateGraph(AgentState)

    def execution_context(state: AgentState) -> ToolExecutionContext:
        return ToolExecutionContext(
            tenant_id=state["tenant_id"],
            client_id=state["client_id"],
            session_id=state["session_id"],
            trace_id=state["trace_id"],
            trusted_context=state["context"],
        )

    def context_budgets(question: str, system_prompt: str) -> tuple[int, int]:
        total = int(
            settings.model_context_limit_tokens * settings.context_budget_ratio
        )
        available = max(
            0,
            total - count_tokens(system_prompt) - count_tokens(question),
        )
        knowledge_budget = available * 6 // 10
        return knowledge_budget, available - knowledge_budget

    def budgeted_history(
        state: AgentState,
        system_prompt: str,
    ) -> tuple[list[dict[str, Any]], dict[str, int | bool], int]:
        knowledge_budget, history_budget = context_budgets(
            state["normalized_input"],
            system_prompt,
        )
        history = db.recent_messages(
            state["session_id"],
            settings.session_history_limit,
        )
        selected, meta = truncate_history(
            history,
            budget_tokens=history_budget,
        )
        return selected, meta, knowledge_budget

    def intake(state: AgentState) -> dict[str, Any]:
        message = normalize_text(state["user_input"])
        if len(message) > settings.max_input_chars:
            message = message[: settings.max_input_chars]
        return {
            "normalized_input": message,
            "context": sanitize_context(state.get("context", {})),
            "execution_mode": state.get("execution_mode") or "live",
            "intent": "general",
            "customer_intent": None,
            "intent_confidence": None,
            "intent_method": None,
            "intent_error": None,
            "intent_routing": {},
            "risk_level": "low",
            "route": "deliberate",
            "route_reason": "pending",
            "retrieved": [],
            "draft": "",
            "answer": "",
            "citations": [],
            "requires_human": False,
            "message_id": state.get("message_id") or f"msg-{uuid.uuid4().hex}",
            "trace_id": state.get("trace_id") or f"trace-{uuid.uuid4().hex}",
            "trace": ["intake"],
            "review_route": "pass",
            "model_fallback": False,
            "model_retry_advised": False,
            "handoff_id": None,
            "handoff_status": None,
            "decision": {},
            "decision_mode": "pending",
            "selected_tool": None,
            "tool_arguments": {},
            "tool_result": {},
            "react_step": 0,
            "active_sop": None,
            "sop_step_run_id": None,
            "context_bundle": {},
            "context_snapshot_id": None,
            "context_readiness": "pending",
            "context_evidence_ids": [],
            "context_conflicts": [],
        }

    def precheck(state: AgentState) -> dict[str, Any]:
        decision = precheck_request(state["normalized_input"], state["context"])
        if decision.route != "deliberate":
            intent_routing = routing_for_intent("chitchat")
            return {
                "route": decision.route,
                "route_reason": decision.reason,
                "risk_level": "blocked" if decision.route == "refuse" else "medium",
                "customer_intent": "chitchat",
                "intent_confidence": 0.0,
                "intent_method": "default",
                "intent_error": "precheck_short_circuit",
                "intent_routing": intent_routing,
                "trace": [
                    *state["trace"],
                    "intent:default:chitchat:precheck_short_circuit",
                    f"precheck:{decision.route}",
                ],
            }
        classifier_model = model if (settings.model_enabled or settings.model_mock_mode) else None
        classified = classify(state["normalized_input"], model=classifier_model)
        intent_routing = routing_for_intent(classified.intent)
        complaint = classified.intent == "complaint"
        route = "retrieve"
        risk_level = "medium" if complaint else "low"
        intent_trace = f"intent:{classified.method}:{classified.intent}"
        if classified.error is not None:
            intent_trace += f":{classified.error}"
        return {
            "route": route,
            "route_reason": decision.reason,
            "risk_level": risk_level,
            "customer_intent": classified.intent,
            "intent_confidence": classified.confidence,
            "intent_method": classified.method,
            "intent_error": classified.error,
            "intent_routing": intent_routing,
            "trace": [*state["trace"], intent_trace, f"precheck:{decision.route}"],
        }

    def retrieve(state: AgentState) -> dict[str, Any]:
        # M6 基线：代词轮继承上轮 trusted subject（对齐 origin/main retrieve）
        effective_context = dict(state.get("context") or {})
        previous_subject = contexts.latest_subject(
            state["tenant_id"], state["session_id"]
        )
        for key, value in previous_subject.items():
            effective_context.setdefault(key, value)
        # P1-2 可观测性：记录检索耗时（P0-2：挂 db，检索日志落 SQLite 重启不丢）
        from .knowledge_engine.observability import get_observer
        observer = get_observer(db)
        _retrieve_start = time.monotonic()
        # 意图路由：precheck 已产出 intent_routing，缺失时按 customer_intent 回落（对齐 origin/main）
        intent_routing = state.get("intent_routing") or routing_for_intent(
            state.get("customer_intent") or "chitchat"
        )
        # P0-1 安全护栏：检索前意图门（超范围请求→拒绝/升级，不进检索）
        from .knowledge_engine.security_guard import get_security_guard
        guard = get_security_guard()
        scope_decision = guard.classify_request(state["normalized_input"])
        if not scope_decision.allowed:
            observer.record_search(
                tenant_id=state["tenant_id"],
                store_id=effective_context.get("store_id") or effective_context.get("shop_id") or "",
                query=state["normalized_input"],
                hits=0,
                guard_blocks=0,
                guard_scope_block=True,
                latency_ms=(time.monotonic() - _retrieve_start) * 1000,
            )
            return {
                "route": "refuse" if scope_decision.action == "block" else "handoff",
                "route_reason": f"{scope_decision.reason}:{scope_decision.detail}",
                "risk_level": "high" if scope_decision.action == "block" else "medium",
                "retrieved": [],
                "citations": [],
                "trace": [*state["trace"], f"guard:{scope_decision.reason}"],
            }
        # R4 修复：检索失败可观测（此前异常直接抛出，failures_total 恒为 0）
        try:
            documents = knowledge.retrieve(
                state["normalized_input"],
                top_k=settings.rag_top_k,
                min_score=settings.rag_min_score,
                intent=intent_routing["knowledge_intent"],
                tenant_id=state["tenant_id"],
                store_id=effective_context.get("store_id") or effective_context.get("shop_id"),
                sku_id=effective_context.get("sku_id"),
                rollout_unit=state["session_id"],
            )
        except Exception as exc:
            observer.record_search(
                tenant_id=state["tenant_id"],
                store_id=effective_context.get("store_id") or effective_context.get("shop_id") or "",
                query=state["normalized_input"],
                hits=0,
                failed=True,
                latency_ms=(time.monotonic() - _retrieve_start) * 1000,
                event_type="failure",
            )
            # P0 降级可观测：检索故障不静默降级，落审计（只记类型/阶段，不外泄内部详情）
            db.audit(
                "knowledge.retrieval_failure",
                "graph:retrieve",
                None,
                {"error_type": type(exc).__name__, "stage": "initial"},
                state["tenant_id"],
            )
            return {
                # 检索故障属"无法自动服务"，直接转人工；reason 保持可区分码，
                # 供 /v1/chat 响应与 request_metrics.route_reason 精确标识
                "route": "handoff",
                "route_reason": "knowledge_unavailable",
                "risk_level": "medium",
                "requires_human": True,
                "model_fallback": True,
                "retrieved": [],
                "citations": [],
                "trace": [*state["trace"], f"retrieve:failed:{type(exc).__name__}"],
            }
        # P0-1 安全护栏：检索结果注入/敏感扫描（污染的知识条目不进回答）
        _clean_docs: list[dict[str, Any]] = []
        _guard_blocks = 0
        for doc in documents:
            _doc_dict = doc.to_dict() if hasattr(doc, "to_dict") else dict(doc)
            result = guard.inspect_item(_doc_dict)
            if result.status in ("injected", "sensitive"):
                # 丢弃污染/敏感条目（记录到 trace，不阻塞其他正常条目）
                _guard_blocks += 1
                continue
            if result.status == "sanitized":
                # 护栏已脱敏该条 answer（内容已改），把标志带给 persist，
                # 否则 persist 里 redact_sensitive 扫已脱敏文本返回 False → redacted=0 丢失
                _doc_dict["_guard_sanitized"] = True
            _clean_docs.append(_doc_dict)
        documents = _clean_docs
        # A2：店铺长期记忆并入检索（记忆默认隔离，这里主动召回作为补充证据）
        store_id = state["context"].get("store_id") or state["context"].get("shop_id")
        _memory_recalled = 0
        if memory is not None and store_id:
            try:
                memory_rows = memory.recall(
                    str(store_id),
                    query=state["normalized_input"],
                    limit=settings.rag_top_k,
                    tenant_id=state["tenant_id"],
                )
                _memory_recalled = len(memory_rows)
                for row in memory_rows:
                    _mem_doc = {
                        "id": row["id"],
                        "source": f"memory:{row['knowledge_key']}",
                        "question": row["question"],
                        "answer": row["answer"],
                        "score": 0.0,  # 记忆不参与 RAG 打分（显式召回）
                        "layer": "evolution",
                        # 下游 build_messages/_knowledge_block/SourceItem 需要
                        "category": row["category"] or "店铺记忆",
                        "intent": row["intent"] or "memory",
                        "version": 1,
                    }
                    # A3 修复：记忆召回同样过安全护栏（此前完全绕过扫描）
                    _mem_result = guard.inspect_item(dict(_mem_doc))
                    if _mem_result.status in ("injected", "sensitive"):
                        _guard_blocks += 1
                        continue
                    documents.append(_mem_doc)
            except Exception:
                # B 修复：记忆召回失败不再静默吞掉（记录 trace，仍不阻塞回答）
                state["trace"] = [*state["trace"], "memory:recall_failed"]
        # P1-2 可观测性：记录检索统计
        observer.record_search(
            tenant_id=state["tenant_id"],
            store_id=store_id or "",
            query=state["normalized_input"],
            hits=len(documents),
            guard_blocks=_guard_blocks,
            memory_recalled=_memory_recalled,
            latency_ms=(time.monotonic() - _retrieve_start) * 1000,
        )
        return {
            "route": "build_decision_context",
            "context": effective_context,
            "retrieved": documents,
            "citations": [document["id"] for document in documents],
            "trace": [
                *state["trace"],
                f"retrieve:initial:{len(documents)}",
                f"guard:blocked:{_guard_blocks}",
                f"memory:recalled:{_memory_recalled}",
            ],
        }

    def build_decision_context(state: AgentState) -> dict[str, Any]:
        history = db.recent_messages(state["session_id"], settings.session_history_limit)
        _, history_budget = context_budgets(
            state["normalized_input"],
            DECISION_SYSTEM_PROMPT,
        )
        snapshot = contexts.build(
            tenant_id=state["tenant_id"],
            session_id=state["session_id"],
            trace_id=state["trace_id"],
            stage="decision",
            sequence=state["react_step"],
            question=state["normalized_input"],
            trusted_context=state["context"],
            documents=state.get("retrieved", []),
            sops=sops.catalog_for_context(state["tenant_id"]),
            tool_catalog=tools.catalog_for_model(),
            history=history,
            history_budget_tokens=history_budget,
            tool_result=state.get("tool_result") or None,
            parent_snapshot_id=state.get("context_snapshot_id"),
        )
        route = "handoff" if snapshot.readiness == "handoff_required" else "deliberate"
        reason = (
            "context_evidence_conflict"
            if snapshot.readiness == "handoff_required"
            else "trusted_context_ready"
        )
        history_meta = snapshot.bundle["recent_history_meta"]
        return {
            "route": route,
            "route_reason": reason,
            "context_bundle": snapshot.bundle,
            "context_snapshot_id": snapshot.id,
            "context_readiness": snapshot.readiness,
            "context_evidence_ids": snapshot.evidence_ids,
            "context_conflicts": snapshot.conflicts,
            "trace": [
                *state["trace"],
                (
                    f"context:budget:kept{history_meta['kept']}"
                    f"/dropped{history_meta['dropped']}"
                ),
                f"context:decision:{snapshot.readiness}",
            ],
        }

    def deliberate(state: AgentState) -> dict[str, Any]:
        top_document = state.get("retrieved", [{}])[0] if state.get("retrieved") else None
        if (
            state["react_step"] == 0
            and top_document
            and settings.rag_direct_approved_answer
            and top_document["source"].startswith("evolution:")
            # 仅精确匹配才复用 approved 答案（对齐 origin/main；merge 误把"或"加进来，
            # 导致高分但问题不匹配的文档被错误复用，绕过模型决策）
            and normalize_text(top_document["question"])
            == normalize_text(state["normalized_input"])
        ):
            decision = AgentDecision(
                intent=top_document["intent"],
                mode="answer",
                reason="approved_knowledge_reuse",
                confidence=top_document["score"],
            )
            return {
                "decision": decision.model_dump(),
                "trace": [*state["trace"], "deliberate:approved_knowledge"],
            }

        history, history_meta, knowledge_budget = budgeted_history(
            state,
            DECISION_SYSTEM_PROMPT,
        )
        bounded_product = _bounded_product_context_ready(state)
        messages = build_decision_messages(
            question=state["normalized_input"],
            documents=state.get("retrieved", []),
            context=state["context_bundle"],
            history=history,
            tool_catalog=[] if bounded_product else tools.catalog_for_model(),
            observation=state.get("tool_result") or None,
            step_count=state["react_step"],
            max_steps=1 if bounded_product else settings.max_react_steps,
            knowledge_budget_tokens=knowledge_budget,
            prompt_variant=(state.get("intent_routing") or {}).get("prompt_variant"),
            sop_intent=(state.get("intent_routing") or {}).get("sop_intent"),
            knowledge_intent=(state.get("intent_routing") or {}).get("knowledge_intent"),
            planning_constraint=("bounded_product_answer" if bounded_product else None),
        )
        budget_trace = (
            f"context:budget:kept{history_meta['kept']}"
            f"/dropped{history_meta['dropped']}"
        )
        try:
            decision = AgentDecision.model_validate(
                model.generate_json(
                    messages,
                    timeout_seconds=settings.model_decision_timeout_seconds,
                    max_tokens=settings.model_decision_max_output_tokens,
                    thinking_enabled=settings.model_decision_thinking_enabled,
                )
            )
            trace_step = f"deliberate:model:{decision.mode}"
            fallback = False
        except (ModelError, ValidationError) as exc:
            db.audit(
                "model.decision_failure",
                "system",
                state["trace_id"],
                {"error_type": type(exc).__name__, "error": str(exc)[:300]},
                state["tenant_id"],
            )
            if isinstance(exc, ModelUnavailableError):
                if state.get("tool_result", {}).get("postcondition_met") is True:
                    decision = AgentDecision(
                        intent=state.get("intent", "general"),
                        mode="finish",
                        reason="verified_tool_result_available",
                        confidence=1,
                    )
                    trace_step = "deliberate:verified_result_fallback"
                    fallback = True
                else:
                    # A rate limit or transport hiccup is not a reason to occupy a human
                    # agent; invite the customer to retry and keep the handoff queue for
                    # cases the agent has actually judged to need a person.
                    return {
                        "model_fallback": True,
                        "model_retry_advised": True,
                        "trace": [
                            *state["trace"],
                            budget_trace,
                            "deliberate:model_temporarily_unavailable",
                        ],
                    }
            else:
                decision = AgentDecision(
                    intent=state.get("intent", "general"),
                    mode="handoff",
                    reason="model_unavailable",
                    confidence=0,
                )
                trace_step = "deliberate:fallback"
                fallback = True
        # 唯一候选商品收敛：bounded 场景不允许模型进入 observe/act 循环
        # （对齐 origin/main：merge 时丢失，导致唯一商品答案计划失控）
        if bounded_product and decision.mode in {"observe", "act"}:
            decision = AgentDecision(
                intent=decision.intent,
                mode="handoff",
                reason="bounded_product_answer_required",
                confidence=decision.confidence,
            )
            trace_step = "deliberate:bounded_product:handoff"
        elif bounded_product:
            trace_step = f"deliberate:bounded_product:{decision.mode}"
        return {
            "decision": decision.model_dump(),
            "model_fallback": fallback,
            "trace": [*state["trace"], budget_trace, trace_step],
        }

    def decision_gate(state: AgentState) -> dict[str, Any]:
        decision = AgentDecision.model_validate(state["decision"])
        decision_payload = decision.model_dump()
        route = decision.mode
        reason = decision.reason
        risk_level = "high" if route == "act" else "medium" if route in {"observe", "handoff"} else "low"
        if route == "refuse":
            risk_level = "blocked"
        if state["react_step"] >= settings.max_react_steps and route not in {
            "finish",
            "handoff",
            "refuse",
        }:
            route = "handoff"
            reason = "react_step_limit_reached"
        if state.get("tool_result") and not state["tool_result"].get("postcondition_met"):
            if route in {"answer", "finish"}:
                route = "handoff"
                reason = "tool_result_not_verified"
        if route == "answer":
            reason = "knowledge_answer_allowed"
        elif route == "clarify":
            reason = "llm_clarification_required"
        elif route == "finish":
            if not state.get("tool_result", {}).get("postcondition_met"):
                route = "handoff"
                reason = "verified_tool_result_missing"
            else:
                reason = "verified_tool_result_complete"
        business_action = is_business_action_request(state["normalized_input"])
        if business_action and route != "refuse":
            risk_level = "high"
        if business_action and route not in {"act", "handoff", "refuse", "finish"}:
            route = "handoff"
            reason = "business_action_requires_verified_execution"
        # M6 基线：低置信度 answer/finish → 转人工（对齐 origin/main decision_gate）
        if (
            decision.confidence < settings.handoff_confidence_threshold
            and route in {"answer", "finish"}
            and decision.reason != "approved_knowledge_reuse"
        ):
            route = "handoff"
            reason = "low_confidence_handoff"
        # 连续 2 次低质回复 → 强制转人工（对齐 origin/main：避免反复低质循环）
        if route not in {"handoff", "refuse"}:
            recent_reasons = db.recent_assistant_route_reasons(state["session_id"], 2)
            if len(recent_reasons) == 2 and all(
                item in LOW_QUALITY_ROUTE_REASONS for item in recent_reasons
            ):
                route = "handoff"
                reason = "consecutive_low_quality"
        shadow = state.get("execution_mode") == "shadow"
        active_sop = None
        if not shadow:
            active_sop = sops.resolve_for_session(
                state["tenant_id"],
                state["session_id"],
                decision.intent,
                create_run=route in {"observe", "act"},
            )
        if route == "act" and shadow:
            route = "handoff"
            reason = "shadow_write_suppressed"
        if route == "act" and active_sop is None:
            route = "handoff"
            reason = "active_sop_required_for_action"
        elif route == "act" and active_sop is not None:
            allowed, sop_reason, missing = sops.validate_action(
                active_sop,
                tool_name=decision.tool_name,
                arguments=decision.arguments,
                context=state["context"],
            )
            if missing:
                route = "clarify"
                reason = sop_reason
                decision_payload["missing_fields"] = missing
            elif not allowed:
                route = "handoff"
                reason = sop_reason
        if (
            route == "finish"
            and active_sop is not None
            and active_sop.get("run_id")
            and active_sop.get("run_status") != "completed"
        ):
            route = "handoff"
            reason = "sop_steps_incomplete"
        return {
            "intent": decision.intent,
            "decision_mode": decision.mode,
            "selected_tool": decision.tool_name,
            "tool_arguments": decision.arguments,
            "route": route,
            "route_reason": reason,
            "risk_level": risk_level,
            "decision": decision_payload,
            "active_sop": active_sop,
            "trace": [*state["trace"], f"decision_gate:{route}"],
        }

    def refine_retrieval(state: AgentState) -> dict[str, Any]:
        # P0-1 修复：精化检索与初始检索同样过安全护栏 + 可观测（此前完全绕过）
        from .knowledge_engine.observability import get_observer
        from .knowledge_engine.security_guard import get_security_guard
        observer = get_observer(db)
        guard = get_security_guard()
        _start = time.monotonic()
        # R1 修复：精化检索前复查意图门（此前绕过，block 请求仍会重新检索）
        _scope = guard.classify_request(state["normalized_input"])
        if not _scope.allowed:
            observer.record_search(
                tenant_id=state["tenant_id"],
                store_id=state["context"].get("store_id") or state["context"].get("shop_id") or "",
                query=state["normalized_input"],
                hits=0,
                guard_scope_block=True,
                latency_ms=(time.monotonic() - _start) * 1000,
            )
            return {
                "route": "refuse" if _scope.action == "block" else "handoff",
                "route_reason": f"{_scope.reason}:{_scope.detail}",
                "risk_level": "high" if _scope.action == "block" else "medium",
                "retrieved": [],
                "citations": [],
                "trace": [*state["trace"], f"guard:{_scope.reason}:refined"],
            }
        # R4 修复：精化检索失败也可观测
        try:
            documents = knowledge.retrieve(
                state["normalized_input"],
                top_k=settings.rag_top_k,
                min_score=settings.rag_min_score,
                intent=state["intent"],
                tenant_id=state["tenant_id"],
                store_id=state["context"].get("store_id") or state["context"].get("shop_id"),
                sku_id=state["context"].get("sku_id"),
                rollout_unit=state["session_id"],
            )
        except Exception as exc:
            observer.record_search(
                tenant_id=state["tenant_id"],
                store_id=state["context"].get("store_id") or state["context"].get("shop_id") or "",
                query=state["normalized_input"],
                hits=0,
                failed=True,
                latency_ms=(time.monotonic() - _start) * 1000,
                event_type="failure",
                source="graph_refine",
            )
            return {
                "route": "build_generation_context",
                "retrieved": [],
                "citations": [],
                "trace": [*state["trace"], f"retrieve:refined:failed:{type(exc).__name__}"],
            }
        # 安全护栏：注入/敏感条目过滤（与 retrieve 节点一致）
        _clean: list[dict[str, Any]] = []
        _guard_blocks = 0
        for doc in documents:
            _doc_dict = doc.to_dict() if hasattr(doc, "to_dict") else dict(doc)
            result = guard.inspect_item(_doc_dict)
            if result.status in ("injected", "sensitive"):
                _guard_blocks += 1
                continue
            if result.status == "sanitized":
                _doc_dict["_guard_sanitized"] = True
            _clean.append(_doc_dict)
        # 可观测：记录精化检索统计
        observer.record_search(
            tenant_id=state["tenant_id"],
            store_id=state["context"].get("store_id") or state["context"].get("shop_id") or "",
            query=state["normalized_input"],
            hits=len(_clean),
            guard_blocks=_guard_blocks,
            latency_ms=(time.monotonic() - _start) * 1000,
            source="graph_refine",
        )
        return {
            "route": "build_generation_context",
            "retrieved": _clean,
            "citations": [document["id"] for document in _clean],
            "trace": [
                *state["trace"],
                f"retrieve:refined:{len(_clean)}",
                f"guard:blocked:{_guard_blocks}",
            ],
        }

    def tool_gate(state: AgentState) -> dict[str, Any]:
        name = state.get("selected_tool") or ""
        requested_mode = state["decision_mode"]
        try:
            tools.validate_selection(
                name=name,
                arguments=state.get("tool_arguments", {}),
                requested_mode=requested_mode,  # type: ignore[arg-type]
                context=execution_context(state),
            )
            sop_step_run_id = None
            active_sop = state.get("active_sop") or {}
            if active_sop.get("run_id"):
                sop_gate = sops.begin_step(
                    tenant_id=state["tenant_id"],
                    run_id=str(active_sop["run_id"]),
                    requested_mode=requested_mode,  # type: ignore[arg-type]
                    tool_name=name,
                    arguments=state.get("tool_arguments", {}),
                    context=state["context"],
                )
                if not sop_gate["allowed"]:
                    missing = sop_gate.get("missing_fields") or []
                    decision = dict(state["decision"])
                    if missing:
                        decision["missing_fields"] = missing
                        route = "clarify"
                    else:
                        route = "handoff"
                    return {
                        "decision": decision,
                        "route": route,
                        "route_reason": sop_gate["reason"],
                        "trace": [*state["trace"], f"sop_step_gate:{route}:{sop_gate['reason']}"],
                    }
                sop_step_run_id = sop_gate["step"]["id"]
            route = "execute"
            reason = "tool_selection_allowed"
        except ValueError as exc:
            error = str(exc)
            if error.startswith("tool_arguments_invalid") or error.startswith(
                "trusted_context_missing"
            ) or error.startswith("idempotency_fields_missing"):
                route = "clarify"
                reason = error
                decision = dict(state["decision"])
                suffix = error.split(":", 1)[1] if ":" in error else "required_information"
                decision["missing_fields"] = [item for item in suffix.split(",") if item]
            else:
                route = "handoff"
                reason = error
                decision = state["decision"]
            return {
                "decision": decision,
                "route": route,
                "route_reason": reason,
                "trace": [*state["trace"], f"tool_gate:{route}:{error}"],
            }
        return {
            "route": route,
            "route_reason": reason,
            "sop_step_run_id": sop_step_run_id,
            "trace": [*state["trace"], "tool_gate:execute"],
        }

    def execute_tool(state: AgentState) -> dict[str, Any]:
        name = state["selected_tool"] or ""
        result: ToolResult
        try:
            spec, arguments = tools.validate_selection(
                name=name,
                arguments=state.get("tool_arguments", {}),
                requested_mode=state["decision_mode"],  # type: ignore[arg-type]
                context=execution_context(state),
            )
            result = tools.execute(
                spec=spec,
                arguments=arguments,
                context=execution_context(state),
            )
            tool_result = {
                "tool_name": name,
                "intent": state["intent"],
                **result.model_dump(),
            }
            trace_step = f"tool_execute:{name}:{result.status}"
        except Exception as exc:
            result = ToolResult(
                status="failed",
                error_code="tool_execution_error",
                retryable=False,
                postcondition_met=False,
            )
            tool_result = {
                "tool_name": name,
                "intent": state["intent"],
                **result.model_dump(),
            }
            trace_step = f"tool_execute:{name}:failed"
            db.audit(
                "tool.execution_failure",
                "system",
                state["trace_id"],
                {"tool": name, "error_type": type(exc).__name__},
                state["tenant_id"],
            )
        active_sop = state.get("active_sop") or {}
        if state.get("sop_step_run_id") and active_sop.get("run_id"):
            sops.record_step_result(
                tenant_id=state["tenant_id"],
                run_id=str(active_sop["run_id"]),
                step_run_id=str(state["sop_step_run_id"]),
                result=result,
            )
        db.audit(
            "tool.executed",
            state["client_id"],
            state["trace_id"],
            {
                "tool": name,
                "status": tool_result["status"],
                "postcondition_met": tool_result["postcondition_met"],
                "react_step": state["react_step"] + 1,
            },
            state["tenant_id"],
        )
        return {
            "tool_result": tool_result,
            "react_step": state["react_step"] + 1,
            "trace": [*state["trace"], trace_step],
        }

    def verify_tool(state: AgentState) -> dict[str, Any]:
        result = state["tool_result"]
        if state["react_step"] >= settings.max_react_steps and not result.get(
            "postcondition_met"
        ):
            route = "handoff"
            reason = "react_step_limit_reached"
        else:
            route = "deliberate"
            reason = (
                "tool_postcondition_verified"
                if result.get("postcondition_met")
                else "tool_observation_requires_decision"
            )
        return {
            "route": route,
            "route_reason": reason,
            "trace": [*state["trace"], f"postcondition:{route}"],
        }

    def build_generation_context(state: AgentState) -> dict[str, Any]:
        history = db.recent_messages(state["session_id"], settings.session_history_limit)
        _, history_budget = context_budgets(
            state["normalized_input"],
            SYSTEM_PROMPT,
        )
        active_sop = state.get("active_sop")
        snapshot = contexts.build(
            tenant_id=state["tenant_id"],
            session_id=state["session_id"],
            trace_id=state["trace_id"],
            stage="generation",
            sequence=state["react_step"],
            question=state["normalized_input"],
            trusted_context=state["context"],
            documents=state.get("retrieved", []),
            sops=[active_sop] if active_sop else [],
            tool_catalog=tools.catalog_for_model(),
            history=history,
            history_budget_tokens=history_budget,
            tool_result=state.get("tool_result") or None,
            parent_snapshot_id=state.get("context_snapshot_id"),
        )
        route = "handoff" if snapshot.readiness == "handoff_required" else "generate"
        history_meta = snapshot.bundle["recent_history_meta"]
        return {
            "route": route,
            "route_reason": (
                "context_evidence_conflict"
                if snapshot.readiness == "handoff_required"
                else state["route_reason"]
            ),
            "context_bundle": snapshot.bundle,
            "context_snapshot_id": snapshot.id,
            "context_readiness": snapshot.readiness,
            "context_evidence_ids": snapshot.evidence_ids,
            "context_conflicts": snapshot.conflicts,
            "trace": [
                *state["trace"],
                (
                    f"context:budget:kept{history_meta['kept']}"
                    f"/dropped{history_meta['dropped']}"
                ),
                f"context:generation:{snapshot.readiness}",
            ],
        }

    def generate(state: AgentState) -> dict[str, Any]:
        verified_result = (
            state.get("tool_result")
            if state.get("tool_result", {}).get("postcondition_met")
            else None
        )
        if not state["retrieved"] and not verified_result:
            return {
                "draft": "当前知识库中没有足够信息，我会为您转人工客服进一步核对。",
                "model_fallback": True,
                "trace": [*state["trace"], "generate:no_evidence"],
            }
        top_document = state["retrieved"][0] if state["retrieved"] else None
        if (
            top_document
            and state["decision"].get("reason") == "approved_knowledge_reuse"
            and settings.rag_direct_approved_answer
            and top_document["source"].startswith("evolution:")
            # 精确匹配才复用（对齐 deliberate 同逻辑）
            and normalize_text(top_document["question"]) == state["normalized_input"]
        ):
            return {
                "draft": top_document["answer"],
                "model_fallback": False,
                "trace": [*state["trace"], "generate:approved_knowledge"],
            }
        history, history_meta, knowledge_budget = budgeted_history(
            state,
            SYSTEM_PROMPT,
        )
        messages = build_messages(
            question=state["normalized_input"],
            documents=state["retrieved"],
            context=state["context_bundle"],
            history=history,
            verified_tool_result=verified_result,
            knowledge_budget_tokens=knowledge_budget,
            prompt_variant=(state.get("intent_routing") or {}).get("prompt_variant"),
        )
        # M3 场景 Prompt 接入：按 intent 映射场景，叠加防幻觉指令（RAG_SCENE_PROMPTS 默认开）
        if settings.rag_scene_prompts and messages and state["retrieved"]:
            scene = _map_scene(state.get("intent") or "")
            try:
                from .knowledge_engine.prompt_templates import render_prompt

                context_text = "\n".join(
                    doc.get("answer") or doc.get("question") or ""
                    for doc in state["retrieved"][:5]
                )
                scene_instructions = render_prompt(
                    scene, context_text, state["normalized_input"]
                )
                messages[0] = {
                    "role": "system",
                    "content": f"{messages[0]['content']}\n\n【本会话场景指令】\n{scene_instructions}",
                }
            except (ValueError, ImportError):
                pass  # 场景注入失败不阻塞回答（保持原 SYSTEM_PROMPT）
        budget_trace = (
            f"context:budget:kept{history_meta['kept']}"
            f"/dropped{history_meta['dropped']}"
        )
        try:
            draft = model.generate(messages)
            fallback = False
            trace_step = "generate:model"
            retry_advised = False
        except ModelError as exc:
            retry_advised = False
            if verified_result:
                draft = "操作已完成，业务系统已经确认处理结果。"
                trace_step = "generate:verified_result_fallback"
            elif isinstance(exc, ModelUnavailableError):
                draft = ""
                trace_step = "generate:model_temporarily_unavailable"
                retry_advised = True
            else:
                draft = "当前模型暂时不可用，我会为您转人工客服，避免给出不准确的信息。"
                trace_step = "generate:fallback"
            fallback = True
            db.audit(
                "model.failure",
                "system",
                state["trace_id"],
                {"error_type": type(exc).__name__, "error": str(exc)[:300]},
                state["tenant_id"],
            )
        return {
            "draft": draft,
            "model_fallback": fallback,
            "model_retry_advised": retry_advised,
            "trace": [*state["trace"], budget_trace, trace_step],
        }

    def verify(state: AgentState) -> dict[str, Any]:
        return verify_response(state)

    def retry_later(state: AgentState) -> dict[str, Any]:
        return {
            "answer": (
                "抱歉，智能客服的模型服务刚刚出现短暂波动，这条消息没能处理完。"
                "请稍等一下再发送一次，我会继续为您跟进；如果希望直接由人工同事接手，回复“转人工”即可。"
            ),
            "requires_human": False,
            "risk_level": "low",
            "route_reason": "model_temporarily_unavailable",
            "model_fallback": True,
            "trace": [*state["trace"], "retry_later", "postcondition:retry_later"],
        }

    def clarify(state: AgentState) -> dict[str, Any]:
        decision = AgentDecision.model_validate(state["decision"])
        missing = customer_facing_missing_fields(decision.missing_fields) or [
            "完成操作所需的信息"
        ]
        fallback = f"为了继续处理，请补充：{'、'.join(missing)}。"
        answer = decision.response or fallback
        if asks_for_internal_identifier(answer):
            # The model drafted a question about SKU/item ids a shopper cannot
            # answer; ask for what they can actually provide instead.
            answer = fallback
        return {
            "answer": answer,
            "requires_human": False,
            "trace": [*state["trace"], "clarify", "postcondition:input_required"],
        }

    def handoff(state: AgentState) -> dict[str, Any]:
        reason = state["route_reason"]
        decision_intent = str(
            state.get("decision", {}).get("intent")
            or state.get("intent")
            or "general"
        )
        model_confirmed_complaint = decision_intent == "complaint"
        if model_confirmed_complaint:
            # 投诉专属文案（对齐 origin/main：此前 merge 丢失，投诉无"抱歉"前缀）
            answer = state.get("decision", {}).get("response") or (
                "很抱歉给您带来困扰。我已将当前问题和必要上下文标记为投诉，"
                "并转交人工客服优先跟进。"
            )
        elif reason == "customer_requested_human":
            answer = "好的，我会将当前问题和必要上下文转给人工客服。请勿发送密码、验证码或银行卡信息。"
        elif reason == "authorized_order_context_missing":
            answer = "这个问题需要核对您的订单信息。我会转人工处理，请只提供平台订单编号，不要发送密码或验证码。"
        elif reason == "tool_not_registered":
            answer = "我已经理解您要办理的业务，但当前环境尚未接入对应的执行工具，我会转人工继续处理。"
        elif reason.startswith("tool_policy_denied"):
            answer = "当前操作未通过已配置的权限或业务规则校验，我会转人工进一步核对。"
        elif reason == "knowledge_unavailable":
            answer = (
                "知识检索服务暂时不可用，当前无法引用知识库；"
                "我会把对话历史和已有信息转给人工客服继续核对。"
            )
        elif reason in {"model_unavailable", "react_step_limit_reached"}:
            answer = MODEL_UNAVAILABLE_HANDOFF_ANSWER
        else:
            decision_response = state.get("decision", {}).get("response")
            answer = decision_response or state.get("answer") or "当前问题存在无法自动消除的不确定性，我会为您转接人工客服。"
        if state.get("execution_mode") == "shadow":
            return {
                "answer": answer,
                "requires_human": True,
                "handoff_id": None,
                "handoff_status": None,
                "trace": [*state["trace"], "shadow_handoff_observed"],
            }
        safe_question, _ = redact_sensitive(state["normalized_input"])
        tool_arguments = state.get("tool_arguments") or {}
        trusted_context = state.get("context") or {}
        order_id = tool_arguments.get("order_id") or trusted_context.get("order_id")
        store_id = (
            tool_arguments.get("store_id")
            or trusted_context.get("store_id")
            or trusted_context.get("shop_id")
        )
        business_context = {}
        if isinstance(order_id, (str, int)) and not isinstance(order_id, bool):
            business_context["order_id"] = normalize_text(str(order_id))[:128]
        if isinstance(store_id, (str, int)) and not isinstance(store_id, bool):
            business_context["store_id"] = normalize_text(str(store_id))[:128]
        handoff_payload = {
            "trace_id": state["trace_id"],
            # 用 decision_intent（永不 KeyError）而非 state["intent"]（对齐 origin/main，
            # 此键在直接调 handoff 节点的测试路径/分类失败路径可能不存在）
            "intent": decision_intent,
            "risk_level": state["risk_level"],
            "question": safe_question,
            "selected_tool": state.get("selected_tool"),
            "react_step": state.get("react_step", 0),
            "context_snapshot_id": state.get("context_snapshot_id"),
            "context_readiness": state.get("context_readiness"),
            "context_conflicts": state.get("context_conflicts", []),
            "business_context": business_context,
        }
        if model_confirmed_complaint:
            handoff_payload["priority_flag"] = "complaint"
        unknown_intent = (
            state.get("intent_method") == "default" and not model_confirmed_complaint
        )
        if unknown_intent:
            # 分类器无法决断（弃权）≠ 闲聊：不进投诉/紧急队列，
            # 但避免落入兜底队列最低 SLA（对齐 origin/main guardrails）
            handoff_payload["priority_flag"] = "intent_unknown"
            handoff_payload["intent_method"] = "default"
            handoff_payload["intent_error"] = state.get("intent_error")
        task = handoffs.create(
            tenant_id=state["tenant_id"],
            session_id=state["session_id"],
            message_id=state["message_id"],
            reason=reason,
            payload=handoff_payload,
            priority="high" if unknown_intent else None,
        )
        return {
            "answer": answer,
            "requires_human": True,
            "handoff_id": task.id,
            "handoff_status": task.status,
            "trace": [*state["trace"], "human_handoff", f"postcondition:handoff_{task.status}"],
        }

    def refuse(state: AgentState) -> dict[str, Any]:
        if state["route_reason"] == "unauthorized_data_request":
            answer = "我只能使用本店已授权的信息，不能提供其他店铺或其他买家的非公开数据。"
        else:
            answer = state.get("decision", {}).get("response") or "我不能更改系统规则、披露内部提示或绕过权限，但可以继续帮助您处理正常的商品、订单和售后问题。"
        return {
            "answer": answer,
            "requires_human": False,
            "trace": [*state["trace"], "refuse", "postcondition:blocked"],
        }

    def persist(state: AgentState) -> dict[str, Any]:
        return persist_response(state, db=db, sops=sops)

    builder.add_node("intake", intake)
    builder.add_node("precheck", precheck)
    builder.add_node("retrieve", retrieve)
    builder.add_node("build_decision_context", build_decision_context)
    builder.add_node("deliberate", deliberate)
    builder.add_node("decision_gate", decision_gate)
    builder.add_node("refine_retrieval", refine_retrieval)
    builder.add_node("tool_gate", tool_gate)
    builder.add_node("execute_tool", execute_tool)
    builder.add_node("verify_tool", verify_tool)
    builder.add_node("build_generation_context", build_generation_context)
    builder.add_node("generate", generate)
    builder.add_node("verify", verify)
    builder.add_node("clarify", clarify)
    builder.add_node("retry_later", retry_later)
    builder.add_node("handoff", handoff)
    builder.add_node("refuse", refuse)
    builder.add_node("persist", persist)

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "precheck")
    builder.add_conditional_edges(
        "precheck",
        lambda state: state["route"],
        {"retrieve": "retrieve", "handoff": "handoff", "refuse": "refuse"},
    )
    # R1 修复：意图门拦截（refuse/handoff）不再被 build_decision_context 无条件覆盖。
    # 此前无条件边 retrieve→build_decision_context，导致意图门 block 路由被重写、拒绝永不生效。
    builder.add_conditional_edges(
        "retrieve",
        lambda state: state["route"],
        {
            "build_decision_context": "build_decision_context",
            "refuse": "refuse",
            "handoff": "handoff",
        },
    )
    builder.add_conditional_edges(
        "build_decision_context",
        lambda state: state["route"],
        {"deliberate": "deliberate", "handoff": "handoff"},
    )
    builder.add_conditional_edges(
        "deliberate",
        lambda state: "retry_later" if state.get("model_retry_advised") else "decision_gate",
        {"decision_gate": "decision_gate", "retry_later": "retry_later"},
    )
    builder.add_conditional_edges(
        "decision_gate",
        lambda state: state["route"],
        {
            "answer": "refine_retrieval",
            "finish": "build_generation_context",
            "clarify": "clarify",
            "observe": "tool_gate",
            "act": "tool_gate",
            "handoff": "handoff",
            "refuse": "refuse",
        },
    )
    # R1 修复：refine_retrieval 的意图门复查结果（refuse/handoff）也应被尊重
    builder.add_conditional_edges(
        "refine_retrieval",
        lambda state: state["route"],
        {
            "build_generation_context": "build_generation_context",
            "refuse": "refuse",
            "handoff": "handoff",
        },
    )
    builder.add_conditional_edges(
        "build_generation_context",
        lambda state: state["route"],
        {"generate": "generate", "handoff": "handoff"},
    )
    builder.add_conditional_edges(
        "tool_gate",
        lambda state: state["route"],
        {
            "execute": "execute_tool",
            "clarify": "clarify",
            "handoff": "handoff",
            "refuse": "refuse",
        },
    )
    builder.add_edge("execute_tool", "verify_tool")
    builder.add_conditional_edges(
        "verify_tool",
        lambda state: state["route"],
        {"deliberate": "build_decision_context", "handoff": "handoff"},
    )
    builder.add_edge("generate", "verify")
    builder.add_conditional_edges(
        "verify",
        lambda state: state["review_route"],
        {"pass": "persist", "handoff": "handoff", "retry_later": "retry_later"},
    )
    builder.add_edge("clarify", "persist")
    builder.add_edge("retry_later", "persist")
    builder.add_edge("handoff", "persist")
    builder.add_edge("refuse", "persist")
    builder.add_edge("persist", END)
    return builder
