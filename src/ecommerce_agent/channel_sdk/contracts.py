from __future__ import annotations

import hashlib
import hmac
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field


CHANNEL_SDK_CONTRACT_VERSION = "1.0.0"

IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9_.:-]+$"

OwnerMode = Literal["bot", "human", "paused"]

MessageKind = Literal[
    "text",
    "image",
    "audio",
    "video",
    "goods_card",
    "order_card",
    "system",
    "unknown",
]

# Kinds the Agent may deliberate on; every other kind must reach a human
# instead of being silently dropped or hallucinated about.
AGENT_READABLE_KINDS: frozenset[str] = frozenset({"text"})

ChannelErrorKind = Literal[
    "authentication",
    "signature",
    "replay",
    "schema",
    "rate_limited",
    "business_rejected",
    "network_uncertain",
    "conflict",
    "not_found",
    "capability_unavailable",
    "non_retryable",
]

_RETRYABLE_KINDS = frozenset({"rate_limited", "network_uncertain"})

FAILURE_DELIVERY_STATES = frozenset({"rejected", "uncertain", "dead_letter"})


class ChannelAdapterError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: ChannelErrorKind = "non_retryable",
        platform: str | None = None,
        retryable: bool | None = None,
    ):
        super().__init__(message)
        self.kind: ChannelErrorKind = kind
        self.platform = platform
        self.retryable = (kind in _RETRYABLE_KINDS) if retryable is None else retryable


class RateLimitDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inbound_per_minute: int = Field(ge=1)
    outbound_per_minute: int = Field(ge=1)
    enforced_by: Literal["adapter", "gateway"]


class ChannelFeatureDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signature_verification: bool
    replay_protection: bool
    inbound_dedup: bool
    outbound_idempotency: bool
    delivery_receipts: bool
    ownership_transfer: bool
    reply_drafts: bool


class ChannelCapabilityDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str = Field(min_length=1, max_length=128)
    contract_version: str
    capability_version: str = Field(min_length=1, max_length=32)
    virtual: bool = False
    message_types: list[str] = Field(min_length=1)
    rate_limits: RateLimitDeclaration
    features: ChannelFeatureDeclaration
    requires_platform_allocated: list[str] = Field(default_factory=list)


class InboundEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = CHANNEL_SDK_CONTRACT_VERSION
    platform: str
    tenant_id: str
    shop_id: str
    conversation_id: str
    external_conversation_id: str
    buyer_hash: str
    owner_mode: OwnerMode
    event_id: str
    external_event_id: str
    message_type: str
    message_kind: MessageKind = "text"
    content_redacted: str
    payload_hash: str
    received_at: str
    is_duplicate: bool
    agent_job_id: str | None = None

    def agent_context(self) -> dict[str, str]:
        """Whitelisted context an Agent invocation may receive from a channel."""
        return {"platform": self.platform, "shop_id": self.shop_id}


class SendCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=64)
    conversation_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=IDEMPOTENCY_KEY_PATTERN)
    source_event_id: str | None = Field(default=None, max_length=128)
    actor: str = Field(min_length=1, max_length=128)
    allow_bot: bool = False


class SendReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str
    outbox_id: str | None
    idempotency_key: str | None
    status: str
    delivery_state: str
    attempt_count: int = 0
    error_kind: str | None = None
    last_error: str | None = None

    @property
    def requires_review(self) -> bool:
        return self.delivery_state in FAILURE_DELIVERY_STATES

    @classmethod
    def from_outbox_view(cls, platform: str, view: Mapping[str, Any]) -> SendReceipt:
        return cls(
            platform=platform,
            outbox_id=str(view["id"]) if view.get("id") else None,
            idempotency_key=view.get("idempotency_key"),
            status=str(view.get("status") or "queued"),
            delivery_state=str(view.get("delivery_state") or "queued"),
            attempt_count=int(view.get("attempt_count") or 0),
            error_kind=view.get("error_kind"),
            last_error=view.get("last_error"),
        )


class OwnershipCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=64)
    conversation_id: str = Field(min_length=1, max_length=128)
    owner_mode: OwnerMode
    expected_version: int = Field(ge=1)
    assigned_to: str | None = Field(default=None, max_length=128)
    actor: str = Field(min_length=1, max_length=128)


class ReplyDraftCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=64)
    conversation_id: str = Field(min_length=1, max_length=128)
    expected_conversation_version: int = Field(ge=1)
    ai_suggestion: str = Field(min_length=1, max_length=2000)
    final_text: str | None = Field(default=None, min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    sop_id: str | None = Field(default=None, max_length=128)
    sop_version: int | None = Field(default=None, ge=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    risk_level: Literal["low", "medium", "high", "critical"] = "low"
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=IDEMPOTENCY_KEY_PATTERN)
    source_event_id: str | None = Field(default=None, max_length=128)
    actor: str = Field(min_length=1, max_length=128)


def hash_subject(key: str, subject_id: str) -> str:
    return hmac.new(key.encode("utf-8"), subject_id.encode("utf-8"), hashlib.sha256).hexdigest()


def mask_nick(nick: str) -> str | None:
    if not nick:
        return None
    return nick[0] + "***" + (nick[-1] if len(nick) > 1 else "")
