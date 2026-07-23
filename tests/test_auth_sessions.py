from __future__ import annotations

from dataclasses import replace

import pytest

from ecommerce_agent.auth import AuthError
from ecommerce_agent.database import SessionScopeError
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


def test_invalid_credentials_are_rejected(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        with pytest.raises(AuthError):
            service.auth.authenticate("client-test", "wrong-key", "buyer-1")
    finally:
        service.close()


def test_session_is_bound_to_authenticated_subject(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        service.chat(principal_for(service, "buyer-a"), "shared-session", "你好")
        with pytest.raises(SessionScopeError):
            service.chat(principal_for(service, "buyer-b"), "shared-session", "你好")
    finally:
        service.close()


def test_request_body_cannot_self_authorize_order_context(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        response = service.chat(
            principal_for(service),
            "order-session",
            "帮我查一下我的订单状态",
            {"authorized": True, "order_status": "已发货"},
        )
        assert response.reason == "llm_clarification_required"
        assert not response.requires_human
        assert "订单编号" in response.answer
    finally:
        service.close()


def test_context_is_sanitized_before_any_checkpoint(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        principal = principal_for(service)
        service.chat(
            principal,
            "checkpoint-safe",
            "你好",
            {"untrusted_field": "CHECKPOINT_SECRET_MARKER", "order_status": "已发货"},
        )
        with service.db.connect() as conn:
            internal = conn.execute(
                "SELECT id FROM sessions WHERE external_session_id='checkpoint-safe'"
            ).fetchone()[0]
        checkpoints = list(
            service.checkpointer.list({"configurable": {"thread_id": internal}})
        )
        assert all("CHECKPOINT_SECRET_MARKER" not in repr(item.checkpoint) for item in checkpoints)
    finally:
        service.close()


def test_capable_upstream_can_supply_read_only_order_context(tmp_path) -> None:
    settings = replace(make_settings(tmp_path), bootstrap_client_can_supply_order_context=True)
    service = AgentService(settings)
    try:
        response = service.chat(
            principal_for(service),
            "trusted-order-session",
            "帮我查一下我的订单状态",
            {"order_status": "已发货"},
        )
        assert response.reason == "knowledge_answer_allowed"
    finally:
        service.close()


def test_model_disabled_mode_never_calls_endpoint_and_hands_off(tmp_path) -> None:
    settings = replace(make_settings(tmp_path), model_mock_mode=False, model_enabled=False)
    service = AgentService(settings)
    try:
        response = service.chat(principal_for(service), "no-model", "尺码怎么选")
        assert response.model_fallback
        assert response.reason == "model_unavailable"
        assert response.handoff_status == "proposed"
    finally:
        service.close()
