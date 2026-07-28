from __future__ import annotations

from typing import Any, Mapping

from ..config import Settings
from ..taobao import (
    ChannelReplyRequest,
    OwnershipRequest,
    ReplyDraftCreateRequest,
    TaobaoError,
    TaobaoIntegrationService,
    TaobaoRemoteError,
)
from .contracts import (
    CHANNEL_SDK_CONTRACT_VERSION,
    ChannelAdapterError,
    ChannelCapabilityDeclaration,
    ChannelErrorKind,
    ChannelFeatureDeclaration,
    InboundEnvelope,
    OwnershipCommand,
    RateLimitDeclaration,
    ReplyDraftCommand,
    SendCommand,
    SendReceipt,
)
from .inbound import ChannelInboundRecorder


class TaobaoChannelAdapter:
    """Standard-contract wrapper around the Qimen/TOP Taobao integration."""

    platform = "taobao"

    def __init__(self, service: TaobaoIntegrationService, settings: Settings):
        self._service = service
        self._settings = settings
        self._recorder = ChannelInboundRecorder(service.db)

    def declaration(self) -> ChannelCapabilityDeclaration:
        return ChannelCapabilityDeclaration(
            platform=self.platform,
            display_name="淘宝客服机器人（奇门入站 + TOP 异步回写）",
            contract_version=CHANNEL_SDK_CONTRACT_VERSION,
            capability_version="2026.07",
            virtual=False,
            message_types=["1"],
            rate_limits=RateLimitDeclaration(
                inbound_per_minute=self._settings.rate_limit_requests_per_minute,
                outbound_per_minute=self._settings.rate_limit_requests_per_minute,
                enforced_by="gateway",
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
            requires_platform_allocated=["customerId", "request_token", "tenant_id"],
        )

    def automation_enabled(self) -> bool:
        return self._settings.taobao_auto_reply_enabled

    def receive_inbound(self, payload: Mapping[str, str]) -> InboundEnvelope:
        try:
            inbound = self._service.receive_qimen(payload)
        except (TaobaoError, TaobaoRemoteError) as exc:
            raise self._translate(exc) from exc
        return self._recorder.load_envelope(
            tenant_id=self._settings.bootstrap_tenant_id,
            event_id=inbound.event_id,
            is_duplicate=not inbound.is_new,
            agent_job_id=inbound.job_id,
        )

    def send_reply(self, command: SendCommand) -> SendReceipt:
        try:
            view = self._service.send_reply(
                command.conversation_id,
                command.tenant_id,
                ChannelReplyRequest(
                    text=command.text,
                    idempotency_key=command.idempotency_key,
                    source_event_id=command.source_event_id,
                ),
                command.actor,
                allow_bot=command.allow_bot,
            )
        except (TaobaoError, TaobaoRemoteError) as exc:
            raise self._translate(exc) from exc
        return SendReceipt.from_outbox_view(self.platform, view)

    def create_reply_draft(self, command: ReplyDraftCommand) -> dict[str, Any]:
        try:
            return self._service.create_reply_draft(
                command.conversation_id,
                command.tenant_id,
                ReplyDraftCreateRequest(
                    expected_conversation_version=command.expected_conversation_version,
                    ai_suggestion=command.ai_suggestion,
                    final_text=command.final_text,
                    evidence_ids=command.evidence_ids,
                    sop_id=command.sop_id,
                    sop_version=command.sop_version,
                    confidence=command.confidence,
                    risk_level=command.risk_level,
                    idempotency_key=command.idempotency_key,
                    source_event_id=command.source_event_id,
                ),
                command.actor,
            )
        except (TaobaoError, TaobaoRemoteError) as exc:
            raise self._translate(exc) from exc

    def change_ownership(self, command: OwnershipCommand) -> dict[str, Any]:
        try:
            return self._service.change_ownership(
                command.conversation_id,
                command.tenant_id,
                OwnershipRequest(
                    owner_mode=command.owner_mode,
                    expected_version=command.expected_version,
                    assigned_to=command.assigned_to,
                ),
                command.actor,
            )
        except (TaobaoError, TaobaoRemoteError) as exc:
            raise self._translate(exc) from exc

    def _translate(self, exc: Exception) -> ChannelAdapterError:
        if isinstance(exc, TaobaoRemoteError):
            kind: ChannelErrorKind = (
                "business_rejected" if exc.outcome == "rejected" else "network_uncertain"
            )
            return ChannelAdapterError(str(exc), kind=kind, platform=self.platform)
        kind = getattr(exc, "kind", None) or "non_retryable"
        return ChannelAdapterError(str(exc), kind=kind, platform=self.platform)
