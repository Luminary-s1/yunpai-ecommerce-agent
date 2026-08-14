from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import make_settings, principal_for
from ecommerce_agent.api import create_app
from ecommerce_agent.knowledge_management import (
    KnowledgeCreateRequest,
    KnowledgeLifecycleError,
    KnowledgeReviseRequest,
    KnowledgeTransitionRequest,
)
from ecommerce_agent.quality import QualityError, QualityReviewRequest, QualityRunRequest
from ecommerce_agent.service import AgentService
from ecommerce_agent.sops import (
    SopCreateRequest,
    SopDsl,
    SopError,
    SopReviseRequest,
    SopTransitionRequest,
)


def _sop_dsl(postcondition: str = "case_created") -> SopDsl:
    return SopDsl.model_validate(
        {
            "trigger": {"intents": ["complaint"]},
            "required_context": ["shop_id"],
            "steps": [
                {"observe": "get_order_facts"},
                {"clarify_if_missing": "complaint_reason"},
                {"propose": "create_handoff_task"},
            ],
            "guards": {"max_auto_compensation_cents": 0},
            "handoff": {"when": ["customer_escalation", "evidence_conflict"]},
            "success": {"postcondition": postcondition},
        }
    )


def test_knowledge_versions_are_scoped_approved_and_rollbackable(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    manager = service.knowledge_management
    try:
        with pytest.raises(KnowledgeLifecycleError, match="requires store_id"):
            manager.create(
                "tenant-test",
                KnowledgeCreateRequest(
                    category="商品参数", intent="product", question="规格是什么？",
                    answer="规格为标准版。", source="manual://operator", layer="product",
                ),
                "admin-test",
            )
        created = manager.create(
            "tenant-test",
            KnowledgeCreateRequest(
                category="商品参数", intent="product", question="SKU-A 的材质是什么？",
                answer="SKU-A 使用 304 不锈钢。", keywords="SKU-A 材质",
                source="manual://operator", layer="product", store_id="store-a", sku_id="SKU-A",
            ),
            "admin-test",
        )
        assert created["status"] == "candidate"
        evaluated = manager.evaluate(
            "tenant-test", created["id"], KnowledgeTransitionRequest(expected_record_version=1),
            "reviewer-a",
        )
        with pytest.raises(KnowledgeLifecycleError, match="version conflict"):
            manager.approve(
                "tenant-test", created["id"],
                KnowledgeTransitionRequest(expected_record_version=1), "reviewer-a",
            )
        active_v1 = manager.approve(
            "tenant-test", created["id"],
            KnowledgeTransitionRequest(expected_record_version=evaluated["record_version"]),
            "reviewer-a",
        )
        assert active_v1["status"] == "active"
        assert not any(
            item["id"] == created["id"]
            for item in service.knowledge.retrieve(
                "SKU-A 材质", top_k=5, min_score=0, tenant_id="tenant-test",
                store_id="store-b", sku_id="SKU-A",
            )
        )
        assert any(
            item["id"] == created["id"] and item["layer"] == "product"
            for item in service.knowledge.retrieve(
                "SKU-A 材质", top_k=5, min_score=0, tenant_id="tenant-test",
                store_id="store-a", sku_id="SKU-A",
            )
        )

        draft_v2 = manager.revise(
            "tenant-test", created["id"],
            KnowledgeReviseRequest(
                expected_record_version=active_v1["record_version"],
                answer="SKU-A 使用食品级 304 不锈钢。",
            ),
            "editor-a",
        )
        evaluated_v2 = manager.evaluate(
            "tenant-test", draft_v2["id"],
            KnowledgeTransitionRequest(expected_record_version=1), "reviewer-a",
        )
        active_v2 = manager.approve(
            "tenant-test", draft_v2["id"],
            KnowledgeTransitionRequest(expected_record_version=evaluated_v2["record_version"]),
            "reviewer-a",
        )
        retired_v1 = manager.get_item("tenant-test", created["id"])
        assert retired_v1 and retired_v1["status"] == "retired"
        rolled_back = manager.rollback(
            "tenant-test", created["id"],
            KnowledgeTransitionRequest(expected_record_version=retired_v1["record_version"]),
            "reviewer-a",
        )
        assert rolled_back["status"] == "active"
        assert manager.get_item("tenant-test", active_v2["id"])["status"] == "retired"
        assert manager.get_item("tenant-other", created["id"]) is None
    finally:
        service.close()


def test_sop_lifecycle_pins_running_session_and_rolls_back(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    sops = service.sops
    try:
        created = sops.create(
            "tenant-test",
            SopCreateRequest(
                sop_key="complaint.standard", name="投诉标准处理", intent="complaint",
                risk_level="high", dsl=_sop_dsl(),
            ),
            "admin-test",
        )
        definition_id = created["definition"]["id"]
        version1_id = created["versions"][0]["id"]
        evaluated = sops.evaluate(
            "tenant-test", version1_id, SopTransitionRequest(expected_record_version=1),
            "reviewer-a",
        )
        assert evaluated["versions"][0]["evaluation"]["passed"] is True
        approved = sops.approve(
            "tenant-test", version1_id,
            SopTransitionRequest(expected_record_version=evaluated["definition"]["record_version"]),
            "reviewer-a",
        )
        active_v1 = sops.activate(
            "tenant-test", version1_id,
            SopTransitionRequest(expected_record_version=approved["definition"]["record_version"]),
            "release-admin",
        )
        session1 = service.db.resolve_session(
            tenant_id="tenant-test", client_id="client-test",
            external_session_id="sop-session-1", subject_hash="subject-a",
        )
        pinned_v1 = sops.resolve_for_session("tenant-test", session1, "complaint")
        assert pinned_v1 and pinned_v1["version"] == 1

        draft_v2 = sops.revise(
            "tenant-test", definition_id,
            SopReviseRequest(
                expected_record_version=active_v1["definition"]["record_version"],
                dsl=_sop_dsl("supervisor_task_created"),
            ),
            "editor-a",
        )
        version2_id = draft_v2["versions"][0]["id"]
        evaluated_v2 = sops.evaluate(
            "tenant-test", version2_id,
            SopTransitionRequest(expected_record_version=draft_v2["definition"]["record_version"]),
            "reviewer-a",
        )
        approved_v2 = sops.approve(
            "tenant-test", version2_id,
            SopTransitionRequest(expected_record_version=evaluated_v2["definition"]["record_version"]),
            "reviewer-a",
        )
        active_v2 = sops.activate(
            "tenant-test", version2_id,
            SopTransitionRequest(expected_record_version=approved_v2["definition"]["record_version"]),
            "release-admin",
        )
        assert sops.resolve_for_session("tenant-test", session1, "complaint")["version"] == 1
        session2 = service.db.resolve_session(
            tenant_id="tenant-test", client_id="client-test",
            external_session_id="sop-session-2", subject_hash="subject-b",
        )
        assert sops.resolve_for_session("tenant-test", session2, "complaint")["version"] == 2
        rolled_back = sops.rollback(
            "tenant-test", version1_id,
            SopTransitionRequest(expected_record_version=active_v2["definition"]["record_version"]),
            "release-admin",
        )
        assert rolled_back["definition"]["current_active_version"] == 1
    finally:
        service.close()


def test_quality_run_review_summary_and_admin_api_contracts(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        service.chat(principal_for(service), "qa-session", "退款多久到账")
        with service.db.connect() as conn:
            session_id = conn.execute(
                "SELECT id FROM sessions WHERE tenant_id=? AND external_session_id=?",
                ("tenant-test", "qa-session"),
            ).fetchone()[0]
        result = service.quality.run(
            "tenant-test", QualityRunRequest(conversation_type="agent", conversation_id=session_id),
            "qa-bot",
        )
        assert result["score"] == 100
        reviewed = service.quality.review(
            "tenant-test", result["id"],
            QualityReviewRequest(review_status="dismissed", expected_record_version=1),
            "reviewer-a",
        )
        assert reviewed["record_version"] == 2
        assert service.quality.summary("tenant-test")["total_runs"] == 1
    finally:
        service.close()

    app = create_app(make_settings(tmp_path / "api"))
    headers = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}
    with TestClient(app) as client:
        assert client.get("/v1/admin/sops").status_code == 401
        assert len(client.get("/v1/admin/sops", headers=headers).json()) >= 2
        created = client.post(
            "/v1/admin/knowledge",
            headers=headers,
            json={
                "category": "店铺政策", "intent": "logistics", "question": "何时发货？",
                "answer": "付款后 48 小时内发货。", "source": "manual://policy",
                "layer": "store", "store_id": "store-a",
            },
        )
        assert created.status_code == 201
        item = created.json()
        assert client.post(
            f"/v1/admin/knowledge/{item['id']}/evaluate", headers=headers,
            json={"expected_record_version": 1},
        ).status_code == 200
        assert client.get("/v1/admin/voc/overview", headers=headers).status_code == 200


def test_quality_marks_redacted_user_messages(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        session_id = service.db.resolve_session(
            tenant_id="tenant-test",
            client_id="client-test",
            external_session_id="qa-redacted-user-session",
            subject_hash="subject-redacted-user",
        )
        now = "2026-08-11T12:00:00+00:00"
        with service.db.connect() as conn:
            conn.executemany(
                """
                INSERT INTO messages(
                    id, trace_id, session_id, role, content, intent, risk_level,
                    route_reason, sources_json, model_fallback, created_at,
                    tenant_id, client_id, redacted
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, '[]', 0, ?,
                          'tenant-test', 'client-test', ?)
                """,
                [
                    (
                        "qa-redacted-user",
                        "qa-redacted-user-trace",
                        session_id,
                        "user",
                        "我的手机号是 138****5678",
                        now,
                        1,
                    ),
                    (
                        "qa-redacted-user-answer",
                        "qa-redacted-user-trace",
                        session_id,
                        "assistant",
                        "已收到。",
                        now,
                        0,
                    ),
                ],
            )

        result = service.quality.run(
            "tenant-test",
            QualityRunRequest(conversation_type="agent", conversation_id=session_id),
            "qa-bot",
        )

        assert result["issues"] == [
            {
                "code": "sensitive_data_redacted",
                "severity": "low",
                "evidence_id": "qa-redacted-user",
            }
        ]
    finally:
        service.close()


def test_quality_rules_cover_evidence_risk_redaction_and_channel_failures(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        session_id = service.db.resolve_session(
            tenant_id="tenant-test", client_id="client-test",
            external_session_id="qa-risk-session", subject_hash="subject-risk",
        )
        now = "2026-07-21T12:00:00+00:00"
        with service.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO messages(
                    id, trace_id, session_id, role, content, intent, risk_level,
                    route_reason, sources_json, model_fallback, created_at,
                    tenant_id, client_id, redacted
                ) VALUES ('qa-risk-message', 'qa-risk-trace', ?, 'assistant', '已处理',
                          'refund', 'high', 'knowledge_answer_allowed', '[]', 1, ?,
                          'tenant-test', 'client-test', 1)
                """,
                (session_id, now),
            )
        agent_result = service.quality.run(
            "tenant-test",
            QualityRunRequest(conversation_type="agent", conversation_id=session_id),
            "qa-bot",
        )
        assert {issue["code"] for issue in agent_result["issues"]} == {
            "fact_evidence_missing", "model_fallback", "missed_handoff",
            "sensitive_data_redacted",
        }
        assert agent_result["score"] == 18
        with pytest.raises(QualityError, match="require a correction"):
            service.quality.review(
                "tenant-test", agent_result["id"],
                QualityReviewRequest(review_status="confirmed", expected_record_version=1),
                "reviewer-a",
            )
        confirmed = service.quality.review(
            "tenant-test", agent_result["id"],
            QualityReviewRequest(
                review_status="confirmed", expected_record_version=1,
                correction="核对证据并转人工，不得声称已处理。",
            ),
            "reviewer-a",
        )
        assert confirmed["review_status"] == "confirmed"
        with pytest.raises(QualityError, match="transition or version conflict"):
            service.quality.review(
                "tenant-test", agent_result["id"],
                QualityReviewRequest(review_status="dismissed", expected_record_version=1),
                "reviewer-a",
            )

        with service.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO channel_conversations(
                    id, tenant_id, platform, shop_id, external_conversation_id,
                    buyer_hash, buyer_nick_masked, owner_mode, assigned_to, version,
                    last_event_id, last_message_at, created_at, updated_at
                ) VALUES ('channel-qa', 'tenant-test', 'taobao', 'shop-a', 'ext-qa',
                          'buyer-hash', '买***家', 'human', 'admin-test', 1,
                          NULL, ?, ?, ?)
                """,
                (now, now, now),
            )
            conn.execute(
                """
                INSERT INTO channel_events(
                    id, tenant_id, platform, shop_id, conversation_id,
                    external_event_id, direction, message_type, content_redacted,
                    payload_hash, routing_ciphertext, request_id, action_mode,
                    status, created_at, updated_at
                ) VALUES ('event-qa', 'tenant-test', 'taobao', 'shop-a', 'channel-qa',
                          'event-ext-qa', 'outbound', 'text', '发送失败', 'hash',
                          NULL, NULL, NULL, 'failed', ?, ?)
                """,
                (now, now),
            )
            for draft_id, status, risk in (
                ("draft-qa-failed", "failed", "medium"),
                ("draft-qa-risk", "sent", "critical"),
            ):
                conn.execute(
                    """
                    INSERT INTO channel_reply_drafts(
                        id, tenant_id, conversation_id, source_event_id,
                        ai_suggestion_redacted, final_text_redacted, diff_json,
                        evidence_json, sop_reference_json, confidence, risk_level,
                        status, idempotency_key, outbox_id, last_error, record_version,
                        created_by, sent_by, created_at, updated_at, sent_at
                    ) VALUES (?, 'tenant-test', 'channel-qa', 'event-qa', '建议', '终稿',
                              '[]', '[]', NULL, 0.8, ?, ?, ?, NULL, 'error', 1,
                              'admin-test', NULL, ?, ?, NULL)
                    """,
                    (draft_id, risk, status, f"idempotency:{draft_id}", now, now),
                )
        channel_result = service.quality.run(
            "tenant-test",
            QualityRunRequest(conversation_type="channel", conversation_id="channel-qa"),
            "qa-bot",
        )
        assert {issue["code"] for issue in channel_result["issues"]} == {
            "channel_send_failure", "draft_send_failure", "high_risk_reply_sent",
        }
        assert service.quality.list_results(
            "tenant-test", review_status="pending"
        )[0]["id"] == channel_result["id"]
        assert service.quality.summary("tenant-test")["severity_counts"]["critical"] == 2
    finally:
        service.close()


def test_sop_validation_and_action_gate_errors_are_explicit(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        refund = next(
            item for item in service.sops.catalog_for_context("tenant-test")
            if item["intent"] == "refund"
        )
        session_id = service.db.resolve_session(
            tenant_id="tenant-test", client_id="client-test",
            external_session_id="sop-validation", subject_hash="subject-validation",
        )
        refund_sop = service.sops.resolve_for_session("tenant-test", session_id, "refund")
        assert refund_sop and refund_sop["version_id"] == refund["version_id"]
        allowed, reason, missing = service.sops.validate_action(
            refund_sop, tool_name="refund_order", arguments={}, context={}
        )
        assert (allowed, reason, missing) == (False, "sop_external_write_not_allowed", [])
        with pytest.raises(SopError, match="invalid SOP run terminal status"):
            service.sops.complete_run(refund_sop["run_id"], "unknown", {})
    finally:
        service.close()
