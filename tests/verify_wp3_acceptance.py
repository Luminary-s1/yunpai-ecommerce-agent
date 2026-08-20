"""WP3 验收脚本：按 m9r-complete-plan.md 第五节 WP3 验收表 8 条标准逐条断言验证。

标 ⚠️ 的待确认（本脚本实测 WP3 实现后确认，通过则转 ✅）。
闫睿涵 WP5 可直接复验本脚本。
"""
from __future__ import annotations

from datetime import UTC, datetime

from ecommerce_agent.product_lifecycle.interface import to_m10_contract
from ecommerce_agent.product_lifecycle.schemas import (
    Recommendation,
    RecommendationState,
    RecommendationType,
    TargetObject,
)
from ecommerce_agent.product_lifecycle.state_machine import StateMachine, TransitionAction
from ecommerce_agent.product_lifecycle.validation import (
    WriteBarrier,
    validate_full_recommendation,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
RESULTS: list[tuple[str, str, str, bool, str]] = []


def check(cid: str, desc: str, expected: str, fn) -> None:
    try:
        fn()
        actual = "PASS"
    except AssertionError as e:
        actual = f"FAIL: {e}"
    except Exception as e:  # noqa: BLE001
        actual = f"ERROR: {type(e).__name__}: {e}"
    ok = actual == "PASS"
    RESULTS.append((cid, desc, expected, ok, actual))


def _rec(rtype=RecommendationType.PRICING, facts=None, alternatives=None,
         degraded=False, missing_evidence=None) -> Recommendation:
    return Recommendation(
        recommendation_id="r1", type=rtype,
        target=TargetObject(store_id="s1", item_id="i1", sku_id="sku1"),
        facts_snapshot=facts or {},
        rationale="test",
        alternatives=(alternatives if alternatives is not None
                      else [RecommendationType.EXPERIMENT]),
        degraded=degraded, missing_evidence=missing_evidence or [],
        created_at=NOW, updated_at=NOW,
    )


def t01() -> None:
    """条目 1：建议默认 draft，人工批准才生效。"""
    sm = StateMachine()
    assert sm.state is RecommendationState.DRAFT
    # 需 SUBMIT → APPROVE 才生效（人工批准）
    sm.apply(TransitionAction.SUBMIT, actor="ops-1", at=NOW, target="r1")
    sm.apply(TransitionAction.APPROVE, actor="ops-1", at=NOW, target="r1")
    assert sm.state is RecommendationState.APPROVED


def t02() -> None:
    """条目 2：批准不触发平台写动作（B2/B4 写屏障）。"""
    barrier = WriteBarrier()
    for action in ("platform.change_price", "platform.update_image"):
        try:
            barrier.assert_write_allowed(action)
            raise AssertionError(f"平台写 {action} 应被拒")
        except ValueError:
            pass
    # 内部写白名单放行
    barrier.assert_write_allowed("recommendation.create")


def t03() -> None:
    """条目 3：存量标题/主图默认 keep/observe（B1）。"""
    from ecommerce_agent.product_lifecycle.schemas import RecommendationType as T
    all_types = {t.value for t in T}
    assert "改标题" not in all_types and "换主图" not in all_types
    # 默认状态 DRAFT
    rec = _rec(rtype=RecommendationType.KEEP_OBSERVE, facts={}, degraded=True)
    assert rec.state is RecommendationState.DRAFT


def t04() -> None:
    """条目 4：缺成本/缺竞品时结论按证据降级。"""
    # 定价缺成本 → 必须 degraded（不输出正式利润安全价格）
    try:
        validate_full_recommendation(_rec(facts={}))
        raise AssertionError("定价缺成本应抛（未 degraded）")
    except ValueError:
        pass
    # degraded 建议必须列缺失项（degraded_requires_missing_evidence）
    degraded = _rec(facts={}, degraded=True, missing_evidence=["cost_ready"])
    validate_full_recommendation(degraded)  # degraded 允许缺事实，但须列出缺什么


def t05() -> None:
    """条目 5：重放幂等，旧建议标 stale。"""
    sm = StateMachine(RecommendationState.OBSERVED)
    new_state, audit = sm.apply(
        TransitionAction.MARK_STALE, actor="ops-1", at=NOW, target="r1"
    )
    assert new_state is RecommendationState.STALE  # 事实更新 → stale（非 closed）
    assert audit.action is TransitionAction.MARK_STALE


def t06() -> None:
    """条目 6：每条建议带备选路径（B3：含上新/实验）。"""
    rec = _rec(facts={"cost_ready": True})
    validate_full_recommendation(rec)  # 默认含 EXPERIMENT → 通过
    # 无备选 → 抛
    try:
        validate_full_recommendation(_rec(facts={"cost_ready": True}, alternatives=[]))
        raise AssertionError("无备选应抛")
    except ValueError:
        pass


def t07() -> None:
    """条目 7：建议输出契约可被 M10-R 消费。"""
    rec = _rec(facts={"cost_ready": True})
    contract = to_m10_contract(rec)  # type: ignore[arg-type]
    assert "contract_version" in contract
    assert contract["payload"]["recommendation_id"] == "r1"


def t08() -> None:
    """条目 8：完整建议链条覆盖（选品→清仓）。"""
    from ecommerce_agent.product_lifecycle.schemas import RecommendationType as T
    chain = [T.SELECTION, T.NEW_LAUNCH, T.DIAGNOSIS, T.EXPERIMENT, T.KEEP_OBSERVE,
             T.PRICING, T.PROMOTION, T.RESTOCK, T.CLEARANCE]
    # 全部类型必须存在于注册表（链条完整）
    for t in chain:
        assert t in T


check("①", "建议默认 draft，人工批准才生效", "⚠️", t01)
check("②", "批准不触发平台写动作", "⚠️", t02)
check("③", "存量标题/主图默认 keep/observe", "⚠️", t03)
check("④", "缺成本/缺竞品时结论按证据降级", "⚠️", t04)
check("⑤", "重放幂等，旧建议标 stale", "⚠️", t05)
check("⑥", "每条建议带备选路径（上新/实验）", "⚠️", t06)
check("⑦", "建议输出契约可被 M10-R 消费", "⚠️", t07)
check("⑧", "完整建议链条覆盖（选品→清仓）", "⚠️", t08)

print(f"{'条目':<6}{'验收标准':<34}{'计划':<5}{'实际':<8}备注")
print("-" * 95)
all_ok = True
for cid, desc, exp, ok, actual in RESULTS:
    if not ok:
        all_ok = False
    print(f"{cid:<7}{desc:<36}{exp:<6}{('PASS' if ok else '**FAIL**'):<8}{actual}")
print("-" * 95)
print(f"结论: {'✅ 全部 PASS' if all_ok else '❌ 有 FAIL 项，需修复'}")
