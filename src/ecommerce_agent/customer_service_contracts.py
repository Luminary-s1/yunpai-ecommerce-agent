from __future__ import annotations

import unicodedata

from .readonly_data.contracts import ReportFieldPolicy


CUSTOMER_SERVICE_SCRIPT_CATEGORY = "customer_service_script"
CUSTOMER_SERVICE_KEYWORD_CATEGORY = "customer_service_keyword_signal"
CUSTOMER_SERVICE_REPORT_TYPE = "customer_service_content"
CUSTOMER_SERVICE_MAPPING_VERSION = "m8r-customer-service-content-v1"
CUSTOMER_SERVICE_SOURCE_PREFIX = "evolution:m8r-customer-service:"

CUSTOMER_SERVICE_ALLOWED_FIELDS = frozenset(
    {
        "row_number",
        "content_type",
        "scenario",
        "question",
        "answer",
        "keyword",
        "risk_level",
        "store_id",
        "sku_id",
        "effective_from",
        "effective_to",
    }
)

CUSTOMER_SERVICE_FIELD_ALIASES = {
    "行号": "row_number",
    "内容类型": "content_type",
    "场景": "scenario",
    "适用场景": "scenario",
    "标准问法": "question",
    "规范问法": "question",
    "批准答复": "answer",
    "标准答复": "answer",
    "关键词": "keyword",
    "风险等级": "risk_level",
    "店铺编号": "store_id",
    "商品编号": "sku_id",
    "SKU编号": "sku_id",
    "生效时间": "effective_from",
    "失效时间": "effective_to",
}

CUSTOMER_SERVICE_FIELD_POLICY = ReportFieldPolicy(
    report_type=CUSTOMER_SERVICE_REPORT_TYPE,
    mapping_version=CUSTOMER_SERVICE_MAPPING_VERSION,
    field_aliases=CUSTOMER_SERVICE_FIELD_ALIASES,
    allowed_fields=CUSTOMER_SERVICE_ALLOWED_FIELDS,
)


def canonical_customer_service_field_name(value: str) -> str | None:
    normalized = _normalized_field_name(value)
    for alias, canonical in CUSTOMER_SERVICE_FIELD_ALIASES.items():
        if _normalized_field_name(alias) == normalized:
            return canonical
    for canonical in CUSTOMER_SERVICE_ALLOWED_FIELDS:
        if _normalized_field_name(canonical) == normalized:
            return canonical
    return None


def _normalized_field_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and character not in {"_", "-"}
    )
