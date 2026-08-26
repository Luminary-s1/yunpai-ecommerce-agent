"""M9-R WP3 建议类型注册表 + 输出契约。

边界声明：
- 建议类型：严格按任务书链条（选品→上新→诊断→实验→定价候选→活动候选→补货联动→清仓预警）。
- 每条建议必须带 alternatives（B3 备选路径：上新/受控实验）。
- required_facts：每类型的前置事实依赖（缺则建议降级，不输出具体结论）。
- 输出契约 RecommendationOutput 为 M10-R 预留消费接口（第 4 周向缪海南评审）。
- 副作用：零——纯 schema，无 I/O。
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RecommendationType(StrEnum):
    SELECTION = "选品候选"
    NEW_LAUNCH = "上新准备"
    DIAGNOSIS = "曝光/点击诊断"
    EXPERIMENT = "受控实验"
    KEEP_OBSERVE = "保持观察"
    PRICING = "定价候选"
    PROMOTION = "活动候选"
    RESTOCK = "补货联动"
    CLEARANCE = "清仓预警"


class RecommendationState(StrEnum):
    DRAFT = "draft"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    OBSERVED = "observed"
    STALE = "stale"
    CLOSED = "closed"


class TargetObject(BaseModel):
    """建议目标对象（store/item/sku）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    store_id: str
    item_id: str | None = None
    sku_id: str | None = None


class Recommendation(BaseModel):
    """一条生命周期建议（带证据，可追溯）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recommendation_id: str
    type: RecommendationType
    target: TargetObject
    facts_snapshot: dict[str, Any]       # 固化事实快照（引用来源）
    rationale: str                       # 模型理由
    missing_evidence: list[str] = Field(default_factory=list)
    alternatives: list[RecommendationType] = Field(default_factory=list)  # B3
    state: RecommendationState = RecommendationState.DRAFT
    degraded: bool = False
    created_at: datetime
    updated_at: datetime


# 前置事实依赖：每类型必须的 facts_snapshot 键（缺则降级）
REQUIRED_FACTS: dict[RecommendationType, tuple[str, ...]] = {
    RecommendationType.SELECTION: ("demand_signal", "competitor_evidence"),
    RecommendationType.NEW_LAUNCH: ("item_ready", "stock_ready"),
    RecommendationType.DIAGNOSIS: ("traffic_facts",),
    RecommendationType.EXPERIMENT: ("revision_evidence",),
    RecommendationType.KEEP_OBSERVE: (),
    RecommendationType.PRICING: ("cost_ready",),       # 缺成本 → 不出正式利润安全价格
    RecommendationType.PROMOTION: ("campaign_window",),
    RecommendationType.RESTOCK: ("stock_facts",),
    RecommendationType.CLEARANCE: ("clearance_signal", "competitor_evidence"),
}


def validate_recommendation(recommendation: Recommendation) -> None:
    """构造后校验：required_facts 缺失 → 必须 degraded + 列 missing_evidence。

    失败暴露：
    - 非 degraded 建议缺必需事实 → 抛 ValueError（不静默通过）。
    - required 键值为 None/False 视为缺失（cost_ready=False 即未就绪）。
    - degraded 建议必须列出缺什么（missing_evidence 非空），对齐任务书「缺什么、影响哪个数字」。
    """
    required = REQUIRED_FACTS[recommendation.type]
    facts = recommendation.facts_snapshot
    missing = [
        key for key in required
        if key not in facts or facts.get(key) in (None, False)
    ]
    if missing:
        if not recommendation.degraded:
            raise ValueError(
                f"recommendation_missing_required_facts:{recommendation.type.value}:"
                f"{','.join(missing)}"
            )
        if not recommendation.missing_evidence:
            raise ValueError(
                f"degraded_requires_missing_evidence:{recommendation.type.value}:"
                f"{','.join(missing)}"
            )


__all__ = [
    "REQUIRED_FACTS",
    "Recommendation",
    "RecommendationState",
    "RecommendationType",
    "TargetObject",
    "validate_recommendation",
]
