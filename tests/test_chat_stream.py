from __future__ import annotations

import json
from dataclasses import replace

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


def test_chat_stream_idempotent_replay_uses_one_delta_without_model_call(
    tmp_path,
) -> None:
    app = create_app(make_settings(tmp_path))
    original = app.state.agent.model.stream_generate
    call_count = 0

    def counted(messages):
        nonlocal call_count
        call_count += 1
        yield from original(messages)

    app.state.agent.model.stream_generate = counted
    headers = {**CLIENT_HEADERS, "Idempotency-Key": "stream-replay-001"}
    payload = {
        "session_id": "sse-replay",
        "message": "尺码怎么选",
        "context": {},
    }
    with TestClient(app) as client:
        first = stream_events(
            client.post("/v1/chat/stream", headers=headers, json=payload)
        )
        replay = stream_events(
            client.post("/v1/chat/stream", headers=headers, json=payload)
        )

        expected = "".join(
            event["text"] for event in first if event["event"] == "delta"
        )
        assert [event["event"] for event in replay] == ["meta", "delta", "done"]
        assert replay[1]["text"] == expected
        assert replay[0]["message_id"] == first[0]["message_id"]
        assert call_count == 1
        with app.state.agent.db.connect() as conn:
            assistant_count = conn.execute(
                """
                SELECT COUNT(*) FROM messages m
                JOIN sessions s ON s.id=m.session_id
                WHERE s.external_session_id=? AND m.role='assistant'
                """,
                ("sse-replay",),
            ).fetchone()[0]
        assert assistant_count == 1


def test_chat_stream_no_hit_matches_non_stream_fallback(tmp_path) -> None:
    settings = replace(make_settings(tmp_path), rag_min_score=1.1)
    app = create_app(settings)
    with TestClient(app) as client:
        expected = client.post(
            "/v1/chat",
            headers=CLIENT_HEADERS,
            json={
                "session_id": "no-hit-sync",
                "message": "火星配送规则",
                "context": {},
            },
        ).json()
        events = stream_events(
            client.post(
                "/v1/chat/stream",
                headers=CLIENT_HEADERS,
                json={
                    "session_id": "no-hit-stream",
                    "message": "火星配送规则",
                    "context": {},
                },
            )
        )

        answer = "".join(
            event["text"] for event in events if event["event"] == "delta"
        )
        assert answer == expected["answer"]
        assert events[-1]["model_fallback"] == expected["model_fallback"] is True
        assert all(event["event"] != "citations" for event in events)


def test_chat_stream_model_disabled_makes_no_external_request(
    tmp_path,
    monkeypatch,
) -> None:
    settings = replace(
        make_settings(tmp_path),
        model_enabled=False,
        model_mock_mode=False,
    )
    app = create_app(settings)
    external_calls = 0

    def unexpected_request(*_args, **_kwargs):
        nonlocal external_calls
        external_calls += 1
        raise AssertionError("model-disabled path attempted an external request")

    monkeypatch.setattr(app.state.agent.model._client, "post", unexpected_request)
    with TestClient(app) as client:
        events = stream_events(
            client.post(
                "/v1/chat/stream",
                headers=CLIENT_HEADERS,
                json={
                    "session_id": "sse-model-disabled",
                    "message": "尺码怎么选",
                    "context": {},
                },
            )
        )

        assert external_calls == 0
        assert events[-1]["event"] == "done"
        assert events[-1]["model_fallback"] is True


def test_session_messages_post_streams_with_path_session_id(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/sessions/sse-path/messages",
            headers=CLIENT_HEADERS,
            json={"message": "尺码怎么选", "context": {}},
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = stream_events(response)
        assert events[0]["session_id"] == "sse-path"
        assert [event["event"] for event in events[-2:]] == ["citations", "done"]
