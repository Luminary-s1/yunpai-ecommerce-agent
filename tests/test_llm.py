from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from ecommerce_agent.llm import ModelError, ModelGateway

from conftest import make_settings

def test_glm_gateway_uses_lightweight_standard_payload(tmp_path) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer test-model-key"
        return httpx.Response(
            200,
            json={
                "model": "glm-4.7",
                "choices": [{"message": {"content": "可以为您说明退货流程。"}}],
                "usage": {"total_tokens": 20},
            },
        )

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_base_url="https://open.bigmodel.cn/api/paas/v4",
        model_name="glm-4.7",
        model_api_key="test-model-key",
        model_thinking_enabled=False,
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        answer = gateway.generate([{"role": "user", "content": "如何退货"}])
        assert answer == "可以为您说明退货流程。"
        assert captured["thinking"] == {"type": "disabled"}
        assert captured["max_tokens"] == settings.model_max_output_tokens
        assert captured["stream"] is False
    finally:
        gateway.close()


def test_structured_decision_requests_json_object_mode(tmp_path) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"intent":"order","mode":"answer","reason":"enough evidence"}'
                        }
                    }
                ]
            },
        )

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_api_key="test-model-key",
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        decision = gateway.generate_json([{"role": "user", "content": "decide"}])
        assert decision["mode"] == "answer"
        assert captured["response_format"] == {"type": "json_object"}
    finally:
        gateway.close()


def test_coding_plan_endpoint_is_rejected_for_application_runtime(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_base_url="https://open.bigmodel.cn/api/coding/paas/v4",
    )
    with pytest.raises(ModelError, match="Coding Plan endpoint"):
        ModelGateway(settings)


def test_coding_plan_endpoint_can_be_explicitly_enabled_for_local_testing(tmp_path) -> None:
    captured: dict = {}

    def handler(_request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(_request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}]},
        )

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_allow_coding_plan=True,
        model_base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        model_api_key="test-model-key",
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        assert gateway.generate([{"role": "user", "content": "test"}]) == "OK"
        assert captured["stream"] is False
    finally:
        gateway.close()


def test_glm_stream_is_assembled_without_exposing_reasoning(tmp_path) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        body = (
            'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
            'data: {"choices":[{"delta":{"reasoning_content":"internal","content":"首次"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"清洗即可"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_api_key="test-model-key",
        model_streaming=True,
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        answer = gateway.generate([{"role": "user", "content": "如何清洗"}])
        assert answer == "首次清洗即可"
        assert "internal" not in answer
        assert captured["stream"] is True
    finally:
        gateway.close()


def test_transient_glm_failure_is_retried_once(tmp_path) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": {"code": "busy"}})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}], "model": "glm-4.7"},
        )

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_api_key="test-model-key",
        model_retry_attempts=1,
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        assert gateway.probe()["ok"] is True
        assert attempts == 2
    finally:
        gateway.close()


def test_provider_error_is_sanitized(tmp_path) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            429,
            json={"error": {"code": "1302", "message": "account detail must stay private"}},
        )

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_api_key="secret-model-key",
        model_retry_attempts=2,
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError) as captured:
            gateway.probe()
        message = str(captured.value)
        assert "HTTP 429" in message
        assert "1302" in message
        assert "secret-model-key" not in message
        assert "account detail" not in message
        assert attempts == 1
    finally:
        gateway.close()


def test_read_timeout_is_not_retried(tmp_path) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("slow model", request=request)

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_api_key="test-model-key",
        model_retry_attempts=2,
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError, match="ReadTimeout"):
            gateway.probe()
        assert attempts == 1
    finally:
        gateway.close()
