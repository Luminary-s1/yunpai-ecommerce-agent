from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import httpx

from .config import Settings
from .policy import is_business_action_request
from .decision import extract_json_object


RATE_LIMIT_PROVIDER_CODES = frozenset({"1302", "1305", "1312"})


class ModelError(RuntimeError):
    pass


class ModelUnavailableError(ModelError):
    """Upstream model service was temporarily unreachable or rate limited.

    Distinguished from other model errors so the agent can invite the customer to
    retry instead of consuming a human handoff for an infrastructure hiccup.
    """


class ModelGateway:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        self.settings = settings
        if (
            settings.model_enabled
            and "/api/coding/" in settings.model_base_url.lower()
            and not settings.model_allow_coding_plan
        ):
            raise ModelError(
                "GLM Coding Plan endpoint requires explicit local-test enablement; "
                "set MODEL_ALLOW_CODING_PLAN=true"
            )
        self._client = httpx.Client(
            timeout=settings.model_timeout_seconds,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            transport=transport,
        )

    @property
    def _is_coding_plan_test(self) -> bool:
        return (
            self.settings.model_allow_coding_plan
            and "/api/coding/" in self.settings.model_base_url.lower()
        )

    @property
    def _uses_streaming(self) -> bool:
        # The Coding Plan test endpoint is invoked through Chat Completions,
        # but its long-lived SSE responses are not suitable for this local UI test.
        return self.settings.model_streaming and not self._is_coding_plan_test

    def generate(self, messages: list[dict[str, str]]) -> str:
        return self._generate_content(messages, json_mode=False)

    def stream_generate(self, messages: list[dict[str, str]]) -> Iterator[str]:
        if self.settings.model_mock_mode:
            yield from self._mock_generate(messages)
            return
        if not self.settings.model_enabled:
            raise ModelError("model integration is disabled")
        payload = self._chat_payload(messages, json_mode=False, stream=True)
        yield from self._stream_deltas(payload)

    def generate_json(
        self,
        messages: list[dict[str, str]],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        content = self._generate_content(
            messages,
            json_mode=True,
            timeout_seconds=timeout_seconds,
        )
        try:
            return extract_json_object(content)
        except ValueError as exc:
            raise ModelError(str(exc)) from exc

    def _generate_content(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool,
        timeout_seconds: float | None = None,
    ) -> str:
        if self.settings.model_mock_mode:
            return self._mock_generate(messages)
        if not self.settings.model_enabled:
            raise ModelError("model integration is disabled")

        payload = self._chat_payload(
            messages,
            json_mode=json_mode,
            stream=self._uses_streaming,
        )
        if self._uses_streaming:
            return self._stream_request(payload, timeout_seconds=timeout_seconds)
        data = self._request(payload, timeout_seconds=timeout_seconds)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("model response did not match the chat completions schema") from exc
        if not isinstance(content, str) or not content.strip():
            raise ModelError("model returned empty content")
        return content.strip()

    def _chat_payload(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.model_name,
            "messages": messages,
            "temperature": self.settings.model_temperature,
            "max_tokens": self.settings.model_max_output_tokens,
            "stream": stream,
        }
        if self.settings.model_provider == "glm":
            payload["thinking"] = {
                "type": "enabled" if self.settings.model_thinking_enabled else "disabled"
            }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def health(self) -> tuple[bool, str]:
        if self.settings.model_mock_mode:
            return True, "mock"
        if not self.settings.model_enabled:
            return False, "disabled"
        if not self.settings.model_api_key:
            return False, "api_key_missing"
        return True, "configured"

    def probe(self) -> dict[str, Any]:
        if not self.settings.model_enabled:
            raise ModelError("model integration is disabled")
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "model": self.settings.model_name,
            "messages": [
                {"role": "system", "content": "你是连通性探针。"},
                {"role": "user", "content": "只回复OK"},
            ],
            "temperature": 0,
            "max_tokens": 8,
            "stream": False,
        }
        if self.settings.model_provider == "glm":
            payload["thinking"] = {"type": "disabled"}
        data = self._request(payload)
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return {
            "ok": bool(str(content).strip()),
            "provider": self.settings.model_provider,
            "model": data.get("model", self.settings.model_name),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "usage": data.get("usage", {}),
        }

    def _request(
        self,
        payload: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        attempts = (
            1
            if timeout_seconds is not None
            else self.settings.model_retry_attempts + 1
        )
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._client.post(
                    f"{self.settings.model_base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                    timeout=(
                        timeout_seconds
                        if timeout_seconds is not None
                        else self.settings.model_timeout_seconds
                    ),
                )
                if self._is_retryable(response) and attempt + 1 < attempts:
                    time.sleep(min(0.2 * (attempt + 1), 0.5))
                    continue
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ModelError("model response is not a JSON object")
                return data
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < attempts and isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout)
                ):
                    time.sleep(min(0.2 * (attempt + 1), 0.5))
                    continue
                break
        error_class = ModelUnavailableError if self._is_transient(last_error) else ModelError
        if isinstance(last_error, httpx.HTTPStatusError):
            status = last_error.response.status_code
            code = self._provider_code(last_error.response)
            raise error_class(
                f"model request failed with HTTP {status} (provider code {code})"
            ) from last_error
        raise error_class(f"model request failed: {type(last_error).__name__}") from last_error

    def _stream_request(
        self,
        payload: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        return "".join(
            self._stream_deltas(payload, timeout_seconds=timeout_seconds)
        ).strip()

    def _stream_deltas(
        self,
        payload: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Iterator[str]:
        attempts = (
            1
            if timeout_seconds is not None
            else self.settings.model_retry_attempts + 1
        )
        last_error: Exception | None = None
        for attempt in range(attempts):
            emitted = False
            try:
                with self._client.stream(
                    "POST",
                    f"{self.settings.model_base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                    timeout=(
                        timeout_seconds
                        if timeout_seconds is not None
                        else self.settings.model_timeout_seconds
                    ),
                ) as response:
                    if response.status_code >= 400:
                        # Error responses arrive as a regular JSON body instead of SSE.
                        # The stream must be consumed before the body can be inspected
                        # here or attached to the raised HTTPStatusError.
                        response.read()
                    if self._is_retryable(response) and attempt + 1 < attempts:
                        time.sleep(min(0.2 * (attempt + 1), 0.5))
                        continue
                    response.raise_for_status()
                    has_non_whitespace = False
                    for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw_event = line.removeprefix("data:").strip()
                        if raw_event == "[DONE]":
                            break
                        event = json.loads(raw_event)
                        if "error" in event:
                            code = str(event.get("error", {}).get("code", "unknown"))
                            stream_error_class = (
                                ModelUnavailableError
                                if code in RATE_LIMIT_PROVIDER_CODES
                                else ModelError
                            )
                            raise stream_error_class(
                                f"model stream failed (provider code {code})"
                            )
                        delta = event.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            emitted = True
                            has_non_whitespace = has_non_whitespace or bool(
                                content.strip()
                            )
                            yield content
                    if not has_non_whitespace:
                        raise ModelError("model stream returned empty content")
                    return
            except ModelError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if not emitted and attempt + 1 < attempts and isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout)
                ):
                    time.sleep(min(0.2 * (attempt + 1), 0.5))
                    continue
                break
        error_class = ModelUnavailableError if self._is_transient(last_error) else ModelError
        if isinstance(last_error, httpx.HTTPStatusError):
            status = last_error.response.status_code
            code = self._provider_code(last_error.response)
            raise error_class(
                f"model request failed with HTTP {status} (provider code {code})"
            ) from last_error
        raise error_class(f"model request failed: {type(last_error).__name__}") from last_error

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.model_api_key:
            headers["Authorization"] = f"Bearer {self.settings.model_api_key}"
        return headers

    def _is_transient(self, error: Exception | None) -> bool:
        """Rate limits, upstream 5xx and transport failures can succeed on a retry."""
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            return status >= 500 or (
                status == 429
                and self._provider_code(error.response) in RATE_LIMIT_PROVIDER_CODES
            )
        return isinstance(error, httpx.TransportError)

    def _is_retryable(self, response: httpx.Response) -> bool:
        if response.status_code in {500, 502, 503, 504}:
            return True
        return response.status_code == 429 and self._provider_code(response) in {
            "1305",
            "1312",
        }

    @staticmethod
    def _provider_code(response: httpx.Response) -> str:
        try:
            if not response.is_closed:
                response.read()
            return str(response.json().get("error", {}).get("code", "unknown"))
        except (AttributeError, ValueError, httpx.StreamError, httpx.HTTPError):
            return "unknown"

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _mock_generate(messages: list[dict[str, str]]) -> str:
        context = messages[-1]["content"]
        try:
            task = json.loads(context)
        except (TypeError, json.JSONDecodeError):
            task = {}
        if task.get("task_type") == "intent_classification":
            message = str(task.get("message", ""))
            intent = "chitchat"
            mappings = (
                (
                    "complaint",
                    ("态度", "不满意", "欺骗", "糟糕", "太差", "失望", "恶劣"),
                ),
                (
                    "after_sales",
                    ("坏了", "破损", "没收到", "退钱", "包裹", "补发", "维修", "少件"),
                ),
                (
                    "product_inquiry",
                    ("颜色", "款式", "功能", "适合", "有货", "库存", "介绍", "重量", "容量"),
                ),
            )
            for candidate, keywords in mappings:
                if any(keyword in message for keyword in keywords):
                    intent = candidate
                    break
            # 刻意套上信封：真实的 glm-4.7-flash 就是这么返回的。mock 若只吐出
            # 解析代码期望的完美形状，它验证的就只是作者的假设，而不是依赖的行为。
            return json.dumps(
                {"answer": {"intent": intent, "confidence": 0.82}},
                ensure_ascii=False,
            )
        if '"task_type": "agent_decision"' in context:
            payload = json.loads(context)
            question = str(payload.get("user_question", ""))
            catalog = payload.get("current_tool_catalog", [])
            tool_names = {item.get("name") for item in catalog if isinstance(item, dict)}
            observation = payload.get("latest_observation") or {}
            trusted_context = payload.get("trusted_context") or {}
            if observation.get("postcondition_met") is True:
                decision = {
                    "intent": observation.get("intent", "general"),
                    "mode": "finish",
                    "tool_name": None,
                    "arguments": {},
                    "missing_fields": [],
                    "expected_outcome": None,
                    "response": None,
                    "reason": "verified_tool_result_available",
                    "confidence": 0.95,
                }
            elif any(word in question for word in ("转人工", "人工客服", "真人客服")):
                decision = {
                    "intent": "human", "mode": "handoff", "tool_name": None,
                    "arguments": {}, "missing_fields": [], "expected_outcome": None,
                    "response": None, "reason": "customer_requested_human", "confidence": 0.99,
                }
            elif (
                any(word in question for word in ("我的订单", "查一下订单", "订单状态", "我的物流"))
                and trusted_context.get("authorized") is not True
            ):
                decision = {
                    "intent": "order", "mode": "clarify", "tool_name": None,
                    "arguments": {}, "missing_fields": ["平台订单编号"],
                    "expected_outcome": None, "response": None,
                    "reason": "order_identity_required", "confidence": 0.9,
                }
            elif is_business_action_request(question):
                intent = (
                    "refund"
                    if "退款" in question or "退钱" in question
                    else "after_sales"
                    if any(word in question for word in ("补发", "赔偿", "赔付", "补偿"))
                    else "order"
                )
                preferred = "refund_order" if "退款" in question else "update_order"
                decision = {
                    "intent": intent,
                    "mode": "act",
                    "tool_name": preferred,
                    "arguments": {},
                    "missing_fields": [],
                    "expected_outcome": "business_operation_verified",
                    "response": None,
                    "reason": "business_action_requested",
                    "confidence": 0.9 if preferred in tool_names else 0.65,
                }
            else:
                intent = "general"
                mappings = [
                    (
                        "product",
                        (
                            "尺码",
                            "材质",
                            "安装",
                            "商品",
                            "产品",
                            "保修",
                            "质保",
                            "维修",
                        ),
                    ),
                    ("inventory", ("现货", "库存", "补货")),
                    ("price_promo", ("优惠", "价格", "券", "到手价")),
                    ("refund", ("退款", "到账")),
                    ("return_exchange", ("退货", "换货", "七天")),
                    ("logistics", ("物流", "快递", "签收", "到哪")),
                    ("shipping", ("预售", "发货", "配送时效")),
                    ("payment", ("扣款", "支付", "付款")),
                    ("after_sales", ("少发", "漏发", "错发", "配件", "破损")),
                    ("security", ("验证码", "密码", "诈骗", "可疑")),
                    ("invoice", ("发票", "开票")),
                    ("order", ("订单",)),
                    ("complaint", ("投诉",)),
                ]
                for name, words in mappings:
                    if any(word in question for word in words):
                        intent = name
                        break
                decision = {
                    "intent": intent, "mode": "answer", "tool_name": None,
                    "arguments": {}, "missing_fields": [], "expected_outcome": None,
                    "response": None, "reason": "knowledge_answer", "confidence": 0.8,
                }
            return json.dumps(decision, ensure_ascii=False)
        context_marker = "当前会话的授权业务上下文："
        if context_marker in context:
            raw_context = context.split(context_marker, 1)[1].split(
                "\n\n已验证工具结果：", 1
            )[0]
            try:
                context_package = json.loads(raw_context)
            except ValueError:
                context_package = {}
            candidates = context_package.get("product_advisor", {}).get(
                "candidates", []
            )
            question = context.split("用户问题：", 1)[1].split("\n\n", 1)[0]
            if len(candidates) == 1 and any(
                word in question for word in ("多少钱", "价格", "价钱", "售价")
            ):
                candidate = candidates[0]
                return (
                    f"{candidate['title']} 当前目录价格为 "
                    f"{candidate['sale_price']} {candidate['currency']}，"
                    "实际支付金额请以结算页实时展示为准。"
                )
        marker = "参考知识："
        if marker in context:
            knowledge = context.split(marker, 1)[1].split("\n\n当前会话", 1)[0]
            for line in knowledge.splitlines():
                if line.startswith("答案："):
                    return line.removeprefix("答案：").strip()
        return "当前信息不足，我会为您转人工客服进一步核对。"
