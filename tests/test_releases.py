from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import make_settings
from ecommerce_agent.api import create_app
from ecommerce_agent.releases import (
    ReleaseError,
    ReleasePolicyCreateRequest,
    ReleaseReplayCase,
    ReleaseReplayRequest,
    ReleaseService,
    ReleaseTransitionRequest,
    ReplayExpectation,
)
from ecommerce_agent.service import AgentService
from ecommerce_agent.taobao import sign_parameters


def _policy(**overrides) -> ReleasePolicyCreateRequest:
    payload = {
        "release_key": "customer-service.reply",
        "name": "客服回复灰度",
        "platform": "taobao",
        "store_id": "store-a",
        "mode": "automatic",
        "traffic_percentage": 100,
        "intent_allowlist": ["product"],
        "max_risk_level": "low",
        "require_sources": True,
        "allow_model_fallback": False,
        "min_replay_cases": 1,
        "max_replay_failure_rate": 0,
        "max_replay_severe_errors": 0,
        "runtime_min_samples": 1,
        "max_runtime_failure_rate": 0,
        "max_runtime_severe_errors": 0,
    }
    payload.update(overrides)
    return ReleasePolicyCreateRequest.model_validate(payload)


def _case(case_id: str = "case-product") -> ReleaseReplayCase:
    return ReleaseReplayCase(
        case_id=case_id,
        message="尺码怎么选",
        expectation=ReplayExpectation(
            expected_intent="product",
            expected_requires_human=False,
            require_sources=True,
        ),
    )


def _passing_response() -> SimpleNamespace:
    return SimpleNamespace(
        answer="请以商品详情页的尺码表为准。",
        intent="product",
        risk_level="low",
        requires_human=False,
        sources=[{"id": "knowledge-1"}],
        model_fallback=False,
    )


def _activate(
    releases: ReleaseService,
    release: dict,
    *,
    tenant_id: str = "tenant-test",
) -> dict:
    replay = releases.run_replay(
        tenant_id,
        release["id"],
        ReleaseReplayRequest(cases=[_case()]),
        "creator-a",
        lambda case: _passing_response(),
    )
    assert replay["passed"] is True
    evaluated = releases.get_policy(tenant_id, release["id"])
    approved = releases.approve(
        tenant_id,
        release["id"],
        ReleaseTransitionRequest(
            expected_record_version=evaluated["record_version"]
        ),
        "reviewer-b",
    )
    return releases.activate(
        tenant_id,
        release["id"],
        ReleaseTransitionRequest(
            expected_record_version=approved["record_version"]
        ),
        "release-admin",
    )


def test_release_lifecycle_versioning_assignment_and_two_person_gate(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    releases = service.releases
    try:
        created = releases.create("tenant-test", _policy(), "creator-a")
        assert created["version"] == 1
        assert created["status"] == "draft"
        replay = releases.run_replay(
            "tenant-test",
            created["id"],
            ReleaseReplayRequest(cases=[_case()]),
            "creator-a",
            lambda case: _passing_response(),
        )
        assert replay["summary"] == {
            "total": 1,
            "passed": 1,
            "failed": 0,
            "failure_rate": 0.0,
            "severe_errors": 0,
            "required_cases": 1,
            "max_failure_rate": 0.0,
            "max_severe_errors": 0,
        }
        evaluated = releases.get_policy("tenant-test", created["id"])
        with pytest.raises(ReleaseError, match="second operator"):
            releases.approve(
                "tenant-test",
                created["id"],
                ReleaseTransitionRequest(
                    expected_record_version=evaluated["record_version"]
                ),
                "creator-a",
            )
        approved = releases.approve(
            "tenant-test",
            created["id"],
            ReleaseTransitionRequest(
                expected_record_version=evaluated["record_version"]
            ),
            "reviewer-b",
        )
        active = releases.activate(
            "tenant-test",
            created["id"],
            ReleaseTransitionRequest(
                expected_record_version=approved["record_version"]
            ),
            "release-admin",
        )
        assert active["status"] == "active"
        assignment1 = releases.assignment(
            "tenant-test", "taobao", "store-a", "conversation-a"
        )
        assignment2 = releases.assignment(
            "tenant-test", "taobao", "store-a", "conversation-a"
        )
        assert assignment1["selected"] is True
        assert assignment1["bucket"] == assignment2["bucket"]
        assert assignment1["policy"]["id"] == created["id"]
        assert releases.assignment(
            "tenant-other", "taobao", "store-a", "conversation-a"
        )["reason"] == "no_active_release"
        with pytest.raises(ReleaseError, match="not found"):
            releases.get_policy("tenant-other", created["id"])

        version2 = releases.create(
            "tenant-test", _policy(name="客服回复灰度 V2"), "creator-c"
        )
        assert version2["version"] == 2
        active2 = _activate(releases, version2)
        assert active2["status"] == "active"
        assert releases.get_policy("tenant-test", created["id"])["status"] == "retired"
    finally:
        service.close()


def test_replay_failure_blocks_approval_and_does_not_store_case_text(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        created = service.releases.create("tenant-test", _policy(), "creator-a")
        failed = service.releases.run_replay(
            "tenant-test",
            created["id"],
            ReleaseReplayRequest(cases=[_case()]),
            "creator-a",
            lambda case: {
                "answer": "已经替你退款完成",
                "intent": "refund",
                "risk_level": "high",
                "requires_human": False,
                "sources": [],
                "model_fallback": True,
            },
        )
        assert failed["passed"] is False
        assert failed["summary"]["severe_errors"] == 1
        assert {
            "intent_not_allowlisted",
            "risk_above_release_limit",
            "evidence_missing",
            "model_fallback_disallowed",
            "intent_mismatch",
        } <= set(failed["results"][0]["violations"])
        evaluated = service.releases.get_policy("tenant-test", created["id"])
        with pytest.raises(ReleaseError, match="pass replay"):
            service.releases.approve(
                "tenant-test",
                created["id"],
                ReleaseTransitionRequest(
                    expected_record_version=evaluated["record_version"]
                ),
                "reviewer-b",
            )
        with service.db.connect() as conn:
            stored = conn.execute(
                "SELECT results_json FROM release_replay_runs WHERE release_id=?",
                (created["id"],),
            ).fetchone()[0]
        assert "尺码怎么选" not in stored
        assert "已经替你退款完成" not in stored
    finally:
        service.close()


def test_runtime_severe_error_is_idempotent_and_auto_pauses_release(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        created = service.releases.create("tenant-test", _policy(), "creator-a")
        active = _activate(service.releases, created)
        assignment = service.releases.assignment(
            "tenant-test", "taobao", "store-a", "conversation-a"
        )
        observation = service.releases.record_response(
            "tenant-test",
            assignment,
            conversation_id="conversation-a",
            event_id="event-a",
            response={
                "answer": "没有证据的直接回答",
                "intent": "product",
                "risk_level": "low",
                "requires_human": False,
                "sources": [],
                "model_fallback": False,
            },
        )
        assert observation["action"] == "handoff"
        assert observation["severe"] is True
        assert observation["release_paused"] is True
        paused = service.releases.get_policy("tenant-test", active["id"])
        assert paused["status"] == "paused"
        assert paused["pause_reason"] == "severe_error_budget_exceeded"
        duplicate = service.releases.record_response(
            "tenant-test",
            assignment,
            conversation_id="conversation-a",
            event_id="event-a",
            response=_passing_response(),
        )
        assert duplicate["id"] == observation["id"]
        assert len(service.releases.list_observations("tenant-test", active["id"])) == 1
        assert service.releases.assignment(
            "tenant-test", "taobao", "store-a", "conversation-b"
        )["reason"] == "no_active_release"
    finally:
        service.close()


def test_agent_replay_runs_in_isolated_snapshot(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        created = service.releases.create(
            "tenant-test",
            _policy(mode="shadow", traffic_percentage=100),
            "admin-test",
        )
        with service.db.connect() as conn:
            before = {
                "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "handoffs": conn.execute("SELECT COUNT(*) FROM handoff_tasks").fetchone()[0],
            }
        report = service.run_release_replay(
            "tenant-test",
            created["id"],
            ReleaseReplayRequest(cases=[_case()]),
            "admin-test",
        )
        assert report["passed"] is True, report
        with service.db.connect() as conn:
            after = {
                "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "handoffs": conn.execute("SELECT COUNT(*) FROM handoff_tasks").fetchone()[0],
            }
        assert after == before
        assert service.releases.get_policy("tenant-test", created["id"])["status"] == "evaluated"
    finally:
        service.close()


def test_release_validation_rejects_unsafe_or_assertion_free_policies() -> None:
    with pytest.raises(ValidationError, match="must require sources"):
        _policy(require_sources=False)
    with pytest.raises(ValidationError, match="cannot allow model fallback"):
        _policy(allow_model_fallback=True)
    with pytest.raises(ValidationError, match="at least one assertion"):
        ReleaseReplayCase(
            case_id="empty",
            message="测试",
            expectation=ReplayExpectation(),
        )


def test_release_admin_api_auth_lifecycle_and_errors(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    headers = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}
    with TestClient(app) as client:
        assert client.get("/v1/admin/releases").status_code == 401
        created_response = client.post(
            "/v1/admin/releases",
            headers=headers,
            json=_policy(
                mode="shadow",
                traffic_percentage=100,
                min_replay_cases=1,
            ).model_dump(),
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        replay = client.post(
            f"/v1/admin/releases/{created['id']}/replay",
            headers=headers,
            json={"cases": [_case().model_dump()]},
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["passed"] is True
        evaluated = client.get(
            f"/v1/admin/releases/{created['id']}", headers=headers
        ).json()
        stale = client.post(
            f"/v1/admin/releases/{created['id']}/approve",
            headers=headers,
            json={"expected_record_version": 1},
        )
        assert stale.status_code == 409
        approved = client.post(
            f"/v1/admin/releases/{created['id']}/approve",
            headers=headers,
            json={"expected_record_version": evaluated["record_version"]},
        )
        assert approved.status_code == 200, approved.text
        active = client.post(
            f"/v1/admin/releases/{created['id']}/activate",
            headers=headers,
            json={"expected_record_version": approved.json()["record_version"]},
        )
        assert active.status_code == 200, active.text
        assignment = client.get(
            "/v1/admin/releases/assignment",
            headers=headers,
            params={
                "platform": "taobao",
                "store_id": "store-a",
                "conversation_id": "conversation-api",
            },
        )
        assert assignment.status_code == 200
        assert assignment.json()["selected"] is True
        assert client.get(
            f"/v1/admin/releases/{created['id']}/runtime", headers=headers
        ).status_code == 200
        assert client.get(
            "/v1/admin/releases/release-missing", headers=headers
        ).status_code == 404


def test_admin_operator_api_enables_real_two_person_release_approval(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    creator_headers = {
        "X-Admin-Id": "admin-test",
        "X-Admin-Key": "test-admin-key-123456",
    }
    reviewer_key = "reviewer-secret-key-123456"
    reviewer_headers = {
        "X-Admin-Id": "reviewer-b",
        "X-Admin-Key": reviewer_key,
    }
    with TestClient(app) as client:
        created_operator = client.post(
            "/v1/admin/operators",
            headers=creator_headers,
            json={
                "admin_id": "reviewer-b",
                "name": "发布复核员",
                "key": reviewer_key,
            },
        )
        assert created_operator.status_code == 201, created_operator.text
        assert reviewer_key not in created_operator.text
        operators = client.get(
            "/v1/admin/operators", headers=creator_headers
        ).json()
        assert {item["admin_id"] for item in operators} == {
            "admin-test",
            "reviewer-b",
        }
        assert reviewer_key not in json.dumps(operators)

        release = client.post(
            "/v1/admin/releases",
            headers=creator_headers,
            json=_policy(release_key="api.two-person").model_dump(),
        ).json()
        replay = client.post(
            f"/v1/admin/releases/{release['id']}/replay",
            headers=creator_headers,
            json={"cases": [_case().model_dump()]},
        )
        assert replay.status_code == 200 and replay.json()["passed"] is True
        evaluated = client.get(
            f"/v1/admin/releases/{release['id']}", headers=creator_headers
        ).json()
        self_approval = client.post(
            f"/v1/admin/releases/{release['id']}/approve",
            headers=creator_headers,
            json={"expected_record_version": evaluated["record_version"]},
        )
        assert self_approval.status_code == 409
        approved = client.post(
            f"/v1/admin/releases/{release['id']}/approve",
            headers=reviewer_headers,
            json={"expected_record_version": evaluated["record_version"]},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["approved_by"] == "reviewer-b"

        self_disable = client.post(
            "/v1/admin/operators/reviewer-b/disable",
            headers=reviewer_headers,
            json={"expected_status": "active", "reason": "self test"},
        )
        assert self_disable.status_code == 409
        disabled = client.post(
            "/v1/admin/operators/reviewer-b/disable",
            headers=creator_headers,
            json={"expected_status": "active", "reason": "rotation completed"},
        )
        assert disabled.status_code == 200
        assert disabled.json()["status"] == "disabled"
        assert client.get(
            "/v1/admin/releases", headers=reviewer_headers
        ).status_code == 401


def _qimen_form(settings, *, message_id: str, text: str) -> str:
    event = {
        "header": {
            "actionMode": 1,
            "requestId": f"request-{message_id}",
            "tenantId": "robot-tenant-1",
            "serializeType": "Json",
            "type": 1,
        },
        "body": {
            "bizUniqueId": "conversation-release-1",
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
    params = {
        "method": "qimen.taobao.message.chatrobot.sync",
        "app_key": settings.taobao_app_key,
        "timestamp": datetime.now(timezone(timedelta(hours=8))).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "v": "2.0",
        "sign_method": "md5",
        "customerId": settings.taobao_qimen_customer_id,
        "event": json.dumps(event, ensure_ascii=False, separators=(",", ":")),
        "buyerId": "buyer-release-1",
        "buyerNick": "买家甲",
        "sellerId": "seller-release-1",
        "sellerNick": "测试店铺",
    }
    params["sign"] = sign_parameters(params, settings.taobao_app_secret, "md5")
    return urlencode(params)


def test_qimen_auto_reply_requires_active_release_and_shadow_never_sends(tmp_path) -> None:
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    settings = replace(
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
        release_gate_required=True,
        channel_agent_worker_enabled=True,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 503
        first = client.post(
            "/v1/integrations/taobao/qimen",
            content=_qimen_form(settings, message_id="release-1", text="尺码怎么选"),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert first.status_code == 200
        service = app.state.agent
        with service.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM channel_outbox").fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE event_type='release.assignment.blocked'"
            ).fetchone()[0] == 1

        created = service.releases.create(
            "tenant-test",
            _policy(
                release_key="qimen.shadow",
                store_id="seller-release-1",
                mode="shadow",
                traffic_percentage=100,
            ),
            "creator-a",
        )
        active = _activate(service.releases, created)
        assert client.get("/ready").status_code == 200
        second = client.post(
            "/v1/integrations/taobao/qimen",
            content=_qimen_form(settings, message_id="release-2", text="尺码怎么选"),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert second.status_code == 200
        observations = service.releases.list_observations(
            "tenant-test", active["id"]
        )
        assert len(observations) == 1
        assert observations[0]["action"] == "shadow"
        assert observations[0]["selected"] is True
        with service.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM channel_outbox").fetchone()[0] == 0
            owner = conn.execute(
                "SELECT owner_mode FROM channel_conversations LIMIT 1"
            ).fetchone()[0]
        assert owner == "bot"


def _release_channel_settings(tmp_path):
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    return replace(
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
        release_gate_required=True,
        channel_agent_worker_enabled=True,
    )


def test_qimen_assist_release_creates_human_owned_draft(tmp_path) -> None:
    settings = _release_channel_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        service = app.state.agent
        created = service.releases.create(
            "tenant-test",
            _policy(
                release_key="qimen.assist",
                store_id="seller-release-1",
                mode="assist",
                traffic_percentage=100,
            ),
            "creator-a",
        )
        active = _activate(service.releases, created)
        response = client.post(
            "/v1/integrations/taobao/qimen",
            content=_qimen_form(settings, message_id="assist-1", text="尺码怎么选"),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 200
        observations = service.releases.list_observations(
            "tenant-test", active["id"]
        )
        assert observations[0]["action"] == "draft"
        with service.db.connect() as conn:
            conversation = conn.execute(
                "SELECT owner_mode, assigned_to FROM channel_conversations LIMIT 1"
            ).fetchone()
            draft = conn.execute(
                """
                SELECT status, evidence_json, created_by
                FROM channel_reply_drafts LIMIT 1
                """
            ).fetchone()
            outbox_count = conn.execute(
                "SELECT COUNT(*) FROM channel_outbox"
            ).fetchone()[0]
        assert dict(conversation) == {
            "owner_mode": "human",
            "assigned_to": "agent-assist",
        }
        assert draft["status"] == "draft"
        assert json.loads(draft["evidence_json"])
        assert draft["created_by"] == "channel-agent"
        assert outbox_count == 0


def test_qimen_automatic_release_sends_only_after_gate(tmp_path, monkeypatch) -> None:
    settings = _release_channel_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        service = app.state.agent
        created = service.releases.create(
            "tenant-test",
            _policy(
                release_key="qimen.automatic",
                store_id="seller-release-1",
                mode="automatic",
                traffic_percentage=100,
            ),
            "creator-a",
        )
        active = _activate(service.releases, created)
        sends: list[dict] = []

        def fake_send(conversation_id, tenant_id, request, actor, *, allow_bot=False):
            sends.append(
                {
                    "conversation_id": conversation_id,
                    "tenant_id": tenant_id,
                    "text": request.text,
                    "actor": actor,
                    "allow_bot": allow_bot,
                }
            )
            return {"status": "sent", "delivery_state": "confirmed"}

        monkeypatch.setattr(service.taobao, "send_reply", fake_send)
        response = client.post(
            "/v1/integrations/taobao/qimen",
            content=_qimen_form(settings, message_id="automatic-1", text="尺码怎么选"),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 200
        assert len(sends) == 1
        assert sends[0]["allow_bot"] is True
        assert sends[0]["actor"] == "agent"
        observations = service.releases.list_observations(
            "tenant-test", active["id"]
        )
        assert observations[0]["action"] == "send"
        assert observations[0]["violations"] == []
        assert service.releases.get_policy("tenant-test", active["id"])["status"] == "active"


def test_qimen_delivery_exception_marks_failure_and_auto_pauses(tmp_path, monkeypatch) -> None:
    settings = _release_channel_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        service = app.state.agent
        created = service.releases.create(
            "tenant-test",
            _policy(
                release_key="qimen.delivery-failure",
                store_id="seller-release-1",
                mode="automatic",
                traffic_percentage=100,
            ),
            "creator-a",
        )
        active = _activate(service.releases, created)

        def fail_send(*args, **kwargs):
            raise RuntimeError("simulated delivery failure")

        monkeypatch.setattr(service.taobao, "send_reply", fail_send)
        response = client.post(
            "/v1/integrations/taobao/qimen",
            content=_qimen_form(
                settings,
                message_id="delivery-failure-1",
                text="尺码怎么选",
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 200
        observations = service.releases.list_observations(
            "tenant-test", active["id"]
        )
        assert len(observations) == 1
        assert observations[0]["action"] == "blocked"
        assert observations[0]["severe"] is True
        assert "delivery_runtimeerror" in observations[0]["violations"]
        paused = service.releases.get_policy("tenant-test", active["id"])
        assert paused["status"] == "paused"
        assert paused["pause_reason"] == "severe_error_budget_exceeded"
        with service.db.connect() as conn:
            failed_audit = conn.execute(
                """
                SELECT detail_json FROM audit_log
                WHERE event_type='taobao.auto_reply.failed'
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
            auto_pause_audit = conn.execute(
                """
                SELECT detail_json FROM audit_log
                WHERE event_type='release.auto_paused' AND subject_id=?
                """,
                (active["id"],),
            ).fetchone()
        assert failed_audit is not None
        assert json.loads(failed_audit["detail_json"])["error_type"] == "RuntimeError"
        assert auto_pause_audit is not None
