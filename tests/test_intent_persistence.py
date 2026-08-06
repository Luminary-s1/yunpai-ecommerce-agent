from __future__ import annotations

from ecommerce_agent.graph import persist_response
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


def test_persist_response_records_customer_intent_metadata_on_message_pair(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        principal = principal_for(service)
        session_id = service.db.resolve_session(
            tenant_id=principal.tenant_id,
            client_id=principal.client_id,
            external_session_id="intent-persistence",
            subject_hash=principal.subject_hash,
        )
        state = {
            "user_message_id": "user-intent-persistence",
            "message_id": "assistant-intent-persistence",
            "trace_id": "trace-intent-persistence",
            "session_id": session_id,
            "tenant_id": principal.tenant_id,
            "client_id": principal.client_id,
            "normalized_input": "我收到的商品坏了，想申请售后",
            "input_redacted": False,
            "answer": "我来帮您处理售后问题。",
            "intent": "general",
            "customer_intent": "after_sales",
            "intent_confidence": 0.88,
            "intent_method": "model",
            "risk_level": "medium",
            "route_reason": "knowledge_answer_allowed",
            "retrieved": [],
            "model_fallback": False,
            "requires_human": False,
            "trace": [],
        }

        persist_response(state, db=service.db, sops=service.sops)  # type: ignore[arg-type]

        with service.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT role, customer_intent, intent_confidence, intent_method
                FROM messages WHERE trace_id=? ORDER BY role
                """,
                (state["trace_id"],),
            ).fetchall()
        assert [dict(row) for row in rows] == [
            {
                "role": "assistant",
                "customer_intent": "after_sales",
                "intent_confidence": 0.88,
                "intent_method": "model",
            },
            {
                "role": "user",
                "customer_intent": "after_sales",
                "intent_confidence": 0.88,
                "intent_method": "model",
            },
        ]
        page = service.db.paginated_messages(
            session_id,
            None,
            20,
            tenant_id=principal.tenant_id,
            subject_hash=principal.subject_hash,
        )
        assert {
            (
                item["customer_intent"],
                item["intent_confidence"],
                item["intent_method"],
            )
            for item in page["items"]
        } == {("after_sales", 0.88, "model")}
    finally:
        service.close()
