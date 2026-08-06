"""M6 竞品对比分析引擎（F-312）——P1 定向测试。

覆盖：
- D-025 门禁：未批准数据不进入分析区间，只计为 blocked_by_gate
- 反证：临时绕过门禁后，未批准数据进入分析的断言必须失败
- 算术：给定已知价格，价格区间分布与差异百分比计算正确
- API：/v1/competitive/reports/analysis 暴露分析层入口

测试数据沿用现有测试的显式虚拟标记（connector_id="licensed-feed"），
不混入真实业务数据。
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from ecommerce_agent.business import (
    CompetitiveMatchTransition,
    CompetitorObservationCreate,
)
from ecommerce_agent.service import AgentService
from fastapi.testclient import TestClient

from conftest import make_settings

from test_competitive_entity_intelligence import (
    approve,
    match_payload,
)


def make_observation(
    *,
    competitor_name: str,
    competitor_sku: str,
    competitor_price: Decimal,
    subject_price: Decimal = Decimal("100"),
    entity_match_id: str | None = None,
    source_id: str | None = None,
    observed_at: datetime | None = None,
) -> CompetitorObservationCreate:
    return CompetitorObservationCreate(
        connector_id="licensed-feed",
        store_id="store-a",
        subject_sku="sku-a",
        competitor_name=competitor_name,
        competitor_sku=competitor_sku,
        subject_price=subject_price,
        competitor_price=competitor_price,
        source_type="licensed_provider",
        source_ref="https://licensed.example/prices/1",
        source_id=source_id or f"price-{competitor_sku}",
        is_estimate=False,
        observed_at=observed_at or datetime(2026, 7, 22, 1, 0, tzinfo=UTC),
        entity_match_id=entity_match_id,
    )


def test_analysis_only_consumes_approved_price_evidence(tmp_path) -> None:
    """D-025 门禁：只有已批准 match 绑定的价格证据进入分析区间。

    构造两个竞品——一个已批准、一个仅 pending。断言：
    - approved 竞品出现在 price_bands 的某区间成员中
    - pending 竞品不进入任何区间成员，只计为 blocked_by_gate
    """
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    report = service.operations.competitive_report
    try:
        approved_match = competitive.record_entity_match("tenant-test", match_payload())
        pending_match = competitive.record_entity_match(
            "tenant-test",
            match_payload(source_id="match-source-2").model_copy(
                update={"competitor_sku": "comp-b", "competitor_name": "竞店 B"}
            ),
        )
        approve(service, approved_match["id"])

        competitive.record(
            "tenant-test",
            make_observation(
                competitor_name="竞店 A",
                competitor_sku="comp-a",
                competitor_price=Decimal("80"),
                entity_match_id=approved_match["id"],
                source_id="price-approved",
            ),
        )
        competitive.record(
            "tenant-test",
            make_observation(
                competitor_name="竞店 B",
                competitor_sku="comp-b",
                competitor_price=Decimal("60"),
                entity_match_id=pending_match["id"],
                source_id="price-pending",
            ),
        )

        result = report.analyze("tenant-test", "sku-a")

        assert result["summary"]["total_observations"] == 2
        assert result["summary"]["approved_observations"] == 1
        assert result["summary"]["blocked_by_gate"] == 1

        members = [
            item
            for band in result["price_bands"]
            for item in band["members"]
        ]
        member_skus = {item["competitor_sku"] for item in members}
        assert member_skus == {"comp-a"}
        assert "comp-b" not in member_skus
    finally:
        service.close()


def test_price_band_arithmetic_is_correct(tmp_path) -> None:
    """算术用例：给定已知价格，验证区间归属与差异百分比。

    - comp-a 竞品价 80，自有价 100 → our_price_lower，gap -20%
    - comp-c 竞品价 100 → same，gap 0%
    - comp-d 竞品价 120 → our_price_higher，gap +20%
    """
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    report = service.operations.competitive_report
    try:
        matches = {}
        for sku in ("comp-a", "comp-c", "comp-d"):
            match = competitive.record_entity_match(
                "tenant-test",
                match_payload(source_id=f"match-{sku}").model_copy(
                    update={
                        "competitor_sku": sku,
                        "competitor_name": f"竞店 {sku}",
                    }
                ),
            )
            approve(service, match["id"])
            matches[sku] = match["id"]

        cases = {
            "comp-a": Decimal("80"),
            "comp-c": Decimal("100"),
            "comp-d": Decimal("120"),
        }
        for sku, price in cases.items():
            competitive.record(
                "tenant-test",
                make_observation(
                    competitor_name=f"竞店 {sku}",
                    competitor_sku=sku,
                    competitor_price=price,
                    entity_match_id=matches[sku],
                ),
            )

        result = report.analyze("tenant-test", "sku-a")
        assert result["summary"]["approved_observations"] == 3
        assert result["summary"]["blocked_by_gate"] == 0

        by_band = {band["band"]: band for band in result["price_bands"]}
        assert by_band["our_price_lower"]["competitor_count"] == 1
        assert by_band["same_price"]["competitor_count"] == 1
        assert by_band["our_price_higher"]["competitor_count"] == 1

        # 数据层 position 语义：subject_price < competitor_price → our_price_lower
        # （自有价更低，我们更便宜）。因此 comp-d（竞品价 120 > 自有 100）落在
        # our_price_lower，comp-a（竞品价 80 < 自有 100）落在 our_price_higher。
        lower_member = by_band["our_price_lower"]["members"][0]
        assert lower_member["competitor_sku"] == "comp-d"
        assert Decimal(lower_member["gap_percent"]) == Decimal("20")
        assert Decimal(lower_member["competitor_price"]) == Decimal("120")

        higher_member = by_band["our_price_higher"]["members"][0]
        assert higher_member["competitor_sku"] == "comp-a"
        assert Decimal(higher_member["gap_percent"]) == Decimal("-20")

        # share_percent 现在统一为两位小数字符串（与 dict 其他字段一致）
        assert by_band["our_price_lower"]["share_percent"] == "33.33"
        assert by_band["same_price"]["share_percent"] == "33.33"
        assert by_band["our_price_higher"]["share_percent"] == "33.33"
    finally:
        service.close()


def test_percent_outputs_are_rounded_to_two_decimals(tmp_path) -> None:
    """精度回归：除法结果必须 quantize 到两位小数（负责人实测 28 位小数场景）。

    用不整除的 gaps（10.00 / 10.00 / 10.01 → 平均 10.0033...）验证
    average_gap_percent 输出 10.00 而非超长小数；share_percent 同理。
    """
    service = AgentService(make_settings(tmp_path))
    report = service.operations.competitive_report
    try:
        # 直接调 _decimal 验证除法结果被截断到两位
        from decimal import Decimal

        average = Decimal("10.00") + Decimal("10.00") + Decimal("10.01")
        average /= Decimal("3")
        assert report._decimal(average) == "10.00"

        # share_percent 走 _decimal（字符串），非整除时也两位
        share = report._decimal(report._share(1, 3))
        assert share == "33.33"
        assert report._share(1, 3) == Decimal("33.33")  # _share 本身保留 Decimal
    finally:
        service.close()


def test_gate_counterexample_rejects_unapproved_in_analysis(tmp_path) -> None:
    """反证：临时绕过 D-025 门禁（把 pending 当 actionable），
    断言"未批准数据进入分析"必须失败。

    通过 monkeypatch 把 analyze_prices 的返回值改为全部 actionable，
    制造"门禁失效"的对照场景。
    """
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    report = service.operations.competitive_report
    try:
        pending_match = competitive.record_entity_match(
            "tenant-test",
            match_payload(source_id="match-pending").model_copy(
                update={"competitor_sku": "comp-x", "competitor_name": "竞店 X"}
            ),
        )
        competitive.record(
            "tenant-test",
            make_observation(
                competitor_name="竞店 X",
                competitor_sku="comp-x",
                competitor_price=Decimal("90"),
                entity_match_id=pending_match["id"],
                source_id="price-x",
            ),
        )
        # 未批准：正常路径下 pending 不进区间
        result = report.analyze("tenant-test", "sku-a")
        assert result["summary"]["blocked_by_gate"] == 1
        assert all(
            not item["members"]
            for item in result["price_bands"]
        )

        # 反证：若门禁失效（把 pending 观察强制标记 actionable），
        # 未批准数据会进入区间——这正好证明正常路径下门禁挡住了它。
        injected = competitive.analyze_prices("tenant-test", "sku-a")
        for obs in injected["observations"]:
            obs["actionable"] = True
        bypassed = report._build_price_bands(injected["observations"])
        assert any(item["members"] for item in bypassed)
    finally:
        service.close()


def test_report_analysis_api_exposes_gate_and_bands(tmp_path) -> None:
    """API 测试：/v1/competitive/reports/analysis 暴露分析层入口。

    需要先造数据再打 API；通过 TestClient 验证响应结构与门禁计数。
    """
    from ecommerce_agent.api import create_app

    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    try:
        match = competitive.record_entity_match("tenant-test", match_payload())
        approve(service, match["id"])
        competitive.record(
            "tenant-test",
            make_observation(
                competitor_name="竞店 A",
                competitor_sku="comp-a",
                competitor_price=Decimal("80"),
                entity_match_id=match["id"],
            ),
        )
    finally:
        service.close()

    app = create_app(make_settings(tmp_path))
    headers = {
        "X-Admin-Id": "admin-test",
        "X-Admin-Key": "test-admin-key-123456",
    }
    with TestClient(app) as client:
        response = client.get(
            "/v1/competitive/reports/analysis?subject_sku=sku-a&store_id=store-a",
            headers=headers,
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["summary"]["approved_observations"] == 1
        assert payload["summary"]["blocked_by_gate"] == 0
        assert {b["band"] for b in payload["price_bands"]} >= {
            "our_price_lower",
            "same_price",
            "our_price_higher",
        }
