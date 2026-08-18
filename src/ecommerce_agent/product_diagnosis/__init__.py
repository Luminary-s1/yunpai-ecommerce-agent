"""M9-R WP2 诊断包：M5-R 证据桥接 + 确定性 Gate + 结构化诊断 + 受控实验。

公开 API 边界：
- 桥接：EvidenceBridge（统一只读证据查询）
- 门控：GateEngine / GateResult / FORBIDDEN_KEYS（确定性判定）
- 诊断：build_diagnosis / Diagnosis / DiagnosisType（结构化诊断）
- 实验：ExperimentGateway / ExperimentNotAvailableError / ExperimentPath（双路径）
"""
from .bridge import EvidenceBridge, PROVENANCE_PATHS
from .diagnosis import Diagnosis, DiagnosisType, build_diagnosis
from .experiment import (
    ExperimentGateway,
    ExperimentNotAvailableError,
    ExperimentPath,
)
from .gates import FORBIDDEN_KEYS, GateEngine, GateResult

__all__ = [
    "Diagnosis",
    "DiagnosisType",
    "EvidenceBridge",
    "ExperimentGateway",
    "ExperimentNotAvailableError",
    "ExperimentPath",
    "FORBIDDEN_KEYS",
    "GateEngine",
    "GateResult",
    "PROVENANCE_PATHS",
    "build_diagnosis",
]
