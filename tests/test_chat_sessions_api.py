from __future__ import annotations

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app

from conftest import make_settings


CLIENT_HEADERS = {
    "X-Client-Id": "client-test",
    "X-Client-Key": "test-client-key-12345",
    "X-Subject-Id": "session-buyer",
}
OTHER_SUBJECT_HEADERS = {
    **CLIENT_HEADERS,
    "X-Subject-Id": "other-session-buyer",
}


def test_session_endpoints_require_client_authentication(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        assert client.post("/v1/chat/sessions", json={"session_id": "auth"}).status_code == 401
        assert client.get("/v1/chat/sessions/auth").status_code == 401
        assert client.get("/v1/chat/sessions/auth/messages").status_code == 401
        assert client.delete("/v1/chat/sessions/auth").status_code == 401


def test_create_session_is_idempotent_and_scope_conflicts_return_409(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        first = client.post(
            "/v1/chat/sessions",
            headers=CLIENT_HEADERS,
            json={"session_id": "created-session"},
        )
        second = client.post(
            "/v1/chat/sessions",
            headers=CLIENT_HEADERS,
            json={"session_id": "created-session"},
        )
        conflict = client.post(
            "/v1/chat/sessions",
            headers=OTHER_SUBJECT_HEADERS,
            json={"session_id": "created-session"},
        )

        assert first.status_code == 201
        assert second.status_code == 200
        assert second.json()["id"] == first.json()["id"] == "created-session"
        assert conflict.status_code == 409


def test_get_session_returns_status_timestamps_and_message_count(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        chat = client.post(
            "/v1/chat",
            headers=CLIENT_HEADERS,
            json={"session_id": "session-detail", "message": "尺码怎么选", "context": {}},
        )
        detail = client.get(
            "/v1/chat/sessions/session-detail",
            headers=CLIENT_HEADERS,
        )

        assert chat.status_code == 200
        assert detail.status_code == 200
        assert detail.json()["status"] == "active"
        assert detail.json()["message_count"] == 2
        assert detail.json()["created_at"]
        assert detail.json()["last_seen_at"]


def test_get_session_hides_other_subject_scope_with_404(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/v1/chat/sessions",
            headers=CLIENT_HEADERS,
            json={"session_id": "private-session"},
        )
        hidden = client.get(
            "/v1/chat/sessions/private-session",
            headers=OTHER_SUBJECT_HEADERS,
        )

        assert created.status_code == 201
        assert hidden.status_code == 404


def test_messages_endpoint_returns_default_page(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        client.post(
            "/v1/chat",
            headers=CLIENT_HEADERS,
            json={"session_id": "messages-default", "message": "尺码怎么选", "context": {}},
        )
        response = client.get(
            "/v1/chat/sessions/messages-default/messages",
            headers=CLIENT_HEADERS,
        )

        assert response.status_code == 200
        assert response.json()["limit"] == 20
        assert [item["role"] for item in response.json()["items"]] == [
            "user",
            "assistant",
        ]
        assert response.json()["next_cursor"] is None


def test_messages_composite_cursor_pages_55_rows_without_gaps(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        client.post(
            "/v1/chat/sessions",
            headers=CLIENT_HEADERS,
            json={"session_id": "messages-paged"},
        )
        service = app.state.agent
        with service.db._write_lock, service.db.connect() as conn:
            session_id = conn.execute(
                """
                SELECT id FROM sessions
                WHERE tenant_id=? AND external_session_id=?
                """,
                ("tenant-test", "messages-paged"),
            ).fetchone()[0]
            for index in range(55):
                conn.execute(
                    """
                    INSERT INTO messages(
                        id, trace_id, session_id, role, content, created_at,
                        tenant_id, client_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"page-{index:03d}",
                        f"trace-page-{index:03d}",
                        session_id,
                        "user" if index % 2 == 0 else "assistant",
                        f"message {index}",
                        f"2026-07-30T00:00:{index // 2:02d}+00:00",
                        "tenant-test",
                        "client-test",
                    ),
                )

        ids: list[str] = []
        cursor = None
        page_sizes: list[int] = []
        while True:
            params = {"limit": 20}
            if cursor:
                params["cursor"] = cursor
            response = client.get(
                "/v1/chat/sessions/messages-paged/messages",
                headers=CLIENT_HEADERS,
                params=params,
            )
            assert response.status_code == 200
            payload = response.json()
            page_sizes.append(len(payload["items"]))
            ids.extend(item["id"] for item in payload["items"])
            cursor = payload["next_cursor"]
            if cursor is None:
                break

        assert page_sizes == [20, 20, 15]
        assert ids == [f"page-{index:03d}" for index in range(55)]
        assert len(ids) == len(set(ids))


def test_invalid_cursor_is_ignored_and_open_handoff_blocks_delete(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        client.post(
            "/v1/chat/sessions",
            headers=CLIENT_HEADERS,
            json={"session_id": "closable-session"},
        )
        invalid_cursor = client.get(
            "/v1/chat/sessions/closable-session/messages",
            headers=CLIENT_HEADERS,
            params={"cursor": "not-a-composite-cursor"},
        )
        closed = client.delete(
            "/v1/chat/sessions/closable-session",
            headers=CLIENT_HEADERS,
        )
        client.post(
            "/v1/chat",
            headers=CLIENT_HEADERS,
            json={"session_id": "handoff-session", "message": "转人工", "context": {}},
        )
        blocked = client.delete(
            "/v1/chat/sessions/handoff-session",
            headers=CLIENT_HEADERS,
        )

        assert invalid_cursor.status_code == 200
        assert invalid_cursor.json()["items"] == []
        assert closed.status_code == 200
        assert closed.json()["status"] == "closed"
        assert blocked.status_code == 409
