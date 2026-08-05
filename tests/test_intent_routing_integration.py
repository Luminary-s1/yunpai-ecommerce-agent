from __future__ import annotations

from dataclasses import replace

from ecommerce_agent.intent import load_intent_routing, routing_for_intent
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


EXPECTED_NODES = {
    "__end__",
    "__start__",
    "build_decision_context",
    "build_generation_context",
    "clarify",
    "decision_gate",
    "deliberate",
    "execute_tool",
    "generate",
    "handoff",
    "intake",
    "persist",
    "precheck",
    "refine_retrieval",
    "refuse",
    "retrieve",
    "retry_later",
    "tool_gate",
    "verify",
    "verify_tool",
}

EXPECTED_EDGES = {
    ("__start__", "intake", None, False),
    ("build_decision_context", "deliberate", None, True),
    ("build_decision_context", "handoff", None, True),
    ("build_generation_context", "generate", None, True),
    ("build_generation_context", "handoff", None, True),
    ("clarify", "persist", None, False),
    ("decision_gate", "build_generation_context", "finish", True),
    ("decision_gate", "clarify", None, True),
    ("decision_gate", "handoff", None, True),
    ("decision_gate", "refine_retrieval", "answer", True),
    ("decision_gate", "refuse", None, True),
    ("decision_gate", "tool_gate", "act", True),
    ("deliberate", "decision_gate", None, True),
    ("deliberate", "retry_later", None, True),
    ("execute_tool", "verify_tool", None, False),
    ("generate", "verify", None, False),
    ("handoff", "persist", None, False),
    ("intake", "precheck", None, False),
    ("persist", "__end__", None, False),
    ("precheck", "handoff", None, True),
    ("precheck", "refuse", None, True),
    ("precheck", "retrieve", None, True),
    ("refine_retrieval", "build_generation_context", None, False),
    ("refuse", "persist", None, False),
    ("retrieve", "build_decision_context", None, False),
    ("retry_later", "persist", None, False),
    ("tool_gate", "clarify", None, True),
    ("tool_gate", "execute_tool", "execute", True),
    ("tool_gate", "handoff", None, True),
    ("tool_gate", "refuse", None, True),
    ("verify", "handoff", None, True),
    ("verify", "persist", "pass", True),
    ("verify", "retry_later", None, True),
    ("verify_tool", "build_decision_context", "deliberate", True),
    ("verify_tool", "handoff", None, True),
}


def _node(service: AgentService, name: str):
    return service.graph.get_graph().nodes[name].data


def _state_edges(service: AgentService) -> set[tuple[str, str, str | None, bool]]:
    return {
        (edge.source, edge.target, edge.data, edge.conditional)
        for edge in service.graph.get_graph().edges
    }


def test_routing_file_declares_all_controlled_intents() -> None:
    routing = load_intent_routing()

    assert set(routing) == {
        "product_inquiry",
        "after_sales",
        "complaint",
        "chitchat",
    }
    for intent, entry in routing.items():
        assert set(entry) == {"knowledge_intent", "prompt_variant", "sop_intent"}
        assert all(isinstance(value, str) and value for value in entry.values()), intent
        assert routing_for_intent(intent) == entry


def test_precheck_classifies_and_exposes_routing_metadata(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        result = _node(service, "precheck").invoke(
            {"normalized_input": "请问这款多少钱", "context": {}, "trace": []}
        )

        assert result["customer_intent"] == "product_inquiry"
        assert result["intent_confidence"] == 0.95
        assert result["intent_method"] == "rule"
        assert result["intent_error"] is None
        assert result["intent_routing"] == routing_for_intent("product_inquiry")
    finally:
        service.close()


def test_precheck_uses_model_for_rule_miss_when_mock_is_enabled(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        result = _node(service, "precheck").invoke(
            {"normalized_input": "今天心情不错", "context": {}, "trace": []}
        )

        assert result["customer_intent"] == "chitchat"
        assert result["intent_confidence"] == 0.82
        assert result["intent_method"] == "model"
        assert result["intent_error"] is None
    finally:
        service.close()


def test_precheck_disabled_model_does_not_call_gateway(tmp_path) -> None:
    service = AgentService(
        replace(make_settings(tmp_path), model_enabled=False, model_mock_mode=False)
    )
    try:
        def unexpected_call(*_args, **_kwargs):
            raise AssertionError("disabled intent model must not be called")

        service.model.generate_json = unexpected_call  # type: ignore[method-assign]
        result = _node(service, "precheck").invoke(
            {"normalized_input": "今天心情不错", "context": {}, "trace": []}
        )

        assert result["customer_intent"] == "chitchat"
        assert result["intent_method"] == "default"
        assert result["intent_error"] == "model_not_configured"
    finally:
        service.close()


def test_retrieve_uses_configured_knowledge_intent(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    captured: dict[str, object] = {}
    try:
        def retrieve(*_args, **kwargs):
            captured.update(kwargs)
            return []

        service.knowledge.retrieve = retrieve  # type: ignore[method-assign]
        _node(service, "retrieve").invoke(
            {
                "normalized_input": "我想申请退款",
                "context": {},
                "tenant_id": "tenant-test",
                "session_id": "routing-session",
                "trace": [],
                "customer_intent": "after_sales",
                "intent_routing": routing_for_intent("after_sales"),
            }
        )

        assert captured["intent"] == routing_for_intent("after_sales")["knowledge_intent"]
    finally:
        service.close()


def test_prompt_and_sop_variants_are_forwarded_to_model_payload(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        state = {
            "normalized_input": "这款多少钱",
            "context": {},
            "context_bundle": {},
            "retrieved": [],
            "trace": [],
            "session_id": "routing-prompt-session",
            "tenant_id": "tenant-test",
            "react_step": 0,
            "tool_result": {},
            "customer_intent": "product_inquiry",
            "intent_routing": routing_for_intent("product_inquiry"),
        }
        captured: list[dict] = []

        def generate_json(messages, **_kwargs):
            import json

            captured.append(json.loads(messages[-1]["content"]))
            return {
                "intent": "general",
                "mode": "answer",
                "reason": "test",
                "confidence": 0.9,
            }

        service.model.generate_json = generate_json  # type: ignore[method-assign]
        _node(service, "deliberate").invoke(state)

        assert captured[-1]["routing"] == routing_for_intent("product_inquiry")
    finally:
        service.close()


def test_graph_topology_has_no_d15_nodes_or_edges(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        graph = service.graph.get_graph()
        assert set(graph.nodes) == EXPECTED_NODES
        assert _state_edges(service) == EXPECTED_EDGES
    finally:
        service.close()


def test_chat_persists_classification_pair(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        principal = principal_for(service)
        response = service.chat(principal, "routing-persist-session", "这款多少钱")
        internal_session_id = service.db.resolve_session(
            tenant_id=principal.tenant_id,
            client_id=principal.client_id,
            external_session_id="routing-persist-session",
            subject_hash=principal.subject_hash,
        )
        with service.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT role, customer_intent, intent_confidence, intent_method
                FROM messages WHERE session_id=? ORDER BY created_at, rowid
                """,
                (internal_session_id,),
            ).fetchall()

        assert response.message_id
        assert [(row["role"], row["customer_intent"], row["intent_method"]) for row in rows[-2:]] == [
            ("user", "product_inquiry", "rule"),
            ("assistant", "product_inquiry", "rule"),
        ]
    finally:
        service.close()
