from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from .contracts import (
    ChannelCapabilityDeclaration,
    InboundEnvelope,
    OwnershipCommand,
    ReplyDraftCommand,
    SendCommand,
    SendReceipt,
)


@runtime_checkable
class ChannelAdapter(Protocol):
    """Contract every customer-service channel implementation must satisfy.

    Adapters translate one platform's wire protocol into the standard envelope,
    send, receipt and error models; verification, replay protection, dedup and
    idempotency are mandatory, and failures raise ChannelAdapterError with a
    classified kind.
    """

    platform: str

    def declaration(self) -> ChannelCapabilityDeclaration: ...

    def automation_enabled(self) -> bool: ...

    def receive_inbound(self, payload: Mapping[str, str]) -> InboundEnvelope: ...

    def send_reply(self, command: SendCommand) -> SendReceipt: ...

    def create_reply_draft(self, command: ReplyDraftCommand) -> dict[str, Any]: ...

    def change_ownership(self, command: OwnershipCommand) -> dict[str, Any]: ...
