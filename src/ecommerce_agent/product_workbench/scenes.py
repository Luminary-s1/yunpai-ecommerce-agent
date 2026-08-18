"""M9-R WP4 冻结场景集 + 独立 oracle。

边界声明：
- 场景：固定输入（SKU 流量/revision/窗口）→ 固定输出（诊断/建议）。
- Oracle：确定性断言——给定输入，输出必须满足期望（不是模型打分）。
- 副作用：零——纯数据 + 断言。
- 失败暴露：场景缺字段 → 抛 ValueError（不静默跳过）。
- 确定性：场景数据硬编码，无时间/随机依赖。
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


# 冻结场景集（2 类：缺货污染 / 合格实验）——对齐任务书「真实粒度不足 / 显式模拟实验」
FROZEN_SCENES: list[FrozenScene] = [
    FrozenScene(
        "缺货污染",
        input_data={
            "sku_id": "sku-a",
            "evidence_state": "actual",
            "exposures": 1000,
            "clicks": 100,
            "stockout": True,
        },
        expected={
            # 缺货污染必须被标记，不得归因标题/主图
            "diagnosis_type": DiagnosisType.STOCKOUT_POLLUTION.value,
            "degraded": True,
        },
    ),
    FrozenScene(
        "合格实验",
        input_data={
            "sku_id": "sku-b",
            "evidence_state": "actual",
            "exposures": 5000,
            "clicks": 500,
            "conversions": 50,
        },
        expected={
            # 合格数据 → 无问题（不编造污染/不足）
            "diagnosis_type": DiagnosisType.EVIDENCE_INSUFFICIENT.value,
            "reason": "no_issue_detected",
        },
    ),
]


def find_scene(name: str) -> FrozenScene:
    """按名称找场景；不存在 → 抛（不静默）。"""
    for scene in FROZEN_SCENES:
        if scene.name == name:
            return scene
    raise ValueError(f"frozen_scene_not_found:{name}")


__all__ = [
    "FROZEN_SCENES",
    "FrozenScene",
    "find_scene",
]
