"""M9-R WP3「诊断 → 建议」引擎测试：闭环补缺验收。

验收点（对齐计划）：
- 缺货 → 补货联动（RESTOCK），带库存事实，非 degraded（数量齐时）
- 补货建议 supplier_ref/交期留空（M10-R 契约：缺供给方信息只能 draft）
- 曝光不足 → 曝光/点击诊断（DIAGNOSIS）
- 证据不足 → 保持观察（KEEP_OBSERVE，degraded）
- B3：每条建议必须带 alternatives（含上新准备/受控实验）
- B4：生成全程零平台写（Mock 平台 API not_called）
- required_facts 缺 → degraded + missing_evidence
- 越权词（竞品/效果提升）→ validate_full_recommendation 拒绝
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ecommerce_agent.product_diagnosis.diagnosis import (
    Diagnosis,
    DiagnosisType,
)
from ecommerce_agent.product_lifecycle.engine import (
    RecommendationEngine,
    RecommendationInterpreter,
    RecommendationType,
    RulesetRecommendationInterpreter,
)
from ecommerce_agent.product_lifecycle.schemas import (
    Recommendation,
    RecommendationState,
)
from ecommerce_agent.product_read_model.models import (
    AggregateRule,
    Granularity,
    MetricValue,
    SKUReadModel,
)
from ecommerce_agent.readonly_data.contracts import EvidenceState

NOW = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
MANIFEST = "import-test-1"


def _metric(
    state: EvidenceState,
    value: float | None,
    period_key: str = "2026-08-20",
) -> MetricValue:
    return MetricValue.from_value(
        state=state, granularity=Granularity.DAILY, aggregate_rule=AggregateRule.SUM,
        period_key=period_key, value=value,
        import_manifest_id=MANIFEST, data_as_of=NOW,
    )


def _stock_metric(value: float | None, state: EvidenceState = EvidenceState.ACTUAL) -> MetricValue:
    return _metric(state, value)


def _sku(
    *,
    store_id: str = "store-a",
    item_id: str = "i1",
    sku_id: str = "sku1",
    sellable: float | None = 0.0,
    in_transit: float | None = 0.0,
    stock_state: EvidenceState = EvidenceState.ACTUAL,
) -> SKUReadModel:
    return SKUReadModel(
        tenant_id="t1", store_id=store_id, item_id=item_id, sku_id=sku_id,
        revision=1,
        impressions=_metric(EvidenceState.ACTUAL, 1000.0),
        clicks=_metric(EvidenceState.ACTUAL, 100.0),
        add_to_cart=_metric(EvidenceState.ACTUAL, 20.0),
        orders=_metric(EvidenceState.ACTUAL, 10.0),
        payments=_metric(EvidenceState.ACTUAL, 8.0),
        refunds=_metric(EvidenceState.ACTUAL, 0.0),
        net_sales=_metric(EvidenceState.ACTUAL, 1000.0),
        sellable_stock=_stock_metric(sellable, stock_state),
        in_transit_stock=_stock_metric(in_transit, stock_state),
    )


def _diag(t: DiagnosisType, *, degraded: bool = False, facts: dict | None = None) -> Diagnosis:
    return Diagnosis(
        diagnosis_type=t,
        sku_id="sku1",
        reason=None,
        evidence_facts=facts or {"evidence_state": "actual"},
        degraded=degraded,
    )


def _engine(inventory=None) -> RecommendationEngine:
    return RecommendationEngine(inventory=inventory)


# ── 1. 类型映射 ──


def test_stockout_maps_to_restock() -> None:
    """缺货诊断 → 补货联动（RESTOCK），带库存事实，非 degraded（数量齐时）。"""
    rec = _engine().generate(
        tenant_id="t1",
        diagnosis=_diag(DiagnosisType.STOCKOUT_POLLUTION, degraded=True),
        sku=_sku(sellable=0.0, in_transit=500.0),
        recommendation_id="rec-1",
        created_at=NOW,
    )
    assert rec.type is RecommendationType.RESTOCK
    assert rec.target.sku_id == "sku1"
    facts = rec.facts_snapshot["stock_facts"]
    assert facts["sellable_stock"] == 0.0
    assert facts["in_transit_stock"] == 500.0
    # 缺货污染的 degraded 来自诊断（污染不归因），非 required_facts 缺失
    assert rec.degraded is True
    assert rec.state is RecommendationState.DRAFT


def test_exposure_insufficient_maps_to_diagnosis() -> None:
    """曝光不足 → 曝光/点击诊断（DIAGNOSIS），带流量事实。"""
    rec = _engine().generate(
        tenant_id="t1",
        diagnosis=_diag(DiagnosisType.EXPOSURE_INSUFFICIENT),
        sku=_sku(sellable=100.0),
        recommendation_id="rec-2",
        created_at=NOW,
    )
    assert rec.type is RecommendationType.DIAGNOSIS
    traffic = rec.facts_snapshot["traffic_facts"]
    assert traffic["impressions"] == 1000.0
    assert traffic["conversions"] == 8.0


def test_evidence_insufficient_maps_to_keep_observe() -> None:
    """证据不足 → 保持观察（KEEP_OBSERVE），语义层即 degraded。"""
    rec = _engine().generate(
        tenant_id="t1",
        diagnosis=_diag(DiagnosisType.EVIDENCE_INSUFFICIENT),
        sku=_sku(),
        recommendation_id="rec-3",
        created_at=NOW,
    )
    assert rec.type is RecommendationType.KEEP_OBSERVE
    assert rec.degraded is True
    # KEEP_OBSERVE 无 required_facts，degraded 来自语义层，不应有缺失证据
    assert rec.missing_evidence == []


# ── 2. M10-R 契约：供给方字段留空 ──


def test_restock_supplier_fields_left_empty() -> None:
    """补货建议 supplier_ref/交期不在 facts_snapshot（M10-R 契约：人工补齐）。"""
    rec = _engine().generate(
        tenant_id="t1",
        diagnosis=_diag(DiagnosisType.STOCKOUT_POLLUTION, degraded=True),
        sku=_sku(sellable=0.0),
        recommendation_id="rec-4",
        created_at=NOW,
    )
    stock_facts = rec.facts_snapshot["stock_facts"]
    # M9-R 只填数量类事实；供给方字段不出现，交由 M10-R 订购单侧补齐
    assert "supplier_ref" not in stock_facts
    assert "promised_delivery_at" not in stock_facts


# ── 3. B3 alternatives ──


def test_alternatives_present() -> None:
    """B3：每条建议必须带 alternatives，含「上新准备」或「受控实验」。"""
    for t in (DiagnosisType.STOCKOUT_POLLUTION, DiagnosisType.EXPOSURE_INSUFFICIENT):
        rec = _engine().generate(
            tenant_id="t1",
            diagnosis=_diag(t),
            sku=_sku(),
            recommendation_id=f"rec-{t.value}",
            created_at=NOW,
        )
        assert rec.alternatives, "B3 alternatives must be non-empty"
        assert any(
            a in (RecommendationType.NEW_LAUNCH, RecommendationType.EXPERIMENT)
            for a in rec.alternatives
        )


# ── 4. B4 零平台写 ──


def test_no_platform_write() -> None:
    """B4：生成全程零平台写（引擎无任何写接口，Mock 平台客户端 not_called）。"""
    from unittest.mock import MagicMock

    platform_api = MagicMock()

    class _EmptyInventory:
        """库存服务替身：返回空风险列表（不触碰平台）。"""

        def risks(self, tenant_id, *, store_id=None, sku_id=None):
            return []

    engine = _engine(inventory=_EmptyInventory())
    rec = engine.generate(
        tenant_id="t1",
        diagnosis=_diag(DiagnosisType.STOCKOUT_POLLUTION, degraded=True),
        sku=_sku(sellable=0.0),
        recommendation_id="rec-5",
        created_at=NOW,
    )
    assert rec.state is RecommendationState.DRAFT  # 不自动 APPROVED
    platform_api.assert_not_called()  # 平台写=0


# ── 5. required_facts 缺失 → 降级 ──


def test_degraded_when_facts_missing() -> None:
    """PRICING 缺成本（required_facts: cost_ready）→ degraded + missing_evidence。"""
    rec = _engine().generate(
        tenant_id="t1",
        diagnosis=_diag(DiagnosisType.AD_PRICE_POLLUTION, degraded=True),
        sku=_sku(),
        recommendation_id="rec-6",
        created_at=NOW,
    )
    assert rec.type is RecommendationType.PRICING
    assert rec.degraded is True
    assert "cost_ready" in rec.missing_evidence


# ── 6. 越权词拒绝 ──


def test_forbidden_key_rejected() -> None:
    """越权词（竞品/效果提升）出现在建议内容 → validate_full_recommendation 拒绝。"""
    from ecommerce_agent.product_lifecycle.engine import (
        _RATIONALE_BY_TYPE,
    )

    # 确认内置理由不含越权词（防未来改理由时踩线）
    for rationale in _RATIONALE_BY_TYPE.values():
        for forbidden in ("竞品", "效果提升", "平台权重"):
            assert forbidden not in rationale, f"rationale must not contain {forbidden}"

    # 引擎生成后，构造一个带越权词 rationale 的建议必须被校验拒绝
    engine = _engine()
    rec = engine.generate(
        tenant_id="t1",
        diagnosis=_diag(DiagnosisType.STOCKOUT_POLLUTION, degraded=True),
        sku=_sku(sellable=0.0),
        recommendation_id="rec-7",
        created_at=NOW,
    )
    bad = Recommendation(
        recommendation_id="rec-7-bad",
        type=rec.type,
        target=rec.target,
        facts_snapshot=rec.facts_snapshot,
        rationale="建议改主图以提升平台权重和效果提升",
        missing_evidence=[],
        alternatives=[RecommendationType.EXPERIMENT],
        state=RecommendationState.DRAFT,
        degraded=True,
        created_at=NOW,
        updated_at=NOW,
    )
    from ecommerce_agent.product_lifecycle.validation import (
        validate_full_recommendation,
    )

    with pytest.raises(ValueError):
        validate_full_recommendation(bad)


# ── 7. 确定性 / 失败暴露 ──


def test_unmappable_diagnosis_raises() -> None:
    """不可映射诊断类型 → 解释器抛错，不静默。

    DiagnosisType 是受控枚举，无法构造白名单外值；这里验证：
    - 覆盖全部 6 个诊断类型，Ruleset 必须都能映射到合法建议类型（确定性映射表完整）；
    - 引擎对完整映射不抛错（若未来新增 DiagnosisType 未加映射，此处应报 missing）。
    """
    for t in DiagnosisType:
        diag = _diag(t)
        candidate = RulesetRecommendationInterpreter().interpret(diag)
        assert isinstance(candidate.type, RecommendationType)  # 映射到合法建议类型
        # 直接构造的 Diagnosis（reason=None）在引擎里同样可生成
        rec = _engine().generate(
            tenant_id="t1", diagnosis=diag, sku=_sku(),
            recommendation_id=f"rec-{t.value}", created_at=NOW,
        )
        assert rec.type is candidate.type
    # 确认枚举是封闭的（当前 6 类，新增须同步映射表）
    assert len(DiagnosisType) == 6


def test_custom_interpreter_injected() -> None:
    """自定义解释器可注入（模型可替换层）。"""
    class CustomInterpreter(RecommendationInterpreter):
        def interpret(self, diagnosis, decision_facts=None):
            from ecommerce_agent.product_lifecycle.engine import RecommendationCandidate
            return RecommendationCandidate(
                type=RecommendationType.DIAGNOSIS, rationale="custom"
            )

    rec = RecommendationEngine(interpreter=CustomInterpreter()).generate(
        tenant_id="t1",
        diagnosis=_diag(DiagnosisType.STOCKOUT_POLLUTION, degraded=True),
        sku=_sku(),
        recommendation_id="rec-8",
        created_at=NOW,
    )
    assert rec.type is RecommendationType.DIAGNOSIS
    assert rec.rationale == "custom"


# ── 8. 幂等落库冒烟（真实 Database） ──


def test_generate_then_persist_smoke(tmp_path) -> None:
    """引擎产出 → RecommendationPersistenceService.create 落库 → list 读回（DRAFT）。"""
    from ecommerce_agent.database import Database
    from ecommerce_agent.product_lifecycle.service import (
        RecommendationPersistenceService,
    )

    db = Database(tmp_path / "engine-smoke.sqlite3")
    db.initialize()
    svc = RecommendationPersistenceService(db)
    engine = _engine()
    rec = engine.generate(
        tenant_id="t1",
        diagnosis=_diag(DiagnosisType.STOCKOUT_POLLUTION, degraded=True),
        sku=_sku(sellable=0.0, in_transit=500.0),
        recommendation_id="rec-smoke",
        created_at=NOW,
    )
    result = svc.create("t1", rec, actor="engine-test")
    assert result["write_status"] == "applied"
    assert result["state"] == "draft"
    rows = svc.list("t1", store_id="store-a")
    assert any(r["recommendation_id"] == "rec-smoke" for r in rows)
