"""M9-R WP2 诊断包：M5-R 证据桥接 + 确定性 Gate + 结构化诊断 + 受控实验。

公开 API 边界：
- 桥接：EvidenceBridge（统一只读证据查询）
- 门控：GateEngine / GateResult / FORBIDDEN_KEYS（确定性判定）
- 诊断：DiagnosisFacts / build_diagnosis_facts / validate_diagnosis_output（D-034：确定性事实 + 模型语义校验）
- 解释器：DiagnosisInterpreter / RulesetDiagnosisInterpreter（语义角色占位）
- 实验：ExperimentGateway / ExperimentNotAvailableError / ExperimentPath（双路径）
"""
from .bridge import EvidenceBridge
from .diagnosis import (
    FORBIDDEN_DIAGNOSIS_KEYS,
    Diagnosis,
    DiagnosisFacts,
    DiagnosisType,
    build_diagnosis_facts,
    validate_diagnosis_output,
)
from .experiment import (
    ExperimentGateway,
    ExperimentNotAvailableError,
    ExperimentPath,
)
from .gates import FORBIDDEN_KEYS, GateEngine, GateResult
from .interpreter import (
    DiagnosisInterpreter,
    RulesetDiagnosisInterpreter,
    run_interpretation,
)

__all__ = [
    "FORBIDDEN_DIAGNOSIS_KEYS",
    "Diagnosis",
    "DiagnosisFacts",
    "DiagnosisInterpreter",
    "DiagnosisType",
    "EvidenceBridge",
    "ExperimentGateway",
    "ExperimentNotAvailableError",
    "ExperimentPath",
    "FORBIDDEN_KEYS",
    "GateEngine",
    "GateResult",
    "RulesetDiagnosisInterpreter",
    "build_diagnosis_facts",
    "run_interpretation",
    "validate_diagnosis_output",
]
