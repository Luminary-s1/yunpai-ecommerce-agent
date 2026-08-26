"""M9-R WP3 建议模型解释器测试（D-034 反假绿）。

验证：模型解释器被调用产出建议类型/理由；失败降级 Ruleset；越权输出被校验拒绝。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from ecommerce_agent.product_diagnosis.diagnosis import Diagnosis, DiagnosisType
from ecommerce_agent.product_lifecycle.engine import (
    RecommendationModelInterpreter,
    RecommendationType,
)


class _MockGateway:
    def __init__(
        self,
        return_value: dict | None = None,
        raise_exc: bool = False,
        responses: list[dict] | None = None,
    ):
        self._return = return_value or {}
        self._responses = list(responses or [])
        self._raise = raise_exc
        self.calls = 0
        self.requests = []
        self.settings = SimpleNamespace(
            model_provider="test-provider", model_name="test-model"
        )

    def generate_json(self, messages, **kwargs):
        self.calls += 1
        self.requests.append(json.loads(messages[-1]["content"]))
        if self._raise:
            raise RuntimeError("model unavailable")
        if self._responses:
            return self._responses.pop(0)
        return self._return


def _diagnosis(dtype: DiagnosisType = DiagnosisType.STOCKOUT_POLLUTION) -> Diagnosis:
    return Diagnosis(
        diagnosis_type=dtype,
        sku_id="sku-x",
        reason="stockout",
        evidence_facts={"stockout": True},
        degraded=True,
    )


def test_recommendation_model_interpreter_called() -> None:
    """模型可用时走模型（D-034 达标：建议类型由模型产生）。"""
    gateway = _MockGateway(
        return_value={"type": "补货联动", "rationale": "model suggested restock"}
    )
    interpreter = RecommendationModelInterpreter(gateway)
    candidate = interpreter.interpret(_diagnosis())
    assert gateway.calls == 1, "模型未被调用"
    assert candidate.type == RecommendationType.RESTOCK
    assert candidate.rationale == "model suggested restock"
    assert "degraded" not in gateway.requests[0]["output_schema"]["properties"]
    assert set(gateway.requests[0]["decision_policy"]) == {
        member.value for member in RecommendationType
    }
    assert "reason" not in gateway.requests[0]["diagnosis"]
    assert candidate.semantic_provenance == {
        "decision_source": "model",
        "model_provider": "test-provider",
        "model_name": "test-model",
        "prompt_version": "m9r-recommendation-v2",
    }


def test_recommendation_model_interpreter_fallback_on_error() -> None:
    """模型抛异常 → 保持观察，不允许规则选择补货等经营语义。"""
    gateway = _MockGateway(raise_exc=True)
    interpreter = RecommendationModelInterpreter(gateway)
    candidate = interpreter.interpret(_diagnosis())
    assert candidate.type == RecommendationType.KEEP_OBSERVE
    assert candidate.degraded is True
    assert candidate.rationale == "model_unavailable"


def test_recommendation_model_interpreter_invalid_type_falls_back() -> None:
    """模型返回非法 type → 保持观察，不启用规则映射。"""
    gateway = _MockGateway(return_value={"type": "非法类型", "rationale": "bad"})
    interpreter = RecommendationModelInterpreter(gateway)
    candidate = interpreter.interpret(_diagnosis())
    assert candidate.type == RecommendationType.KEEP_OBSERVE
    assert candidate.degraded is True


def test_recommendation_model_interpreter_evidence_insufficient() -> None:
    """EVIDENCE_INSUFFICIENT 诊断 → 模型应产保持观察（或降级）。"""
    gateway = _MockGateway(
        return_value={"type": "保持观察", "rationale": "keep observe"}
    )
    interpreter = RecommendationModelInterpreter(gateway)
    candidate = interpreter.interpret(_diagnosis(DiagnosisType.EVIDENCE_INSUFFICIENT))
    assert candidate.type == RecommendationType.KEEP_OBSERVE


def test_missing_revision_is_fed_back_to_model_for_redeliberation() -> None:
    gateway = _MockGateway(responses=[
        {"type": "受控实验", "rationale": "first choice"},
        {"type": "选品候选", "rationale": "reconsidered"},
    ])
    interpreter = RecommendationModelInterpreter(gateway)

    candidate = interpreter.interpret(
        _diagnosis(DiagnosisType.CONVERSION_INSUFFICIENT),
        {"product": {"listing_revision": None}, "metrics": {}},
    )

    assert candidate.type is RecommendationType.SELECTION
    assert gateway.calls == 2
    assert gateway.requests[1]["execution_feedback"] == {
        "rejected_type": "受控实验",
        "verified_failure": "trusted_listing_revision_missing",
        "instruction": (
            "该类型缺少可信执行前提，请重新综合全部事实选择语义类型；"
            "反馈不是目标标签。"
        ),
    }


def test_forbidden_rationale_is_fed_back_for_safe_rewrite() -> None:
    gateway = _MockGateway(responses=[
        {"type": "选品候选", "rationale": "宣称效果提升"},
        {"type": "选品候选", "rationale": "作为待验证候选进入人工评审"},
    ])
    interpreter = RecommendationModelInterpreter(gateway)

    candidate = interpreter.interpret(_diagnosis(), {"product": {}, "metrics": {}})

    assert candidate.type is RecommendationType.SELECTION
    assert candidate.rationale == "作为待验证候选进入人工评审"
    assert gateway.calls == 2
    assert gateway.requests[1]["execution_feedback"] == {
        "rejected_type": "选品候选",
        "verified_failure": "forbidden_rationale_content",
        "instruction": "保持仍受事实支持的语义类型，仅用谨慎且不含禁用表述的理由重写。",
    }


def test_present_revision_rejects_selection_and_returns_to_model() -> None:
    gateway = _MockGateway(responses=[
        {"type": "选品候选", "rationale": "incorrectly ignored revision"},
        {"type": "受控实验", "rationale": "uses the trusted revision"},
    ])
    interpreter = RecommendationModelInterpreter(gateway)

    candidate = interpreter.interpret(
        _diagnosis(),
        {
            "product": {"listing_revision": {"revision_id": "rev-1"}},
            "metric_values": {"ad_spend": 0},
        },
    )

    assert candidate.type is RecommendationType.EXPERIMENT
    assert gateway.calls == 2
    assert gateway.requests[1]["execution_feedback"]["verified_failure"] == (
        "trusted_listing_revision_present"
    )


def test_conservative_proposal_gets_model_self_review() -> None:
    gateway = _MockGateway(responses=[
        {"type": "曝光/点击诊断", "rationale": "first conservative draft"},
        {"type": "受控实验", "rationale": "specific direction after review"},
    ])
    interpreter = RecommendationModelInterpreter(gateway)

    candidate = interpreter.interpret(
        _diagnosis(),
        {
            "product": {"listing_revision": {"revision_id": "rev-1"}},
            "metric_values": {"ad_spend": 0},
        },
    )

    assert candidate.type is RecommendationType.EXPERIMENT
    assert gateway.calls == 2
    review = gateway.requests[1]["semantic_self_review"]
    assert review["proposed_type"] == "曝光/点击诊断"
    assert "目标标签" in review["instruction"]


def test_persistent_missing_precondition_fails_closed_after_retries() -> None:
    gateway = _MockGateway(
        responses=[
            {"type": "受控实验", "rationale": f"attempt {attempt}"}
            for attempt in range(3)
        ]
    )
    interpreter = RecommendationModelInterpreter(gateway)

    candidate = interpreter.interpret(
        _diagnosis(DiagnosisType.CONVERSION_INSUFFICIENT),
        {"product": {"listing_revision": None}, "metric_values": {}},
    )

    assert gateway.calls == 3
    assert candidate.type is RecommendationType.KEEP_OBSERVE
    assert candidate.degraded is True
    assert candidate.rationale == (
        "model_output_rejected:trusted_listing_revision_missing"
    )
    assert candidate.semantic_provenance["decision_source"] == (
        "model_output_rejected"
    )


def test_persistent_forbidden_rationale_fails_closed_after_retries() -> None:
    gateway = _MockGateway(
        responses=[
            {"type": "保持观察", "rationale": "宣称平台权重提升"},
            {"type": "保持观察", "rationale": "继续宣称效果提升"},
            {"type": "保持观察", "rationale": "仍然宣称流量扶持"},
        ]
    )
    interpreter = RecommendationModelInterpreter(gateway)

    candidate = interpreter.interpret(_diagnosis(), {"product": {}, "metrics": {}})

    assert gateway.calls == 3
    assert candidate.type is RecommendationType.KEEP_OBSERVE
    assert candidate.degraded is True
    assert candidate.rationale == "model_output_rejected:forbidden_rationale_content"
    assert candidate.semantic_provenance["decision_source"] == (
        "model_output_rejected"
    )
