from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.business import (
    CompetitiveEntityMatchCreate,
    CompetitiveMatchTransition,
    CompetitiveMonitorUpsert,
    CompetitiveProductIdentity,
    CompetitiveSignalCreate,
    CompetitorObservationCreate,
)
from ecommerce_agent.service import AgentService
from ecommerce_agent.tools import ToolExecutionContext

from conftest import make_settings


def identity(
    title: str,
    *,
    gtin: str | None = "06912345678901",
    brand: str = "云湃",
    model: str = "YP-100",
    color: str = "曜石黑",
) -> CompetitiveProductIdentity:
    return CompetitiveProductIdentity(
        title=title,
        brand=brand,
        model=model,
        category="智能客服一体机",
        gtin=gtin,
        attributes={"颜色": color, "内存": "32GB"},
    )


def match_payload(
    *,
    source_id: str = "match-source-1",
    competitor_gtin: str = "06912345678901",
    competitor_color: str = "曜石黑",
) -> CompetitiveEntityMatchCreate:
    return CompetitiveEntityMatchCreate(
        connector_id="licensed-feed",
        store_id="store-a",
        subject_sku="sku-a",
        competitor_name="竞店 A",
        competitor_sku="comp-a",
        subject_identity=identity("云湃智能客服一体机 YP-100 32GB 曜石黑"),
        competitor_identity=identity(
            "云湃 YP-100 智能客服一体机 32GB 黑色",
            gtin=competitor_gtin,
            color=competitor_color,
        ),
        comparison_keys=["颜色", "内存"],
        source_type="licensed_provider",
        source_ref="https://licensed.example/matches/1",
        source_id=source_id,
        is_estimate=False,
        observed_at=datetime(2026, 7, 22, 1, 0, tzinfo=UTC),
    )


def approve(service: AgentService, match_id: str, version: int = 1) -> dict:
    return service.operations.competitive.transition_entity_match(
        "tenant-test",
        match_id,
        CompetitiveMatchTransition(
            target_status="approved",
            expected_record_version=version,
            note="核对 GTIN、型号和关键规格一致",
        ),
        actor="reviewer-a",
    )


def test_match_assessment_is_explainable_idempotent_and_versioned(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    try:
        created = competitive.record_entity_match("tenant-test", match_payload())
        assert created["status"] == "pending"
        assert created["recommended_status"] == "approved"
        assert created["score"] >= 70
        assert {item["field"] for item in created["matched_fields"]} >= {
            "gtin",
            "brand",
            "model",
            "attributes.颜色",
            "attributes.内存",
        }
        assert created["conflicts"] == []

        repeated = competitive.record_entity_match("tenant-test", match_payload())
        assert repeated["id"] == created["id"]
        assert repeated["write_status"] == "idempotent"

        changed_source = match_payload().model_copy(
            update={"competitor_name": "被篡改的竞店"}
        )
        with pytest.raises(ValueError, match="competitive_match_version_conflict"):
            competitive.record_entity_match("tenant-test", changed_source)
        with pytest.raises(ValueError, match="competitive_match_version_conflict"):
            approve(service, created["id"], version=99)

        approved = approve(service, created["id"])
        assert approved["status"] == "approved"
        assert approved["record_version"] == 2
        rejected = competitive.transition_entity_match(
            "tenant-test",
            created["id"],
            CompetitiveMatchTransition(
                target_status="rejected",
                expected_record_version=2,
                note="复核发现活动赠品口径不同，撤销匹配",
            ),
            actor="reviewer-b",
        )
        assert rejected["status"] == "rejected"
        detail = competitive.get_entity_match("tenant-test", created["id"])
        assert [item["to_status"] for item in detail["decisions"]] == [
            "rejected",
            "approved",
        ]
        assert detail["decisions"][0]["match_record_version"] == 3
        with pytest.raises(ValueError, match="competitive_match_not_found"):
            competitive.get_entity_match("tenant-other", created["id"])
    finally:
        service.close()


def test_conflicting_identity_cannot_be_approved(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    try:
        created = competitive.record_entity_match(
            "tenant-test",
            match_payload(
                source_id="match-conflict",
                competitor_gtin="06999999999999",
                competitor_color="珍珠白",
            ),
        )
        assert created["recommended_status"] == "rejected"
        assert {item["field"] for item in created["conflicts"]} >= {
            "gtin",
            "attributes.颜色",
        }
        with pytest.raises(ValueError, match="competitive_match_not_approvable"):
            approve(service, created["id"])
        rejected = competitive.transition_entity_match(
            "tenant-test",
            created["id"],
            CompetitiveMatchTransition(
                target_status="rejected",
                expected_record_version=1,
                note="GTIN 与颜色关键属性冲突，判定非同款",
            ),
            actor="reviewer-a",
        )
        assert rejected["status"] == "rejected"
    finally:
        service.close()


def test_review_signals_are_aggregate_redacted_and_match_gated(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    try:
        match = competitive.record_entity_match("tenant-test", match_payload())
        approve(service, match["id"])
        subject_signal = CompetitiveSignalCreate(
            match_id=match["id"],
            connector_id="licensed-review-feed",
            entity_role="subject",
            signal_type="review_summary",
            aspect="响应速度",
            summary="样本显示响应快；数据联系人 13800138000 已脱敏",
            sample_size=100,
            positive_count=82,
            negative_count=8,
            source_type="licensed_provider",
            source_ref="https://licensed.example/reviews/subject",
            source_id="review-subject-1",
            is_estimate=False,
            observed_at=datetime(2026, 7, 22, 2, 0, tzinfo=UTC),
        )
        saved = competitive.record_signal("tenant-test", subject_signal)
        assert saved["summary"] == "样本显示响应快；数据联系人 138****8000 已脱敏"
        assert saved["redacted"] is True
        assert saved["eligible"] is True
        assert competitive.record_signal("tenant-test", subject_signal)[
            "write_status"
        ] == "idempotent"
        with pytest.raises(ValueError, match="competitive_signal_version_conflict"):
            competitive.record_signal(
                "tenant-test",
                subject_signal.model_copy(update={"summary": "同源标识但内容变化"}),
            )

        competitor_signal = subject_signal.model_copy(
            update={
                "entity_role": "competitor",
                "summary": "竞品响应速度正向反馈更多",
                "sample_size": 120,
                "positive_count": 108,
                "negative_count": 4,
                "source_ref": "https://licensed.example/reviews/competitor",
                "source_id": "review-competitor-1",
                "observed_at": datetime(2026, 7, 22, 2, 5, tzinfo=UTC),
            }
        )
        competitive.record_signal("tenant-test", competitor_signal)
        analysis = competitive.analyze_prices("tenant-test", "sku-a")
        comparison = analysis["content_review_insights"]["review_comparisons"][0]
        assert comparison["comparison"] == "competitor_advantage"
        assert comparison["positive_rate_delta"] == "8.00"
        assert len(analysis["signals"]) == 2
        context = ToolExecutionContext(
            tenant_id="tenant-test",
            client_id="client-test",
            session_id="session-test",
            trace_id="trace-competitive-intelligence",
            trusted_context={},
        )
        spec, arguments = service.tools.validate_selection(
            name="get_competitive_intelligence",
            arguments={"subject_sku": "sku-a", "store_id": "store-a"},
            requested_mode="observe",
            context=context,
        )
        tool_result = service.tools.execute(
            spec=spec, arguments=arguments, context=context
        )
        assert tool_result.status == "success"
        assert len(tool_result.output["signals"]) == 2
        assert tool_result.output["quality_gate"]["approved_match_required"] is True

        rejected = competitive.transition_entity_match(
            "tenant-test",
            match["id"],
            CompetitiveMatchTransition(
                target_status="rejected",
                expected_record_version=2,
                note="复核后撤销同款关系，相关信号停止参与分析",
            ),
            actor="reviewer-a",
        )
        assert rejected["status"] == "rejected"
        assert competitive.list_signals("tenant-test", eligible_only=True) == []
        assert len(competitive.list_signals("tenant-test")) == 2
    finally:
        service.close()


def test_review_signal_schema_rejects_raw_or_inconsistent_counts() -> None:
    base = {
        "match_id": "match-a",
        "connector_id": "feed-a",
        "entity_role": "competitor",
        "signal_type": "review_summary",
        "aspect": "服务",
        "summary": "聚合摘要",
        "source_type": "manual",
        "source_ref": "file://review-summary.csv",
        "source_id": "review-a",
        "is_estimate": False,
        "observed_at": datetime.now(UTC),
    }
    with pytest.raises(ValidationError, match="sample_size"):
        CompetitiveSignalCreate(**base)
    with pytest.raises(ValidationError, match="greater than or equal to 5"):
        CompetitiveSignalCreate(**base, sample_size=1)
    with pytest.raises(ValidationError, match="cannot exceed"):
        CompetitiveSignalCreate(
            **base, sample_size=10, positive_count=8, negative_count=4
        )
    with pytest.raises(ValidationError, match="cannot contain review sample counts"):
        CompetitiveSignalCreate(
            **{**base, "signal_type": "product_claim"}, sample_size=10
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CompetitiveSignalCreate(
            **base,
            sample_size=10,
            positive_count=8,
            negative_count=1,
            raw_reviews=[{"reviewer_id": "buyer-1", "text": "原始评论"}],
        )
    with pytest.raises(ValidationError, match="gtin must contain"):
        identity("无效条码商品", gtin="not-a-gtin")


def test_complete_non_gtin_identity_can_enter_human_review(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    try:
        payload = match_payload(source_id="match-without-gtin").model_copy(
            update={
                "subject_identity": identity("云湃 YP-100", gtin=None),
                "competitor_identity": identity("云湃 YP-100", gtin=None),
            }
        )
        created = competitive.record_entity_match("tenant-test", payload)
        assert created["score"] >= 70
        assert created["recommended_status"] == "approved"
        assert "gtin" in created["missing_fields"]
    finally:
        service.close()


def test_approved_match_controls_price_alerts_and_agent_recommendations(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    observed_at = datetime.now(UTC).replace(microsecond=0)
    try:
        match = competitive.record_entity_match("tenant-test", match_payload())
        monitor = competitive.upsert_monitor(
            "tenant-test",
            CompetitiveMonitorUpsert(
                store_id="store-a",
                subject_sku="sku-a",
                include_estimates=False,
                expected_record_version=0,
            ),
            actor="admin-a",
        )
        assert monitor["require_approved_match"] is True
        observation = CompetitorObservationCreate(
            connector_id="licensed-feed",
            store_id="store-a",
            subject_sku="sku-a",
            competitor_name="竞店 A",
            competitor_sku="comp-a",
            subject_price=Decimal("100"),
            competitor_price=Decimal("80"),
            source_type="licensed_provider",
            source_ref="https://licensed.example/prices/1",
            source_id="price-1",
            is_estimate=False,
            observed_at=observed_at,
            entity_match_id=match["id"],
        )
        pending = competitive.record("tenant-test", observation)
        assert pending["actionable"] is False
        assert pending["alert_evaluation"]["eligible_competitors"] == 0
        assert competitive.analyze_prices("tenant-test", "sku-a")["recommendations"][
            0
        ]["type"] == "entity_quality"

        approve(service, match["id"])
        alerts = competitive.list_alerts("tenant-test")
        undercut = next(item for item in alerts if item["alert_code"] == "competitor_undercut")
        assert undercut["status"] == "open"
        assert undercut["details"]["entity_match_id"] == match["id"]
        analysis = competitive.analyze_prices("tenant-test", "sku-a")
        assert analysis["summary"]["actionable_competitors"] == 1
        assert analysis["observations"][0]["actionable"] is True
        assert analysis["recommendations"][0]["type"] == "price_review"

        competitive.transition_entity_match(
            "tenant-test",
            match["id"],
            CompetitiveMatchTransition(
                target_status="rejected",
                expected_record_version=2,
                note="后续核验发现商品套装内容不同，撤销同款",
            ),
            actor="reviewer-a",
        )
        after_reject = competitive.analyze_prices("tenant-test", "sku-a")
        assert after_reject["summary"]["actionable_competitors"] == 0
        assert after_reject["recommendations"][0]["type"] == "entity_quality"
        undercut_after = next(
            item
            for item in competitive.list_alerts("tenant-test")
            if item["id"] == undercut["id"]
        )
        assert undercut_after["status"] == "resolved"

        with pytest.raises(ValueError, match="competitive_match_scope_mismatch"):
            competitive.record(
                "tenant-test",
                observation.model_copy(update={"competitor_sku": "comp-wrong"}),
            )
    finally:
        service.close()


def test_quality_overview_counts_match_and_signal_states(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    try:
        match_a = competitive.record_entity_match("tenant-test", match_payload())
        competitive.record_entity_match(
            "tenant-test",
            match_payload(
                source_id="match-source-2",
                competitor_gtin="06999999999999",
            ),
        )
        approve(service, match_a["id"])
        overview = competitive.competitive_quality_overview("tenant-test")
        assert overview["matches"]["total"] == 2
        assert overview["matches"]["status"] == {
            "pending": 1,
            "approved": 1,
            "rejected": 0,
        }
        assert overview["matches"]["pending_approvable"] == 0
        assert overview["matches"]["approval_rate"] == "50.00"
        assert competitive.competitive_quality_overview("tenant-other")["matches"][
            "total"
        ] == 0
    finally:
        service.close()


def test_competitive_entity_api_exposes_review_queue_and_evidence(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    headers = {
        "X-Admin-Id": "admin-test",
        "X-Admin-Key": "test-admin-key-123456",
    }
    with TestClient(app) as client:
        created_response = client.post(
            "/v1/competitive/matches",
            headers=headers,
            json=match_payload().model_dump(mode="json"),
        )
        assert created_response.status_code == 200
        created = created_response.json()
        queue = client.get(
            "/v1/competitive/matches?status=pending", headers=headers
        )
        assert queue.status_code == 200
        assert queue.json()[0]["id"] == created["id"]

        approved = client.post(
            f"/v1/competitive/matches/{created['id']}/transition",
            headers=headers,
            json={
                "target_status": "approved",
                "expected_record_version": 1,
                "note": "API 复核 GTIN、型号与关键规格一致",
            },
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"

        signal = client.post(
            "/v1/competitive/signals",
            headers=headers,
            json={
                "match_id": created["id"],
                "connector_id": "licensed-review-feed",
                "entity_role": "competitor",
                "signal_type": "review_summary",
                "aspect": "响应速度",
                "summary": "聚合样本表明响应较快",
                "sample_size": 50,
                "positive_count": 42,
                "negative_count": 3,
                "source_type": "licensed_provider",
                "source_ref": "https://licensed.example/reviews/api",
                "source_id": "api-review-1",
                "is_estimate": False,
                "observed_at": "2026-07-22T03:00:00+00:00",
            },
        )
        assert signal.status_code == 200
        assert signal.json()["eligible"] is True
        listed_signals = client.get(
            "/v1/competitive/signals?eligible_only=true", headers=headers
        )
        assert listed_signals.status_code == 200
        assert listed_signals.json()[0]["id"] == signal.json()["id"]

        quality = client.get("/v1/competitive/quality", headers=headers)
        assert quality.status_code == 200
        assert quality.json()["matches"]["status"]["approved"] == 1
        detail = client.get(
            f"/v1/competitive/matches/{created['id']}", headers=headers
        )
        assert detail.status_code == 200
        assert detail.json()["decisions"][0]["actor"] == "admin-test"

        stale_transition = client.post(
            f"/v1/competitive/matches/{created['id']}/transition",
            headers=headers,
            json={
                "target_status": "rejected",
                "expected_record_version": 1,
                "note": "故意提交过期版本验证乐观锁冲突",
            },
        )
        assert stale_transition.status_code == 409
        assert stale_transition.json()["detail"] == "competitive_match_version_conflict"


def test_concurrent_match_ingestion_and_decision_have_single_winners(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    try:
        payload = match_payload(source_id="concurrent-match")
        with ThreadPoolExecutor(max_workers=12) as pool:
            recorded = list(
                pool.map(
                    lambda _: competitive.record_entity_match("tenant-test", payload),
                    range(12),
                )
            )
        assert len({item["id"] for item in recorded}) == 1
        assert sum(item["write_status"] == "applied" for item in recorded) == 1
        assert sum(item["write_status"] == "idempotent" for item in recorded) == 11

        match_id = recorded[0]["id"]

        def decide(index: int) -> str:
            try:
                competitive.transition_entity_match(
                    "tenant-test",
                    match_id,
                    CompetitiveMatchTransition(
                        target_status="approved",
                        expected_record_version=1,
                        note=f"并发裁决 {index} 核对同款证据完整有效",
                    ),
                    actor=f"reviewer-{index}",
                )
                return "approved"
            except ValueError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(decide, range(12)))
        assert outcomes.count("approved") == 1
        assert outcomes.count("competitive_match_version_conflict") == 11
        detail = competitive.get_entity_match("tenant-test", match_id)
        assert detail["status"] == "approved"
        assert detail["record_version"] == 2
        assert len(detail["decisions"]) == 1
    finally:
        service.close()
