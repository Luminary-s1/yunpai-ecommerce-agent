from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Callable, Literal, Mapping
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field

from .channel_sdk import drafts as channel_drafts
from .channel_sdk import ownership as channel_ownership
from .channel_sdk.contracts import (
    ChannelAdapterError,
    OwnershipCommand,
    ReplyDraftCommand,
    hash_subject,
    mask_nick,
)
from .channel_sdk.inbound import ChannelInboundRecorder
from .config import Settings
from .database import Database, utc_now
from .outbox import DurableOutbox, OutboxError, OutboxReconcileRequest
from .text_utils import redact_sensitive


class TaobaoError(ValueError):
    def __init__(self, message: str, *, kind: str | None = None):
        super().__init__(message)
        self.kind = kind


class TaobaoRemoteError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        outcome: Literal["rejected", "uncertain"] = "uncertain",
        code: str | None = None,
    ):
        super().__init__(message)
        self.outcome = outcome
        self.code = code


class OwnershipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_mode: Literal["bot", "human", "paused"]
    expected_version: int = Field(ge=1)
    assigned_to: str | None = Field(default=None, max_length=128)


class ChannelReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    source_event_id: str | None = Field(default=None, max_length=128)


class ReplyDraftCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_conversation_version: int = Field(ge=1)
    ai_suggestion: str = Field(min_length=1, max_length=2000)
    final_text: str | None = Field(default=None, min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    sop_id: str | None = Field(default=None, max_length=128)
    sop_version: int | None = Field(default=None, ge=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    risk_level: Literal["low", "medium", "high", "critical"] = "low"
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    source_event_id: str | None = Field(default=None, max_length=128)


class ReplyDraftUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_record_version: int = Field(ge=1)
    final_text: str = Field(min_length=1, max_length=2000)


class ReplyDraftSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_record_version: int = Field(ge=1)


class SubscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_nicks: list[str] = Field(min_length=1, max_length=20)
    enabled: bool = True


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def sign_parameters(
    params: Mapping[str, Any], app_secret: str, sign_method: str = "hmac"
) -> str:
    """Generate a TOP/Qimen signature from all non-empty parameters except sign."""
    canonical = "".join(
        f"{key}{_stringify(value)}"
        for key, value in sorted(params.items())
        if key != "sign" and value is not None
    ).encode("utf-8")
    secret = app_secret.encode("utf-8")
    normalized = sign_method.lower().replace("_", "-")
    if normalized == "md5":
        return hashlib.md5(secret + canonical + secret).hexdigest().upper()  # noqa: S324
    if normalized == "hmac":
        return hmac.new(secret, canonical, hashlib.md5).hexdigest().upper()  # noqa: S324
    if normalized in {"hmac-sha256", "sha256"}:
        return hmac.new(secret, canonical, hashlib.sha256).hexdigest().upper()
    raise TaobaoError(f"unsupported Taobao sign method: {sign_method}")


def verify_signature(params: Mapping[str, Any], app_secret: str) -> bool:
    supplied = str(params.get("sign") or "")
    if not supplied:
        return False
    expected = sign_parameters(params, app_secret, str(params.get("sign_method") or "md5"))
    return hmac.compare_digest(expected, supplied.upper())


class CredentialCipher:
    def __init__(self, encoded_key: str):
        try:
            key = base64.urlsafe_b64decode(encoded_key.encode("ascii"))
        except Exception as exc:
            raise TaobaoError("TAOBAO_CREDENTIAL_KEY must be URL-safe base64") from exc
        if len(key) != 32:
            raise TaobaoError("TAOBAO_CREDENTIAL_KEY must decode to exactly 32 bytes")
        self._cipher = AESGCM(key)

    def encrypt(self, value: Mapping[str, Any]) -> str:
        nonce = secrets.token_bytes(12)
        plaintext = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        encrypted = self._cipher.encrypt(nonce, plaintext, b"yunpai-taobao-v1")
        return base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")

    def decrypt(self, value: str) -> dict[str, Any]:
        try:
            payload = base64.urlsafe_b64decode(value.encode("ascii"))
            plaintext = self._cipher.decrypt(payload[:12], payload[12:], b"yunpai-taobao-v1")
            result = json.loads(plaintext.decode("utf-8"))
        except Exception as exc:
            raise TaobaoError("stored Taobao credential cannot be decrypted") from exc
        if not isinstance(result, dict):
            raise TaobaoError("stored Taobao credential has an invalid shape")
        return result


class TaobaoTopClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None

    def exchange_authorization_code(self, code: str) -> dict[str, Any]:
        response = self._client.post(
            self.settings.taobao_oauth_token_url,
            data={
                "grant_type": "authorization_code",
                "client_id": self.settings.taobao_app_key,
                "client_secret": self.settings.taobao_app_secret,
                "code": code,
                "redirect_uri": self.settings.taobao_redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        return self._decode_response(response, oauth=True)

    def call(
        self,
        method: str,
        business_params: Mapping[str, Any],
        *,
        session: str | None = None,
    ) -> dict[str, Any]:
        common: dict[str, Any] = {
            "method": method,
            "app_key": self.settings.taobao_app_key,
            "timestamp": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
            "format": "json",
            "v": "2.0",
            "sign_method": "hmac",
            "simplify": "true",
        }
        if session:
            common["session"] = session
        payload = {**common, **{key: _stringify(value) for key, value in business_params.items()}}
        payload["sign"] = sign_parameters(payload, self.settings.taobao_app_secret, "hmac")
        response = self._client.post(self.settings.taobao_top_gateway, data=payload)
        return self._decode_response(response)

    @staticmethod
    def _decode_response(response: httpx.Response, *, oauth: bool = False) -> dict[str, Any]:
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TaobaoRemoteError(f"Taobao returned an invalid HTTP response: {exc}") from exc
        if not isinstance(payload, dict):
            raise TaobaoRemoteError("Taobao returned a non-object response")
        error = payload.get("error_response") or (payload if oauth and payload.get("error") else None)
        if error:
            code = error.get("code") or error.get("error") or "unknown"
            message = error.get("msg") or error.get("error_description") or "Taobao API error"
            raise TaobaoRemoteError(
                f"Taobao API {code}: {message}", outcome="rejected", code=str(code)
            )
        return payload

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


@dataclass(frozen=True, slots=True)
class InboundMessage:
    conversation_id: str
    event_id: str
    is_new: bool
    text: str
    buyer_id: str
    owner_mode: str
    job_id: str | None = None


class TaobaoIntegrationService:
    PLATFORM = "taobao"

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        top_client: TaobaoTopClient | None = None,
    ):
        self.db = db
        self.settings = settings
        self.top = top_client or TaobaoTopClient(settings)
        self.recorder = ChannelInboundRecorder(db)
        self.outbox = DurableOutbox(
            db,
            lease_seconds=settings.outbox_lease_seconds,
            max_attempts=settings.outbox_max_attempts,
            retry_base_seconds=settings.outbox_retry_base_seconds,
            retry_max_seconds=settings.outbox_retry_max_seconds,
            platform=self.PLATFORM,
        )
        self._worker_thread: threading.Thread | None = None
        self._worker_stop = threading.Event()
        self._worker_lock = threading.Lock()
        self._worker_last_error: str | None = None
        self._worker_processed = 0
        self._delivery_observer: Callable[[dict[str, Any]], None] | None = None

    def set_delivery_observer(
        self, observer: Callable[[dict[str, Any]], None]
    ) -> None:
        self._delivery_observer = observer

    @property
    def cipher(self) -> CredentialCipher:
        if not self.settings.taobao_credential_key:
            raise TaobaoError("TAOBAO_CREDENTIAL_KEY is not configured")
        return CredentialCipher(self.settings.taobao_credential_key)

    def capabilities(self, tenant_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='authorized' THEN 1 ELSE 0 END) AS authorized
                FROM platform_connections WHERE tenant_id=? AND platform=?
                """,
                (tenant_id, self.PLATFORM),
            ).fetchone()
        app_credentials = bool(self.settings.taobao_app_key and self.settings.taobao_app_secret)
        secure_store = self._cipher_configured()
        oauth = bool(app_credentials and self.settings.taobao_redirect_uri and secure_store)
        receiver_configured = bool(
            self.settings.taobao_enabled
            and app_credentials
            and secure_store
            and self.settings.taobao_qimen_customer_id
        )
        inbound = bool(receiver_configured and self.settings.taobao_qimen_route_verified)
        outbound = bool(
            inbound
            and self.settings.taobao_chatrobot_request_token
            and self.settings.taobao_chatrobot_tenant_id
            and int(row["authorized"] or 0) > 0
        )
        return {
            "platform": self.PLATFORM,
            "enabled": self.settings.taobao_enabled,
            "mode": "auto_reply" if self.settings.taobao_auto_reply_enabled else "manual_takeover",
            "connections": {"total": int(row["total"] or 0), "authorized": int(row["authorized"] or 0)},
            "official_contract": {
                "access_model": "service_market_customer_service_robot",
                "merchant_ui": "independent_ecommerce_backend",
                "inbound_method": "qimen.taobao.message.chatrobot.sync",
                "outbound_method": "taobao.message.chatrobot.async",
                "subscription_methods": [
                    "taobao.message.chatrobot.assist.subscribe",
                    "taobao.message.chatrobot.assist.query",
                ],
                "top_gateway": self.settings.taobao_top_gateway,
                "requires_platform_allocated": ["customerId", "request_token", "tenant_id"],
            },
            "capabilities": {
                "oauth_authorization": self._gate(oauth, "AppKey/AppSecret、回调地址和凭据加密密钥"),
                "qimen_receiver_configured": self._gate(
                    receiver_configured, "奇门 customerId、应用密钥和凭据加密密钥"
                ),
                "qimen_inbound": self._gate(
                    inbound, "收到淘宝测试消息并设置 TAOBAO_QIMEN_ROUTE_VERIFIED=true"
                ),
                "chatrobot_outbound": self._gate(
                    outbound, "数据平台分配的 request_token/tenant_id 和已授权店铺"
                ),
                "manual_takeover": self._gate(outbound, "先打通消息接收与机器人异步回写"),
                "automatic_reply": self._gate(
                    outbound and self.settings.taobao_auto_reply_enabled,
                    "显式 TAOBAO_AUTO_REPLY_ENABLED=true 并通过灰度验收",
                ),
            },
        }

    @staticmethod
    def _gate(available: bool, missing: str) -> dict[str, Any]:
        return {"available": available, "missing_when_unavailable": None if available else missing}

    def _cipher_configured(self) -> bool:
        try:
            self.cipher
        except TaobaoError:
            return False
        return True

    def begin_authorization(self, tenant_id: str, shop_id: str) -> dict[str, str]:
        if not self.capabilities(tenant_id)["capabilities"]["oauth_authorization"]["available"]:
            raise TaobaoError("Taobao OAuth is not fully configured")
        state = secrets.token_urlsafe(32)
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                "DELETE FROM platform_oauth_states WHERE platform=? AND expires_at < ?",
                (self.PLATFORM, now.isoformat()),
            )
            conn.execute(
                """
                INSERT INTO platform_oauth_states(
                    state_hash, tenant_id, platform, shop_id, expires_at, used_at, created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    state_hash,
                    tenant_id,
                    self.PLATFORM,
                    shop_id,
                    (now + timedelta(minutes=10)).isoformat(),
                    now.isoformat(),
                ),
            )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.taobao_app_key,
                "redirect_uri": self.settings.taobao_redirect_uri,
                "state": state,
                "view": "web",
            }
        )
        return {"authorization_url": f"{self.settings.taobao_oauth_authorize_url}?{query}", "state": state}

    def complete_authorization(self, code: str, state: str) -> dict[str, Any]:
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)
        with self.db._write_lock, self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM platform_oauth_states WHERE state_hash=? AND platform=?",
                (state_hash, self.PLATFORM),
            ).fetchone()
            if row is None or row["used_at"] is not None:
                raise TaobaoError("OAuth state is invalid or has already been used")
            if datetime.fromisoformat(str(row["expires_at"])) < now:
                raise TaobaoError("OAuth state has expired")
            conn.execute(
                "UPDATE platform_oauth_states SET used_at=? WHERE state_hash=?",
                (now.isoformat(), state_hash),
            )
        token = self.top.exchange_authorization_code(code)
        access_token = str(token.get("access_token") or "")
        if not access_token:
            raise TaobaoRemoteError("Taobao OAuth response did not contain access_token")
        account_id = str(token.get("taobao_user_id") or token.get("user_id") or "") or None
        account_nick = str(token.get("taobao_user_nick") or token.get("user_nick") or "") or None
        expires_in = int(token.get("expires_in") or 0)
        expires_at = (now + timedelta(seconds=expires_in)).isoformat() if expires_in else None
        encrypted = self.cipher.encrypt(token)
        connection_id = f"connection-{uuid.uuid4().hex}"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_connections(
                    id, tenant_id, platform, shop_id, status, account_id, account_nick,
                    credential_ciphertext, token_expires_at, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'authorized', ?, ?, ?, ?, '{}', ?, ?)
                ON CONFLICT(tenant_id, platform, shop_id) DO UPDATE SET
                    status='authorized', account_id=excluded.account_id,
                    account_nick=excluded.account_nick,
                    credential_ciphertext=excluded.credential_ciphertext,
                    token_expires_at=excluded.token_expires_at, updated_at=excluded.updated_at
                """,
                (
                    connection_id,
                    row["tenant_id"],
                    self.PLATFORM,
                    row["shop_id"],
                    account_id,
                    account_nick,
                    encrypted,
                    expires_at,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            saved = conn.execute(
                """
                SELECT id, shop_id, status, account_id, account_nick, token_expires_at
                FROM platform_connections WHERE tenant_id=? AND platform=? AND shop_id=?
                """,
                (row["tenant_id"], self.PLATFORM, row["shop_id"]),
            ).fetchone()
        self.db.audit(
            "taobao.oauth.authorized",
            "taobao-oauth",
            str(saved["id"]),
            {"shop_id": saved["shop_id"], "account_id": saved["account_id"]},
            str(row["tenant_id"]),
        )
        return dict(saved)

    def receive_qimen(self, params: Mapping[str, str]) -> InboundMessage:
        self._validate_qimen_request(params)
        try:
            event = json.loads(params["event"])
            header = event["header"]
            body = event["body"]
        except (KeyError, TypeError, ValueError) as exc:
            raise TaobaoError(
                "Qimen event is not valid JSON or misses header/body", kind="schema"
            ) from exc
        raw_message_type = str(body.get("contentType") or "1")
        if raw_message_type == "1":
            content = body.get("content") or ""
            if isinstance(content, str):
                try:
                    decoded_content = json.loads(content)
                    text = str(decoded_content.get("text") or content)
                except (ValueError, AttributeError):
                    text = content
            else:
                text = str(content.get("text") or "") if isinstance(content, dict) else str(content)
            if not text.strip():
                raise TaobaoError("Qimen chat message does not contain text", kind="schema")
        else:
            # Non-text payloads (images, cards, …) are recorded instead of
            # dropped, but only as a redaction-safe marker: the media payload
            # itself is never trusted as conversation text.
            text = f"[非文本消息:contentType={raw_message_type}]"

        buyer_id = str(params.get("buyerId") or self._party_value(body.get("sender"), "id") or "")
        buyer_nick = str(params.get("buyerNick") or self._party_value(body.get("sender"), "nick") or "")
        seller_id = str(params.get("sellerId") or "")
        seller_nick = str(params.get("sellerNick") or "")
        shop_id = seller_id or seller_nick or self.settings.taobao_qimen_customer_id
        external_event_id = str(body.get("msgId") or body.get("bizUniqueId") or header.get("requestId") or "")
        if not buyer_id or not shop_id or not external_event_id:
            raise TaobaoError(
                "Qimen event misses buyer, shop, or message identifier", kind="schema"
            )
        external_conversation_id = str(body.get("bizUniqueId") or f"{shop_id}:{buyer_id}")
        buyer_hash = hash_subject(
            self.settings.subject_hash_key or self.settings.taobao_app_secret, buyer_id
        )
        routing = {
            "header": header,
            "sender": body.get("sender"),
            "receivers": body.get("receivers") or [],
            "channel_type": body.get("channelType") or "bc",
        }
        routing_ciphertext = self.cipher.encrypt(routing)
        safe_text, _ = redact_sensitive(text)
        record = self.recorder.record(
            tenant_id=self.settings.bootstrap_tenant_id,
            platform=self.PLATFORM,
            shop_id=shop_id,
            external_conversation_id=external_conversation_id,
            external_event_id=external_event_id,
            message_type=raw_message_type,
            content_redacted=safe_text,
            payload_hash=hashlib.sha256(params["event"].encode("utf-8")).hexdigest(),
            buyer_hash=buyer_hash,
            buyer_nick_masked=mask_nick(buyer_nick),
            routing_ciphertext=routing_ciphertext,
            request_id=str(header.get("requestId") or ""),
            action_mode=str(header.get("actionMode") or ""),
            default_owner_mode=(
                "bot" if self.settings.taobao_auto_reply_enabled else "human"
            ),
            job_max_attempts=self.settings.channel_agent_max_attempts,
        )
        if record.is_new:
            self.db.audit(
                "taobao.message.received",
                "qimen",
                record.event_id,
                {"conversation_id": record.conversation_id, "shop_id": shop_id},
                self.settings.bootstrap_tenant_id,
            )
        return InboundMessage(
            record.conversation_id,
            record.event_id,
            record.is_new,
            text,
            buyer_id,
            record.owner_mode,
            record.job_id,
        )

    def _validate_qimen_request(self, params: Mapping[str, str]) -> None:
        if not self.settings.taobao_enabled:
            raise TaobaoError("Taobao integration is disabled", kind="capability_unavailable")
        if not self.settings.taobao_app_secret:
            raise TaobaoError("TAOBAO_APP_SECRET is not configured", kind="capability_unavailable")
        if params.get("app_key") != self.settings.taobao_app_key:
            raise TaobaoError("Qimen app_key does not match this appliance", kind="authentication")
        if params.get("customerId") != self.settings.taobao_qimen_customer_id:
            raise TaobaoError(
                "Qimen customerId does not match this appliance", kind="authentication"
            )
        if not verify_signature(params, self.settings.taobao_app_secret):
            raise TaobaoError("Qimen signature verification failed", kind="signature")
        raw_timestamp = params.get("timestamp") or ""
        try:
            parsed = datetime.strptime(raw_timestamp, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone(timedelta(hours=8))
            )
        except ValueError as exc:
            raise TaobaoError("Qimen timestamp has an invalid format", kind="schema") from exc
        skew = abs((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds())
        if skew > self.settings.taobao_callback_max_skew_seconds:
            raise TaobaoError(
                "Qimen timestamp is outside the replay-protection window", kind="replay"
            )

    def list_conversations(self, tenant_id: str, owner_mode: str | None = None) -> list[dict[str, Any]]:
        where = " AND owner_mode=?" if owner_mode else ""
        params: tuple[Any, ...] = (tenant_id, self.PLATFORM, owner_mode) if owner_mode else (
            tenant_id,
            self.PLATFORM,
        )
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, shop_id, buyer_nick_masked, owner_mode, assigned_to, version,
                       last_message_at, created_at, updated_at
                FROM channel_conversations WHERE tenant_id=? AND platform=?{where}
                ORDER BY updated_at DESC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def conversation_detail(self, conversation_id: str, tenant_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            conversation = conn.execute(
                "SELECT * FROM channel_conversations WHERE id=? AND tenant_id=? AND platform=?",
                (conversation_id, tenant_id, self.PLATFORM),
            ).fetchone()
            if conversation is None:
                raise TaobaoError("Taobao conversation not found", kind="not_found")
            events = conn.execute(
                """
                SELECT id, external_event_id, direction, message_type, content_redacted,
                       request_id, action_mode, status, created_at, updated_at
                FROM channel_events WHERE conversation_id=? ORDER BY created_at
                """,
                (conversation_id,),
            ).fetchall()
            drafts = conn.execute(
                "SELECT * FROM channel_reply_drafts WHERE conversation_id=? AND tenant_id=? "
                "ORDER BY created_at DESC",
                (conversation_id, tenant_id),
            ).fetchall()
            agent_jobs = conn.execute(
                """
                SELECT j.*, i.response_json FROM channel_agent_jobs j
                LEFT JOIN agent_invocations i ON i.id=j.agent_invocation_id
                WHERE j.conversation_id=? AND j.tenant_id=?
                ORDER BY j.created_at DESC
                """,
                (conversation_id, tenant_id),
            ).fetchall()
        job_views: list[dict[str, Any]] = []
        for row in agent_jobs:
            view = dict(row)
            response_json = view.pop("response_json", None)
            view["agent_response"] = json.loads(response_json) if response_json else None
            job_views.append(view)
        return {
            "conversation": dict(conversation),
            "events": [dict(row) for row in events],
            "drafts": [self._draft_view(row) for row in drafts],
            "agent_jobs": job_views,
        }

    def change_ownership(
        self,
        conversation_id: str,
        tenant_id: str,
        request: OwnershipRequest,
        operator: str,
    ) -> dict[str, Any]:
        try:
            return channel_ownership.change_ownership(
                self.db,
                platform=self.PLATFORM,
                command=OwnershipCommand(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    owner_mode=request.owner_mode,
                    expected_version=request.expected_version,
                    assigned_to=request.assigned_to,
                    actor=operator,
                ),
            )
        except ChannelAdapterError as exc:
            raise TaobaoError(str(exc), kind=exc.kind) from exc

    def create_reply_draft(
        self,
        conversation_id: str,
        tenant_id: str,
        request: ReplyDraftCreateRequest,
        operator: str,
    ) -> dict[str, Any]:
        try:
            view, created = channel_drafts.create_reply_draft(
                self.db,
                platform=self.PLATFORM,
                command=ReplyDraftCommand(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    expected_conversation_version=request.expected_conversation_version,
                    ai_suggestion=request.ai_suggestion,
                    final_text=request.final_text,
                    evidence_ids=request.evidence_ids,
                    sop_id=request.sop_id,
                    sop_version=request.sop_version,
                    confidence=request.confidence,
                    risk_level=request.risk_level,
                    idempotency_key=request.idempotency_key,
                    source_event_id=request.source_event_id,
                    actor=operator,
                ),
            )
        except ChannelAdapterError as exc:
            raise TaobaoError(str(exc), kind=exc.kind) from exc
        if created:
            self.db.audit(
                "taobao.reply_draft.created", operator, str(view["id"]),
                {"conversation_id": conversation_id, "risk_level": request.risk_level}, tenant_id,
            )
        return view

    def update_reply_draft(
        self,
        conversation_id: str,
        draft_id: str,
        tenant_id: str,
        request: ReplyDraftUpdateRequest,
        operator: str,
    ) -> dict[str, Any]:
        final_text, _ = redact_sensitive(request.final_text)
        with self.db._write_lock, self.db.connect() as conn:
            current = conn.execute(
                "SELECT * FROM channel_reply_drafts WHERE id=? AND conversation_id=? AND tenant_id=?",
                (draft_id, conversation_id, tenant_id),
            ).fetchone()
            if current is None:
                raise TaobaoError("reply draft not found", kind="not_found")
            diff = self._reply_diff(current["ai_suggestion_redacted"], final_text)
            cursor = conn.execute(
                """
                UPDATE channel_reply_drafts SET final_text_redacted=?, diff_json=?,
                    status='draft', last_error=NULL, record_version=record_version+1, updated_at=?
                WHERE id=? AND conversation_id=? AND tenant_id=?
                  AND record_version=? AND status IN ('draft','failed')
                """,
                (
                    final_text, json.dumps(diff, ensure_ascii=False), utc_now(), draft_id,
                    conversation_id, tenant_id, request.expected_record_version,
                ),
            )
            if cursor.rowcount != 1:
                raise TaobaoError("reply draft transition or version conflict", kind="conflict")
            saved = conn.execute(
                "SELECT * FROM channel_reply_drafts WHERE id=?", (draft_id,)
            ).fetchone()
        self.db.audit(
            "taobao.reply_draft.edited", operator, draft_id,
            {"changed_segments": len(diff)}, tenant_id,
        )
        return self._draft_view(saved)

    def send_reply_draft(
        self,
        conversation_id: str,
        draft_id: str,
        tenant_id: str,
        request: ReplyDraftSendRequest,
        operator: str,
    ) -> dict[str, Any]:
        capability = self.capabilities(tenant_id)["capabilities"]["chatrobot_outbound"]
        if not capability["available"]:
            raise TaobaoError(
                f"Taobao outbound is unavailable: {capability['missing_when_unavailable']}",
                kind="capability_unavailable",
            )
        with self.db._write_lock, self.db.connect() as conn:
            draft = conn.execute(
                "SELECT * FROM channel_reply_drafts WHERE id=? AND conversation_id=? AND tenant_id=?",
                (draft_id, conversation_id, tenant_id),
            ).fetchone()
            if draft is None:
                raise TaobaoError("reply draft not found", kind="not_found")
            cursor = conn.execute(
                """
                UPDATE channel_reply_drafts SET status='sending',
                    record_version=record_version+1, updated_at=?
                WHERE id=? AND record_version=? AND status='draft'
                """,
                (utc_now(), draft_id, request.expected_record_version),
            )
            if cursor.rowcount != 1:
                raise TaobaoError("reply draft transition or version conflict", kind="conflict")
        try:
            outbox = self.send_reply(
                conversation_id,
                tenant_id,
                ChannelReplyRequest(
                    text=draft["final_text_redacted"],
                    idempotency_key=draft["idempotency_key"],
                ),
                operator,
            )
        except Exception as exc:
            outbox = self.outbox.get_by_key(tenant_id, str(draft["idempotency_key"]))
            draft_status = (
                "sending"
                if outbox is not None and outbox["status"] in {"queued", "sending"}
                else "failed"
            )
            with self.db._write_lock, self.db.connect() as conn:
                conn.execute(
                    """
                    UPDATE channel_reply_drafts SET status=?, outbox_id=?, last_error=?,
                        sent_by=?, record_version=record_version+1, updated_at=? WHERE id=?
                    """,
                    (
                        draft_status,
                        outbox["id"] if outbox else None,
                        str(exc)[:500],
                        operator,
                        utc_now(),
                        draft_id,
                    ),
                )
            raise
        sent = outbox["status"] == "sent" and outbox["delivery_state"] == "confirmed"
        with self.db._write_lock, self.db.connect() as conn:
            now = utc_now()
            conn.execute(
                """
                UPDATE channel_reply_drafts SET status=?, outbox_id=?, sent_by=?,
                    sent_at=?, last_error=NULL, record_version=record_version+1,
                    updated_at=? WHERE id=?
                """,
                (
                    "sent" if sent else "sending",
                    outbox["id"],
                    operator,
                    now if sent else None,
                    now,
                    draft_id,
                ),
            )
            saved = conn.execute(
                "SELECT * FROM channel_reply_drafts WHERE id=?", (draft_id,)
            ).fetchone()
        self.db.audit(
            "taobao.reply_draft.sent" if sent else "taobao.reply_draft.queued",
            operator,
            draft_id,
            {"conversation_id": conversation_id, "outbox_id": outbox["id"]},
            tenant_id,
        )
        return self._draft_view(saved)

    def send_reply(
        self,
        conversation_id: str,
        tenant_id: str,
        request: ChannelReplyRequest,
        operator: str,
        *,
        allow_bot: bool = False,
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            conversation = conn.execute(
                "SELECT * FROM channel_conversations WHERE id=? AND tenant_id=? AND platform=?",
                (conversation_id, tenant_id, self.PLATFORM),
            ).fetchone()
            existing = conn.execute(
                "SELECT * FROM channel_outbox WHERE tenant_id=? AND idempotency_key=?",
                (tenant_id, request.idempotency_key),
            ).fetchone()
            if existing is not None:
                return self._existing_outbox_result(dict(existing), conversation_id)
            if request.source_event_id:
                inbound = conn.execute(
                    """
                    SELECT * FROM channel_events
                    WHERE id=? AND tenant_id=? AND conversation_id=? AND direction='inbound'
                    """,
                    (request.source_event_id, tenant_id, conversation_id),
                ).fetchone()
            else:
                inbound = conn.execute(
                    """
                    SELECT * FROM channel_events
                    WHERE conversation_id=? AND direction='inbound'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (conversation_id,),
                ).fetchone()
        if conversation is None or inbound is None:
            raise TaobaoError(
                "Taobao conversation or inbound routing context not found", kind="not_found"
            )
        self._assert_send_ownership(str(conversation["owner_mode"]), allow_bot)
        capability = self.capabilities(tenant_id)["capabilities"]["chatrobot_outbound"]
        if not capability["available"]:
            raise TaobaoError(
                f"Taobao outbound is unavailable: {capability['missing_when_unavailable']}",
                kind="capability_unavailable",
            )
        routing = self.cipher.decrypt(str(inbound["routing_ciphertext"] or ""))
        action = self._build_reply_action(routing, request.text)
        safe_text, _ = redact_sensitive(request.text)
        payload_ciphertext = self.cipher.encrypt(
            {
                "action": action,
                "shop_id": str(conversation["shop_id"]),
                "operator": operator,
                "allow_bot": allow_bot,
            }
        )
        queued = self.outbox.enqueue(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            event_id=str(inbound["id"]),
            idempotency_key=request.idempotency_key,
            content_redacted=safe_text,
            payload_ciphertext=payload_ciphertext,
            actor=operator,
            allow_bot=allow_bot,
        )
        self.db.audit(
            "taobao.message.queued",
            operator,
            str(queued["id"]),
            {"conversation_id": conversation_id, "idempotency_key": request.idempotency_key},
            tenant_id,
        )
        if self.settings.outbox_sync_dispatch:
            return self.dispatch_outbox_item(
                str(queued["id"]),
                worker_id=f"sync-{uuid.uuid4().hex}",
                raise_on_failure=True,
            )
        return self._outbox_view(queued)

    def dispatch_outbox_item(
        self,
        outbox_id: str,
        *,
        worker_id: str,
        raise_on_failure: bool = False,
    ) -> dict[str, Any]:
        self.outbox.recover_expired_leases()
        self._notify_terminal_outbox_items()
        claimed = self.outbox.claim_due(worker_id, limit=1, outbox_id=outbox_id)
        if not claimed:
            current = self.outbox.get(outbox_id)
            if current is None:
                raise TaobaoError("outbox item not found", kind="not_found")
            return self._outbox_view(current)
        return self._dispatch_claimed(claimed[0], worker_id, raise_on_failure=raise_on_failure)

    def run_outbox_once(self, *, worker_id: str, limit: int | None = None) -> dict[str, Any]:
        recovery = self.outbox.recover_expired_leases()
        self._notify_terminal_outbox_items()
        results: list[dict[str, Any]] = []
        claimed_count = 0
        for _ in range(limit or self.settings.outbox_batch_size):
            claimed = self.outbox.claim_due(worker_id, limit=1)
            if not claimed:
                break
            claimed_count += 1
            results.append(
                self._dispatch_claimed(claimed[0], worker_id, raise_on_failure=False)
            )
        self._worker_processed += len(results)
        return {"claimed": claimed_count, "recovery": recovery, "items": results}

    def reconcile_outbox(
        self,
        outbox_id: str,
        tenant_id: str,
        request: OutboxReconcileRequest,
        operator: str,
    ) -> dict[str, Any]:
        try:
            row = self.outbox.reconcile(outbox_id, tenant_id, request, operator)
        except OutboxError as exc:
            raise TaobaoError(str(exc)) from exc
        self._notify_delivery(row)
        return self._outbox_view(row)

    def list_outbox(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        delivery_state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return [
            self._outbox_view(row)
            for row in self.outbox.list_items(
                tenant_id,
                status=status,
                delivery_state=delivery_state,
                limit=limit,
            )
        ]

    def outbox_summary(self, tenant_id: str) -> dict[str, Any]:
        return {**self.outbox.summary(tenant_id), "worker": self.outbox_worker_status()}

    def _dispatch_claimed(
        self,
        item: dict[str, Any],
        worker_id: str,
        *,
        raise_on_failure: bool,
    ) -> dict[str, Any]:
        outbox_id = str(item["id"])
        tenant_id = str(item["tenant_id"])
        try:
            payload = self.cipher.decrypt(str(item["payload_ciphertext"] or ""))
            with self.db.connect() as conn:
                conversation = conn.execute(
                    "SELECT * FROM channel_conversations WHERE id=? AND tenant_id=? AND platform=?",
                    (item["conversation_id"], tenant_id, self.PLATFORM),
                ).fetchone()
            if conversation is None:
                raise TaobaoError("Taobao conversation no longer exists")
            self._assert_send_ownership(
                str(conversation["owner_mode"]), bool(item["allow_bot"])
            )
            connection = self._connection_for_shop(tenant_id, str(payload.get("shop_id") or ""))
            token = self.cipher.decrypt(str(connection["credential_ciphertext"]))
            access_token = str(token.get("access_token") or "")
            if not access_token:
                raise TaobaoError("authorized Taobao connection has no access token")
        except Exception as exc:
            kind: Literal["retryable", "cancelled"] = (
                "cancelled" if isinstance(exc, TaobaoError) and "owned" in str(exc) else "retryable"
            )
            saved = self.outbox.mark_failed(
                outbox_id, worker_id, kind=kind, error=str(exc)
            )
            self._audit_outbox_failure(saved, kind, str(exc))
            self._notify_delivery(saved)
            if raise_on_failure:
                raise
            return self._outbox_view(saved)

        self.outbox.mark_dispatch_started(outbox_id, worker_id)
        result: dict[str, Any] | None = None
        try:
            result = self.top.call(
                "taobao.message.chatrobot.async",
                {
                    "auth_param": {"request_token": self.settings.taobao_chatrobot_request_token},
                    "actions": [payload["action"]],
                },
                session=access_token,
            )
            self._assert_business_success(result, "taobao.message.chatrobot.async")
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            saved = self.outbox.mark_failed(
                outbox_id, worker_id, kind="retryable", error=str(exc)
            )
            self._audit_outbox_failure(saved, "retryable", str(exc))
            self._notify_delivery(saved)
            if raise_on_failure:
                raise
            return self._outbox_view(saved)
        except TaobaoRemoteError as exc:
            kind = "rejected" if exc.outcome == "rejected" else "uncertain"
            saved = self.outbox.mark_failed(
                outbox_id,
                worker_id,
                kind=kind,
                error=str(exc),
                platform_result=result,
            )
            self._audit_outbox_failure(saved, kind, str(exc))
            self._notify_delivery(saved)
            if raise_on_failure:
                raise
            return self._outbox_view(saved)
        except Exception as exc:
            saved = self.outbox.mark_failed(
                outbox_id, worker_id, kind="uncertain", error=str(exc)
            )
            self._audit_outbox_failure(saved, "uncertain", str(exc))
            self._notify_delivery(saved)
            if raise_on_failure:
                raise
            return self._outbox_view(saved)

        saved = self.outbox.mark_confirmed(outbox_id, worker_id, result)
        self.db.audit(
            "taobao.message.sent",
            str(item["actor"]),
            outbox_id,
            {
                "conversation_id": item["conversation_id"],
                "idempotency_key": item["idempotency_key"],
                "attempt_count": saved["attempt_count"],
            },
            tenant_id,
        )
        return self._outbox_view(saved)

    def _notify_terminal_outbox_items(self) -> None:
        if self._delivery_observer is None:
            return
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM channel_outbox
                WHERE delivery_state IN ('rejected','uncertain','dead_letter')
                ORDER BY updated_at DESC LIMIT 100
                """
            ).fetchall()
        for row in rows:
            self._notify_delivery(dict(row))

    def _notify_delivery(self, row: dict[str, Any]) -> None:
        if self._delivery_observer is None or row.get("delivery_state") not in {
            "rejected",
            "uncertain",
            "dead_letter",
        }:
            return
        try:
            self._delivery_observer(dict(row))
        except Exception as exc:
            self.db.audit(
                "channel.delivery_observer.failed",
                "outbox-worker",
                str(row.get("id") or "unknown"),
                {"error_type": type(exc).__name__},
                str(row.get("tenant_id") or self.settings.bootstrap_tenant_id),
            )

    def _audit_outbox_failure(self, row: dict[str, Any], kind: str, error: str) -> None:
        self.db.audit(
            "taobao.message.delivery_deferred" if row["status"] == "queued" else "taobao.message.failed",
            str(row["actor"]),
            str(row["id"]),
            {
                "kind": kind,
                "delivery_state": row["delivery_state"],
                "attempt_count": row["attempt_count"],
                "error_present": bool(error),
            },
            str(row["tenant_id"]),
        )

    def _assert_send_ownership(self, owner_mode: str, allow_bot: bool) -> None:
        try:
            channel_ownership.assert_send_ownership(owner_mode, allow_bot, self.PLATFORM)
        except ChannelAdapterError as exc:
            raise TaobaoError(str(exc), kind=exc.kind) from exc

    def _existing_outbox_result(
        self, row: dict[str, Any], conversation_id: str
    ) -> dict[str, Any]:
        if row["conversation_id"] != conversation_id:
            raise TaobaoError(
                "outbox idempotency key belongs to another conversation", kind="conflict"
            )
        if row["status"] == "failed":
            raise TaobaoError(
                "previous send did not complete; reconcile delivery state before retrying",
                kind="conflict",
            )
        return self._outbox_view(row)

    def subscribe(self, tenant_id: str, request: SubscribeRequest, operator: str) -> dict[str, Any]:
        capability = self.capabilities(tenant_id)["capabilities"]["chatrobot_outbound"]
        if not capability["available"]:
            raise TaobaoError(f"Taobao subscription is unavailable: {capability['missing_when_unavailable']}")
        connection = self._connection_for_shop(tenant_id, "")
        token = self.cipher.decrypt(str(connection["credential_ciphertext"]))
        result = self.top.call(
            "taobao.message.chatrobot.assist.subscribe",
            {
                "auth_param": {"request_token": self.settings.taobao_chatrobot_request_token},
                "tenant_id": self.settings.taobao_chatrobot_tenant_id,
                "user_nicks": request.user_nicks,
                "status": request.enabled,
            },
            session=str(token.get("access_token") or ""),
        )
        self._assert_business_success(result, "taobao.message.chatrobot.assist.subscribe")
        self.db.audit(
            "taobao.chatrobot.subscription_changed",
            operator,
            str(connection["id"]),
            {"user_nicks": request.user_nicks, "enabled": request.enabled},
            tenant_id,
        )
        status = self.subscription_status(tenant_id)
        active_nicks = {
            str(item.get("user_nick"))
            for item in self._subscription_values(status)
            if item.get("user_nick")
        }
        requested = set(request.user_nicks)
        verified = requested <= active_nicks if request.enabled else requested.isdisjoint(active_nicks)
        return {"mutation": result, "verification": status, "verified": verified}

    def subscription_status(self, tenant_id: str) -> dict[str, Any]:
        capability = self.capabilities(tenant_id)["capabilities"]["chatrobot_outbound"]
        if not capability["available"]:
            raise TaobaoError(
                f"Taobao subscription query is unavailable: {capability['missing_when_unavailable']}"
            )
        connection = self._connection_for_shop(tenant_id, "")
        token = self.cipher.decrypt(str(connection["credential_ciphertext"]))
        result = self.top.call(
            "taobao.message.chatrobot.assist.query",
            {
                "auth_param": {"request_token": self.settings.taobao_chatrobot_request_token},
                "tenant_id": self.settings.taobao_chatrobot_tenant_id,
            },
            session=str(token.get("access_token") or ""),
        )
        self._assert_business_success(result, "taobao.message.chatrobot.assist.query")
        return result

    def _connection_for_shop(self, tenant_id: str, shop_id: str) -> Mapping[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM platform_connections
                WHERE tenant_id=? AND platform=? AND status='authorized'
                  AND (?='' OR shop_id=? OR account_id=?)
                ORDER BY updated_at DESC LIMIT 1
                """,
                (tenant_id, self.PLATFORM, shop_id, shop_id, shop_id),
            ).fetchone()
        if row is None:
            raise TaobaoError("no authorized Taobao connection matches this shop")
        return row

    def _build_reply_action(self, routing: Mapping[str, Any], text: str) -> dict[str, Any]:
        inbound_header = routing.get("header") or {}
        sender = routing.get("sender") or {}
        receivers = routing.get("receivers") or []
        service_sender = self._outbound_user(receivers[0] if receivers else {})
        buyer_receiver = self._outbound_user(sender)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        return {
            "header": {
                "action_mode": inbound_header.get("actionMode"),
                "request_id": inbound_header.get("requestId"),
                "tenant_id": inbound_header.get("tenantId")
                or self.settings.taobao_chatrobot_tenant_id,
                "type": 1,
                "create_time": now_ms,
                "serialize_type": "Json",
            },
            "body": {
                "sender": service_sender,
                "receivers": [buyer_receiver],
                "create_time": now_ms,
                "channel_type": routing.get("channel_type") or "bc",
                "content": json.dumps({"text": text}, ensure_ascii=False, separators=(",", ":")),
                "content_type": 1,
            },
        }

    @staticmethod
    def _outbound_user(party: Any) -> dict[str, str]:
        if not isinstance(party, dict):
            return {}
        mapped = {
            "nick": party.get("nick") or party.get("userNick") or party.get("user_nick"),
            "user_domain": party.get("user_domain") or party.get("domain"),
            "open_uid": party.get("open_uid") or party.get("openUid"),
        }
        return {key: str(value) for key, value in mapped.items() if value not in (None, "")}

    @staticmethod
    def _party_value(party: Any, key: str) -> str:
        if isinstance(party, dict):
            aliases = {"id": ("id", "userId", "user_id"), "nick": ("nick", "userNick", "user_nick")}
            for alias in aliases[key]:
                if party.get(alias):
                    return str(party[alias])
        return ""

    @staticmethod
    def _mask_nick(nick: str) -> str | None:
        return mask_nick(nick)

    @staticmethod
    def _outbox_view(row: dict[str, Any]) -> dict[str, Any]:
        return DurableOutbox.public_view(row)

    @staticmethod
    def _reply_diff(before: str, after: str) -> list[dict[str, str]]:
        return channel_drafts.reply_diff(before, after)

    @staticmethod
    def _draft_view(row: Mapping[str, Any]) -> dict[str, Any]:
        return channel_drafts.draft_view(row)

    @staticmethod
    def _assert_business_success(result: Mapping[str, Any], method: str) -> None:
        response_keys = (
            method.replace(".", "_") + "_response",
            "_".join(method.split(".")[1:]) + "_response",
        )
        payload: Any = next((result[key] for key in response_keys if key in result), result)
        if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
            payload = payload["result"]
        if isinstance(payload, dict):
            success = payload.get("is_success")
            if success is None:
                success = payload.get("success")
            if success is False or str(success).lower() == "false":
                message = payload.get("message") or payload.get("error_message") or "business rejected"
                code = payload.get("error_code") or payload.get("code")
                detail = f"{code}: {message}" if code else str(message)
                raise TaobaoRemoteError(
                    f"{method} failed: {detail}",
                    outcome="rejected",
                    code=str(code) if code else None,
                )

    @staticmethod
    def _subscription_values(result: Mapping[str, Any]) -> list[dict[str, Any]]:
        method = "taobao.message.chatrobot.assist.query"
        response_keys = (
            method.replace(".", "_") + "_response",
            "_".join(method.split(".")[1:]) + "_response",
        )
        payload: Any = next((result[key] for key in response_keys if key in result), result)
        if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
            payload = payload["result"]
        values = payload.get("values", []) if isinstance(payload, dict) else []
        return [item for item in values if isinstance(item, dict)]

    def start_outbox_worker(self) -> None:
        if not self.settings.outbox_worker_enabled:
            return
        with self._worker_lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._worker_stop.clear()
            worker_id = f"worker-{uuid.uuid4().hex}"
            self._worker_thread = threading.Thread(
                target=self._outbox_worker_loop,
                args=(worker_id,),
                name="taobao-outbox-worker",
                daemon=True,
            )
            self._worker_thread.start()

    def stop_outbox_worker(self) -> None:
        with self._worker_lock:
            thread = self._worker_thread
            self._worker_stop.set()
        if thread is not None:
            thread.join(timeout=max(25.0, self.settings.outbox_poll_seconds * 2))
        with self._worker_lock:
            if self._worker_thread is thread and (thread is None or not thread.is_alive()):
                self._worker_thread = None

    def outbox_worker_status(self) -> dict[str, Any]:
        thread = self._worker_thread
        return {
            "enabled": self.settings.outbox_worker_enabled,
            "running": bool(thread and thread.is_alive()),
            "processed": self._worker_processed,
            "last_error": self._worker_last_error,
        }

    def _outbox_worker_loop(self, worker_id: str) -> None:
        while not self._worker_stop.is_set():
            try:
                report = self.run_outbox_once(worker_id=worker_id)
                self._worker_last_error = None
                if report["claimed"]:
                    continue
            except Exception as exc:
                self._worker_last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                self.db.audit(
                    "taobao.outbox.worker_failed",
                    worker_id,
                    None,
                    {"error_type": type(exc).__name__},
                    self.settings.bootstrap_tenant_id,
                )
            self._worker_stop.wait(self.settings.outbox_poll_seconds)

    def close(self) -> None:
        self.stop_outbox_worker()
        self.top.close()
