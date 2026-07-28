from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, Mapping

from ..config import Settings
from ..database import Database
from ..outbox import DurableOutbox
from ..rate_limit import RateLimitError, SlidingWindowRateLimiter
from ..text_utils import redact_sensitive
from . import drafts, ownership
from .contracts import (
    CHANNEL_SDK_CONTRACT_VERSION,
    ChannelAdapterError,
    ChannelCapabilityDeclaration,
    ChannelFeatureDeclaration,
    InboundEnvelope,
    MessageKind,
    OwnershipCommand,
    RateLimitDeclaration,
    ReplyDraftCommand,
    SendCommand,
    SendReceipt,
    hash_subject,
    mask_nick,
)
from .inbound import ChannelInboundRecorder

MockChatBehavior = Literal["confirm", "reject", "uncertain", "network"]

_REQUIRED_FIELDS = (
    "channel",
    "shop_id",
    "conversation_id",
    "message_id",
    "sent_at",
    "buyer_id",
)

_MOCKCHAT_MESSAGE_KINDS: dict[str, MessageKind] = {
    "text": "text",
    "image": "image",
    "audio": "audio",
    "video": "video",
    "goods_card": "goods_card",
    "order_card": "order_card",
}


class MockChatRejected(RuntimeError):
    pass


class MockChatUncertain(RuntimeError):
    pass


def sign_mockchat(payload: Mapping[str, str], secret: str) -> str:
    canonical = "&".join(
        f"{key}={payload[key]}" for key in sorted(payload) if key != "signature"
    ).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


class MockChatTransport:
    """In-memory delivery endpoint of the simulated channel.

    Tests program the next outcome to exercise every receipt state without any
    network access.
    """

    def __init__(self) -> None:
        self.behavior: MockChatBehavior = "confirm"
        self.delivered: list[dict[str, Any]] = []

    def set_behavior(self, behavior: MockChatBehavior) -> None:
        self.behavior = behavior

    def deliver(self, action: dict[str, Any]) -> dict[str, Any]:
        if self.behavior == "network":
            raise ConnectionError("mockchat endpoint is unreachable")
        if self.behavior == "reject":
            raise MockChatRejected("mockchat platform rejected the message")
        if self.behavior == "uncertain":
            raise MockChatUncertain("mockchat delivery outcome is unknown")
        self.delivered.append(action)
        return {"success": True, "receipt_id": f"mock-receipt-{len(self.delivered)}"}


class MockChatChannelAdapter:
    """Second, fully local channel used to validate the generic adapter contract.

    The wire protocol is deliberately different from Taobao Qimen: a flat JSON
    document signed with HMAC-SHA256 over "key=value" pairs and a unix-epoch
    replay window. Persistence reuses the shared SDK recorder/outbox, so the
    channel Agent runtime processes mockchat jobs exactly like real channels.
    """

    platform = "mockchat"

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        transport: MockChatTransport | None = None,
    ):
        self.db = db
        self.settings = settings
        self.transport = transport or MockChatTransport()
        self._recorder = ChannelInboundRecorder(db)
        self.outbox = DurableOutbox(
            db,
            lease_seconds=settings.outbox_lease_seconds,
            max_attempts=settings.outbox_max_attempts,
            retry_base_seconds=settings.outbox_retry_base_seconds,
            retry_max_seconds=settings.outbox_retry_max_seconds,
            platform=self.platform,
        )
        self._limiter = SlidingWindowRateLimiter(settings.mockchat_messages_per_minute)

    def declaration(self) -> ChannelCapabilityDeclaration:
        return ChannelCapabilityDeclaration(
            platform=self.platform,
            display_name="模拟客服渠道（本地验证用）",
            contract_version=CHANNEL_SDK_CONTRACT_VERSION,
            capability_version="2026.07-virtual",
            virtual=True,
            message_types=sorted(_MOCKCHAT_MESSAGE_KINDS),
            rate_limits=RateLimitDeclaration(
                inbound_per_minute=self.settings.mockchat_messages_per_minute,
                outbound_per_minute=self.settings.mockchat_messages_per_minute,
                enforced_by="adapter",
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
            requires_platform_allocated=[],
        )

    def automation_enabled(self) -> bool:
        return self.settings.mockchat_auto_reply_enabled

    def message_kind(self, message_type: str) -> MessageKind:
        return _MOCKCHAT_MESSAGE_KINDS.get(str(message_type), "unknown")

    def receive_inbound(self, payload: Mapping[str, str]) -> InboundEnvelope:
        self._require_enabled()
        for field in _REQUIRED_FIELDS:
            if not str(payload.get(field) or "").strip():
                raise ChannelAdapterError(
                    f"mockchat payload misses required field: {field}",
                    kind="schema",
                    platform=self.platform,
                )
        if payload["channel"] != self.platform:
            raise ChannelAdapterError(
                "payload is not a mockchat message", kind="schema", platform=self.platform
            )
        supplied = str(payload.get("signature") or "")
        expected = sign_mockchat(payload, self.settings.mockchat_secret)
        if not supplied or not hmac.compare_digest(expected, supplied):
            raise ChannelAdapterError(
                "mockchat signature verification failed",
                kind="signature",
                platform=self.platform,
            )
        try:
            sent_at = int(payload["sent_at"])
        except ValueError as exc:
            raise ChannelAdapterError(
                "mockchat sent_at must be unix seconds", kind="schema", platform=self.platform
            ) from exc
        skew = abs(datetime.now(UTC).timestamp() - sent_at)
        if skew > self.settings.mockchat_callback_max_skew_seconds:
            raise ChannelAdapterError(
                "mockchat message is outside the replay-protection window",
                kind="replay",
                platform=self.platform,
            )
        tenant_id = self.settings.bootstrap_tenant_id
        self._check_rate(f"inbound:{tenant_id}")
        message_type = str(payload.get("message_type") or "text")
        kind = self.message_kind(message_type)
        text = str(payload.get("text") or "")
        if kind == "text":
            if not text.strip():
                raise ChannelAdapterError(
                    "mockchat text message misses text",
                    kind="schema",
                    platform=self.platform,
                )
            safe_text, _ = redact_sensitive(text)
        else:
            # Media payloads are recorded as a marker only; their body is
            # never trusted as conversation text.
            safe_text = f"[{message_type}]"
        buyer_id = str(payload["buyer_id"])
        buyer_hash = hash_subject(
            self.settings.subject_hash_key or self.settings.mockchat_secret, buyer_id
        )
        # A virtual channel never persists raw counterpart identifiers; replies
        # are routed by the same stable hash the envelope exposes.
        routing = json.dumps(
            {"virtual": True, "receiver_hash": buyer_hash},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        record = self._recorder.record(
            tenant_id=tenant_id,
            platform=self.platform,
            shop_id=str(payload["shop_id"]),
            external_conversation_id=str(payload["conversation_id"]),
            external_event_id=str(payload["message_id"]),
            message_type=message_type,
            content_redacted=safe_text,
            payload_hash=hashlib.sha256(
                json.dumps(dict(payload), ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            buyer_hash=buyer_hash,
            buyer_nick_masked=mask_nick(str(payload.get("buyer_nick") or "")),
            routing_ciphertext=routing,
            request_id=str(payload.get("request_id") or ""),
            action_mode=None,
            default_owner_mode=(
                "bot" if self.settings.mockchat_auto_reply_enabled else "human"
            ),
            job_max_attempts=self.settings.channel_agent_max_attempts,
        )
        if record.is_new:
            self.db.audit(
                "mockchat.message.received",
                "mockchat",
                record.event_id,
                {"conversation_id": record.conversation_id, "shop_id": payload["shop_id"]},
                tenant_id,
            )
        return self._recorder.load_envelope(
            tenant_id=tenant_id,
            event_id=record.event_id,
            is_duplicate=not record.is_new,
            agent_job_id=record.job_id,
            kind_resolver=self.message_kind,
        )

    def send_reply(self, command: SendCommand) -> SendReceipt:
        self._require_enabled()
        with self.db.connect() as conn:
            conversation = conn.execute(
                "SELECT * FROM channel_conversations WHERE id=? AND tenant_id=? AND platform=?",
                (command.conversation_id, command.tenant_id, self.platform),
            ).fetchone()
            existing = conn.execute(
                "SELECT * FROM channel_outbox WHERE tenant_id=? AND idempotency_key=?",
                (command.tenant_id, command.idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["conversation_id"] != command.conversation_id:
                    raise ChannelAdapterError(
                        "outbox idempotency key belongs to another conversation",
                        kind="conflict",
                        platform=self.platform,
                    )
                if existing["status"] == "failed":
                    raise ChannelAdapterError(
                        "previous send did not complete; reconcile delivery state "
                        "before retrying",
                        kind="conflict",
                        platform=self.platform,
                    )
                return SendReceipt.from_outbox_view(self.platform, dict(existing))
            inbound = self._inbound_for(conn, command)
        if conversation is None or inbound is None:
            raise ChannelAdapterError(
                "channel conversation or inbound routing context not found",
                kind="not_found",
                platform=self.platform,
            )
        ownership.assert_send_ownership(
            str(conversation["owner_mode"]), command.allow_bot, self.platform
        )
        self._check_rate(f"outbound:{command.tenant_id}")
        try:
            routing = json.loads(str(inbound["routing_ciphertext"] or "{}"))
        except ValueError:
            routing = {}
        safe_text, _ = redact_sensitive(command.text)
        action = {
            "receiver_hash": routing.get("receiver_hash"),
            "conversation": str(conversation["external_conversation_id"]),
            "text": safe_text,
        }
        queued = self.outbox.enqueue(
            tenant_id=command.tenant_id,
            conversation_id=command.conversation_id,
            event_id=str(inbound["id"]),
            idempotency_key=command.idempotency_key,
            content_redacted=safe_text,
            payload_ciphertext=json.dumps(
                {"virtual": True, "action": action}, ensure_ascii=False
            ),
            actor=command.actor,
            allow_bot=command.allow_bot,
        )
        return self._dispatch(str(queued["id"]))

    def _dispatch(self, outbox_id: str) -> SendReceipt:
        # The simulated platform is local, so delivery settles synchronously;
        # a targeted claim keeps concurrent same-key sends single-delivery.
        worker_id = f"mockchat-{uuid.uuid4().hex}"
        claimed = self.outbox.claim_due(worker_id, limit=1, outbox_id=outbox_id)
        if not claimed:
            current = self.outbox.get(outbox_id)
            if current is None:
                raise ChannelAdapterError(
                    "mockchat outbox item disappeared", kind="not_found", platform=self.platform
                )
            return SendReceipt.from_outbox_view(self.platform, current)
        item = claimed[0]
        payload = json.loads(str(item["payload_ciphertext"]))
        self.outbox.mark_dispatch_started(outbox_id, worker_id)
        try:
            result = self.transport.deliver(payload["action"])
        except MockChatRejected as exc:
            saved = self.outbox.mark_failed(
                outbox_id, worker_id, kind="rejected", error=str(exc)
            )
        except MockChatUncertain as exc:
            saved = self.outbox.mark_failed(
                outbox_id, worker_id, kind="uncertain", error=str(exc)
            )
        except ConnectionError as exc:
            saved = self.outbox.mark_failed(
                outbox_id, worker_id, kind="retryable", error=str(exc)
            )
        else:
            saved = self.outbox.mark_confirmed(outbox_id, worker_id, result)
            self.db.audit(
                "mockchat.message.sent",
                str(item["actor"]),
                outbox_id,
                {"conversation_id": item["conversation_id"]},
                str(item["tenant_id"]),
            )
        return SendReceipt.from_outbox_view(self.platform, saved)

    def retry_pending(self, outbox_id: str) -> SendReceipt:
        """Re-dispatch an item whose earlier attempt hit a retryable failure."""
        return self._dispatch(outbox_id)

    def create_reply_draft(self, command: ReplyDraftCommand) -> dict[str, Any]:
        self._require_enabled()
        view, created = drafts.create_reply_draft(
            self.db, platform=self.platform, command=command
        )
        if created:
            self.db.audit(
                "mockchat.reply_draft.created",
                command.actor,
                str(view["id"]),
                {"conversation_id": command.conversation_id, "risk_level": command.risk_level},
                command.tenant_id,
            )
        return view

    def change_ownership(self, command: OwnershipCommand) -> dict[str, Any]:
        self._require_enabled()
        return ownership.change_ownership(
            self.db, platform=self.platform, command=command
        )

    def _inbound_for(self, conn: Any, command: SendCommand) -> Any:
        if command.source_event_id:
            return conn.execute(
                """
                SELECT * FROM channel_events
                WHERE id=? AND tenant_id=? AND conversation_id=? AND direction='inbound'
                """,
                (command.source_event_id, command.tenant_id, command.conversation_id),
            ).fetchone()
        return conn.execute(
            """
            SELECT * FROM channel_events
            WHERE conversation_id=? AND direction='inbound'
            ORDER BY created_at DESC LIMIT 1
            """,
            (command.conversation_id,),
        ).fetchone()

    def _require_enabled(self) -> None:
        if not self.settings.mockchat_enabled:
            raise ChannelAdapterError(
                "mockchat channel is disabled",
                kind="capability_unavailable",
                platform=self.platform,
            )
        if not self.settings.mockchat_secret:
            raise ChannelAdapterError(
                "MOCKCHAT_SECRET is not configured",
                kind="capability_unavailable",
                platform=self.platform,
            )

    def _check_rate(self, key: str) -> None:
        try:
            self._limiter.check(f"mockchat:{key}")
        except RateLimitError as exc:
            raise ChannelAdapterError(
                "mockchat declared rate limit exceeded",
                kind="rate_limited",
                platform=self.platform,
            ) from exc
