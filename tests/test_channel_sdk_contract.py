"""Adapter contract suite (F-101): every channel adapter must pass every case.

The suite runs the Taobao adapter and the deliberately different simulated
mockchat adapter through one set of assertions covering capability
declaration, signature verification, replay protection, the standard inbound
envelope, dedup, ordering, idempotent sends, delivery receipt states,
ownership, reply drafts and error classification.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal

import httpx
import pytest

from ecommerce_agent.channel_sdk import (
    CHANNEL_SDK_CONTRACT_VERSION,
    ChannelAdapterError,
    SendCommand,
    OwnershipCommand,
    ReplyDraftCommand,
)
from ecommerce_agent.channel_sdk.mockchat import sign_mockchat
from ecommerce_agent.service import AgentService
from ecommerce_agent.taobao import TaobaoRemoteError, sign_parameters

from conftest import make_settings

Outcome = Literal["confirm", "reject", "uncertain", "network"]

TERMINAL_STATES = {"confirmed", "rejected", "uncertain", "dead_letter"}


class FakeTopClient:
    def __init__(self) -> None:
        self.behavior: Outcome = "confirm"
        self.async_calls = 0

    def call(self, method: str, params: dict, *, session: str | None = None) -> dict:
        if self.behavior == "network":
            raise httpx.ConnectError("simulated network partition")
        if self.behavior == "reject":
            raise TaobaoRemoteError("platform denied", outcome="rejected", code="DENIED")
        if self.behavior == "uncertain":
            raise TaobaoRemoteError("gateway timeout without result", outcome="uncertain")
        if method == "taobao.message.chatrobot.async":
            self.async_calls += 1
        return {}

    def close(self) -> None:
        pass


class TaobaoHarness:
    platform = "taobao"

    def __init__(self, tmp_path) -> None:
        key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
        self.settings = replace(
            make_settings(tmp_path),
            taobao_enabled=True,
            taobao_auto_reply_enabled=True,
            taobao_app_key="app-key-1",
            taobao_app_secret="app-secret-1",
            taobao_credential_key=key,
            taobao_qimen_customer_id="customer-1",
            taobao_qimen_route_verified=True,
            taobao_chatrobot_request_token="request-token-1",
            taobao_chatrobot_tenant_id="robot-tenant-1",
            release_gate_required=False,
            channel_agent_worker_enabled=False,
            outbox_worker_enabled=False,
            outbox_sync_dispatch=False,
            outbox_retry_base_seconds=0,
            outbox_retry_max_seconds=0,
        )
        self.service = AgentService(self.settings)
        self.top = FakeTopClient()
        self.service.taobao.top = self.top
        self.adapter = self.service.channel_adapters.get(self.platform)
        credential = self.service.taobao.cipher.encrypt({"access_token": "token-1"})
        now = datetime.now(UTC).isoformat()
        with self.service.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_connections(
                    id, tenant_id, platform, shop_id, status, account_id, account_nick,
                    credential_ciphertext, token_expires_at, metadata_json,
                    created_at, updated_at
                ) VALUES ('connection-contract-1', 'tenant-test', 'taobao',
                          'seller-contract-1', 'authorized', 'seller-1', '测试店铺',
                          ?, NULL, '{}', ?, ?)
                """,
                (credential, now, now),
            )

    def inbound_payload(
        self,
        message_id: str,
        text: str = "尺码怎么选",
        *,
        conversation: str = "conversation-contract-1",
        buyer_id: str = "buyer-contract-1",
        stale: bool = False,
        tamper: bool = False,
    ) -> dict[str, str]:
        event = {
            "header": {
                "actionMode": 1,
                "requestId": f"request-{message_id}",
                "tenantId": "robot-tenant-1",
                "serializeType": "Json",
                "type": 1,
            },
            "body": {
                "bizUniqueId": conversation,
                "channelType": "bc",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "contentType": 1,
                "messageType": 1,
                "msgId": message_id,
                "sender": {"domain": "cntaobao", "nick": "买家甲", "role": "buyer"},
                "receivers": [
                    {"domain": "cntaobao", "nick": "客服甲", "role": "customService"}
                ],
            },
        }
        moment = datetime.now(timezone(timedelta(hours=8)))
        if stale:
            moment -= timedelta(seconds=7200)
        params = {
            "method": "qimen.taobao.message.chatrobot.sync",
            "app_key": "app-key-1",
            "timestamp": moment.strftime("%Y-%m-%d %H:%M:%S"),
            "v": "2.0",
            "sign_method": "md5",
            "customerId": "customer-1",
            "event": json.dumps(event, ensure_ascii=False, separators=(",", ":")),
            "buyerId": buyer_id,
            "buyerNick": "买家甲",
            "sellerId": "seller-contract-1",
            "sellerNick": "测试店铺",
        }
        params["sign"] = sign_parameters(params, "app-secret-1", "md5")
        if tamper:
            params["sign"] = "0" * 32
        return params

    def program_outcome(self, outcome: Outcome) -> None:
        self.top.behavior = outcome

    def deliver_pending(self) -> None:
        self.service.taobao.run_outbox_once(worker_id="contract-worker", limit=1)

    def refresh(self, outbox_id: str):
        from ecommerce_agent.channel_sdk import SendReceipt

        view = self.service.taobao.outbox.get(outbox_id)
        assert view is not None
        return SendReceipt.from_outbox_view(self.platform, view)

    def delivery_count(self) -> int:
        return self.top.async_calls

    def close(self) -> None:
        self.service.close()


class MockChatHarness:
    platform = "mockchat"

    def __init__(self, tmp_path, *, messages_per_minute: int = 200) -> None:
        self.settings = replace(
            make_settings(tmp_path),
            mockchat_enabled=True,
            mockchat_auto_reply_enabled=True,
            mockchat_secret="mockchat-secret-1",
            mockchat_messages_per_minute=messages_per_minute,
            release_gate_required=False,
            channel_agent_worker_enabled=False,
            outbox_worker_enabled=False,
            outbox_retry_base_seconds=0,
            outbox_retry_max_seconds=0,
        )
        self.service = AgentService(self.settings)
        self.adapter = self.service.channel_adapters.get(self.platform)

    def inbound_payload(
        self,
        message_id: str,
        text: str = "尺码怎么选",
        *,
        conversation: str = "mock-conversation-1",
        buyer_id: str = "buyer-mock-1",
        stale: bool = False,
        tamper: bool = False,
    ) -> dict[str, str]:
        sent_at = int(time.time()) - (7200 if stale else 0)
        payload = {
            "channel": "mockchat",
            "shop_id": "mock-shop-1",
            "conversation_id": conversation,
            "message_id": message_id,
            "sent_at": str(sent_at),
            "buyer_id": buyer_id,
            "buyer_nick": "买家乙",
            "message_type": "text",
            "text": text,
        }
        payload["signature"] = sign_mockchat(payload, "mockchat-secret-1")
        if tamper:
            payload["signature"] = "f" * 64
        return payload

    def program_outcome(self, outcome: Outcome) -> None:
        self.adapter.transport.set_behavior(outcome)

    def deliver_pending(self) -> None:
        now = datetime.now(UTC).isoformat()
        with self.service.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM channel_outbox
                WHERE status='queued' AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                """,
                (now,),
            ).fetchall()
        for row in rows:
            self.adapter.retry_pending(str(row["id"]))

    def refresh(self, outbox_id: str):
        from ecommerce_agent.channel_sdk import SendReceipt

        view = self.adapter.outbox.get(outbox_id)
        assert view is not None
        return SendReceipt.from_outbox_view(self.platform, view)

    def delivery_count(self) -> int:
        return len(self.adapter.transport.delivered)

    def close(self) -> None:
        self.service.close()


@pytest.fixture(params=["taobao", "mockchat"])
def harness(request, tmp_path):
    built = (
        TaobaoHarness(tmp_path)
        if request.param == "taobao"
        else MockChatHarness(tmp_path)
    )
    try:
        yield built
    finally:
        built.close()


def _receive(harness, message_id: str, **kwargs):
    return harness.adapter.receive_inbound(harness.inbound_payload(message_id, **kwargs))


def _send_command(harness, envelope, *, key: str, allow_bot: bool = True) -> SendCommand:
    return SendCommand(
        tenant_id=envelope.tenant_id,
        conversation_id=envelope.conversation_id,
        text="请参考商品页尺码表。",
        idempotency_key=key,
        source_event_id=envelope.event_id,
        actor="agent" if allow_bot else "operator-a",
        allow_bot=allow_bot,
    )


def _settled(harness, receipt):
    receipt = harness.refresh(receipt.outbox_id)
    if receipt.delivery_state not in TERMINAL_STATES:
        harness.deliver_pending()
        receipt = harness.refresh(receipt.outbox_id)
    return receipt


def _event_count(harness) -> int:
    with harness.service.db.connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM channel_events WHERE platform=? AND direction='inbound'",
            (harness.platform,),
        ).fetchone()[0]


def test_declaration_versions_and_registry_routing(harness) -> None:
    declaration = harness.adapter.declaration()
    assert declaration.platform == harness.platform
    assert declaration.contract_version == CHANNEL_SDK_CONTRACT_VERSION
    assert declaration.capability_version
    assert declaration.message_types
    assert declaration.rate_limits.inbound_per_minute >= 1
    assert declaration.rate_limits.outbound_per_minute >= 1
    features = declaration.features
    assert features.signature_verification is True
    assert features.replay_protection is True
    assert features.inbound_dedup is True
    assert features.outbound_idempotency is True
    assert features.delivery_receipts is True
    catalog = {item.platform for item in harness.service.channel_adapters.catalog()}
    assert harness.platform in catalog
    assert harness.service.channel_adapters.get(harness.platform) is harness.adapter


def test_inbound_envelope_carries_trusted_context_and_redacts_content(harness) -> None:
    envelope = _receive(
        harness, "envelope-1", text="我的手机号是13800138000，帮我改地址"
    )
    assert envelope.platform == harness.platform
    assert envelope.tenant_id == "tenant-test"
    assert envelope.shop_id
    assert envelope.conversation_id.startswith("conversation-")
    assert envelope.external_conversation_id
    assert envelope.event_id.startswith("event-")
    assert envelope.external_event_id == "envelope-1"
    assert envelope.message_type
    assert envelope.received_at
    assert envelope.is_duplicate is False
    assert envelope.agent_job_id
    assert envelope.owner_mode == "bot"
    assert "13800138000" not in envelope.content_redacted
    assert "138****8000" in envelope.content_redacted
    assert "buyer" not in envelope.buyer_hash
    assert len(envelope.buyer_hash) == 64
    assert envelope.agent_context() == {
        "platform": harness.platform,
        "shop_id": envelope.shop_id,
    }


def test_inbound_signature_verification_rejects_tampered_payloads(harness) -> None:
    with pytest.raises(ChannelAdapterError) as excinfo:
        _receive(harness, "tampered-1", tamper=True)
    assert excinfo.value.kind == "signature"
    assert excinfo.value.retryable is False
    assert _event_count(harness) == 0


def test_inbound_replay_window_rejects_stale_payloads(harness) -> None:
    with pytest.raises(ChannelAdapterError) as excinfo:
        _receive(harness, "stale-1", stale=True)
    assert excinfo.value.kind == "replay"
    assert _event_count(harness) == 0


def test_inbound_redelivery_is_deduplicated_without_side_effects(harness) -> None:
    payload = harness.inbound_payload("duplicate-1")
    first = harness.adapter.receive_inbound(payload)
    second = harness.adapter.receive_inbound(payload)
    assert first.is_duplicate is False
    assert second.is_duplicate is True
    assert second.event_id == first.event_id
    assert second.conversation_id == first.conversation_id
    assert second.agent_job_id is None
    with harness.service.db.connect() as conn:
        events = conn.execute(
            "SELECT COUNT(*) FROM channel_events WHERE direction='inbound'"
        ).fetchone()[0]
        jobs = conn.execute("SELECT COUNT(*) FROM channel_agent_jobs").fetchone()[0]
    assert events == 1
    assert jobs == 1


def test_inbound_events_preserve_arrival_order_per_conversation(harness) -> None:
    first = _receive(harness, "order-1", text="第一条")
    second = _receive(harness, "order-2", text="第二条")
    assert first.conversation_id == second.conversation_id
    with harness.service.db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id FROM channel_events
            WHERE conversation_id=? AND direction='inbound' ORDER BY created_at, id
            """,
            (first.conversation_id,),
        ).fetchall()
    assert [str(row["id"]) for row in rows] == [first.event_id, second.event_id]


@pytest.mark.parametrize(
    ("outcome", "expected_state"),
    [("confirm", "confirmed"), ("reject", "rejected"), ("uncertain", "uncertain")],
)
def test_send_receipts_report_each_delivery_state(
    harness, outcome: Outcome, expected_state: str
) -> None:
    envelope = _receive(harness, f"receipt-{outcome}-1")
    harness.program_outcome(outcome)
    receipt = harness.adapter.send_reply(
        _send_command(harness, envelope, key=f"contract:{outcome}:1")
    )
    assert receipt.outbox_id
    receipt = _settled(harness, receipt)
    assert receipt.delivery_state == expected_state
    assert receipt.requires_review is (expected_state != "confirmed")
    if expected_state != "confirmed":
        assert receipt.error_kind == expected_state


def test_network_uncertainty_schedules_retry_then_confirms(harness) -> None:
    envelope = _receive(harness, "retry-1")
    harness.program_outcome("network")
    receipt = harness.adapter.send_reply(
        _send_command(harness, envelope, key="contract:retry:1")
    )
    receipt = harness.refresh(receipt.outbox_id)
    if receipt.delivery_state == "queued":
        harness.deliver_pending()
        receipt = harness.refresh(receipt.outbox_id)
    assert receipt.status == "queued"
    assert receipt.delivery_state == "retry_scheduled"
    harness.program_outcome("confirm")
    harness.deliver_pending()
    receipt = harness.refresh(receipt.outbox_id)
    assert receipt.delivery_state == "confirmed"
    assert receipt.attempt_count >= 2


def test_send_is_idempotent_with_single_platform_delivery(harness) -> None:
    envelope = _receive(harness, "idempotent-1")
    harness.program_outcome("confirm")
    first = harness.adapter.send_reply(
        _send_command(harness, envelope, key="contract:idempotent:1")
    )
    first = _settled(harness, first)
    assert first.delivery_state == "confirmed"
    second = harness.adapter.send_reply(
        _send_command(harness, envelope, key="contract:idempotent:1")
    )
    assert second.outbox_id == first.outbox_id
    assert second.delivery_state == "confirmed"
    assert harness.delivery_count() == 1


def test_send_ownership_is_enforced_in_both_directions(harness) -> None:
    envelope = _receive(harness, "ownership-1")
    with pytest.raises(ChannelAdapterError) as manual:
        harness.adapter.send_reply(
            _send_command(harness, envelope, key="contract:owner:1", allow_bot=False)
        )
    assert manual.value.kind == "conflict"
    harness.adapter.change_ownership(
        OwnershipCommand(
            tenant_id="tenant-test",
            conversation_id=envelope.conversation_id,
            owner_mode="human",
            expected_version=1,
            assigned_to="operator-a",
            actor="operator-a",
        )
    )
    with pytest.raises(ChannelAdapterError) as automatic:
        harness.adapter.send_reply(
            _send_command(harness, envelope, key="contract:owner:2", allow_bot=True)
        )
    assert automatic.value.kind == "conflict"


def test_unknown_conversation_and_version_conflicts_are_classified(harness) -> None:
    with pytest.raises(ChannelAdapterError) as missing:
        harness.adapter.send_reply(
            SendCommand(
                tenant_id="tenant-test",
                conversation_id="conversation-missing",
                text="不存在的会话",
                idempotency_key="contract:missing:1",
                actor="agent",
                allow_bot=True,
            )
        )
    assert missing.value.kind == "not_found"
    envelope = _receive(harness, "conflict-1")
    with pytest.raises(ChannelAdapterError) as stale_version:
        harness.adapter.change_ownership(
            OwnershipCommand(
                tenant_id="tenant-test",
                conversation_id=envelope.conversation_id,
                owner_mode="human",
                expected_version=99,
                actor="operator-a",
            )
        )
    assert stale_version.value.kind == "conflict"
    harness.adapter.change_ownership(
        OwnershipCommand(
            tenant_id="tenant-test",
            conversation_id=envelope.conversation_id,
            owner_mode="human",
            expected_version=1,
            actor="operator-a",
        )
    )
    with pytest.raises(ChannelAdapterError) as same_mode:
        harness.adapter.change_ownership(
            OwnershipCommand(
                tenant_id="tenant-test",
                conversation_id=envelope.conversation_id,
                owner_mode="human",
                expected_version=2,
                actor="operator-a",
            )
        )
    assert same_mode.value.kind == "conflict"


def test_reply_drafts_require_human_owner_and_replay_idempotently(harness) -> None:
    envelope = _receive(harness, "draft-1")

    def draft_command() -> ReplyDraftCommand:
        return ReplyDraftCommand(
            tenant_id="tenant-test",
            conversation_id=envelope.conversation_id,
            expected_conversation_version=2,
            ai_suggestion="您的验证码: 998877 请勿泄露",
            evidence_ids=["knowledge-1"],
            risk_level="low",
            idempotency_key="contract:draft:1",
            source_event_id=envelope.event_id,
            actor="operator-a",
        )

    with pytest.raises(ChannelAdapterError) as bot_owned:
        harness.adapter.create_reply_draft(
            ReplyDraftCommand(
                **{**draft_command().model_dump(), "expected_conversation_version": 1}
            )
        )
    assert bot_owned.value.kind == "conflict"
    harness.adapter.change_ownership(
        OwnershipCommand(
            tenant_id="tenant-test",
            conversation_id=envelope.conversation_id,
            owner_mode="human",
            expected_version=1,
            actor="operator-a",
        )
    )
    created = harness.adapter.create_reply_draft(draft_command())
    assert created["status"] == "draft"
    assert "998877" not in created["ai_suggestion_redacted"]
    replayed = harness.adapter.create_reply_draft(draft_command())
    assert replayed["id"] == created["id"]
    with harness.service.db.connect() as conn:
        drafts = conn.execute("SELECT COUNT(*) FROM channel_reply_drafts").fetchone()[0]
    assert drafts == 1
