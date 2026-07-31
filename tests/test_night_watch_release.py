"""Night-watch windows and SOP-level release policy (F-107).

A policy carries an optional UTC time window with a night mode: inside the
window the effective mode swaps (e.g. assist by day, automatic at night) and
every consumer sees only the effective mode. A policy may also restrict which
SOPs are allowed to automate; responses using other SOPs are flagged and
downgraded.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ecommerce_agent.releases import (
    ReleasePolicyCreateRequest,
    ReleaseReplayCase,
    ReleaseReplayRequest,
    ReleaseTransitionRequest,
    ReplayExpectation,
)
from ecommerce_agent.service import AgentService
from ecommerce_agent.sops import SopCreateRequest, SopDsl

from conftest import make_settings
from test_channel_sdk_contract import MockChatHarness


def _hhmm(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%H:%M")


def _activate(
    service: AgentService,
    *,
    platform: str,
    store_id: str,
    mode: str = "assist",
    night: dict | None = None,
    sop_allowlist: list[str] | None = None,
) -> dict:
    release = service.releases.create(
        "tenant-test",
        ReleasePolicyCreateRequest(
            release_key=f"night.{platform}.{store_id}",
            name="夜间值守策略",
            platform=platform,
            store_id=store_id,
            mode=mode,
            traffic_percentage=100,
            intent_allowlist=["product"],
            max_risk_level="low",
            require_sources=True,
            allow_model_fallback=False,
            min_replay_cases=1,
            max_replay_failure_rate=0,
            max_replay_severe_errors=0,
            runtime_min_samples=1,
            max_runtime_failure_rate=1,
            max_runtime_severe_errors=100,
            sop_allowlist=sop_allowlist,
            **(night or {}),
        ),
        "creator-a",
    )
    replay = service.releases.run_replay(
        "tenant-test",
        release["id"],
        ReleaseReplayRequest(
            cases=[
                ReleaseReplayCase(
                    case_id="product-1",
                    message="尺码怎么选",
                    expectation=ReplayExpectation(
                        expected_intent="product",
                        expected_requires_human=False,
                        require_sources=True,
                    ),
                )
            ]
        ),
        "creator-a",
        lambda case: SimpleNamespace(
            answer="请参考尺码表。",
            intent="product",
            risk_level="low",
            requires_human=False,
            sources=[{"id": "knowledge-1"}],
            model_fallback=False,
        ),
    )
    assert replay["passed"] is True
    evaluated = service.releases.get_policy("tenant-test", release["id"])
    approved = service.releases.approve(
        "tenant-test",
        release["id"],
        ReleaseTransitionRequest(expected_record_version=evaluated["record_version"]),
        "reviewer-b",
    )
    return service.releases.activate(
        "tenant-test",
        release["id"],
        ReleaseTransitionRequest(expected_record_version=approved["record_version"]),
        "release-admin",
    )


def test_night_fields_validate_together() -> None:
    base = dict(
        release_key="night.validation",
        name="夜间校验",
        platform="mockchat",
        store_id="shop-1",
        mode="assist",
        traffic_percentage=100,
        intent_allowlist=["product"],
        min_replay_cases=1,
        max_replay_failure_rate=0,
        max_replay_severe_errors=0,
        runtime_min_samples=1,
        max_runtime_failure_rate=0,
        max_runtime_severe_errors=0,
    )
    with pytest.raises(ValueError, match="together"):
        ReleasePolicyCreateRequest(**base, night_window_start_utc="22:00")
    with pytest.raises(ValueError, match="empty"):
        ReleasePolicyCreateRequest(
            **base,
            night_window_start_utc="22:00",
            night_window_end_utc="22:00",
            night_mode="automatic",
        )
    ok = ReleasePolicyCreateRequest(
        **base,
        night_window_start_utc="22:00",
        night_window_end_utc="07:00",
        night_mode="automatic",
    )
    assert ok.night_mode == "automatic"


def test_assignment_swaps_effective_mode_inside_night_window(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        now = datetime(2026, 7, 27, 23, 30, tzinfo=UTC)
        _activate(
            service,
            platform="mockchat",
            store_id="shop-night-1",
            mode="assist",
            night={
                "night_window_start_utc": "22:00",
                "night_window_end_utc": "07:00",
                "night_mode": "automatic",
            },
        )
        inside = service.releases.assignment(
            "tenant-test", "mockchat", "shop-night-1", "conversation-1", now=now
        )
        assert inside["policy"]["mode"] == "automatic"
        assert inside["policy"]["configured_mode"] == "assist"
        assert inside["policy"]["night_watch_active"] is True

        wrapped_morning = service.releases.assignment(
            "tenant-test",
            "mockchat",
            "shop-night-1",
            "conversation-1",
            now=datetime(2026, 7, 28, 6, 59, tzinfo=UTC),
        )
        assert wrapped_morning["policy"]["mode"] == "automatic"

        daytime = service.releases.assignment(
            "tenant-test",
            "mockchat",
            "shop-night-1",
            "conversation-1",
            now=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        )
        assert daytime["policy"]["mode"] == "assist"
        assert daytime["policy"]["night_watch_active"] is False
    finally:
        service.close()


def test_sop_allowlist_flags_unlisted_sop_and_accepts_listed_key(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        listed = service.sops.create(
            "tenant-test",
            SopCreateRequest(
                sop_key="allowed.flow",
                name="允许的流程",
                intent="complaint",
                risk_level="high",
                dsl=SopDsl.model_validate(
                    {
                        "trigger": {"intents": ["complaint"]},
                        "required_context": ["shop_id"],
                        "steps": [{"observe": "get_order_facts"}],
                        "guards": {"max_auto_compensation_cents": 0},
                        "handoff": {"when": ["customer_escalation"]},
                        "success": {"postcondition": "case_created"},
                    }
                ),
            ),
            "admin-test",
        )
        unlisted = service.sops.create(
            "tenant-test",
            SopCreateRequest(
                sop_key="other.flow",
                name="未列入的流程",
                intent="complaint",
                risk_level="high",
                dsl=SopDsl.model_validate(
                    {
                        "trigger": {"intents": ["complaint"]},
                        "required_context": ["shop_id"],
                        "steps": [{"observe": "get_order_facts"}],
                        "guards": {"max_auto_compensation_cents": 0},
                        "handoff": {"when": ["customer_escalation"]},
                        "success": {"postcondition": "case_created"},
                    }
                ),
            ),
            "admin-test",
        )
        active = _activate(
            service,
            platform="mockchat",
            store_id="shop-sop-1",
            mode="automatic",
            sop_allowlist=["allowed.flow"],
        )

        def response(sop_definition_id: str | None) -> SimpleNamespace:
            return SimpleNamespace(
                answer="按流程处理。",
                intent="product",
                risk_level="low",
                requires_human=False,
                sources=[{"id": "knowledge-1"}],
                model_fallback=False,
                sop_id=sop_definition_id,
            )

        assignment = service.releases.assignment(
            "tenant-test", "mockchat", "shop-sop-1", "conversation-sop-1"
        )
        blocked = service.releases.record_response(
            "tenant-test",
            assignment,
            conversation_id="conversation-sop-1",
            event_id="event-sop-1",
            response=response(unlisted["definition"]["id"]),
        )
        assert "sop_not_allowlisted" in blocked["violations"]
        assert blocked["action"] == "handoff"
        assert blocked["severe"] is True

        allowed = service.releases.record_response(
            "tenant-test",
            assignment,
            conversation_id="conversation-sop-1",
            event_id="event-sop-2",
            response=response(listed["definition"]["id"]),
        )
        assert allowed["violations"] == []
        assert allowed["action"] == "send"

        no_sop = service.releases.record_response(
            "tenant-test",
            assignment,
            conversation_id="conversation-sop-1",
            event_id="event-sop-3",
            response=response(None),
        )
        assert no_sop["violations"] == []
        assert active["sop_allowlist"] == ["allowed.flow"]
    finally:
        service.close()


def _gated_mock_harness(tmp_path) -> MockChatHarness:
    harness = MockChatHarness(tmp_path)
    harness.service.close()
    harness.settings = replace(harness.settings, release_gate_required=True)
    harness.service = __import__(
        "ecommerce_agent.service", fromlist=["AgentService"]
    ).AgentService(harness.settings)
    harness.adapter = harness.service.channel_adapters.get("mockchat")
    return harness


def test_night_watch_automates_mockchat_channel_inside_window(tmp_path) -> None:
    harness = _gated_mock_harness(tmp_path)
    try:
        now = datetime.now(UTC)
        _activate(
            harness.service,
            platform="mockchat",
            store_id="mock-shop-1",
            mode="assist",
            night={
                "night_window_start_utc": _hhmm(now - timedelta(hours=1)),
                "night_window_end_utc": _hhmm(now + timedelta(hours=1)),
                "night_mode": "automatic",
            },
        )
        envelope = harness.adapter.receive_inbound(
            harness.inbound_payload("night-1", text="尺码怎么选")
        )
        result = harness.service.channel_agents.run_job_once(envelope.agent_job_id)
        assert result["status"] == "completed"
        assert result["action"] == "send"
        assert result["release_mode"] == "automatic"
        assert harness.adapter.transport.delivered
    finally:
        harness.close()


def test_daytime_keeps_assist_drafts_outside_night_window(tmp_path) -> None:
    harness = _gated_mock_harness(tmp_path)
    try:
        now = datetime.now(UTC)
        _activate(
            harness.service,
            platform="mockchat",
            store_id="mock-shop-1",
            mode="assist",
            night={
                "night_window_start_utc": _hhmm(now + timedelta(hours=2)),
                "night_window_end_utc": _hhmm(now + timedelta(hours=3)),
                "night_mode": "automatic",
            },
        )
        envelope = harness.adapter.receive_inbound(
            harness.inbound_payload("day-1", text="尺码怎么选")
        )
        result = harness.service.channel_agents.run_job_once(envelope.agent_job_id)
        assert result["status"] == "completed"
        assert result["action"] == "draft"
        assert result["release_mode"] == "assist"
        assert not harness.adapter.transport.delivered
        with harness.service.db.connect() as conn:
            conversation = conn.execute(
                "SELECT owner_mode FROM channel_conversations WHERE id=?",
                (envelope.conversation_id,),
            ).fetchone()
            drafts = conn.execute(
                "SELECT COUNT(*) FROM channel_reply_drafts"
            ).fetchone()[0]
        assert conversation["owner_mode"] == "human"
        assert drafts == 1
    finally:
        harness.close()
