"""Cross-channel runtime, registry and API coverage for the channel SDK.

Proves the channel Agent runtime is decoupled from any single platform: jobs
created by the simulated second channel are routed through the adapter
registry and complete against mockchat delivery, receipts and ownership.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.channel_sdk import (
    CHANNEL_SDK_CONTRACT_VERSION,
    ChannelAdapterError,
    ChannelAdapterRegistry,
    ChannelCapabilityDeclaration,
    ChannelFeatureDeclaration,
    RateLimitDeclaration,
    SendCommand,
)
from ecommerce_agent.channel_sdk.mockchat import MockChatChannelAdapter
from ecommerce_agent.service import AgentService

from conftest import make_settings
from test_channel_sdk_contract import MockChatHarness

ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}


def _mock_service(tmp_path, *, automatic: bool = True) -> MockChatHarness:
    harness = MockChatHarness(tmp_path)
    if not automatic:
        harness.service.close()
        harness.settings = replace(harness.settings, mockchat_auto_reply_enabled=False)
        harness.service = AgentService(harness.settings)
        harness.adapter = harness.service.channel_adapters.get("mockchat")
    return harness


def test_runtime_processes_mockchat_job_end_to_end_via_registry(tmp_path) -> None:
    harness = _mock_service(tmp_path)
    try:
        service = harness.service
        envelope = harness.adapter.receive_inbound(
            harness.inbound_payload("runtime-1", text="尺码怎么选")
        )
        assert envelope.agent_job_id
        result = service.channel_agents.run_job_once(envelope.agent_job_id)
        assert result["status"] == "completed"
        assert result["action"] == "send"
        assert result["outbox_id"]
        receipt = harness.refresh(result["outbox_id"])
        assert receipt.delivery_state == "confirmed"
        assert harness.adapter.transport.delivered
        assert harness.adapter.transport.delivered[0]["conversation"] == "mock-conversation-1"
        with service.db.connect() as conn:
            session = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE external_session_id=?",
                (f"mockchat:{envelope.conversation_id}",),
            ).fetchone()[0]
            invocations = conn.execute(
                "SELECT COUNT(*) FROM agent_invocations"
            ).fetchone()[0]
            sent_audit = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE event_type='mockchat.message.sent'"
            ).fetchone()[0]
        assert session == 1
        assert invocations == 1
        assert sent_audit == 1

        replayed = harness.adapter.receive_inbound(
            harness.inbound_payload("runtime-1", text="尺码怎么选")
        )
        assert replayed.is_duplicate is True
        assert replayed.agent_job_id is None
        with service.db.connect() as conn:
            jobs = conn.execute("SELECT COUNT(*) FROM channel_agent_jobs").fetchone()[0]
        assert jobs == 1
    finally:
        harness.close()


def test_runtime_blocks_mockchat_job_when_automation_disabled(tmp_path) -> None:
    harness = _mock_service(tmp_path, automatic=False)
    try:
        envelope = harness.adapter.receive_inbound(
            harness.inbound_payload("runtime-disabled-1")
        )
        assert envelope.owner_mode == "human"
        result = harness.service.channel_agents.run_job_once(envelope.agent_job_id)
        assert result["status"] == "blocked"
        assert result["action"] == "disabled"
        assert result["error_kind"] == "automation_disabled"
        with harness.service.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM agent_invocations").fetchone()[0] == 0
    finally:
        harness.close()


def test_runtime_records_rejected_mockchat_delivery_for_review(tmp_path) -> None:
    harness = _mock_service(tmp_path)
    try:
        envelope = harness.adapter.receive_inbound(
            harness.inbound_payload("runtime-reject-1")
        )
        harness.program_outcome("reject")
        result = harness.service.channel_agents.run_job_once(envelope.agent_job_id)
        assert result["status"] == "completed"
        assert result["action"] == "send"
        assert result["error_kind"] == "rejected"
        assert result["last_error"] == "delivery requires review: rejected"
        receipt = harness.refresh(result["outbox_id"])
        assert receipt.requires_review is True
    finally:
        harness.close()


def test_runtime_handoff_moves_mockchat_conversation_to_human(tmp_path) -> None:
    harness = _mock_service(tmp_path)
    try:
        envelope = harness.adapter.receive_inbound(
            harness.inbound_payload("runtime-handoff-1", text="我要转人工客服")
        )
        result = harness.service.channel_agents.run_job_once(envelope.agent_job_id)
        assert result["status"] == "completed"
        assert result["action"] == "handoff"
        with harness.service.db.connect() as conn:
            conversation = conn.execute(
                "SELECT owner_mode, assigned_to FROM channel_conversations WHERE id=?",
                (envelope.conversation_id,),
            ).fetchone()
            handoffs = conn.execute("SELECT COUNT(*) FROM handoff_tasks").fetchone()[0]
            outbox = conn.execute("SELECT COUNT(*) FROM channel_outbox").fetchone()[0]
        assert dict(conversation) == {"owner_mode": "human", "assigned_to": "agent-handoff"}
        assert handoffs >= 1
        assert outbox == 0
        assert not harness.adapter.transport.delivered
    finally:
        harness.close()


def test_registry_enforces_contract_version_uniqueness_and_lookup(tmp_path) -> None:
    class BadContractAdapter:
        platform = "badchannel"

        def declaration(self) -> ChannelCapabilityDeclaration:
            return ChannelCapabilityDeclaration(
                platform="badchannel",
                display_name="坏契约渠道",
                contract_version="0.0.1",
                capability_version="1",
                message_types=["text"],
                rate_limits=RateLimitDeclaration(
                    inbound_per_minute=1, outbound_per_minute=1, enforced_by="adapter"
                ),
                features=ChannelFeatureDeclaration(
                    signature_verification=True,
                    replay_protection=True,
                    inbound_dedup=True,
                    outbound_idempotency=True,
                    delivery_receipts=True,
                    ownership_transfer=True,
                    reply_drafts=True,
                ),
            )

    registry = ChannelAdapterRegistry()
    with pytest.raises(ChannelAdapterError) as bad:
        registry.register(BadContractAdapter())
    assert bad.value.kind == "capability_unavailable"
    assert len(registry) == 0

    harness = _mock_service(tmp_path)
    try:
        registry.register(harness.adapter)
        with pytest.raises(ChannelAdapterError) as duplicate:
            registry.register(harness.adapter)
        assert duplicate.value.kind == "conflict"
        with pytest.raises(ChannelAdapterError) as missing:
            registry.get("jingdong")
        assert missing.value.kind == "not_found"
        assert "mockchat" in registry
    finally:
        harness.close()


def test_channel_adapters_api_lists_declarations_for_admins(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        taobao_enabled=True,
        taobao_app_key="app-key-1",
        taobao_app_secret="app-secret-1",
        mockchat_enabled=True,
        mockchat_secret="mockchat-secret-1",
        channel_agent_worker_enabled=False,
        outbox_worker_enabled=False,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/v1/channels/adapters").status_code == 401
        response = client.get("/v1/channels/adapters", headers=ADMIN_HEADERS)
        assert response.status_code == 200
        catalog = {item["platform"]: item for item in response.json()}
        assert set(catalog) == {"mockchat", "taobao"}
        for item in catalog.values():
            assert item["contract_version"] == CHANNEL_SDK_CONTRACT_VERSION
            assert item["rate_limits"]["inbound_per_minute"] >= 1
            assert item["features"]["signature_verification"] is True
        assert catalog["mockchat"]["virtual"] is True
        assert catalog["taobao"]["virtual"] is False


def test_mockchat_rate_limit_is_declared_and_enforced(tmp_path) -> None:
    harness = MockChatHarness(tmp_path, messages_per_minute=3)
    try:
        declaration = harness.adapter.declaration()
        assert declaration.rate_limits.inbound_per_minute == 3
        assert declaration.rate_limits.enforced_by == "adapter"
        for index in range(3):
            harness.adapter.receive_inbound(
                harness.inbound_payload(f"rate-{index}")
            )
        with pytest.raises(ChannelAdapterError) as limited:
            harness.adapter.receive_inbound(harness.inbound_payload("rate-overflow"))
        assert limited.value.kind == "rate_limited"
        assert limited.value.retryable is True
    finally:
        harness.close()


def test_mockchat_concurrent_same_key_sends_deliver_exactly_once(tmp_path) -> None:
    harness = _mock_service(tmp_path)
    try:
        envelope = harness.adapter.receive_inbound(
            harness.inbound_payload("concurrent-1")
        )
        command = SendCommand(
            tenant_id="tenant-test",
            conversation_id=envelope.conversation_id,
            text="并发发送验证",
            idempotency_key="mockchat:concurrent:1",
            source_event_id=envelope.event_id,
            actor="agent",
            allow_bot=True,
        )
        with ThreadPoolExecutor(max_workers=8) as pool:
            receipts = list(
                pool.map(lambda _: harness.adapter.send_reply(command), range(8))
            )
        assert len({receipt.outbox_id for receipt in receipts}) == 1
        assert len(harness.adapter.transport.delivered) == 1
        settled = harness.refresh(receipts[0].outbox_id)
        assert settled.delivery_state == "confirmed"
    finally:
        harness.close()


def test_outbox_dispatch_is_isolated_per_platform(tmp_path) -> None:
    harness = _mock_service(tmp_path)
    try:
        envelope = harness.adapter.receive_inbound(
            harness.inbound_payload("isolation-1")
        )
        harness.program_outcome("network")
        receipt = harness.adapter.send_reply(
            SendCommand(
                tenant_id="tenant-test",
                conversation_id=envelope.conversation_id,
                text="平台隔离验证",
                idempotency_key="mockchat:isolation:1",
                source_event_id=envelope.event_id,
                actor="agent",
                allow_bot=True,
            )
        )
        pending = harness.refresh(receipt.outbox_id)
        assert pending.status == "queued"
        taobao_run = harness.service.taobao.run_outbox_once(worker_id="taobao-worker")
        assert taobao_run["claimed"] == 0
        harness.program_outcome("confirm")
        harness.deliver_pending()
        assert harness.refresh(receipt.outbox_id).delivery_state == "confirmed"
    finally:
        harness.close()


def test_mockchat_requires_secret_before_any_traffic(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        mockchat_enabled=True,
        mockchat_secret="",
    )
    adapter = MockChatChannelAdapter.__new__(MockChatChannelAdapter)
    adapter.settings = settings
    adapter.platform = "mockchat"
    with pytest.raises(ChannelAdapterError) as excinfo:
        adapter._require_enabled()
    assert excinfo.value.kind == "capability_unavailable"
