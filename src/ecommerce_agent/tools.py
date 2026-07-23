from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


ToolKind = Literal["read", "write"]
ToolStatus = Literal["success", "failed", "uncertain"]


class EmptyToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolResult(BaseModel):
    status: ToolStatus
    output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=128)
    retryable: bool = False
    postcondition_met: bool = False


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    tenant_id: str
    client_id: str
    session_id: str
    trace_id: str
    trusted_context: dict[str, Any]


ToolHandler = Callable[[BaseModel, ToolExecutionContext], ToolResult]
ToolVerifier = Callable[[BaseModel, ToolResult, ToolExecutionContext], bool]
ToolPolicy = Callable[[BaseModel, ToolExecutionContext], str | None]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    kind: ToolKind
    input_model: type[BaseModel]
    handler: ToolHandler
    required_context_fields: tuple[str, ...] = ()
    idempotency_fields: tuple[str, ...] = ()
    timeout_seconds: float = 10.0
    max_retries: int = 0
    verifier: ToolVerifier | None = None
    policy: ToolPolicy | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError("tool name must contain only letters, numbers, and underscores")
        if self.kind == "write" and not self.idempotency_fields:
            raise ValueError("write tools require idempotency_fields")
        if self.kind == "write" and self.verifier is None:
            raise ValueError("write tools require a postcondition verifier")
        if self.timeout_seconds <= 0:
            raise ValueError("tool timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ValueError("tool max_retries cannot be negative")


class ToolRegistry:
    """Typed capability catalog. The model selects tools; the registry authorizes them."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="agent-tool")
        self._closed = False

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def catalog_for_model(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "kind": spec.kind,
                "input_schema": spec.input_model.model_json_schema(),
                "required_context_fields": list(spec.required_context_fields),
            }
            for spec in self._tools.values()
        ]

    def validate_selection(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        requested_mode: Literal["observe", "act"],
        context: ToolExecutionContext,
    ) -> tuple[ToolSpec, BaseModel]:
        spec = self._tools.get(name)
        if spec is None:
            raise ValueError("tool_not_registered")
        if requested_mode == "observe" and spec.kind != "read":
            raise ValueError("observe_cannot_call_write_tool")
        if requested_mode == "act" and spec.kind != "write":
            raise ValueError("act_requires_write_tool")
        missing_context = [
            key for key in spec.required_context_fields if not context.trusted_context.get(key)
        ]
        if missing_context:
            raise ValueError(f"trusted_context_missing:{','.join(missing_context)}")
        try:
            validated = spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            missing = [
                str(item["loc"][-1])
                for item in exc.errors()
                if item.get("type") == "missing" and item.get("loc")
            ]
            suffix = ",".join(missing) if missing else "invalid"
            raise ValueError(f"tool_arguments_invalid:{suffix}") from exc
        if spec.kind == "write":
            payload = validated.model_dump()
            missing_keys = [key for key in spec.idempotency_fields if not payload.get(key)]
            if missing_keys:
                raise ValueError(f"idempotency_fields_missing:{','.join(missing_keys)}")
        if spec.policy:
            denial = spec.policy(validated, context)
            if denial:
                raise ValueError(f"tool_policy_denied:{denial}")
        return spec, validated

    def execute(
        self,
        *,
        spec: ToolSpec,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> ToolResult:
        if self._closed:
            raise RuntimeError("tool_registry_closed")
        attempts = spec.max_retries + 1
        result: ToolResult | None = None
        for attempt in range(attempts):
            future = self._executor.submit(spec.handler, arguments, context)
            try:
                raw_result = future.result(timeout=spec.timeout_seconds)
                result = (
                    raw_result
                    if isinstance(raw_result, ToolResult)
                    else ToolResult.model_validate(raw_result)
                )
                if (
                    spec.kind == "read"
                    and result.status == "failed"
                    and result.retryable
                    and attempt + 1 < attempts
                ):
                    continue
                break
            except FutureTimeoutError:
                future.cancel()
                # A timed-out write may still complete remotely. Retrying it immediately
                # would be unsafe even with an idempotency key.
                if spec.kind == "write":
                    return ToolResult(
                        status="uncertain",
                        error_code="tool_timeout",
                        retryable=False,
                        postcondition_met=False,
                    )
                if attempt + 1 == attempts:
                    return ToolResult(
                        status="failed",
                        error_code="tool_timeout",
                        retryable=True,
                        postcondition_met=False,
                    )
            except Exception:
                if spec.kind == "write":
                    return ToolResult(
                        status="uncertain",
                        error_code="tool_handler_error",
                        retryable=False,
                        postcondition_met=False,
                    )
                if attempt + 1 == attempts:
                    return ToolResult(
                        status="failed",
                        error_code="tool_handler_error",
                        retryable=False,
                        postcondition_met=False,
                    )
        if result is None:
            return ToolResult(
                status="failed",
                error_code="tool_execution_exhausted",
                retryable=False,
                postcondition_met=False,
            )
        postcondition_met = result.status == "success"
        if spec.verifier:
            try:
                postcondition_met = bool(spec.verifier(arguments, result, context))
            except Exception:
                postcondition_met = False
        updates: dict[str, Any] = {"postcondition_met": postcondition_met}
        if spec.kind == "write" and result.status == "success" and not postcondition_met:
            updates.update(
                status="uncertain",
                error_code=result.error_code or "postcondition_not_verified",
                retryable=False,
            )
        return result.model_copy(update=updates)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._executor.shutdown(wait=False, cancel_futures=True)

    def __len__(self) -> int:
        return len(self._tools)
