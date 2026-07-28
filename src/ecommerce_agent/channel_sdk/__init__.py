"""Channel adapter SDK: the standard contracts every customer-service channel
implements — capability declaration, verified inbound envelopes, durable dedup,
idempotent sends with delivery receipts, classified errors and replay-safe
persistence (F-101)."""

from .adapter import ChannelAdapter
from .contracts import (
    CHANNEL_SDK_CONTRACT_VERSION,
    FAILURE_DELIVERY_STATES,
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
    hash_subject,
    mask_nick,
)
from .inbound import ChannelInboundRecorder, InboundRecord
from .registry import ChannelAdapterRegistry

__all__ = [
    "CHANNEL_SDK_CONTRACT_VERSION",
    "FAILURE_DELIVERY_STATES",
    "ChannelAdapter",
    "ChannelAdapterError",
    "ChannelAdapterRegistry",
    "ChannelCapabilityDeclaration",
    "ChannelErrorKind",
    "ChannelFeatureDeclaration",
    "ChannelInboundRecorder",
    "InboundEnvelope",
    "InboundRecord",
    "OwnershipCommand",
    "RateLimitDeclaration",
    "ReplyDraftCommand",
    "SendCommand",
    "SendReceipt",
    "hash_subject",
    "mask_nick",
]
