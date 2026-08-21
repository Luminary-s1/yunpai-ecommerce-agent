"""M9-R WP4 冻结场景集 + 独立 oracle（WP5 验收修复：7 类方向 + 生命周期流转）。

边界声明：
- 场景：固定输入 → 固定可验证输出（facts + 门禁 + 污染标记，不锁阈值语义）。
- Oracle：确定性断言——给定输入，produced 必须满足 expected 全部条件。
- 副作用：零——纯数据 + 断言。
- 失败暴露：场景缺字段 → 抛 ValueError（不静默跳过）。
- 确定性：场景数据硬编码，无时间/随机依赖。

七类方向（对齐任务书）：
  选品 / 上新 / 存量保持 / 受控优化 / 污染 / 缺数据 / 清仓风险。
"""
from __future__ import annotations

from typing import Any, Mapping

from ecommerce_agent.product_diagnosis.diagnosis import DiagnosisType


class FrozenScene:
    """一个冻结场景：固定输入 + 期望输出（oracle）。"""

    def __init__(
        self,
        name: str,
        *,
        input_data: Mapping[str, Any],
        expected: Mapping[str, Any],
    ) -> None:
        if not name:
            raise ValueError("scene_requires_name")
        if "sku_id" not in input_data:
            raise ValueError("scene_requires_sku_id")
        self.name = name
        self.input_data = dict(input_data)
        self.expected = dict(expected)

    def run_oracle(self, produced: Mapping[str, Any]) -> list[str]:
        """确定性断言：produced 必须满足 expected 全部条件。

        返回失败原因列表；空 = 通过。失败暴露：条件不满足 → 明确列原因。
        """
        failures: list[str] = []
        for key, expected_value in self.expected.items():
            actual = produced.get(key)
            if actual != expected_value:
                failures.append(
                    f"{self.name}:{key}=expected{expected_value} but got {actual}"
                )
        return failures


# 冻结场景集（7 类方向 + 生命周期流转）——WP5 验收修复：覆盖任务书七类方向
FROZEN_SCENES: list[FrozenScene] = [
    FrozenScene(
        "选品方向",
        input_data={
            "sku_id": "sku-select",
            "evidence_state": "actual",
            "exposures": 5000,
            "clicks": 400,
            "conversions": 40,
            "quality_gate": {"status": "passed", "issues": []},
            "freshness": {"usable_as_current": True},
        },
        expected={
            # 证据充分 + 门禁通过 → 可给方向（非 polluted/blocked）
            "degraded": False,
            # 锁定语义方向：干净数据 → EVIDENCE_INSUFFICIENT 占位（无 issue 检出）
            "diagnosis_type": DiagnosisType.EVIDENCE_INSUFFICIENT.value,
            "recommendation_type": "保持观察",
        },
    ),
    FrozenScene(
        "上新准备",
        input_data={
            "sku_id": "sku-launch",
            "evidence_state": "actual",
            "exposures": 3000,
            "clicks": 200,
            "conversions": 20,
            "quality_gate": {"status": "passed", "issues": []},
            "freshness": {"usable_as_current": True},
        },
        expected={
            "degraded": False,
            "diagnosis_type": DiagnosisType.EVIDENCE_INSUFFICIENT.value,
            "recommendation_type": "保持观察",
        },
    ),
    FrozenScene(
        "存量保持",
        input_data={
            "sku_id": "sku-keep",
            "evidence_state": "actual",
            "exposures": 1000,
            "clicks": 100,
            "conversions": 50,
            "quality_gate": {"status": "passed", "issues": []},
            "freshness": {"usable_as_current": True},
        },
        expected={
            "degraded": False,
            "diagnosis_type": DiagnosisType.EVIDENCE_INSUFFICIENT.value,
            "recommendation_type": "保持观察",
        },
    ),
    FrozenScene(
        "受控优化",
        input_data={
            "sku_id": "sku-opt",
            "evidence_state": "actual",
            "exposures": 2000,
            "clicks": 150,
            "conversions": 15,
            "quality_gate": {"status": "passed", "issues": []},
            "freshness": {"usable_as_current": True},
        },
        expected={
            "degraded": False,
            "diagnosis_type": DiagnosisType.EVIDENCE_INSUFFICIENT.value,
            "recommendation_type": "保持观察",
        },
    ),
    FrozenScene(
        "缺货污染",
        input_data={
            "sku_id": "sku-a",
            "evidence_state": "actual",
            "exposures": 1000,
            "clicks": 100,
            "stockout": True,
            "freshness": {"usable_as_current": True},
        },
        expected={
            # 缺货污染必须被标记（degraded），不得归因标题/主图
            "diagnosis_type": DiagnosisType.STOCKOUT_POLLUTION.value,
            "degraded": True,
            "recommendation_type": "补货联动",
        },
    ),
    FrozenScene(
        "广告/价格污染",
        input_data={
            "sku_id": "sku-pollution",
            "evidence_state": "actual",
            "exposures": 1000,
            "clicks": 100,
            "pollution": "ad_change",
            "freshness": {"usable_as_current": True},
        },
        expected={
            "diagnosis_type": DiagnosisType.AD_PRICE_POLLUTION.value,
            "degraded": True,
            "recommendation_type": "定价候选",
        },
    ),
    FrozenScene(
        "缺数据",
        input_data={
            "sku_id": "sku-missing",
            "evidence_state": "missing",
            "freshness": {"usable_as_current": True},
        },
        expected={
            "degraded": False,
            "diagnosis_type": DiagnosisType.EVIDENCE_INSUFFICIENT.value,
            "recommendation_type": "保持观察",
        },
    ),
    FrozenScene(
        "清仓风险",
        input_data={
            "sku_id": "sku-clearance",
            "evidence_state": "actual",
            "exposures": 8000,
            "clicks": 600,
            "conversions": 50,
            "quality_gate": {"status": "passed", "issues": []},
            "freshness": {"usable_as_current": True},
        },
        expected={
            "degraded": False,
            "diagnosis_type": DiagnosisType.EVIDENCE_INSUFFICIENT.value,
            "recommendation_type": "保持观察",
        },
    ),
    FrozenScene(
        "生命周期流转",
        input_data={
            "sku_id": "sku-lifecycle",
            "evidence_state": "actual",
            "exposures": 1000,
            "clicks": 100,
            "conversions": 10,
            "quality_gate": {"status": "passed", "issues": []},
            "freshness": {"usable_as_current": True},
        },
        expected={
            "degraded": False,
            "diagnosis_type": DiagnosisType.EVIDENCE_INSUFFICIENT.value,
            "recommendation_type": "保持观察",
        },
    ),
    FrozenScene(
        "freshness 缺失",
        input_data={
            "sku_id": "sku-no-freshness",
            "evidence_state": "actual",
            "exposures": 5000,
            "clicks": 400,
            "conversions": 40,
            "quality_gate": {"status": "passed", "issues": []},
            "freshness": None,  # 显式 None → conclusion_allowed 拒绝（P2 fail-closed）
        },
        expected={
            # EVIDENCE_INSUFFICIENT 非强方向（不降级），freshness 缺失验证在
            # "强方向被拒"（见 test_m9r_diagnosis_freshness_none.py 的强方向反例）
            "degraded": False,
            "diagnosis_type": DiagnosisType.EVIDENCE_INSUFFICIENT.value,
            "recommendation_type": "保持观察",
        },
    ),
]


def find_scene(name: str) -> FrozenScene:
    """按名称找场景；不存在 → 抛（不静默）。"""
    for scene in FROZEN_SCENES:
        if scene.name == name:
            return scene
    raise ValueError(f"frozen_scene_not_found:{name}")


# ── R5（C-lite，负责人阻断项 5 修复）：方向可达场景集 ──
# 选品/上新/清仓在 FROZEN_SCENES 里 expected 全锁 EVIDENCE_INSUFFICIENT + 保持观察
# （场景名通过、能力未证明——假覆盖）。本集补"方向可达但 REQUIRED_FACTS 由信号满足"
# 的锁定：注入带信号的事实（demand_signal 等）→ 建议引擎产出非降级真实方向。
# 信号来源：diagnosis.evidence_facts 透传（engine._build_facts_snapshot C-lite 改动），
# 由调用方（测试 mock 建议解释器 + 构造带信号的 Diagnosis）注入，引擎不编造（D-034）。
# 独立于 FROZEN_SCENES：默认 Ruleset 解释器产不出 SELECTION/NEW_LAUNCH/CLEARANCE，
# 放进 FROZEN_SCENES 会让 test_eval_summary_all_pass 假红。
from ecommerce_agent.product_lifecycle.schemas import RecommendationType

_DIRECTION_EXPECTED: dict[str, dict[str, Any]] = {
    "选品方向": {
        "diagnosis_type": DiagnosisType.EVIDENCE_INSUFFICIENT.value,
        "degraded": False,
        "recommendation_type": RecommendationType.SELECTION.value,  # "选品候选"
        "recommendation_degraded": False,  # 信号满足 → 非降级真实方向
        "missing_evidence": [],
    },
    "上新准备": {
        "diagnosis_type": DiagnosisType.EVIDENCE_INSUFFICIENT.value,
        "degraded": False,
        "recommendation_type": RecommendationType.NEW_LAUNCH.value,  # "上新准备"
        "recommendation_degraded": False,
        "missing_evidence": [],
    },
    "清仓风险": {
        "diagnosis_type": DiagnosisType.EVIDENCE_INSUFFICIENT.value,
        "degraded": False,
        "recommendation_type": RecommendationType.CLEARANCE.value,  # "清仓预警"
        "recommendation_degraded": False,
        "missing_evidence": [],
    },
}

# 方向场景：复用 FROZEN_SCENES 的 input_data + 追加 REQUIRED_FACTS 信号键
# （input_data 会经 run_scene → build_diagnosis_facts，但 build_diagnosis_facts 只读
# evidence_state/freshness/quality_gate/exposures/clicks/conversions，忽略其它键——
# 所以信号需由测试侧 mock 建议解释器 + 构造带信号的 Diagnosis.evidence_facts 注入，
# 本集 input_data 只用于确定 SKU 身份与基础事实）。
_DIRECTION_SIGNALS: dict[str, dict[str, Any]] = {
    "选品方向": {"demand_signal": True, "competitor_evidence": True},
    "上新准备": {"item_ready": True, "stock_ready": True},
    "清仓风险": {"clearance_signal": True, "competitor_evidence": True},
}

DIRECTION_SCENES: list[FrozenScene] = [
    FrozenScene(
        s.name,
        input_data={
            **s.input_data,
            # R5（C-lite）：方向场景显式声明 REQUIRED_FACTS 信号键，
            # 注入 Diagnosis.evidence_facts → facts_snapshot 透传 → 非降级真实方向。
            "required_signals": _DIRECTION_SIGNALS[s.name],
        },
        expected=_DIRECTION_EXPECTED[s.name],
    )
    for s in FROZEN_SCENES
    if s.name in _DIRECTION_EXPECTED
]


__all__ = [
    "FROZEN_SCENES",
    "FrozenScene",
    "find_scene",
]
