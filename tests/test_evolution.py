from ecommerce_agent.evolution import EvolutionService
from ecommerce_agent.schemas import FeedbackRequest
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


def test_safe_candidate_requires_gate_and_human_approval(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    evolution = EvolutionService(service.db, service.knowledge)
    try:
        principal = principal_for(service)
        chat = service.chat(principal, "session-evo-1", "尺码怎么选")
        before = service.knowledge.count_active(principal.tenant_id)
        feedback = evolution.submit_feedback(
            FeedbackRequest(
                message_id=chat.message_id,
                rating=-1,
                corrected_answer="请先查看商品尺寸表；如果仍无法判断，请提供必要的测量信息并转人工核对。",
                evidence_source="人工复核：商品尺寸说明",
                submitted_by="qa",
            ),
            tenant_id=principal.tenant_id,
        )
        assert feedback.candidate_id
        assert service.knowledge.count_active(principal.tenant_id) == before

        evaluated = evolution.evaluate(feedback.candidate_id, tenant_id=principal.tenant_id)
        assert evaluated.gate_passed is True
        approved = evolution.approve(
            feedback.candidate_id,
            "reviewer",
            "verified",
            tenant_id=principal.tenant_id,
        )
        assert approved.status == "approved"
        assert service.knowledge.count_active(principal.tenant_id) == before + 1
        tenant_results = service.knowledge.retrieve(
            "尺码怎么选",
            top_k=5,
            min_score=0.05,
            intent="product",
            tenant_id=principal.tenant_id,
        )
        other_tenant_results = service.knowledge.retrieve(
            "尺码怎么选",
            top_k=5,
            min_score=0.05,
            intent="product",
            tenant_id="other-tenant",
        )
        assert any(item["source"] == f"evolution:{feedback.candidate_id}" for item in tenant_results)
        assert all(
            item["source"] != f"evolution:{feedback.candidate_id}"
            for item in other_tenant_results
        )
    finally:
        service.close()


def test_dangerous_candidate_cannot_be_approved(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    evolution = EvolutionService(service.db, service.knowledge)
    try:
        principal = principal_for(service)
        chat = service.chat(principal, "session-evo-2", "帮我退款")
        feedback = evolution.submit_feedback(
            FeedbackRequest(
                message_id=chat.message_id,
                rating=-1,
                corrected_answer="已经为您完成退款。",
                evidence_source="人工复核：退款处理记录",
                submitted_by="qa",
            ),
            tenant_id=principal.tenant_id,
        )
        evaluated = evolution.evaluate(feedback.candidate_id, tenant_id=principal.tenant_id)
        assert evaluated.gate_passed is False
    finally:
        service.close()


def test_semantically_contradictory_candidate_fails_gate(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    evolution = EvolutionService(service.db, service.knowledge)
    try:
        principal = principal_for(service)
        chat = service.chat(principal, "session-evo-3", "尺码怎么选")
        feedback = evolution.submit_feedback(
            FeedbackRequest(
                message_id=chat.message_id,
                rating=-1,
                corrected_answer="请选择与尺寸表完全相反的尺码，这样一定最合身。",
                evidence_source="人工复核：商品尺寸说明",
                submitted_by="qa",
            ),
            tenant_id=principal.tenant_id,
        )
        evaluated = evolution.evaluate(feedback.candidate_id, tenant_id=principal.tenant_id)
        assert evaluated.gate_passed is False
        assert evaluated.gate_report["checks"]["no_contradiction_markers"] is False
    finally:
        service.close()


def test_candidate_without_evidence_source_cannot_be_approved(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    evolution = EvolutionService(service.db, service.knowledge)
    try:
        principal = principal_for(service)
        chat = service.chat(principal, "session-evo-no-evidence", "尺码怎么选")
        feedback = evolution.submit_feedback(
            FeedbackRequest(
                message_id=chat.message_id,
                rating=-1,
                corrected_answer="请先查看商品尺寸表，如仍无法判断请联系人工核对。",
                submitted_by="qa",
            ),
            tenant_id=principal.tenant_id,
        )
        evaluated = evolution.evaluate(feedback.candidate_id, tenant_id=principal.tenant_id)
        assert evaluated.gate_passed is False
        assert evaluated.gate_report["checks"]["source_traceable"] is False
    finally:
        service.close()
