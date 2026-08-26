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
        "活动候选",
        input_data={
            "sku_id": "sku-promotion",
            "evidence_state": "actual",
            "exposures": 2500,
            "clicks": 180,
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


# ── 方向发现场景 ──
# production_input 只含读模型可表达的原始业务事实；expected 是调用完成后才读取的
# assertion-only oracle。固定 mock 只能证明链路机械性，真实方向结论必须由 live runner
# 调用生产模型解释器得出。
from ecommerce_agent.product_lifecycle.schemas import RecommendationType

_DIRECTION_EXPECTED: dict[str, dict[str, Any]] = {
    "选品方向": {
        "recommendation_type": RecommendationType.SELECTION.value,  # "选品候选"
        "recommendation_degraded": False,  # 信号满足 → 非降级真实方向
        "missing_evidence": [],
    },
    "上新准备": {
        "recommendation_type": RecommendationType.NEW_LAUNCH.value,  # "上新准备"
        "recommendation_degraded": False,
        "missing_evidence": [],
    },
    "清仓风险": {
        "recommendation_type": RecommendationType.CLEARANCE.value,  # "清仓预警"
        "recommendation_degraded": False,
        "missing_evidence": [],
    },
    "受控优化": {
        "recommendation_type": RecommendationType.EXPERIMENT.value,  # "受控实验"
        "recommendation_degraded": False,
        "missing_evidence": [],
    },
    "活动候选": {
        "recommendation_type": RecommendationType.PROMOTION.value,  # "活动候选"
        "recommendation_degraded": True,
        "missing_evidence": ["campaign_window"],
    },
}

_DIRECTION_BUSINESS_FACTS: dict[str, dict[str, Any]] = {
    "选品方向": {
        "title": "模块化桌面收纳架",
        "metrics": {
            "impressions": 5000, "clicks": 400, "add_to_cart": 180,
            "orders": 120, "payments": 110, "refunds": 0,
            "net_sales": 13200, "ad_spend": 0, "competitor_price": 129,
        },
    },
    "上新准备": {
        "listing_revision": {
            "revision_id": "rev-blind-2", "revision_no": 1,
            "active_from": "2026-08-20T00:00:00+00:00",
        },
        "title": "轻量折叠桌 80cm",
        "metrics": {
            "impressions": 0, "clicks": 0, "add_to_cart": 0,
            "orders": 0, "payments": 0,
            "sellable_stock": 80, "in_transit_stock": 20,
        },
    },
    "清仓风险": {
        "metrics": {
            "impressions": 600, "clicks": 24, "add_to_cart": 3,
            "orders": 1, "payments": 0, "refunds": 0, "net_sales": 0,
            "sellable_stock": 100000, "in_transit_stock": 10000,
            "ad_spend": 0, "competitor_price": 79,
        },
    },
    "受控优化": {
        "listing_revision": {
            "revision_id": "rev-blind-4", "revision_no": 3,
            "active_from": "2026-08-01T00:00:00+00:00",
        },
        "title": "模块化桌面收纳架",
        "metrics": {
            "impressions": 1000000, "clicks": 1000, "add_to_cart": 950,
            "orders": 920, "payments": 900, "refunds": 0, "net_sales": 117000,
            "sellable_stock": 300, "in_transit_stock": 20,
            "ad_spend": 0, "competitor_price": 120, "experiment_state": 0,
        },
    },
    "活动候选": {
        "listing_revision": {
            "revision_id": "rev-blind-5", "revision_no": 2,
            "active_from": "2026-07-15T00:00:00+00:00",
        },
        "metrics": {
            "impressions": 10000, "clicks": 900, "add_to_cart": 400,
            "orders": 220, "payments": 200, "refunds": 0,
            "net_sales": 26000, "sellable_stock": 500,
            "in_transit_stock": 50, "ad_spend": 500,
        },
    },
}

_DIRECTION_DIAGNOSIS_FACTS: dict[str, dict[str, Any]] = {
    "选品方向": {"exposures": 5000, "clicks": 400, "conversions": 40},
    "上新准备": {"exposures": 0, "clicks": 0, "conversions": 0},
    "清仓风险": {"exposures": 600, "clicks": 24, "conversions": 1},
    "受控优化": {"exposures": 1000000, "clicks": 1000, "conversions": 900},
    "活动候选": {"exposures": 10000, "clicks": 900, "conversions": 200},
}

# 盲 SKU 名：方向与 SKU 名无关（防答案编码）。若 Eval 按 SKU 名映射方向，
# 重命名这里任一个盲名会让对应场景变红。
_DIRECTION_SKUS: dict[str, str] = {
    "选品方向": "sku-blind-1",
    "上新准备": "sku-blind-2",
    "清仓风险": "sku-blind-3",
    "受控优化": "sku-blind-4",
    "活动候选": "sku-blind-5",
}

DIRECTION_SCENES: list[FrozenScene] = [
    FrozenScene(
        name,
        input_data={
            "sku_id": _DIRECTION_SKUS[name],
            "evidence_state": "actual",
            "quality_gate": {"status": "passed", "issues": []},
            "freshness": {"usable_as_current": True},
            **_DIRECTION_DIAGNOSIS_FACTS[name],
            "business_facts": _DIRECTION_BUSINESS_FACTS[name],
        },
        expected=_DIRECTION_EXPECTED[name],
    )
    for name in _DIRECTION_EXPECTED
]


__all__ = [
    "DIRECTION_SCENES",
    "FROZEN_SCENES",
    "FrozenScene",
]
