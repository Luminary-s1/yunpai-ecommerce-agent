from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from ecommerce_agent.llm import ModelError, ModelGateway, ModelUnavailableError

from conftest import make_settings


class StreamingBody(httpx.SyncByteStream):
    def __init__(self, content: bytes):
        self.content = content

    def __iter__(self):
        yield self.content


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


def test_structured_generation_accepts_a_per_call_timeout(tmp_path) -> None:
    captured_timeout: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_timeout.update(request.extensions["timeout"])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"intent":"chitchat"}'}}]},
        )

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_api_key="test-model-key",
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        gateway.generate_json(
            [{"role": "user", "content": "classify"}],
            timeout_seconds=0.25,
        )
    finally:
        gateway.close()

    assert captured_timeout["connect"] == 0.25
    assert captured_timeout["read"] == 0.25


def test_deepseek_decision_can_disable_thinking_and_bound_output(tmp_path) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"intent":"after_sales","mode":"answer","reason":"enough evidence"}'
                        }
                    }
                ]
            },
        )

    settings = replace(
        make_settings(tmp_path),
        model_provider="deepseek",
        model_name="deepseek-v4-flash",
        model_enabled=True,
        model_mock_mode=False,
        model_api_key="test-model-key",
        model_max_output_tokens=1600,
        model_thinking_enabled=True,
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        decision = gateway.generate_json(
            [{"role": "user", "content": "decide"}],
            timeout_seconds=15.0,
            max_tokens=300,
            thinking_enabled=False,
        )
    finally:
        gateway.close()

    assert decision["mode"] == "answer"
    assert captured["thinking"] == {"type": "disabled"}
    assert captured["max_tokens"] == 300
    assert captured["response_format"] == {"type": "json_object"}


def test_deepseek_text_generation_keeps_provider_thinking_default(tmp_path) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "已按证据回复。"}}]},
        )

    settings = replace(
        make_settings(tmp_path),
        model_provider="deepseek",
        model_name="deepseek-v4-flash",
        model_enabled=True,
        model_mock_mode=False,
        model_api_key="test-model-key",
        model_thinking_enabled=False,
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        assert gateway.generate([{"role": "user", "content": "answer"}]) == "已按证据回复。"
    finally:
        gateway.close()

    assert "thinking" not in captured


def test_structured_generation_retries_malformed_json(tmp_path) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        content = (
            '{"intent":"shipping","mode":"answer","reason":"ok"}\n'
            "extra explanation"
            if attempts == 1
            else '{"intent":"shipping","mode":"answer","reason":"ok"}'
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_streaming=False,
        model_retry_attempts=1,
        model_api_key="test-model-key",
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        decision = gateway.generate_json([{"role": "user", "content": "decide"}])
    finally:
        gateway.close()

    assert decision["intent"] == "shipping"
    assert attempts == 2


def test_bounded_structured_generation_does_not_retry_connect_timeout(tmp_path) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("classifier deadline", request=request)

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_streaming=False,
        model_api_key="test-model-key",
        model_retry_attempts=2,
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelUnavailableError, match="ConnectTimeout"):
            gateway.generate_json(
                [{"role": "user", "content": "classify"}],
                timeout_seconds=0.02,
            )
    finally:
        gateway.close()

    assert attempts == 1


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


def test_empty_reasoning_only_stream_falls_back_to_non_stream_response(tmp_path) -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["stream"]:
            body = (
                'data: {"choices":[{"delta":{"reasoning_content":"internal"}}]}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(
                200, text=body, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "可见答案"}}]},
        )

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_api_key="test-model-key",
        model_streaming=True,
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        assert gateway.generate([{"role": "user", "content": "回答"}]) == "可见答案"
    finally:
        gateway.close()

    assert [item["stream"] for item in requests] == [True, False]


def test_empty_non_stream_response_uses_bounded_retry_budget(tmp_path) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": ""}}]},
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "恢复答案"}}]},
        )

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_streaming=False,
        model_api_key="test-model-key",
        model_retry_attempts=1,
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        assert gateway.generate([{"role": "user", "content": "回答"}]) == "恢复答案"
    finally:
        gateway.close()
    assert attempts == 2


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


def test_streaming_rate_limit_is_surfaced_as_unavailable_error(tmp_path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"content-type": "application/json"},
            stream=StreamingBody(
                b'{"error":{"code":"1302","message":"concurrency limit reached"}}'
            ),
        )

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_api_key="test-model-key",
        model_streaming=True,
        model_retry_attempts=0,
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelUnavailableError) as captured:
            gateway.generate_json([{"role": "user", "content": "decide"}])
        message = str(captured.value)
        assert "HTTP 429" in message
        assert "1302" in message
        assert "concurrency limit reached" not in message
    finally:
        gateway.close()


def test_streaming_account_error_is_not_marked_temporarily_unavailable(tmp_path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"content-type": "application/json"},
            stream=StreamingBody(b'{"error":{"code":"1113","message":"account overdue"}}'),
        )

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_api_key="test-model-key",
        model_streaming=True,
        model_retry_attempts=0,
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError) as captured:
            gateway.generate([{"role": "user", "content": "在吗"}])
        assert type(captured.value) is ModelError
        assert "HTTP 429" in str(captured.value)
        assert "1113" in str(captured.value)
        assert "account overdue" not in str(captured.value)
    finally:
        gateway.close()


def test_streaming_upstream_error_body_is_read_before_retry(tmp_path) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                503,
                headers={"content-type": "application/json"},
                stream=StreamingBody(b'{"error":{"code":"1113"}}'),
            )
        body = 'data: {"choices":[{"delta":{"content":"已恢复"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_api_key="test-model-key",
        model_streaming=True,
        model_retry_attempts=1,
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        assert gateway.generate([{"role": "user", "content": "在吗"}]) == "已恢复"
        assert attempts == 2
    finally:
        gateway.close()


def test_stream_generate_mock_yields_multiple_deltas_matching_generate(tmp_path) -> None:
    gateway = ModelGateway(make_settings(tmp_path))
    messages = [{"role": "user", "content": "没有匹配知识"}]
    try:
        expected = gateway.generate(messages)
        deltas = list(gateway.stream_generate(messages))
        assert len(deltas) > 1
        assert "".join(deltas) == expected
    finally:
        gateway.close()


def test_mock_decision_covers_generic_chitchat_and_pending_complaint_routes(tmp_path) -> None:
    gateway = ModelGateway(make_settings(tmp_path))
    try:
        complaint = gateway.generate_json(
            [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task_type": "agent_decision",
                            "user_question": "收到的商品外壳破损怎么处理",
                            "current_tool_catalog": [],
                            "trusted_context": {},
                            "latest_observation": {},
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            ]
        )
        chitchat = gateway.generate_json(
            [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task_type": "agent_decision",
                            "user_question": "今天天气不错",
                            "current_tool_catalog": [],
                            "trusted_context": {},
                            "latest_observation": {},
                        },
                        ensure_ascii=False,
                    ),
                }
            ]
        )
    finally:
        gateway.close()

    assert complaint["mode"] == "handoff"
    assert complaint["reason"] == "complaint_requires_human"
    assert chitchat["intent"] == "chitchat"


def test_stream_generate_yields_only_content_deltas(tmp_path) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        body = (
            'data: {"choices":[{"delta":{"reasoning_content":"hidden","content":"逐"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"段"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_api_key="test-model-key",
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        assert list(gateway.stream_generate([{"role": "user", "content": "回答"}])) == [
            "逐",
            "段",
        ]
        assert captured["stream"] is True
    finally:
        gateway.close()


def test_stream_generate_surfaces_midstream_provider_error(tmp_path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = (
            'data: {"choices":[{"delta":{"content":"部分"}}]}\n\n'
            'data: {"error":{"code":"1302","message":"private detail"}}\n\n'
        )
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )

    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        model_api_key="test-model-key",
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelUnavailableError, match="provider code 1302"):
            list(gateway.stream_generate([{"role": "user", "content": "回答"}]))
    finally:
        gateway.close()
