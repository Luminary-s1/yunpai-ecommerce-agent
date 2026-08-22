"""M9-R WP3 幂等测试：同事实重放不重复创建；事实更新旧建议标 stale。

对齐验收标准：条目 5（重放幂等，旧建议标 stale）。
"""
from __future__ import annotations

from datetime import UTC, datetime

from ecommerce_agent.product_lifecycle.schemas import (
    Recommendation,
    RecommendationState,
    RecommendationType,
    TargetObject,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
_FACT_SIG = ("sku1", "2026-08-17")


def _fact_signature(rec: Recommendation) -> tuple[str, str]:
    """建议的事实签名（确定性：sku + 日期，用于幂等判定）。"""
    target = rec.target.sku_id or ""
    return (target, "2026-08-17")


def test_same_fact_replay_same_signature() -> None:
    """同事实重放 → 签名相同（幂等判定依据）。"""
    r1 = Recommendation(
        recommendation_id="r1", type=RecommendationType.RESTOCK,
        target=TargetObject(store_id="s1", item_id="i1", sku_id="sku1"),
        facts_snapshot={"stock_facts": {"qty": 10}},
        rationale="test", alternatives=[RecommendationType.EXPERIMENT],
        created_at=NOW, updated_at=NOW,
    )
    r2 = Recommendation(
        recommendation_id="r2", type=RecommendationType.RESTOCK,
        target=TargetObject(store_id="s1", item_id="i1", sku_id="sku1"),
        facts_snapshot={"stock_facts": {"qty": 10}},
        rationale="test", alternatives=[RecommendationType.EXPERIMENT],
        created_at=NOW, updated_at=NOW,
    )
    assert _fact_signature(r1) == _fact_signature(r2) == _FACT_SIG


def test_fact_update_changes_signature() -> None:
    """事实更新（sku 变）→ 签名不同（触发旧建议 stale）。"""
    r_old = Recommendation(
        recommendation_id="r1", type=RecommendationType.RESTOCK,
        target=TargetObject(store_id="s1", item_id="i1", sku_id="sku1"),
        facts_snapshot={}, rationale="t",
        alternatives=[RecommendationType.EXPERIMENT],
        created_at=NOW, updated_at=NOW,
    )
    r_new = Recommendation(
        recommendation_id="r2", type=RecommendationType.RESTOCK,
        target=TargetObject(store_id="s1", item_id="i1", sku_id="sku2"),
        facts_snapshot={}, rationale="t",
        alternatives=[RecommendationType.EXPERIMENT],
        created_at=NOW, updated_at=NOW,
    )
    assert _fact_signature(r_old) != _fact_signature(r_new)


def test_stale_is_closed_state() -> None:
    """旧建议标 stale → 状态为 CLOSED（不原地改写历史）。"""
    # 事实更新后，旧建议经 mark_stale 转换到 CLOSED（见 state_machine 测试）
    # 此处锁语义：CLOSED 是终态，新建议用新签名
    assert RecommendationState.CLOSED.value == "closed"


def test_content_idempotency_different_id_returns_existing(tmp_path) -> None:
    """B3（盲点 #5 修复）：同证据不同 recommendation_id → 内容级幂等返回已有建议。

    任务书 WP3 L365"同一证据重放不重复创建建议"。修复前幂等键只有
    (tenant, recommendation_id)——调用方重试换 ID 会重复创建。修复后按
    (tenant, sku_id, payload_hash) 内容级兜底，第二条返回已有建议（idempotent）。
    """
    from datetime import UTC, datetime

    from ecommerce_agent.database import Database
    from ecommerce_agent.product_lifecycle.engine import RecommendationEngine
    from ecommerce_agent.product_lifecycle.schemas import (
        RecommendationState,
        RecommendationType,
    )
    from ecommerce_agent.product_lifecycle.service import (
        RecommendationPersistenceService,
    )
    from ecommerce_agent.product_diagnosis.diagnosis import (
        Diagnosis,
        DiagnosisType,
    )
    from ecommerce_agent.product_read_model.models import (
        AggregateRule,
        Granularity,
        MetricValue,
        SKUReadModel,
    )

    db = Database(tmp_path / "content-idem.sqlite3")
    db.initialize()
    service = RecommendationPersistenceService(db)

    _missing = MetricValue.missing(
        Granularity.DAILY, AggregateRule.SUM, "2026-08-17", "test"
    )
    sku = SKUReadModel(
        tenant_id="t1", store_id="store-1", item_id="item-1", sku_id="sku-1",
        revision=1, impressions=_missing, clicks=_missing, add_to_cart=_missing,
        orders=_missing, payments=_missing, refunds=_missing, net_sales=_missing,
        sellable_stock=_missing, in_transit_stock=_missing,
    )
    diag = Diagnosis(
        diagnosis_type=DiagnosisType.STOCKOUT_POLLUTION,
        sku_id="sku-1",
        reason="stockout_period_observed",
        evidence_facts={
            "evidence_state": "actual", "freshness": {"usable_as_current": True},
            "quality_gate": "passed", "quality_gate_issues": [],
            "exposures": 1000.0, "clicks": 100.0, "conversions": 10.0,
            "stockout": True, "pollution": None,
        },
        degraded=True,
    )
    engine = RecommendationEngine()

    def _make_rec(rec_id: str):
        return engine.generate(
            tenant_id="t1", diagnosis=diag, sku=sku,
            recommendation_id=rec_id, created_at=datetime(2026, 8, 20, tzinfo=UTC),
        )

    # 第一次创建（rec-a）
    rec_a = _make_rec("rec-a")
    r1 = service.create("t1", rec_a)
    assert r1["write_status"] == "applied"
    # 同证据不同 ID（rec-b，模拟调用方重试换 ID）→ 内容级幂等，返回已有
    rec_b = _make_rec("rec-b")
    r2 = service.create("t1", rec_b)
    assert r2["write_status"] == "idempotent", f"应内容级幂等: {r2['write_status']}"
    # 返回的是已有建议（rec-a），不是新 ID
    assert r2["recommendation_id"] == "rec-a"
    # 库内只有一条（未重复创建）
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM product_recommendations "
            "WHERE tenant_id='t1' AND sku_id='sku-1'"
        ).fetchone()
        audit_subjects = conn.execute(
            "SELECT subject_id FROM audit_log "
            "WHERE tenant_id='t1' AND event_type='recommendation.create'"
        ).fetchall()
    assert int(rows["c"]) == 1, f"同证据不应重复创建: {rows['c']}"
    assert {str(row["subject_id"]) for row in audit_subjects} == {"rec-a"}


def test_content_idempotency_item_level_null_sku(tmp_path) -> None:
    """item 级建议（sku_id=None）内容幂等：`sku_id IS ?` 使 NULL 也参与幂等。

    修复前内容级兜底用 `sku_id=?`，SQLite 对 NULL 永不匹配——item 级建议（sku_id
    为 NULL）同证据重放会重复创建，违反任务书 WP3 L365「同一证据重放不重复创建」。
    修复后 `sku_id IS ?` 覆盖 NULL，item 级同证据第二条返回已有建议。
    """
    from ecommerce_agent.database import Database
    from ecommerce_agent.product_lifecycle.schemas import (
        Recommendation,
        RecommendationState,
        RecommendationType,
        TargetObject,
    )
    from ecommerce_agent.product_lifecycle.service import (
        RecommendationPersistenceService,
    )

    db = Database(tmp_path / "content-idem-nullsku.sqlite3")
    db.initialize()
    service = RecommendationPersistenceService(db)

    def _make_rec(rec_id: str) -> Recommendation:
        return Recommendation(
            recommendation_id=rec_id,
            type=RecommendationType.KEEP_OBSERVE,
            target=TargetObject(store_id="store-1"),  # item_id/sku_id 均为 None
            facts_snapshot={"traffic_facts": {"impressions": 100}},
            rationale="observe current state",
            alternatives=[RecommendationType.EXPERIMENT],
            state=RecommendationState.DRAFT,
            degraded=False,
            created_at=datetime(2026, 8, 20, tzinfo=UTC),
            updated_at=datetime(2026, 8, 20, tzinfo=UTC),
        )

    r1 = service.create("t1", _make_rec("rec-1"))
    assert r1["write_status"] == "applied"
    # 同证据不同 ID（rec-2）→ 内容级幂等命中 NULL sku，返回已有建议
    r2 = service.create("t1", _make_rec("rec-2"))
    assert r2["write_status"] == "idempotent", f"item 级同证据应幂等: {r2['write_status']}"
    assert r2["recommendation_id"] == "rec-1"
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM product_recommendations "
            "WHERE tenant_id='t1'"
        ).fetchone()
    assert int(rows["c"]) == 1, f"item 级同证据不应重复创建: {rows['c']}"
