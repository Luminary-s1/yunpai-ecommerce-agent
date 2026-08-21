"""M9-R WP2 诊断语义解释器（D-034：语义由模型产生，确定性代码只校验）。

边界声明（D-034）：
- 确定性代码（diagnosis.py）只产出可执行事实；语义诊断类型由「解释器」产出。
- `DiagnosisModelInterpreter`：模型解释器（生产路径）——复用 ModelGateway +
  M5-R 三件套模式（system prompt 约束"无执行权 + 只产 diagnosis_type/reason +
  按 output_schema 返回 JSON"），输出经 validate_diagnosis_output 校验。
- `RulesetDiagnosisInterpreter`：确定性降级（fail-safe）——模型不可用/失败时
  退回固定规则，保证链路不因模型故障中断；但这不等于 D-034 达标（达标 = 模型
  可用时确实走模型）。
- 失败暴露：模型抛异常 → 降级 Ruleset（不崩溃）；模型产出非法类型/越权词 →
  validate_diagnosis_output 抛 ValueError（不静默）。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Mapping, Protocol

from pydantic import BaseModel, ConfigDict

from .diagnosis import Diagnosis, DiagnosisFacts, DiagnosisType, validate_diagnosis_output

# 诊断模型输出 schema：只允许模型产 diagnosis_type + reason，不产数值/证据/越权字段。
# extra=forbid 使模型多填字段即 schema 校验失败（由 generate_json 的 Pydantic 校验兜底）。
_DIAGNOSIS_SYSTEM_PROMPT = """\
你是商品流量诊断的语义解释器。确定性代码已经固化统计事实（证据状态、门禁、污染旗标、原始漏斗数值），你没有执行权，且不得修改、替代或重算 effect、confidence interval、sample size、quality gate 或任何证据引用。

只做两件事：
1. 根据给出的固化事实选择唯一的诊断类型（diagnosis_type）；
2. 用谨慎语言给出诊断理由（reason），把机制描述为待验证假设，不宣称掌握平台内部权重或因果机制。

可选诊断类型（严格取值）：
- stockout_pollution：缺货污染（facts.stockout 为真时）
- ad_price_pollution：广告/价格变更污染（facts.pollution 非空时）
- evidence_insufficient：证据缺失或不足（evidence_state 为 missing 时）
- exposure_insufficient：曝光不足
- click_insufficient：点击不足（CTR 偏低）
- conversion_insufficient：转化不足（转化率偏低）

严格按用户消息中的 output_schema 返回一个 JSON object，不要添加数值字段、统计字段或模型元数据。\
"""


class _DiagnosisModelOutput(BaseModel):
    """模型诊断输出契约（仅诊断类型 + 理由，无任何数值/证据/越权字段）。"""

    model_config = ConfigDict(extra="forbid")

    diagnosis_type: DiagnosisType
    reason: str | None = None


class DiagnosisInterpreter(Protocol):
    """语义解释器：输入可执行事实，产出诊断候选。"""

    def interpret(self, facts: DiagnosisFacts) -> dict[str, Any]: ...


class DiagnosisModelInterpreter:
    """模型诊断解释器（D-034 生产路径）：复用 ModelGateway 三件套。

    失败/超时/模型禁用 → 降级 RulesetDiagnosisInterpreter（fail-safe）。
    """

    def __init__(self, gateway: Any) -> None:
        self.gateway = gateway
        self._fallback = RulesetDiagnosisInterpreter()

    def interpret(self, facts: DiagnosisFacts) -> dict[str, Any]:
        output_schema = _DiagnosisModelOutput.model_json_schema()
        request = {
            "facts_authority": "deterministic_code",
            "facts": asdict(facts),
            "output_schema": output_schema,
        }
        try:
            raw = self.gateway.generate_json(
                [
                    {"role": "system", "content": _DIAGNOSIS_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(request, ensure_ascii=False, sort_keys=True),
                    },
                ],
                thinking_enabled=False,
            )
            # 模型输出经 Pydantic 校验（diagnosis_type 必须合法，extra=forbid 拒绝
            # 越权字段）；非法 → 抛异常 → 降级 Ruleset（不让非法值透传下游）。
            parsed = _DiagnosisModelOutput.model_validate(raw)
        except Exception:  # noqa: BLE001 — 模型故障/输出非法 → fail-safe 降级
            return self._fallback.interpret(facts)
        return {"diagnosis_type": parsed.diagnosis_type.value, "reason": parsed.reason}


class RulesetDiagnosisInterpreter:
    """确定性降级：按固定规则选语义类型（fail-safe，非生产语义决策）。

    注意：固定规则把「语义下一步」写在确定性代码里，违反 D-034——
    仅用于模型不可用时的降级，生产语义决策由 DiagnosisModelInterpreter 承担。
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
    "DiagnosisModelInterpreter",
    "RulesetDiagnosisInterpreter",
    "run_interpretation",
]
