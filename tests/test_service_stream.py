from __future__ import annotations

from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


def test_chat_stream_mock_matches_non_stream_answer(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    principal = principal_for(service)
    try:
        expected = service.chat(principal, "stream-baseline", "尺码怎么选")
        events = list(
            service.chat_stream(
                principal,
                "stream-generate",
                "尺码怎么选",
                {},
                idempotency_key=None,
            )
        )

        assert events[0]["event"] == "meta"
        assert events[-1]["event"] == "result"
        assert "".join(
            event["text"] for event in events if event["event"] == "delta"
        ) == expected.answer
        assert events[-1]["response"]["answer"] == expected.answer
    finally:
        service.close()


def test_chat_stream_close_mid_generation_persists_no_assistant_message(
    tmp_path,
) -> None:
    service = AgentService(make_settings(tmp_path))
    principal = principal_for(service)
    iterator = service.chat_stream(
        principal,
        "stream-interrupted",
        "尺码怎么选",
        {},
        idempotency_key=None,
    )
    try:
        assert next(iterator)["event"] == "meta"
        assert next(iterator)["event"] == "delta"
        iterator.close()

        with service.db.connect() as conn:
            assistant_count = conn.execute(
                """
                SELECT COUNT(*) FROM messages m
                JOIN sessions s ON s.id=m.session_id
                WHERE s.external_session_id=? AND m.role='assistant'
                """,
                ("stream-interrupted",),
            ).fetchone()[0]
        assert assistant_count == 0
    finally:
        iterator.close()
        service.close()


def test_chat_stream_non_generation_route_emits_single_complete_result(
    tmp_path,
) -> None:
    service = AgentService(make_settings(tmp_path))
    principal = principal_for(service)
    try:
        events = list(
            service.chat_stream(
                principal,
                "stream-handoff",
                "转人工",
                {},
                idempotency_key=None,
            )
        )

        assert [event["event"] for event in events] == ["meta", "result"]
        assert events[-1]["response"]["requires_human"] is True
        assert events[-1]["response"]["handoff_id"]
    finally:
        service.close()
