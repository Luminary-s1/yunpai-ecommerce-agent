from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from conftest import make_settings
from ecommerce_agent.database import Database
from ecommerce_agent.taobao import (
    ChannelReplyRequest,
    CredentialCipher,
    OwnershipRequest,
    ReplyDraftCreateRequest,
    ReplyDraftSendRequest,
    ReplyDraftUpdateRequest,
    SubscribeRequest,
    TaobaoError,
    TaobaoIntegrationService,
    TaobaoRemoteError,
    TaobaoTopClient,
    sign_parameters,
    verify_signature,
)


def configured_settings(tmp_path):
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    return replace(
        make_settings(tmp_path),
        taobao_enabled=True,
        taobao_app_key="app-key-1",
        taobao_app_secret="app-secret-1",
        taobao_redirect_uri="https://example.test/taobao/callback",
        taobao_credential_key=key,
        taobao_qimen_customer_id="customer-1",
        taobao_qimen_route_verified=True,
        taobao_chatrobot_request_token="request-token-1",
        taobao_chatrobot_tenant_id="robot-tenant-1",
        taobao_oauth_token_url="https://mock.test/oauth/token",
        taobao_top_gateway="https://mock.test/top",
    )


def test_signatures_and_credential_cipher_are_deterministic_and_tamper_evident() -> None:
    params = {
        "method": "qimen.taobao.message.chatrobot.sync",
        "app_key": "app-key-1",
        "timestamp": "2026-07-21 12:00:00",
        "sign_method": "md5",
        "event": '{"hello":"world"}',
    }
    params["sign"] = sign_parameters(params, "app-secret-1", "md5")
    assert verify_signature(params, "app-secret-1")
    assert not verify_signature({**params, "event": '{"hello":"changed"}'}, "app-secret-1")

    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    cipher = CredentialCipher(key)
    encrypted = cipher.encrypt({"access_token": "secret", "refresh_token": "refresh"})
    assert "secret" not in encrypted
    assert cipher.decrypt(encrypted)["access_token"] == "secret"
    corrupted = encrypted[:-2] + ("AA" if encrypted[-2:] != "AA" else "BB")
    with pytest.raises(TaobaoError, match="cannot be decrypted"):
        cipher.decrypt(corrupted)


def test_oauth_qimen_manual_takeover_and_idempotent_reply(tmp_path) -> None:
    settings = configured_settings(tmp_path)
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("utf-8"))
        calls.append({"url": str(request.url), "form": form})
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-secret",
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                    "taobao_user_id": "seller-1",
                    "taobao_user_nick": "测试店铺",
                },
            )
        method = form.get("method", [""])[0]
        if method == "taobao.message.chatrobot.assist.subscribe":
            return httpx.Response(
                200,
                json={
                    "message_chatrobot_assist_subscribe_response": {
                        "result": {"success": True, "value": True}
                    }
                },
            )
        if method == "taobao.message.chatrobot.assist.query":
            return httpx.Response(
                200,
                json={
                    "message_chatrobot_assist_query_response": {
                        "result": {
                            "success": True,
                            "values": [{"user_nick": "客服甲", "tenant_id": 43}],
                        }
                    }
                },
            )
        return httpx.Response(200, json={"message_chatrobot_async_response": {"is_success": True}})

    db = Database(settings.app_db_path)
    db.initialize()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    top = TaobaoTopClient(settings, client=http_client)
    service = TaobaoIntegrationService(db, settings, top_client=top)

    started = service.begin_authorization("tenant-test", "seller-1")
    assert "oauth.taobao.com/authorize" in started["authorization_url"]
    connected = service.complete_authorization("authorization-code", started["state"])
    assert connected["status"] == "authorized"
    with pytest.raises(TaobaoError, match="already been used"):
        service.complete_authorization("authorization-code", started["state"])
    assert service.capabilities("tenant-test")["capabilities"]["chatrobot_outbound"][
        "available"
    ]

    event = {
        "header": {
            "actionMode": 1,
            "requestId": "request-100",
            "tenantId": "robot-tenant-1",
            "serializeType": "Json",
            "type": 1,
        },
        "body": {
            "bizUniqueId": "conversation-external-1",
            "channelType": "bc",
            "content": json.dumps({"text": "订单 13800138000 什么时候发货"}, ensure_ascii=False),
            "contentType": 1,
            "messageType": 1,
            "msgId": "message-100",
            "sender": {"domain": "cntaobao", "nick": "买家甲", "role": "buyer"},
            "receivers": [
                {"domain": "cntaobao", "nick": "客服甲", "role": "customService"}
            ],
        },
    }
    timestamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    params = {
        "method": "qimen.taobao.message.chatrobot.sync",
        "app_key": settings.taobao_app_key,
        "timestamp": timestamp,
        "v": "2.0",
        "sign_method": "md5",
        "customerId": settings.taobao_qimen_customer_id,
        "event": json.dumps(event, ensure_ascii=False, separators=(",", ":")),
        "buyerId": "buyer-1",
        "buyerNick": "买家甲",
        "sellerId": "seller-1",
        "sellerNick": "测试店铺",
    }
    params["sign"] = sign_parameters(params, settings.taobao_app_secret, "md5")
    inbound = service.receive_qimen(params)
    duplicate = service.receive_qimen(params)
    assert inbound.is_new is True
    assert duplicate.is_new is False
    assert inbound.owner_mode == "human"

    conversations = service.list_conversations("tenant-test")
    assert len(conversations) == 1
    assert conversations[0]["buyer_nick_masked"] == "买***甲"
    assert conversations[0]["owner_mode"] == "human"
    with db.connect() as conn:
        stored = conn.execute("SELECT content_redacted FROM channel_events").fetchone()[0]
    assert "138****8000" in stored

    paused = service.change_ownership(
        inbound.conversation_id,
        "tenant-test",
        OwnershipRequest(owner_mode="paused", expected_version=1),
        "admin-test",
    )
    assert paused["version"] == 2
    with pytest.raises(TaobaoError, match="owned by the bot"):
        service.send_reply(
            inbound.conversation_id,
            "tenant-test",
            ChannelReplyRequest(text="自动回复不应发送", idempotency_key="auto:message-100"),
            "agent",
            allow_bot=True,
        )
    with pytest.raises(TaobaoError, match="owned by a human"):
        service.send_reply(
            inbound.conversation_id,
            "tenant-test",
            ChannelReplyRequest(text="您好，正在核实", idempotency_key="reply:message-100"),
            "admin-test",
        )
    human = service.change_ownership(
        inbound.conversation_id,
        "tenant-test",
        OwnershipRequest(owner_mode="human", expected_version=2),
        "admin-test",
    )
    assert human["assigned_to"] == "admin-test"

    reply = ChannelReplyRequest(text="您好，预计今天发货", idempotency_key="reply:message-100")
    sent = service.send_reply(inbound.conversation_id, "tenant-test", reply, "admin-test")
    repeated = service.send_reply(inbound.conversation_id, "tenant-test", reply, "admin-test")
    assert sent["status"] == "sent"
    assert sent["delivery_state"] == "confirmed"
    assert repeated["id"] == sent["id"]
    top_calls = [call for call in calls if str(call["url"]).endswith("/top")]
    assert len(top_calls) == 1
    top_form = top_calls[0]["form"]
    assert top_form["method"] == ["taobao.message.chatrobot.async"]
    assert top_form["session"] == ["access-secret"]
    assert top_form["sign"]
    assert top_form["sign_method"] == ["hmac"]
    assert "您好，预计今天发货" in top_form["actions"][0]
    action = json.loads(top_form["actions"][0])[0]
    assert action["body"]["sender"] == {"nick": "客服甲", "user_domain": "cntaobao"}
    assert action["body"]["receivers"] == [
        {"nick": "买家甲", "user_domain": "cntaobao"}
    ]

    draft = service.create_reply_draft(
        inbound.conversation_id,
        "tenant-test",
        ReplyDraftCreateRequest(
            expected_conversation_version=3,
            ai_suggestion="您好，预计今天发货，联系电话 13800138000",
            final_text="您好，仓库核实后预计今天发货",
            evidence_ids=["message-100"],
            sop_id="builtin.order_change_control",
            sop_version=1,
            confidence=0.91,
            risk_level="medium",
            idempotency_key="reply:draft:message-100",
        ),
        "admin-test",
    )
    assert draft["status"] == "draft"
    assert "13800138000" not in draft["ai_suggestion_redacted"]
    assert draft["diff"]
    repeated_draft = service.create_reply_draft(
        inbound.conversation_id,
        "tenant-test",
        ReplyDraftCreateRequest(
            expected_conversation_version=3,
            ai_suggestion="任意文本",
            idempotency_key="reply:draft:message-100",
        ),
        "admin-test",
    )
    assert repeated_draft["id"] == draft["id"]
    edited = service.update_reply_draft(
        inbound.conversation_id,
        draft["id"],
        "tenant-test",
        ReplyDraftUpdateRequest(
            expected_record_version=1,
            final_text="您好，已核实，预计今天发货。",
        ),
        "admin-test",
    )
    sent_draft = service.send_reply_draft(
        inbound.conversation_id,
        draft["id"],
        "tenant-test",
        ReplyDraftSendRequest(expected_record_version=edited["record_version"]),
        "admin-test",
    )
    assert sent_draft["status"] == "sent"
    assert sent_draft["outbox_id"]
    detail = service.conversation_detail(inbound.conversation_id, "tenant-test")
    assert detail["drafts"][0]["diff"] == edited["diff"]
    with pytest.raises(TaobaoError, match="transition or version conflict"):
        service.send_reply_draft(
            inbound.conversation_id,
            draft["id"],
            "tenant-test",
            ReplyDraftSendRequest(expected_record_version=edited["record_version"]),
            "admin-test",
        )

    original_call = service.top.call

    service.top.call = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "message_chatrobot_async_response": {
            "is_success": False,
            "error_code": "DENIED",
            "error_message": "not allowed",
        }
    }
    rejected_request = ChannelReplyRequest(
        text="平台会明确拒绝这条消息",
        idempotency_key="reply:rejected:message-100",
    )
    with pytest.raises(TaobaoRemoteError, match="DENIED"):
        service.send_reply(
            inbound.conversation_id, "tenant-test", rejected_request, "admin-test"
        )
    with db.connect() as conn:
        rejected = conn.execute(
            "SELECT status, delivery_state FROM channel_outbox WHERE idempotency_key=?",
            (rejected_request.idempotency_key,),
        ).fetchone()
    assert dict(rejected) == {"status": "failed", "delivery_state": "rejected"}

    def timeout_call(*_args, **_kwargs):
        raise httpx.ReadTimeout("delivery outcome unknown")

    service.top.call = timeout_call  # type: ignore[method-assign]
    uncertain_request = ChannelReplyRequest(
        text="这条消息的投递结果需要对账",
        idempotency_key="reply:uncertain:message-100",
    )
    with pytest.raises(httpx.ReadTimeout):
        service.send_reply(
            inbound.conversation_id, "tenant-test", uncertain_request, "admin-test"
        )
    with db.connect() as conn:
        uncertain = conn.execute(
            "SELECT status, delivery_state FROM channel_outbox WHERE idempotency_key=?",
            (uncertain_request.idempotency_key,),
        ).fetchone()
    assert dict(uncertain) == {"status": "failed", "delivery_state": "uncertain"}
    with pytest.raises(TaobaoError, match="reconcile delivery state"):
        service.send_reply(
            inbound.conversation_id, "tenant-test", uncertain_request, "admin-test"
        )
    service.top.call = original_call  # type: ignore[method-assign]

    subscription = service.subscribe(
        "tenant-test",
        SubscribeRequest(user_nicks=["客服甲"], enabled=True),
        "admin-test",
    )
    assert subscription["verified"] is True
    top_methods = [call["form"]["method"][0] for call in calls if str(call["url"]).endswith("/top")]
    assert top_methods == [
        "taobao.message.chatrobot.async",
        "taobao.message.chatrobot.async",
        "taobao.message.chatrobot.assist.subscribe",
        "taobao.message.chatrobot.assist.query",
    ]


def test_qimen_rejects_stale_or_invalid_requests(tmp_path) -> None:
    settings = configured_settings(tmp_path)
    db = Database(settings.app_db_path)
    db.initialize()
    service = TaobaoIntegrationService(db, settings)
    params = {
        "app_key": settings.taobao_app_key,
        "customerId": settings.taobao_qimen_customer_id,
        "timestamp": "2020-01-01 00:00:00",
        "sign_method": "md5",
        "event": "{}",
    }
    params["sign"] = sign_parameters(params, settings.taobao_app_secret, "md5")
    with pytest.raises(TaobaoError, match="replay-protection"):
        service.receive_qimen(params)
    service.close()
