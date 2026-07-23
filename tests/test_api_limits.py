from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app

from conftest import make_settings


def test_client_rate_limit_is_enforced_before_chat(tmp_path) -> None:
    app = create_app(replace(make_settings(tmp_path), rate_limit_requests_per_minute=2))
    headers = {
        "X-Client-Id": "client-test",
        "X-Client-Key": "test-client-key-12345",
        "X-Subject-Id": "rate-buyer",
    }
    with TestClient(app) as client:
        assert client.post(
            "/v1/chat", headers=headers,
            json={"session_id": "rate-1", "message": "你好", "context": {}},
        ).status_code == 200
        assert client.post(
            "/v1/chat", headers=headers,
            json={"session_id": "rate-2", "message": "你好", "context": {}},
        ).status_code == 200
        assert client.post(
            "/v1/chat", headers=headers,
            json={"session_id": "rate-3", "message": "你好", "context": {}},
        ).status_code == 429


def test_request_body_limit_rejects_oversized_context(tmp_path) -> None:
    app = create_app(replace(make_settings(tmp_path), max_request_body_bytes=1024))
    headers = {
        "X-Client-Id": "client-test",
        "X-Client-Key": "test-client-key-12345",
        "X-Subject-Id": "size-buyer",
    }
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "large", "message": "你好", "context": {"shop_policy": "x" * 3000}},
        )
        assert response.status_code == 413

