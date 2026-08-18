"""M9-R WP1 隐私红线反例测试：敏感字段/敏感值绝不进入读模型。

确定性前提：M7-R 契约 sanitize_report_row 的隐私机制（已核实 contracts.py 实现）：
1. 敏感字段名：对原始行字段名归一化后与全局 SENSITIVE_FIELD_NAMES 比对，命中即剥离
   （含「买家姓名」「手机号」等中文字段），**无论是否在白名单**——白名单只能含
   ASCII 规范字段名（invalid_canonical_field 拒绝中文），敏感名物理进不了白名单。
2. 敏感值：合法字段值中含手机号/邮箱/地址模式 → 该值被剥离。

本测试锁「隐私不得流入工厂输出」：若上游剥离失效，测试必须 FAIL。
"""
from __future__ import annotations

from datetime import UTC, datetime

from ecommerce_agent.product_read_model.factory import build_read_model_from_manifest
from ecommerce_agent.readonly_data.contracts import (
    ImportManifestInput,
    ImportReference,
    ReferenceKind,
    ReportFieldPolicy,
    SourceKind,
)

DATA_AS_OF = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
DIGEST = "a" * 64


def _manifest() -> ImportManifestInput:
    return ImportManifestInput(
        store_id="s1", source_kind=SourceKind.ACTUAL, source_system="taobao",
        report_type="store_traffic", report_period="2026-08-17",
        exported_at=DATA_AS_OF, schema_fingerprint=DIGEST, content_digest=DIGEST,
        mapping_version="v1", parsed_rows=1, data_as_of=DATA_AS_OF,
        references=(ImportReference(
            kind=ReferenceKind.RAW_FILE,
            reference="objects/readonly-imports/x.csv", content_digest=DIGEST,
        ),),
    )


def _policy() -> ReportFieldPolicy:
    return ReportFieldPolicy(
        report_type="orders", mapping_version="orders-v1",
        allowed_fields=frozenset({"item_id", "sku_id", "net_sales", "remark"}),
        required_fields=frozenset({"item_id", "sku_id", "net_sales"}),
    )


def test_sensitive_field_name_stripped_before_model() -> None:
    """原始行含「买家姓名」字段 → sanitize 按全局敏感名剥离 → 模型无该属性。"""
    rows = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0, "买家姓名": "张三"}]
    models = build_read_model_from_manifest(_manifest(), _policy(), rows, tenant_id="t1")
    sku = models[0]
    assert not hasattr(sku, "买家姓名")
    assert sku.net_sales.safe_value == 100.0  # type: ignore[union-attr]


def test_sensitive_value_stripped_before_model() -> None:
    """合法字段值含手机号 → M7-R 契约剥离该值 → remark 不进入 payload。"""
    rows = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0,
             "remark": "买家电话 13812345678"}]
    models = build_read_model_from_manifest(_manifest(), _policy(), rows, tenant_id="t1")
    sku = models[0]
    assert not hasattr(sku, "remark")
    assert sku.net_sales.safe_value == 100.0  # type: ignore[union-attr]


def test_sensitive_phone_field_name_stripped() -> None:
    """「手机号」字段名同样被全局敏感名剥离。"""
    rows = [{"item_id": "i1", "sku_id": "sku1", "net_sales": 100.0, "手机号": "13812345678"}]
    models = build_read_model_from_manifest(_manifest(), _policy(), rows, tenant_id="t1")
    sku = models[0]
    assert not hasattr(sku, "手机号")
