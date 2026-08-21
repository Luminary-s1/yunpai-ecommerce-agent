"""M9-R WP3 建议模型解释器测试（D-034 反假绿）。

验证：模型解释器被调用产出建议类型/理由；失败降级 Ruleset；越权输出被校验拒绝。
"""
from __future__ import annotations

from datetime import UTC, datetime

from ecommerce_agent.product_diagnosis.diagnosis import Diagnosis, DiagnosisType
from ecommerce_agent.product_lifecycle.engine import (
    RecommendationModelInterpreter,
    RecommendationType,
)


class _MockGateway:
    def __init__(self, return_value: dict | None = None, raise_exc: bool = False):
        self._return = return_value or {}
        self._raise = raise_exc
        self.calls = 0

    def generate_json(self, messages, **kwargs):
        self.calls += 1
        if self._raise:
            raise RuntimeError("model unavailable")
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
        return_value={"type": "补货联动", "rationale": "model suggested restock", "degraded": True}
    )
    interpreter = RecommendationModelInterpreter(gateway)
    candidate = interpreter.interpret(_diagnosis())
    assert gateway.calls == 1, "模型未被调用"
    assert candidate.type == RecommendationType.RESTOCK
    assert candidate.rationale == "model suggested restock"


def test_recommendation_model_interpreter_fallback_on_error() -> None:
    """模型抛异常 → 降级 Ruleset（不崩溃）。"""
    gateway = _MockGateway(raise_exc=True)
    interpreter = RecommendationModelInterpreter(gateway)
    candidate = interpreter.interpret(_diagnosis())
    # Ruleset 降级：STOCKOUT_POLLUTION → RESTOCK
    assert candidate.type == RecommendationType.RESTOCK


def test_recommendation_model_interpreter_invalid_type_falls_back() -> None:
    """模型返回非法 type → Pydantic 校验失败 → 降级 Ruleset。"""
    gateway = _MockGateway(return_value={"type": "非法类型", "rationale": "bad"})
    interpreter = RecommendationModelInterpreter(gateway)
    candidate = interpreter.interpret(_diagnosis())
    # 非法 type 经 _RecommendationModelOutput 校验失败 → 降级
    assert candidate.type == RecommendationType.RESTOCK


def test_recommendation_model_interpreter_evidence_insufficient() -> None:
    """EVIDENCE_INSUFFICIENT 诊断 → 模型应产保持观察（或降级）。"""
    gateway = _MockGateway(
        return_value={"type": "保持观察", "rationale": "keep observe", "degraded": True}
    )
    interpreter = RecommendationModelInterpreter(gateway)
    candidate = interpreter.interpret(_diagnosis(DiagnosisType.EVIDENCE_INSUFFICIENT))
    assert candidate.type == RecommendationType.KEEP_OBSERVE
