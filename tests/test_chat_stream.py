from __future__ import annotations

import json

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.llm import ModelUnavailableError

from conftest import make_settings


CLIENT_HEADERS = {
    "X-Client-Id": "client-test",
    "X-Client-Key": "test-client-key-12345",
    "X-Subject-Id": "stream-buyer",
}


def stream_events(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data:").strip())
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]


def test_chat_stream_generation_event_sequence(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/stream",
            headers=CLIENT_HEADERS,
            json={"session_id": "sse-generate", "message": "尺码怎么选", "context": {}},
        )

        assert response.status_code == 200
        events = stream_events(response)
        assert events[0]["event"] == "meta"
        assert [event["event"] for event in events[-2:]] == ["citations", "done"]
        assert all(event["event"] == "delta" for event in events[1:-2])
        assert len(events[1:-2]) > 1


def test_chat_stream_meta_citations_and_done_payloads(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/stream",
            headers=CLIENT_HEADERS,
            json={"session_id": "sse-payloads", "message": "尺码怎么选", "context": {}},
        )
        events = stream_events(response)

        assert {
            "session_id",
            "message_id",
            "trace_id",
        } <= events[0].keys()
        citations = next(event for event in events if event["event"] == "citations")
        assert citations["sources"]
        assert {
            "message_id",
            "intent",
            "risk_level",
            "model_fallback",
        } <= events[-1].keys()


def test_chat_stream_handoff_event_reuses_response_fields(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/stream",
            headers=CLIENT_HEADERS,
            json={"session_id": "sse-handoff", "message": "转人工", "context": {}},
        )
        events = stream_events(response)

        assert [event["event"] for event in events] == ["meta", "handoff", "done"]
        assert events[1]["requires_human"] is True
        assert events[1]["handoff_id"]
        assert events[1]["handoff_status"] == "proposed"
        assert events[1]["reason"] == "customer_requested_human"


def test_chat_stream_error_is_immediately_followed_by_done(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))

    def unavailable(_messages):
        raise ModelUnavailableError("provider unavailable")
        yield ""

    app.state.agent.model.stream_generate = unavailable
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/stream",
            headers=CLIENT_HEADERS,
            json={"session_id": "sse-error", "message": "尺码怎么选", "context": {}},
        )
        events = stream_events(response)

        assert [event["event"] for event in events[-2:]] == ["error", "done"]
        assert events[-2] == {
            "event": "error",
            "code": "model_unavailable",
            "message": "model service is temporarily unavailable",
            "retry_advised": True,
        }
        assert events[-1]["model_fallback"] is True


def test_chat_stream_requires_auth_and_uses_sse_media_type(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        unauthorized = client.post(
            "/v1/chat/stream",
            json={"session_id": "sse-auth", "message": "尺码怎么选", "context": {}},
        )
        authorized = client.post(
            "/v1/chat/stream",
            headers=CLIENT_HEADERS,
            json={"session_id": "sse-media", "message": "尺码怎么选", "context": {}},
        )

        assert unauthorized.status_code == 401
        assert authorized.headers["content-type"].startswith("text/event-stream")
        assert all(
            line.startswith("data: ")
            for line in authorized.text.split("\n\n")
            if line
        )
