"""M9-R WP3 建议输出契约（M10-R 消费侧，缪海南 2026-08-18 评审确认）。

对齐 M10-R 评审意见（缪海南确认）：
1. type 用受控枚举（restock/clearance/pricing），不是自由 str；
2. state 只消费 approved，draft 不进订购单/决策台；
3. facts_snapshot 需可追溯来源（source_id/data_as_of/payload_hash），按 D-014 落证据；
4. target 保持 store/item/sku，material_no 由 M7-R WP3 映射，M10-R 订购单从料号侧对齐；
5. restock 至少含 recommended_qty/supplier_ref/promised_delivery_at/missing_fields；
   缺料号/交期时 M10-R 只出 draft、不自动 confirmed。

契约版本：V0 已确认 → 冻结为 v1（缪海南评审通过后升版）。
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .schemas import RecommendationState, RecommendationType


class M10RecommendationType(StrEnum):
    """M10-R 消费的受控枚举（对齐评审点 1）。"""

    RESTOCK = "restock"
    CLEARANCE = "clearance"
    PRICING = "pricing"


class FactSnapshot(BaseModel):
    """可追溯事实快照（对齐评审点 3，D-014 落证据）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str                              # 证据来源（import manifest / analysis run）
    data_as_of: datetime                        # 数据时间
    payload_hash: str = Field(min_length=64, max_length=64)  # 内容摘要（确定性溯源）


class RestockPayload(BaseModel):
    """补货联动专属字段（对齐评审点 5）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recommended_qty: int = Field(ge=0)
    supplier_ref: str | None = None             # 供应商引用（缺则 M10-R 只出 draft）
    promised_delivery_at: datetime | None = None  # 承诺交期（缺则 M10-R 只出 draft）
    missing_fields: list[str] = Field(default_factory=list)  # 缺料号/交期等


class RecommendationOutput(BaseModel):
    """M10-R 消费侧建议输出契约（缪海南确认，冻结 v1）。"""

    model_config = ConfigDict(extra="forbid")

    recommendation_id: str
    type: M10RecommendationType                 # 受控枚举（评审点 1）
    target: dict[str, str]                      # {"store_id":..., "item_id":..., "sku_id":...}（评审点 4）
    facts_snapshot: FactSnapshot                # 可追溯来源（评审点 3）
    rationale: str
    missing_evidence: list[str] = Field(default_factory=list)
    alternatives: list[RecommendationType] = Field(default_factory=list)  # B3
    state: RecommendationState = RecommendationState.DRAFT
    degraded: bool = False
    # 补货/清仓/定价专属字段（M10-R 消费）
    restock: RestockPayload | None = None
    clearance: dict[str, Any] | None = None
    pricing: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _validate_m10_rules(self) -> "RecommendationOutput":
        """确定性规则（对齐评审点 2/5）：
        - restock 缺 promised_delivery_at/supplier_ref 时，state 不得是 approved
          （M10-R 只出 draft、不自动 confirmed）。
        - state 非 approved 时明确：draft 不进订购单（由 M10-R 侧消费判断）。
        """
        if self.restock is not None:
            missing = []
            if not self.restock.supplier_ref:
                missing.append("supplier_ref")
            if self.restock.promised_delivery_at is None:
                missing.append("promised_delivery_at")
            if missing:
                # 缺料号/交期 → 只出 draft，不得 approved（评审点 5）
                if self.state is RecommendationState.APPROVED:
                    raise ValueError(
                        "restock_incomplete_cannot_approve:"
                        f"{','.join(missing)}"
                    )
                self.restock = RestockPayload(
                    recommended_qty=self.restock.recommended_qty,
                    supplier_ref=self.restock.supplier_ref,
                    promised_delivery_at=self.restock.promised_delivery_at,
                    missing_fields=sorted(set(self.restock.missing_fields) | set(missing)),
                )
        return self


# 契约版本：V0 缪海南确认 → 冻结 v1
M10_CONTRACT_VERSION: Literal["m10-recommendation-v1"] = "m10-recommendation-v1"


def m10_type_from_recommendation(
    rtype: RecommendationType,
) -> M10RecommendationType | None:
    """RecommendationType → M10 受控枚举；非补货/清仓/定价 → None（显式，不静默）。"""
    mapping = {
        RecommendationType.RESTOCK: M10RecommendationType.RESTOCK,
        RecommendationType.CLEARANCE: M10RecommendationType.CLEARANCE,
        RecommendationType.PRICING: M10RecommendationType.PRICING,
    }
    return mapping.get(rtype)


def to_m10_contract(recommendation: RecommendationOutput) -> dict[str, Any]:
    """包装成 M10-R 可消费的契约结构（含版本号，确定性）。"""
    return {
        "contract_version": M10_CONTRACT_VERSION,
        "payload": recommendation.model_dump(),
    }


__all__ = [
    "M10_CONTRACT_VERSION",
    "FactSnapshot",
    "M10RecommendationType",
    "RecommendationOutput",
    "RestockPayload",
    "m10_type_from_recommendation",
    "to_m10_contract",
]
