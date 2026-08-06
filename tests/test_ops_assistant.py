from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ecommerce_agent.api import create_app
from ecommerce_agent.business import (
    CopywritingRegenerateRequest,
    CopywritingRequest,
    OpsOperationRecordUpsert,
    OpsReportQuery,
)
from ecommerce_agent.service import AgentService

from conftest import make_settings


ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}
TENANT = "tenant-test"
STORE_ID = "qingchuan-flagship-001"

CSV_SAMPLE = (
    "日期,渠道,访客数,订单数,销售额,推广花费\n"
    "2026-07-01,搜索,1200,48,9600.00,600.00\n"
    "2026-07-02,搜索,1300,50,10100.00,620.00\n"
    "2026-07-01,直播,900,36,7200.00,900.00\n"
    "2026-07-02,直播,880,30,6100.00,980.00\n"
    "bad-date,直播,10,2,100.00,10.00\n"
    "2026-07-03,直播,100,200,100.00,10.00\n"
)

JSON_SAMPLE = (
    '{"records": ['
    '{"record_date": "2026-07-03", "channel": "搜索", "visitors": 1250, "orders": 44, '
    '"sales_amount": "8900.00", "ad_spend": "660.00"},'
    '{"record_date": "2026-07-04", "channel": "搜索", "visitors": 1180, "orders": 35, '
    '"sales_amount": "7000.00", "ad_spend": "760.00"}'
    "]}"
)


class FakeOpsModel:
    def generate_json(self, messages: list[dict[str, str]]) -> dict[str, str]:
        assert '"task_type": "ops_copywriting"' in messages[-1]["content"]
        return {
            "title": "模型标题",
            "body": "模型正文：无油低脂，购买前请核对参数并以详情页为准。",
        }

    def generate(self, messages: list[dict[str, str]]) -> str:
        assert '"task_type": "ops_report_narrative"' in messages[-1]["content"]
        return "模型解读：销售与投放趋势已按既定统计结果说明。"


class FailingOpsModel:
    def generate_json(self, messages: list[dict[str, str]]) -> dict[str, str]:
        raise RuntimeError("model unavailable")

    def generate(self, messages: list[dict[str, str]]) -> str:
        raise RuntimeError("model unavailable")


class CapturingOpsModel:
    def __init__(self) -> None:
        self.copy_prompts: list[list[dict[str, str]]] = []

    def generate_json(self, messages: list[dict[str, str]]) -> dict[str, str]:
        self.copy_prompts.append(messages)
        return {
            "title": "模型标题",
            "body": "这是一段满足中等长度要求的模型正文，围绕已提供的商品卖点展开，并提醒用户在选择前仔细核对商品详情页中的规格、价格与活动信息。",
        }


class OverlongOpsModel:
    def generate_json(self, messages: list[dict[str, str]]) -> dict[str, str]:
        return {"title": "模型标题", "body": "超长正文" * 100}


class MixedOpsModel:
    def __init__(self) -> None:
        self.calls = 0

    def generate_json(self, messages: list[dict[str, str]]) -> dict[str, str]:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("one item failed")
        return {
            "title": "模型标题",
            "body": "模型正文：无油低脂，购买前请核对参数并以详情页为准。",
        }


def _record(record_date: str, channel: str, visitors: int, orders: int,
            sales: str, spend: str) -> OpsOperationRecordUpsert:
    return OpsOperationRecordUpsert(
        dataset_key="ops-week-30",
        store_id=STORE_ID,
        record_date=record_date,
        channel=channel,
        visitors=visitors,
        orders=orders,
        sales_amount=sales,
        ad_spend=spend,
        source_format="form",
    )


def test_csv_import_returns_structured_records_and_rejects_bad_rows(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        ops = service.operations.ops_assistant
        result = ops.parse_dataset(
            TENANT,
            dataset_key="ops-week-30",
            store_id=STORE_ID,
            source_format="csv",
            content=CSV_SAMPLE,
        )
        assert result["total_rows"] == 6
        assert result["accepted_rows"] == 4
        assert result["rejected_rows"] == 2
        reasons = {item["reason"] for item in result["rejected"]}
        assert any(reason.startswith("record_date") for reason in reasons)
        assert any("ops_orders_exceed_visitors" in reason for reason in reasons)
        first = result["records"][0]
        assert first["dataset_key"] == "ops-week-30"
        assert first["source_format"] == "csv"
        assert first["conversion_rate"] is not None
        assert first["version"] == 1

        # 同一份 CSV 重复导入必须幂等，不产生新版本。
        again = ops.parse_dataset(
            TENANT,
            dataset_key="ops-week-30",
            store_id=STORE_ID,
            source_format="csv",
            content=CSV_SAMPLE,
        )
        assert again["applied"] == 0
        assert again["idempotent"] == 4
    finally:
        service.close()


def test_json_import_and_form_entry_share_versioning(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        ops = service.operations.ops_assistant
        result = ops.parse_dataset(
            TENANT,
            dataset_key="ops-week-30",
            store_id=STORE_ID,
            source_format="json",
            content=JSON_SAMPLE,
        )
        assert result["accepted_rows"] == 2
        # 表单修正同一天同渠道的数据：版本 +1。
        updated = ops.upsert_record(
            TENANT,
            _record("2026-07-03", "搜索", 1250, 46, "9200.00", "660.00"),
        )
        assert updated["write_status"] == "applied"
        assert updated["version"] == 2
        assert updated["source_format"] == "form"
        # 完全一致的表单重复提交：幂等。
        idempotent = ops.upsert_record(
            TENANT,
            _record("2026-07-03", "搜索", 1250, 46, "9200.00", "660.00"),
        )
        assert idempotent["write_status"] == "idempotent"
        assert idempotent["version"] == 2
        rows = ops.list_records(TENANT, dataset_key="ops-week-30")
        assert len(rows) == 2
    finally:
        service.close()


def test_dataset_parse_errors_are_rejected(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        ops = service.operations.ops_assistant
        with pytest.raises(ValueError, match="ops_json_invalid"):
            ops.parse_dataset(
                TENANT,
                dataset_key="bad",
                store_id=STORE_ID,
                source_format="json",
                content="not-json",
            )
        with pytest.raises(ValueError, match="ops_dataset_empty"):
            ops.parse_dataset(
                TENANT,
                dataset_key="empty",
                store_id=STORE_ID,
                source_format="csv",
                content="日期,渠道,访客数,订单数,销售额\n",
            )
    finally:
        service.close()


def test_copywriting_generates_style_variants_with_risk_flags(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        ops = service.operations.ops_assistant
        result = ops.generate_copy(
            TENANT,
            CopywritingRequest(
                store_id=STORE_ID,
                product_name="青川空气炸锅 AF5",
                selling_points=["无油低脂", "一键预设菜单", "全网最低价保障"],
                price="499.00",
                target_audience="租房青年",
                styles=["formal", "playful", "urgent"],
                variants_per_style=2,
            ),
        )
        assert result["batch_size"] == 6
        assert {item["style"] for item in result["variants"]} == {"formal", "playful", "urgent"}
        assert result["publication_allowed"] is False
        # 每种风格的两个变体正文不应完全相同。
        for style in ("formal", "playful", "urgent"):
            bodies = [item["body"] for item in result["variants"] if item["style"] == style]
            assert len(bodies) == 2 and bodies[0] != bodies[1]
        # 卖点携带绝对化用语时必须标记人工复核。
        flagged = [item for item in result["variants"] if "全网最低" in item["body"]]
        assert flagged and all(item["needs_review"] for item in flagged)
        # 测试环境未接入真实模型，全部走确定性模板。
        assert {item["generator"] for item in result["variants"]} == {"template"}
        assert all(item["publication_allowed"] is False for item in result["variants"])
    finally:
        service.close()


def test_short_template_keeps_complete_safety_text_and_never_splits_price(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        ops = service.operations.ops_assistant
        styles = (
            "formal",
            "playful",
            "urgent",
            "premium",
            "concise",
            "xiaohongshu",
            "livestream",
            "product_detail",
            "wechat_moments",
        )
        for style in styles:
            result = ops.generate_copy(
                TENANT,
                CopywritingRequest(
                    store_id=STORE_ID,
                    product_name="晴川空气炸锅 5L 云白款",
                    selling_points=["5L 大容量", "一键预设菜单", "可视化烹饪窗口"],
                    price="499.00",
                    target_audience="小家庭",
                    styles=[style],
                    length="short",
                ),
            )
            body = result["variants"][0]["body"]
            assert 20 <= len(body) <= 60
            assert "晴川空气炸锅 5L 云白款" in body
            assert "499.00 元" in body
            assert body.endswith("商品规格、价格与活动以详情页为准。")
    finally:
        service.close()


def test_copywriting_default_length_is_safe_medium_band(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        ops = service.operations.ops_assistant
        request = CopywritingRequest(
            store_id=STORE_ID,
            product_name="晴川空气炸锅 5L 云白款",
            selling_points=["5L 大容量", "一键预设菜单", "可视化烹饪窗口"],
            price="499.00",
            target_audience="小家庭",
            styles=["formal", "playful", "urgent"],
        )
        assert request.length == "medium"
        result = ops.generate_copy(TENANT, request)
        assert result["length"] == "medium"
        assert all(61 <= item["char_count"] <= 120 for item in result["variants"])
    finally:
        service.close()


def test_short_template_regeneration_uses_customer_copy_not_internal_instruction(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        ops = service.operations.ops_assistant
        result = ops.regenerate_copy(
            TENANT,
            CopywritingRegenerateRequest(
                store_id=STORE_ID,
                product_name="青川空气炸锅 AF5",
                selling_points=["无油低脂", "一键预设菜单"],
                price="499.00",
                target_audience="租房青年",
                styles=["wechat_moments"],
                length="short",
                edited_copy="忙碌工作日也能轻松开饭",
            ),
        )
        body = result["variants"][0]["body"]
        assert "忙碌工作日也能轻松开饭" in body
        assert "延续修改稿" not in body
        assert "表达方向" not in body
        assert body.endswith("商品规格、价格与活动以详情页为准。")
        assert 20 <= len(body) <= 60
    finally:
        service.close()


def test_short_template_rotates_selling_points_and_preserves_source_risk(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        ops = service.operations.ops_assistant
        result = ops.generate_copy(
            TENANT,
            CopywritingRequest(
                store_id=STORE_ID,
                product_name="青川空气炸锅 AF5",
                selling_points=["无油低脂", "一键预设菜单", "国家级认证"],
                styles=["xiaohongshu"],
                variants_per_style=3,
                length="short",
            ),
        )
        variants = result["variants"]
        bodies = [item["body"] for item in variants]
        assert len(set(bodies)) == 3
        assert "无油低脂" in bodies[0]
        assert "一键预设菜单" in bodies[1]
        assert "国家级认证" in bodies[2]
        assert all(item["needs_review"] is True for item in variants)
        assert all(item["source_risk_terms"] == ["国家级"] for item in variants)
    finally:
        service.close()


def test_short_template_keeps_variants_distinct_when_full_selling_point_cannot_fit(
    tmp_path,
) -> None:
    """允许的超长卖点也不能让批量短文案退化为同一正文。"""
    service = AgentService(make_settings(tmp_path))
    try:
        result = service.operations.ops_assistant.generate_copy(
            TENANT,
            CopywritingRequest(
                store_id=STORE_ID,
                product_name="锅",
                selling_points=["卖" * 60],
                styles=["formal"],
                variants_per_style=3,
                length="short",
            ),
        )
        bodies = [item["body"] for item in result["variants"]]
        assert len(set(bodies)) == 3
        assert all(body.endswith("商品规格、价格与活动以详情页为准。") for body in bodies)
    finally:
        service.close()


def test_regeneration_rejects_edit_that_cannot_fit_requested_length() -> None:
    edited_copy = "修改后的文案内容" * 18
    for length in ("short", "medium"):
        with pytest.raises(ValidationError, match="copy_revision_too_long_for_length"):
            CopywritingRegenerateRequest(
                store_id=STORE_ID,
                product_name="青川空气炸锅 AF5",
                selling_points=["无油低脂"],
                styles=["formal"],
                length=length,
                edited_copy=edited_copy,
            )


def test_copywriting_supports_campaign_styles_and_length_bands(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        ops = service.operations.ops_assistant
        expected_features = {
            "xiaohongshu": "种草笔记",
            "livestream": "直播口播",
            "product_detail": "核心卖点",
            "wechat_moments": "朋友圈",
        }
        for length, (minimum, maximum) in {
            "short": (20, 60),
            "medium": (61, 120),
            "long": (121, 200),
        }.items():
            result = ops.generate_copy(
                TENANT,
                CopywritingRequest(
                    store_id=STORE_ID,
                    product_name="青川空气炸锅 AF5",
                    selling_points=["无油低脂", "一键预设菜单", "可视化烹饪窗口"],
                    price="499.00",
                    target_audience="租房青年",
                    styles=list(expected_features),
                    variants_per_style=1,
                    length=length,
                ),
            )
            assert result["length"] == length
            assert len(result["variants"]) == 4
            for item in result["variants"]:
                assert minimum <= item["char_count"] <= maximum
                combined = f'{item["title"]}{item["body"]}'
                assert expected_features[item["style"]] in combined
    finally:
        service.close()


def test_copywriting_uses_independent_style_prompts_and_rejects_wrong_length_model_output(
    tmp_path,
) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        ops = service.operations.ops_assistant
        model = CapturingOpsModel()
        ops.attach_model(model)
        request = CopywritingRequest(
            store_id=STORE_ID,
            product_name="青川空气炸锅 AF5",
            selling_points=["无油低脂"],
            styles=["xiaohongshu", "livestream", "product_detail", "wechat_moments"],
            length="medium",
        )
        generated = ops.generate_copy(TENANT, request)
        assert {item["generator"] for item in generated["variants"]} == {"model"}
        system_prompts = [messages[0]["content"] for messages in model.copy_prompts]
        assert len(system_prompts) == len(set(system_prompts)) == 4
        for marker in ("体验分享", "口播节奏", "卖点分层", "熟人分享"):
            assert any(marker in prompt for prompt in system_prompts)

        ops.attach_model(OverlongOpsModel())
        fallback = ops.generate_copy(TENANT, request)
        assert {item["generator"] for item in fallback["variants"]} == {
            "template_fallback"
        }
        assert all(61 <= item["char_count"] <= 120 for item in fallback["variants"])
    finally:
        service.close()


def test_copywriting_regeneration_uses_edited_copy(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/ops-assistant/copywriting/regenerate",
            headers=ADMIN_HEADERS,
            json={
                "store_id": STORE_ID,
                "product_name": "青川空气炸锅 AF5",
                "selling_points": ["无油低脂", "一键预设菜单"],
                "price": "499.00",
                "target_audience": "租房青年",
                "styles": ["wechat_moments"],
                "variants_per_style": 1,
                "length": "medium",
                "edited_copy": "忙碌工作日也能轻松开饭",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["revision_applied"] is True
        assert body["revision_source"] == "忙碌工作日也能轻松开饭"
        assert "忙碌工作日也能轻松开饭" in body["variants"][0]["body"]
        audit = client.get(
            "/v1/admin/audit?event_type=ops.copywriting.regenerated",
            headers=ADMIN_HEADERS,
        )
        assert audit.status_code == 200
        assert audit.json()[0]["detail"]["length"] == "medium"

        too_long = client.post(
            "/v1/ops-assistant/copywriting/regenerate",
            headers=ADMIN_HEADERS,
            json={
                "store_id": STORE_ID,
                "product_name": "青川空气炸锅 AF5",
                "selling_points": ["无油低脂"],
                "styles": ["formal"],
                "length": "medium",
                "edited_copy": "修改后的文案内容" * 18,
            },
        )
        assert too_long.status_code == 422
        assert "copy_revision_too_long_for_length" in str(too_long.json())


def test_copywriting_rejects_oversized_batch() -> None:
    with pytest.raises(ValidationError, match="copy_batch_too_large"):
        CopywritingRequest(
            store_id=STORE_ID,
            product_name="青川空气炸锅 AF5",
            selling_points=["无油低脂"],
            styles=["formal", "playful", "urgent", "premium"],
            variants_per_style=3,
        )


def test_model_generation_and_fallback_paths_are_explicit(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        ops = service.operations.ops_assistant
        request = CopywritingRequest(
            store_id=STORE_ID,
            product_name="青川空气炸锅 AF5",
            selling_points=["无油低脂"],
            styles=["formal"],
            length="short",
        )
        ops.attach_model(FakeOpsModel())
        generated = ops.generate_copy(TENANT, request)
        assert generated["variants"][0]["generator"] == "model"
        assert generated["variants"][0]["title"] == "模型标题"

        ops.upsert_record(
            TENANT,
            _record("2026-07-01", "搜索", 1000, 50, "10000.00", "500.00"),
        )
        report = ops.analysis_report(TENANT, OpsReportQuery(dataset_key="ops-week-30"))
        assert report["narrative_generator"] == "model"
        assert report["narrative"].startswith("模型解读")

        ops.attach_model(FailingOpsModel())
        fallback = ops.generate_copy(TENANT, request)
        assert fallback["variants"][0]["generator"] == "template_fallback"
        report_fallback = ops.analysis_report(
            TENANT, OpsReportQuery(dataset_key="ops-week-30")
        )
        assert report_fallback["narrative"] is None
        assert report_fallback["narrative_generator"] == "fallback_summary_only"
    finally:
        service.close()


def test_copywriting_mixed_batch_marks_each_generator(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        ops = service.operations.ops_assistant
        ops.attach_model(MixedOpsModel())
        result = ops.generate_copy(
            TENANT,
            CopywritingRequest(
                store_id=STORE_ID,
                product_name="青川空气炸锅 AF5",
                selling_points=["无油低脂"],
                styles=["formal"],
                variants_per_style=2,
                length="short",
            ),
        )
        assert [item["generator"] for item in result["variants"]] == [
            "model",
            "template_fallback",
        ]
        assert all(item["publication_allowed"] is False for item in result["variants"])
    finally:
        service.close()


def test_analysis_report_produces_trends_and_recommendations(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        ops = service.operations.ops_assistant
        seed = [
            ("2026-07-01", "搜索", 1000, 50, "10000.00", "500.00"),
            ("2026-07-02", "搜索", 1050, 52, "10400.00", "520.00"),
            ("2026-07-03", "搜索", 1100, 40, "8000.00", "700.00"),
            ("2026-07-04", "搜索", 1200, 30, "6000.00", "900.00"),
            ("2026-07-01", "直播", 800, 6, "1200.00", "800.00"),
            ("2026-07-04", "直播", 900, 5, "1000.00", "950.00"),
        ]
        for row in seed:
            ops.upsert_record(TENANT, _record(*row))
        report = ops.analysis_report(
            TENANT, OpsReportQuery(dataset_key="ops-week-30", store_id=STORE_ID)
        )
        assert report["totals"]["visitors"] == 6050
        assert report["totals"]["orders"] == 183
        assert report["data_quality"]["record_count"] == 6
        assert report["data_quality"]["numbers_computed_by_code"] is True
        directions = {item["metric"]: item["direction"] for item in report["trends"]}
        assert directions["sales_amount"] == "down"
        assert directions["ad_spend"] == "up"
        codes = {item["code"] for item in report["findings"]}
        assert "sales_declining" in codes
        assert "spend_up_sales_flat" in codes
        assert "channel_conversion_low" in codes
        assert any("统计周期覆盖" in line for line in report["summary"])
        # 未接入模型时报告仍完整，只是没有模型叙述。
        assert report["narrative"] is None
        assert report["narrative_generator"] == "disabled"
    finally:
        service.close()


def test_records_and_reports_are_tenant_isolated(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        ops = service.operations.ops_assistant
        ops.upsert_record(TENANT, _record("2026-07-01", "搜索", 100, 5, "1000.00", "50.00"))
        assert ops.list_records("tenant-other") == []
        other_report = ops.analysis_report("tenant-other", OpsReportQuery())
        assert other_report["data_quality"]["record_count"] == 0
        assert other_report["findings"][0]["code"] == "no_data"
    finally:
        service.close()


def test_analysis_report_does_not_truncate_after_list_page_limit(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        ops = service.operations.ops_assistant
        for index in range(501):
            ops.upsert_record(
                TENANT,
                _record(
                    "2026-07-01",
                    f"渠道-{index:03d}",
                    100,
                    5,
                    "1000.00",
                    "50.00",
                ),
            )
        assert len(ops.list_records(TENANT, dataset_key="ops-week-30")) == 500
        report = ops.analysis_report(
            TENANT, OpsReportQuery(dataset_key="ops-week-30", store_id=STORE_ID)
        )
        assert report["data_quality"]["record_count"] == 501
        assert report["totals"]["visitors"] == 50_100
    finally:
        service.close()


def test_ops_assistant_api_end_to_end(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        upload = client.post(
            "/v1/ops-assistant/datasets/import"
            "?dataset_key=ops-week-30&store_id=qingchuan-flagship-001&source_format=csv",
            headers=ADMIN_HEADERS,
            content=CSV_SAMPLE.encode("utf-8"),
        )
        assert upload.status_code == 200
        payload = upload.json()
        assert payload["accepted_rows"] == 4
        assert payload["rejected_rows"] == 2

        records = client.get(
            "/v1/ops-assistant/records?dataset_key=ops-week-30", headers=ADMIN_HEADERS
        )
        assert records.status_code == 200
        assert len(records.json()) == 4

        form_entry = client.post(
            "/v1/ops-assistant/records",
            headers=ADMIN_HEADERS,
            json={
                "dataset_key": "ops-week-30",
                "store_id": STORE_ID,
                "record_date": "2026-07-03",
                "channel": "搜索",
                "visitors": 1250,
                "orders": 44,
                "sales_amount": "8900.00",
                "ad_spend": "660.00",
            },
        )
        assert form_entry.status_code == 200
        assert form_entry.json()["source_format"] == "form"

        copy = client.post(
            "/v1/ops-assistant/copywriting/generate",
            headers=ADMIN_HEADERS,
            json={
                "store_id": STORE_ID,
                "product_name": "青川空气炸锅 AF5",
                "selling_points": ["无油低脂", "一键预设菜单"],
                "price": "499.00",
                "styles": ["formal", "concise"],
                "variants_per_style": 1,
            },
        )
        assert copy.status_code == 200
        assert copy.json()["batch_size"] == 2
        assert copy.json()["publication_allowed"] is False

        report = client.post(
            "/v1/ops-assistant/reports/analysis",
            headers=ADMIN_HEADERS,
            json={"dataset_key": "ops-week-30", "store_id": STORE_ID},
        )
        assert report.status_code == 200
        body = report.json()
        assert body["data_quality"]["record_count"] == 5
        assert body["summary"]
        assert body["action_boundary"].startswith("仅输出数据解读")
        report_audit = client.get(
            "/v1/admin/audit?event_type=ops.report.generated", headers=ADMIN_HEADERS
        )
        assert report_audit.status_code == 200
        assert report_audit.json()[0]["detail"]["record_count"] == 5

        bad_format = client.post(
            "/v1/ops-assistant/datasets/import"
            "?dataset_key=x&store_id=s&source_format=xml",
            headers=ADMIN_HEADERS,
            content=b"whatever",
        )
        assert bad_format.status_code == 422

        unauthorized = client.get("/v1/ops-assistant/records")
        assert unauthorized.status_code in (401, 503)


def test_dataset_import_api_accepts_utf8_bom_csv_and_json(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        bom_csv = (
            "\ufeff日期,渠道,访客数,订单数,销售额,推广花费\n"
            "2026-07-05,推荐,600,24,4800.00,300.00\n"
        )
        csv_response = client.post(
            "/v1/ops-assistant/datasets/import"
            "?dataset_key=ops-bom&store_id=qingchuan-flagship-001&source_format=csv",
            headers={**ADMIN_HEADERS, "Content-Type": "text/csv; charset=utf-8"},
            content=bom_csv.encode("utf-8"),
        )
        assert csv_response.status_code == 200
        assert csv_response.json()["accepted_rows"] == 1
        assert csv_response.json()["rejected_rows"] == 0

        json_response = client.post(
            "/v1/ops-assistant/datasets/import"
            "?dataset_key=ops-json&store_id=qingchuan-flagship-001&source_format=json",
            headers={**ADMIN_HEADERS, "Content-Type": "application/json"},
            content=JSON_SAMPLE.encode("utf-8"),
        )
        assert json_response.status_code == 200
        assert json_response.json()["accepted_rows"] == 2
        assert json_response.json()["source_format"] == "json"
