from __future__ import annotations

from dataclasses import replace

from ecommerce_agent.evals import run_offline_evaluation
from ecommerce_agent.evolution import EvolutionService
from ecommerce_agent.schemas import FeedbackRequest
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


def test_three_evolution_cycles_improve_target_answers_without_regression(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    evolution = EvolutionService(service.db, service.knowledge)
    scenarios = [
        (
            "商品是羊毛衫，起球后怎么护理",
            "建议用毛球修剪器轻柔处理，避免用力拉扯；如果面料异常破损，请保留照片并联系人工核对。",
            "人工复核：羊毛商品护理说明",
        ),
        (
            "商品是陶瓷杯，第一次使用前怎么清洁",
            "首次使用前建议用中性清洁剂和清水清洗，充分冲净并晾干后再使用。",
            "人工复核：陶瓷商品使用说明",
        ),
        (
            "商品的可拆洗坐垫有轻微异味怎么处理",
            "建议先将坐垫放在通风处散味，并按商品洗护说明清洁；若异味持续，请联系人工核对。",
            "人工复核：坐垫商品洗护说明",
        ),
    ]
    try:
        principal = principal_for(service)
        learned_sources: list[str] = []
        for index, (question, corrected_answer, evidence_source) in enumerate(scenarios):
            before = service.chat(principal, f"learn-before-{index}", question)
            assert before.answer != corrected_answer
            feedback = evolution.submit_feedback(
                FeedbackRequest(
                    message_id=before.message_id,
                    rating=-1,
                    corrected_answer=corrected_answer,
                    evidence_source=evidence_source,
                    note="isolated learning evaluation",
                    submitted_by="qa",
                ),
                tenant_id=principal.tenant_id,
            )
            evaluated = evolution.evaluate(
                feedback.candidate_id,
                tenant_id=principal.tenant_id,
            )
            assert evaluated.gate_passed is True, evaluated.gate_report
            approved = evolution.approve(
                feedback.candidate_id,
                "reviewer",
                "verified in isolated evaluation",
                tenant_id=principal.tenant_id,
            )
            assert approved.status == "approved"

            after = service.chat(principal, f"learn-after-{index}", question)
            source = f"evolution:{feedback.candidate_id}"
            assert after.answer == corrected_answer
            assert any(item.source == source for item in after.sources)
            learned_sources.append(source)

        assert len(set(learned_sources)) == len(scenarios)
        assert run_offline_evaluation(
            service.knowledge,
            tenant_id=principal.tenant_id,
        )["passed"] is True
    finally:
        service.close()


def test_high_confidence_approved_answer_bypasses_disabled_model(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        model_enabled=False,
        model_mock_mode=False,
    )
    service = AgentService(settings)
    evolution = EvolutionService(service.db, service.knowledge)
    question = "商品是羊毛衫，起球后怎么护理"
    corrected = (
        "建议用毛球修剪器轻柔处理，避免用力拉扯；"
        "如果面料异常破损，请保留照片并联系人工核对。"
    )
    try:
        principal = principal_for(service)
        before = service.chat(principal, "direct-before", question)
        assert before.model_fallback is True

        feedback = evolution.submit_feedback(
            FeedbackRequest(
                message_id=before.message_id,
                rating=-1,
                corrected_answer=corrected,
                evidence_source="人工复核：羊毛商品护理说明",
                submitted_by="qa",
            ),
            tenant_id=principal.tenant_id,
        )
        evaluated = evolution.evaluate(feedback.candidate_id, tenant_id=principal.tenant_id)
        assert evaluated.gate_passed is True
        evolution.approve(
            feedback.candidate_id,
            "reviewer",
            "verified",
            tenant_id=principal.tenant_id,
        )

        after = service.chat(principal, "direct-after", question)
        assert after.answer == corrected
        assert after.model_fallback is False
        assert after.requires_human is False
        assert any(
            item.source == f"evolution:{feedback.candidate_id}"
            for item in after.sources
        )
    finally:
        service.close()
