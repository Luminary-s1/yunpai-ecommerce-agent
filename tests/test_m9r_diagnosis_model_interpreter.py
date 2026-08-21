"""M9-R WP2/WP3 诊断与建议模型解释器测试（D-034 反假绿）。

验证：模型解释器被调用产出语义类型；模型输出经校验；失败/越权降级 Ruleset。
反假绿：mock 模型返回非法类型/抛异常时，链路不崩溃、正确降级或拒绝。
"""
from __future__ import annotations

from ecommerce_agent.product_diagnosis.diagnosis import (
    DiagnosisType,
    build_diagnosis_facts,
)
from ecommerce_agent.product_diagnosis.interpreter import (
    DiagnosisModelInterpreter,
    run_interpretation,
)


class _MockGateway:
    """mock ModelGateway：返回固定诊断 JSON，或按需抛异常。"""

    def __init__(self, return_value: dict | None = None, raise_exc: bool = False):
        self._return = return_value or {}
        self._raise = raise_exc
        self.calls = 0

    def generate_json(self, messages, **kwargs):
        self.calls += 1
        if self._raise:
            raise RuntimeError("model unavailable")
        return self._return


def _facts(**kw):
    return build_diagnosis_facts(
        "sku-x",
        {
            "evidence_state": kw.get("evidence_state", "actual"),
            "exposures": kw.get("exposures"),
            "clicks": kw.get("clicks"),
            "conversions": kw.get("conversions"),
            "quality_gate": {"status": "passed", "issues": []},
        },
        stockout=kw.get("stockout", False),
        pollution=kw.get("pollution"),
    )


def test_diagnosis_model_interpreter_called() -> None:
    """模型可用时走模型（D-034 达标：语义由模型产生）。"""
    gateway = _MockGateway(
        return_value={"diagnosis_type": "stockout_pollution", "reason": "model said stockout"}
    )
    interpreter = DiagnosisModelInterpreter(gateway)
    facts = _facts(stockout=True)
    produced = interpreter.interpret(facts)
    assert gateway.calls == 1, "模型未被调用"
    assert produced["diagnosis_type"] == "stockout_pollution"


def test_diagnosis_model_interpreter_fallback_on_error() -> None:
    """模型抛异常 → 降级 Ruleset（不崩溃）。"""
    gateway = _MockGateway(raise_exc=True)
    interpreter = DiagnosisModelInterpreter(gateway)
    facts = _facts(stockout=True)
    produced = interpreter.interpret(facts)
    # Ruleset 降级：stockout → STOCKOUT_POLLUTION
    assert produced["diagnosis_type"] == "stockout_pollution"


def test_diagnosis_model_interpreter_invalid_type_falls_back() -> None:
    """模型产出非法类型 → Pydantic 校验失败 → 降级 Ruleset（不抛、不静默透传）。"""
    gateway = _MockGateway(
        return_value={"diagnosis_type": "not_a_real_type", "reason": "bad"}
    )
    interpreter = DiagnosisModelInterpreter(gateway)
    facts = _facts(stockout=True)
    produced = interpreter.interpret(facts)
    # 非法类型经 _DiagnosisModelOutput.model_validate 失败 → 降级 Ruleset
    # stockout → STOCKOUT_POLLUTION（合法类型，非非法值透传）
    assert produced["diagnosis_type"] == "stockout_pollution"


def test_diagnosis_model_interpreter_missing_type_falls_back() -> None:
    """模型返回缺 diagnosis_type → 降级 Ruleset。"""
    gateway = _MockGateway(return_value={"reason": "no type"})
    interpreter = DiagnosisModelInterpreter(gateway)
    facts = _facts(stockout=True)
    produced = interpreter.interpret(facts)
    assert produced["diagnosis_type"] == "stockout_pollution"
