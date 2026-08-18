"""M9-R WP3 M10-R 契约测试：缪海南 2026-08-18 评审 5 点对齐意见逐条验证。

对齐点：
1. type 用受控枚举（restock/clearance/pricing），不是自由 str；
2. state 只消费 approved，draft 不进订购单/决策台；
3. facts_snapshot 需可追溯来源（source_id/data_as_of/payload_hash），按 D-014 落证据；
4. target 保持 store/item/sku，material_no 由 M7-R WP3 映射；
5. restock 至少含 recommended_qty/supplier_ref/promised_delivery_at/missing_fields；
   缺料号/交期时 M10-R 只出 draft、不自动 confirmed。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ecommerce_agent.product_lifecycle.interface import (
    M10_CONTRACT_VERSION,
    FactSnapshot,
    M10RecommendationType,
    RecommendationOutput,
    RestockPayload,
    m10_type_from_recommendation,
    to_m10_contract,
)
from ecommerce_agent.product_lifecycle.schemas import (
    RecommendationState,
    RecommendationType,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
DIGEST = "a" * 64


def _snapshot() -> FactSnapshot:
    return FactSnapshot(source_id="analysis-run-1", data_as_of=NOW, payload_hash=DIGEST)


def _output(**overrides) -> RecommendationOutput:
    base = dict(
        recommendation_id="r1",
        type=M10RecommendationType.RESTOCK,
        target={"store_id": "s1", "item_id": "i1", "sku_id": "sku1"},
        facts_snapshot=_snapshot(),
        rationale="test",
        alternatives=[RecommendationType.EXPERIMENT],
        state=RecommendationState.DRAFT,
        restock=RestockPayload(
            recommended_qty=10, supplier_ref="sup-1",
            promised_delivery_at=NOW, missing_fields=[],
        ),
        created_at=NOW, updated_at=NOW,
    )
    base.update(overrides)
    return RecommendationOutput(**base)


def test_type_is_controlled_enum() -> None:
    """评审点 1：type 用受控枚举，不是自由 str。"""
    # 非法 str → 构造即抛
    with pytest.raises(Exception):
        RecommendationOutput(**{
            "recommendation_id": "r1", "type": "自由字符串",
            "target": {"store_id": "s1"}, "facts_snapshot": _snapshot(),
            "rationale": "t", "state": RecommendationState.DRAFT,
            "created_at": NOW, "updated_at": NOW,
        })
    assert M10RecommendationType.RESTOCK.value == "restock"


def test_state_only_approved_consumed() -> None:
    """评审点 2：state 受控枚举，draft 明确（不进订购单由 M10-R 判断）。"""
    output = _output(state=RecommendationState.DRAFT)
    assert output.state is RecommendationState.DRAFT
    # 契约显式保留 state，供 M10-R 判断「只消费 approved」
    assert "state" in to_m10_contract(output)["payload"]


def test_facts_snapshot_traceable() -> None:
    """评审点 3：facts_snapshot 含 source_id/data_as_of/payload_hash（D-014）。"""
    output = _output()
    assert output.facts_snapshot.source_id == "analysis-run-1"
    assert output.facts_snapshot.payload_hash == DIGEST
    assert output.facts_snapshot.data_as_of == NOW


def test_target_keeps_store_item_sku() -> None:
    """评审点 4：target 保持 store/item/sku。"""
    output = _output()
    assert output.target == {"store_id": "s1", "item_id": "i1", "sku_id": "sku1"}
    assert "material_no" not in output.target  # material_no 由 M7-R WP3 映射


def test_restock_complete_allows_approve() -> None:
    """评审点 5：restock 完整（有料号/交期）→ 可 approved。"""
    output = _output(state=RecommendationState.APPROVED)
    assert output.state is RecommendationState.APPROVED


def test_restock_missing_delivery_forces_draft() -> None:
    """评审点 5：restock 缺 promised_delivery_at → 不得 approved（只出 draft）。"""
    # 缺交期且试图 approved → 构造即抛
    with pytest.raises(Exception, match="restock_incomplete_cannot_approve"):
        RecommendationOutput(**{
            "recommendation_id": "r2", "type": M10RecommendationType.RESTOCK,
            "target": {"store_id": "s1", "item_id": "i1", "sku_id": "sku1"},
            "facts_snapshot": _snapshot(), "rationale": "t",
            "state": RecommendationState.APPROVED,
            "restock": RestockPayload(recommended_qty=10, missing_fields=[]),
            "created_at": NOW, "updated_at": NOW,
        })


def test_m10_type_mapping_explicit() -> None:
    """RecommendationType → M10 枚举；非补货/清仓/定价 → None（不静默）。"""
    assert m10_type_from_recommendation(RecommendationType.RESTOCK) is M10RecommendationType.RESTOCK
    assert m10_type_from_recommendation(RecommendationType.PRICING) is M10RecommendationType.PRICING
    assert m10_type_from_recommendation(RecommendationType.DIAGNOSIS) is None


def test_contract_version_frozen_v1() -> None:
    """契约版本冻结为 v1（缪海南确认后升版）。"""
    assert M10_CONTRACT_VERSION == "m10-recommendation-v1"
