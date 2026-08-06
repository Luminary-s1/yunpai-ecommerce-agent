from __future__ import annotations

import threading
import time

import pytest
from pydantic import BaseModel, ConfigDict

from ecommerce_agent.tools import (
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


class OrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str


def context() -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id="tenant-a",
        client_id="client-a",
        session_id="session-a",
        trace_id="trace-a",
        trusted_context={"authorized": True},
    )


def test_write_tool_requires_idempotency_and_postcondition_verifier() -> None:
    handler = lambda _args, _context: ToolResult(status="success")
    with pytest.raises(ValueError, match="idempotency_fields"):
        ToolSpec(
            name="cancel_order",
            description="Cancel an order",
            kind="write",
            input_model=OrderInput,
            handler=handler,
            verifier=lambda *_args: True,
        )
    with pytest.raises(ValueError, match="postcondition verifier"):
        ToolSpec(
            name="cancel_order",
            description="Cancel an order",
            kind="write",
            input_model=OrderInput,
            handler=handler,
            idempotency_fields=("order_id",),
        )


def test_observe_cannot_select_a_write_tool() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="cancel_order",
            description="Cancel an order",
            kind="write",
            input_model=OrderInput,
            handler=lambda _args, _context: ToolResult(status="success"),
            idempotency_fields=("order_id",),
            verifier=lambda *_args: True,
        )
    )
    with pytest.raises(ValueError, match="observe_cannot_call_write_tool"):
        registry.validate_selection(
            name="cancel_order",
            arguments={"order_id": "order-1"},
            requested_mode="observe",
            context=context(),
        )
    registry.close()


def test_act_cannot_select_a_read_tool() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="get_order",
            description="Get an order",
            kind="read",
            input_model=OrderInput,
            handler=lambda _args, _context: ToolResult(status="success"),
        )
    )
    with pytest.raises(ValueError, match="act_requires_write_tool"):
        registry.validate_selection(
            name="get_order",
            arguments={"order_id": "order-1"},
            requested_mode="act",
            context=context(),
        )
    registry.close()


def test_read_tool_retries_handler_error_then_succeeds() -> None:
    registry = ToolRegistry()
    calls = 0

    def flaky(_args, _context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("temporary")
        return ToolResult(status="success", output={"calls": calls})

    spec = ToolSpec(
        name="get_order",
        description="Get order",
        kind="read",
        input_model=OrderInput,
        handler=flaky,
        max_retries=1,
    )
    registry.register(spec)
    result = registry.execute(
        spec=spec,
        arguments=OrderInput(order_id="order-1"),
        context=context(),
    )
    assert result.status == "success"
    assert result.postcondition_met is True
    assert result.output["calls"] == 2
    registry.close()


def test_read_tool_retries_explicit_retryable_result_then_succeeds() -> None:
    registry = ToolRegistry()
    calls = 0

    def flaky_result(_args, _context):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ToolResult(status="failed", error_code="temporary", retryable=True)
        return ToolResult(status="success", output={"calls": calls})

    spec = ToolSpec(
        name="get_order_retryable",
        description="Get order with explicit retry signal",
        kind="read",
        input_model=OrderInput,
        handler=flaky_result,
        max_retries=1,
    )
    registry.register(spec)
    result = registry.execute(
        spec=spec,
        arguments=OrderInput(order_id="order-1"),
        context=context(),
    )
    assert result.status == "success"
    assert result.postcondition_met is True
    assert calls == 2
    registry.close()


def test_write_handler_exception_is_uncertain_and_never_retried() -> None:
    registry = ToolRegistry()
    calls = 0

    def ambiguous_write(_args, _context):
        nonlocal calls
        calls += 1
        raise ConnectionError("connection lost after request may have been sent")

    spec = ToolSpec(
        name="ambiguous_write",
        description="Ambiguous write",
        kind="write",
        input_model=OrderInput,
        handler=ambiguous_write,
        idempotency_fields=("order_id",),
        verifier=lambda *_args: True,
        max_retries=3,
    )
    registry.register(spec)
    result = registry.execute(
        spec=spec,
        arguments=OrderInput(order_id="order-1"),
        context=context(),
    )
    assert result.status == "uncertain"
    assert result.error_code == "tool_handler_error"
    assert result.retryable is False
    assert calls == 1
    registry.close()


def test_read_timeout_is_bounded_and_reported() -> None:
    registry = ToolRegistry()
    handler_seconds = 2.0
    timeout_seconds = 0.02
    # The abandoned worker keeps running after the registry gives up; releasing it once
    # the assertions are done keeps the interpreter from joining a sleeping thread at exit.
    release = threading.Event()

    def slow(_args, _context):
        release.wait(timeout=handler_seconds)
        return ToolResult(status="success")

    spec = ToolSpec(
        name="slow_read",
        description="Slow read",
        kind="read",
        input_model=OrderInput,
        handler=slow,
        timeout_seconds=timeout_seconds,
    )
    try:
        started = time.perf_counter()
        result = registry.execute(
            spec=spec,
            arguments=OrderInput(order_id="order-1"),
            context=context(),
        )
        elapsed = time.perf_counter() - started
        # The registry must abandon the call at its own timeout instead of waiting for the
        # handler. Bounding at half the handler duration keeps that meaning while leaving
        # enough headroom for thread scheduling on a loaded CI machine.
        assert elapsed < handler_seconds / 2
        assert result.status == "failed"
        assert result.error_code == "tool_timeout"
        assert result.postcondition_met is False
    finally:
        release.set()
        registry.close()


def test_write_timeout_is_uncertain_and_not_retried() -> None:
    registry = ToolRegistry()
    calls = 0

    def slow_write(_args, _context):
        nonlocal calls
        calls += 1
        time.sleep(0.1)
        return ToolResult(status="success")

    spec = ToolSpec(
        name="slow_write",
        description="Slow write",
        kind="write",
        input_model=OrderInput,
        handler=slow_write,
        idempotency_fields=("order_id",),
        verifier=lambda *_args: True,
        timeout_seconds=0.01,
        max_retries=2,
    )
    result = registry.execute(
        spec=spec,
        arguments=OrderInput(order_id="order-1"),
        context=context(),
    )
    assert result.status == "uncertain"
    assert result.error_code == "tool_timeout"
    assert calls == 1
    registry.close()


def test_unverified_write_result_becomes_uncertain() -> None:
    registry = ToolRegistry()
    spec = ToolSpec(
        name="write_without_proof",
        description="Write without proof",
        kind="write",
        input_model=OrderInput,
        handler=lambda *_args: ToolResult(status="success"),
        idempotency_fields=("order_id",),
        verifier=lambda *_args: False,
    )
    result = registry.execute(
        spec=spec,
        arguments=OrderInput(order_id="order-1"),
        context=context(),
    )
    assert result.status == "uncertain"
    assert result.error_code == "postcondition_not_verified"
    assert result.postcondition_met is False
    registry.close()
