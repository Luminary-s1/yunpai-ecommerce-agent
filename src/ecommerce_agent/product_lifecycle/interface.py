"""M9-R WP3 建议输出契约（M10-R 消费侧预留）。

边界声明：
- 本模块定义 M10-R 可消费的建议输出结构（补货/清仓/定价建议的上游输入）。
- 契约字段需向缪海南评审冻结（第 4 周发起，第 5 周冻结）。
- 副作用：零——纯 schema。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .schemas import RecommendationState, RecommendationType


class RecommendationOutput(BaseModel):
    """M10-R 消费侧建议输出契约（预留字段，待缪海南评审冻结）。

    对齐任务书：M9-R 的补货/清仓/定价建议是 M10-R 销量预测与订购单的上游输入，
    输出格式需为 M10-R 预留契约接口。
    """

    model_config = ConfigDict(extra="forbid")

    recommendation_id: str
    type: RecommendationType
    target: dict[str, str]                # {"store_id":..., "item_id":..., "sku_id":...}
    facts_snapshot: dict[str, Any]        # 事实快照（引用来源）
    rationale: str                        # 模型理由
    missing_evidence: list[str] = Field(default_factory=list)
    alternatives: list[RecommendationType] = Field(default_factory=list)  # B3
    state: RecommendationState = RecommendationState.DRAFT
    degraded: bool = False
    # 补货/清仓/定价建议的 M10-R 消费字段（预留）
    restock: dict[str, Any] | None = None        # 补货联动（M10-R 订购单上游）
    clearance: dict[str, Any] | None = None      # 清仓预警
    pricing: dict[str, Any] | None = None        # 定价候选
    created_at: datetime
    updated_at: datetime


# M10-R 契约版本（冻结后版本号固定，缪海南消费侧对齐）
M10_CONTRACT_VERSION: Literal["m10-recommendation-v0"] = "m10-recommendation-v0"


def to_m10_contract(recommendation: RecommendationOutput) -> dict[str, Any]:
    """包装成 M10-R 可消费的契约结构（含版本号，确定性）。"""
    return {
        "contract_version": M10_CONTRACT_VERSION,
        "payload": recommendation.model_dump(),
    }


__all__ = [
    "M10_CONTRACT_VERSION",
    "RecommendationOutput",
    "to_m10_contract",
]
