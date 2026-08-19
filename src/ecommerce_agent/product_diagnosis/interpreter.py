"""M9-R WP2 诊断语义解释器（模型角色占位，对齐 D-034）。

边界声明（D-034）：
- 确定性代码（diagnosis.py）只产出可执行事实；语义诊断类型由「解释器」产出。
- 本模块是解释器的**确定性占位实现**（Ruleset），用于端到端链路可测；
  真实场景替换为模型（prompt 走 _TRAFFIC_ANALYSIS_SYSTEM_PROMPT 同款约束：
  「你已经拿到固化统计事实，没有执行权，只做三件事」）。
- 占位不等于验收依据：机制 Eval 的验收点是「事实 + 门禁 + 校验」，
  不是解释器选择的类型（避免把 stub 锁成自洽假绿）。
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol

from .diagnosis import Diagnosis, DiagnosisFacts, DiagnosisType, validate_diagnosis_output


class DiagnosisInterpreter(Protocol):
    """语义解释器：输入可执行事实，产出诊断候选。"""

    def interpret(self, facts: DiagnosisFacts) -> dict[str, Any]: ...


class RulesetDiagnosisInterpreter:
    """确定性占位：按固定规则选语义类型（仅链路演示，非生产语义决策）。

    注意：固定规则把「语义下一步」写在确定性代码里，违反 D-034——
    本实现仅用于测试链路端到端可跑，生产必须替换为模型解释器。
    """

    def interpret(self, facts: DiagnosisFacts) -> dict[str, Any]:
        if facts.stockout:
            return {"diagnosis_type": DiagnosisType.STOCKOUT_POLLUTION.value, "reason": "stockout_period_observed"}
        if facts.pollution is not None:
            return {"diagnosis_type": DiagnosisType.AD_PRICE_POLLUTION.value, "reason": f"pollution:{facts.pollution}"}
        if facts.evidence_state in (None, "missing"):
            return {"diagnosis_type": DiagnosisType.EVIDENCE_INSUFFICIENT.value, "reason": "evidence_missing"}
        if facts.exposures is not None and facts.exposures < 100.0:
            return {"diagnosis_type": DiagnosisType.EXPOSURE_INSUFFICIENT.value, "reason": f"exposures_below_threshold:{facts.exposures}"}
        if facts.clicks is not None and facts.exposures:
            ctr = facts.clicks / facts.exposures
            if ctr < 0.01:
                return {"diagnosis_type": DiagnosisType.CLICK_INSUFFICIENT.value, "reason": f"ctr_below_threshold:{ctr:.4f}"}
        if facts.conversions is not None and facts.clicks:
            conv_rate = facts.conversions / facts.clicks
            if conv_rate < 0.02:
                return {"diagnosis_type": DiagnosisType.CONVERSION_INSUFFICIENT.value, "reason": f"conv_below_threshold:{conv_rate:.4f}"}
        return {"diagnosis_type": DiagnosisType.EVIDENCE_INSUFFICIENT.value, "reason": "no_issue_detected"}


def run_interpretation(facts: DiagnosisFacts, interpreter: DiagnosisInterpreter) -> Diagnosis:
    """跑解释器并校验（确定性代码锁住语义边界）。"""
    produced = interpreter.interpret(facts)
    return validate_diagnosis_output(facts, produced)


__all__ = [
    "DiagnosisInterpreter",
    "RulesetDiagnosisInterpreter",
    "run_interpretation",
]
