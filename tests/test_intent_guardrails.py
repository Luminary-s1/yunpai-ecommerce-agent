from __future__ import annotations

import pytest

from ecommerce_agent.config import Settings
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


def _decision_state(
    *,
    session_id: str,
    mode: str = "answer",
    confidence: float = 0.9,
    customer_intent: str = "product_inquiry",
) -> dict:
    return {
        "session_id": session_id,
        "normalized_input": "请帮我处理这个问题",
        "context": {},
        "execution_mode": "shadow",
        "customer_intent": customer_intent,
        "decision": {
            "intent": "general",
            "mode": mode,
            "reason": "model_decision",
            "confidence": confidence,
        },
        "tool_result": {"postcondition_met": True} if mode == "finish" else {},
        "react_step": 0,
        "trace": [],
    }


def _node(service: AgentService, name: str):
    return service.graph.get_graph().nodes[name].data


def test_handoff_confidence_threshold_defaults_and_clamps(monkeypatch) -> None:
    monkeypatch.delenv("HANDOFF_CONFIDENCE_THRESHOLD", raising=False)
    assert Settings.from_env().handoff_confidence_threshold == 0.6

    monkeypatch.setenv("HANDOFF_CONFIDENCE_THRESHOLD", "1.4")
    assert Settings.from_env().handoff_confidence_threshold == 1.0

    monkeypatch.setenv("HANDOFF_CONFIDENCE_THRESHOLD", "-0.2")
    assert Settings.from_env().handoff_confidence_threshold == 0.0


@pytest.mark.parametrize("mode", ["answer", "finish"])
def test_low_confidence_answer_or_finish_is_handed_off(tmp_path, mode: str) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        result = _node(service, "decision_gate").invoke(
            _decision_state(
                session_id="low-confidence-session",
                mode=mode,
                confidence=0.5,
            )
        )

        assert result["route"] == "handoff"
        assert result["route_reason"] == "low_confidence_handoff"
    finally:
        service.close()


def test_two_previous_low_quality_answers_force_handoff(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        principal = principal_for(service)
        session_id = service.db.resolve_session(
            tenant_id=principal.tenant_id,
            client_id=principal.client_id,
            external_session_id="consecutive-low-quality",
            subject_hash=principal.subject_hash,
        )
        with service.db._write_lock, service.db.connect() as conn:
            for index, reason in enumerate(("model_unavailable", "no_evidence"), start=1):
                conn.execute(
                    """
                    INSERT INTO messages(
                        id, trace_id, session_id, role, content, route_reason,
                        sources_json, model_fallback, created_at, tenant_id,
                        client_id, redacted
                    ) VALUES (?, ?, ?, 'assistant', ?, ?, '[]', 1, ?, ?, ?, 0)
                    """,
                    (
                        f"low-quality-{index}",
                        f"low-quality-trace-{index}",
                        session_id,
                        "历史低质回复",
                        reason,
                        f"2026-08-05T00:00:0{index}+00:00",
                        principal.tenant_id,
                        principal.client_id,
                    ),
                )

        result = _node(service, "decision_gate").invoke(
            _decision_state(session_id=session_id)
        )

        assert result["route"] == "handoff"
        assert result["route_reason"] == "consecutive_low_quality"
    finally:
        service.close()


def test_low_quality_detection_requires_both_most_recent_assistant_rows(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        principal = principal_for(service)
        session_id = service.db.resolve_session(
            tenant_id=principal.tenant_id,
            client_id=principal.client_id,
            external_session_id="interrupted-low-quality",
            subject_hash=principal.subject_hash,
        )
        with service.db._write_lock, service.db.connect() as conn:
            for index, reason in enumerate(
                ("model_unavailable", "knowledge_answer_allowed", "no_evidence"),
                start=1,
            ):
                conn.execute(
                    """
                    INSERT INTO messages(
                        id, trace_id, session_id, role, content, route_reason,
                        sources_json, model_fallback, created_at, tenant_id,
                        client_id, redacted
                    ) VALUES (?, ?, ?, 'assistant', ?, ?, '[]', 0, ?, ?, ?, 0)
                    """,
                    (
                        f"mixed-quality-{index}",
                        f"mixed-quality-trace-{index}",
                        session_id,
                        "历史回复",
                        reason,
                        f"2026-08-05T00:00:0{index}+00:00",
                        principal.tenant_id,
                        principal.client_id,
                    ),
                )

        result = _node(service, "decision_gate").invoke(
            _decision_state(session_id=session_id)
        )

        assert result["route"] == "answer"
        assert result["route_reason"] == "knowledge_answer_allowed"
    finally:
        service.close()


def test_complaint_is_urgent_and_can_be_automatically_dispatched(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        principal = principal_for(service)
        session_id = service.db.resolve_session(
            tenant_id=principal.tenant_id,
            client_id=principal.client_id,
            external_session_id="complaint-priority",
            subject_hash=principal.subject_hash,
        )
        state = {
            **_decision_state(
                session_id=session_id,
                confidence=0.9,
                customer_intent="complaint",
            ),
            "tenant_id": principal.tenant_id,
            "client_id": principal.client_id,
            "execution_mode": "live",
            "message_id": "complaint-priority-message",
            "trace_id": "complaint-priority-trace",
        }

        gated = _node(service, "decision_gate").invoke(state)

        assert gated["route"] == "handoff"
        assert gated["route_reason"] == "complaint_attention_required"
        assert gated["risk_level"] == "medium"

        handed_off = _node(service, "handoff").invoke({**state, **gated})
        task = service.handoffs.get(
            tenant_id=principal.tenant_id,
            handoff_id=handed_off["handoff_id"],
        )
        assert task.queue_key == "complaints"
        assert task.priority == "urgent"
        assert task.payload["priority_flag"] == "complaint"
        assert task.payload["intent"] == "complaint"

        dispatched = service.handoff_dispatch.run_once(
            worker_id="intent-guardrail-test",
            tenant_id=principal.tenant_id,
            limit=1,
        )
        assert dispatched["assigned"] == 1
        assigned = service.handoffs.get(
            tenant_id=principal.tenant_id,
            handoff_id=task.id,
        )
        assert assigned.assigned_to == service.settings.bootstrap_admin_id
    finally:
        service.close()
