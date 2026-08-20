from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..business.catalog import CatalogStatus
from ..business.marketing import CampaignStatus
from ..business.orders import (
    AfterSaleCaseType,
    AfterSaleStatus,
    LogisticsStatus,
    OrderStatus,
    PaymentStatus,
)
from ..business.source_versioning import canonical_source_time
from .contracts import (
    REPORT_CONTRACTS,
    ReportFieldPolicy,
    SourceKind,
)


class ReportFileFormat(StrEnum):
    CSV = "csv"
    XLSX = "xlsx"


class ReportDomain(StrEnum):
    CATALOG = "catalog"
    INVENTORY = "inventory"
    ORDERS = "orders"
    FULFILLMENT = "fulfillment"
    OPERATIONS = "operations"
    MARKETING = "marketing"
    REFUNDS = "refunds"
    FINANCE = "finance"


class ReportImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    store_id: str = Field(min_length=1, max_length=128)
    source_kind: SourceKind
    source_system: str = Field(min_length=1, max_length=128)
    report_type: str = Field(min_length=1, max_length=128)
    mapping_version: str = Field(min_length=1, max_length=128)
    report_period: str = Field(min_length=1, max_length=256)
    exported_at: datetime
    data_as_of: datetime
    file_format: ReportFileFormat
    storage_ref: str = Field(min_length=1, max_length=2048)
    source_timezone: str = Field(min_length=1, max_length=64)
    sheet_name: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("exported_at", "data_as_of")
    @classmethod
    def require_aware_times(cls, value: datetime) -> datetime:
        canonical_source_time(value)
        return value

    @field_validator("source_timezone")
    @classmethod
    def require_iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("invalid_report_source_timezone") from exc
        return value

    @field_validator("sheet_name")
    @classmethod
    def require_safe_sheet_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("invalid_xlsx_sheet_name")
        return value

    @model_validator(mode="after")
    def validate_request(self) -> "ReportImportRequest":
        if self.data_as_of > self.exported_at:
            raise ValueError("data_as_of_after_export")
        if self.file_format is ReportFileFormat.CSV and self.sheet_name is not None:
            raise ValueError("csv_sheet_name_not_allowed")
        return self


class ReportImportJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request: ReportImportRequest
    content: bytes = Field(min_length=1)


class _ReportRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    store_id: str = Field(min_length=1, max_length=128)


class CatalogSnapshotRow(_ReportRow):
    item_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    status: CatalogStatus
    sale_price: Decimal = Field(ge=0)
    currency: str = Field(default="CNY", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    merchant_code: str | None = Field(default=None, min_length=1, max_length=128)


class InventorySnapshotRow(_ReportRow):
    warehouse_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    on_hand: Decimal = Field(ge=0)
    reserved: Decimal = Field(default=Decimal("0"), ge=0)
    inbound: Decimal = Field(default=Decimal("0"), ge=0)
    average_daily_sales: Decimal = Field(default=Decimal("0"), ge=0)


class OrderSnapshotRow(_ReportRow):
    order_id: str = Field(min_length=1, max_length=128)
    order_status: OrderStatus
    payment_status: PaymentStatus
    currency: str = Field(default="CNY", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    total_amount: Decimal = Field(ge=0)
    placed_at: datetime
    line_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    quantity: int = Field(ge=1, le=100000)
    unit_price: Decimal = Field(ge=0)


class FulfillmentSnapshotRow(_ReportRow):
    order_id: str = Field(min_length=1, max_length=128)
    carrier: str = Field(min_length=1, max_length=128)
    tracking_no_masked: str = Field(min_length=3, max_length=128)
    logistics_status: LogisticsStatus
    last_event: str = Field(min_length=1, max_length=500)
    last_event_at: datetime


class OperationsDailyRow(_ReportRow):
    metric_date: date
    channel: str = Field(min_length=1, max_length=64)
    visitors: int = Field(ge=0, le=2_000_000_000)
    orders: int = Field(ge=0, le=2_000_000_000)
    sales_amount: Decimal = Field(ge=0)
    ad_spend: Decimal = Field(default=Decimal("0"), ge=0)
    currency: Literal["CNY"] = "CNY"

    @model_validator(mode="after")
    def validate_funnel(self) -> "OperationsDailyRow":
        if self.orders > self.visitors:
            raise ValueError("ops_orders_exceed_visitors")
        return self


class MarketingDailyRow(_ReportRow):
    campaign_id: str = Field(min_length=1, max_length=128)
    metric_date: date
    campaign_name: str = Field(min_length=1, max_length=256)
    channel: str = Field(min_length=1, max_length=64)
    objective: str = Field(min_length=1, max_length=64)
    status: CampaignStatus
    spend: Decimal = Field(ge=0)
    attributed_revenue: Decimal = Field(ge=0)
    attributed_orders: int = Field(ge=0, le=10_000_000)
    impressions: int = Field(ge=0, le=2_000_000_000)
    clicks: int = Field(ge=0, le=2_000_000_000)
    currency: Literal["CNY"] = "CNY"

    @model_validator(mode="after")
    def validate_funnel(self) -> "MarketingDailyRow":
        if self.clicks > self.impressions:
            raise ValueError("marketing_clicks_exceed_impressions")
        return self


class RefundSnapshotRow(_ReportRow):
    order_id: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=128)
    case_type: AfterSaleCaseType
    status: AfterSaleStatus
    requested_amount: Decimal = Field(default=Decimal("0"), ge=0)
    approved_amount: Decimal = Field(default=Decimal("0"), ge=0)
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)
    opened_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_times(self) -> "RefundSnapshotRow":
        if self.updated_at < self.opened_at:
            raise ValueError("refund_updated_before_opened")
        return self


class SettlementStatementRow(_ReportRow):
    statement_key: str = Field(min_length=1, max_length=128)
    period_start: date
    period_end: date
    gross_sales: Decimal = Field(ge=0)
    refund_amount: Decimal = Field(ge=0)
    fee_amount: Decimal = Field(ge=0)
    settlement_amount: Decimal = Field(ge=0)
    currency: str = Field(default="CNY", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def validate_period(self) -> "SettlementStatementRow":
        if self.period_start > self.period_end:
            raise ValueError("statement_date_range_invalid")
        return self


@dataclass(frozen=True, slots=True)
class ReportAdapter:
    policy: ReportFieldPolicy
    domain: ReportDomain
    grain: str
    amount_unit: str
    source_timezone: str
    row_model: type[BaseModel]
    identity_fields: tuple[str, ...]
    formats: frozenset[ReportFileFormat]
    value_aliases: Mapping[str, Mapping[str, str]]

    @property
    def report_type(self) -> str:
        return self.policy.report_type

    @property
    def mapping_version(self) -> str:
        return self.policy.mapping_version

    def normalize_values(
        self,
        value: Mapping[str, Any],
        *,
        excel_date_system: int | None = None,
    ) -> dict[str, Any]:
        normalized = dict(value)
        for field, aliases in self.value_aliases.items():
            raw = normalized.get(field)
            if isinstance(raw, str):
                normalized[field] = aliases.get(raw.strip(), raw.strip())
        if excel_date_system is not None:
            for field_name, field in self.row_model.model_fields.items():
                if field_name not in normalized:
                    continue
                if field.annotation in {date, datetime}:
                    normalized[field_name] = _excel_temporal_value(
                        normalized[field_name],
                        temporal_type=field.annotation,
                        date_system=excel_date_system,
                    )
        return normalized


def _excel_temporal_value(
    value: Any,
    *,
    temporal_type: Any,
    date_system: int,
) -> Any:
    if not isinstance(value, str):
        return value
    try:
        serial = Decimal(value.strip())
    except InvalidOperation:
        return value
    if not serial.is_finite() or serial < 0:
        return value
    epoch = datetime(1899, 12, 30) if date_system == 1900 else datetime(1904, 1, 1)
    try:
        converted = epoch + timedelta(seconds=float(serial * Decimal(86_400)))
    except (OverflowError, ValueError):
        return value
    return converted.date() if temporal_type is date else converted


class ReportAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[tuple[str, str], ReportAdapter] = {}

    def register(self, adapter: ReportAdapter) -> None:
        key = (adapter.report_type, adapter.mapping_version)
        if key in self._adapters:
            raise ValueError("duplicate_report_adapter")
        self._adapters[key] = adapter
        REPORT_CONTRACTS.register(adapter.policy)

    def get(self, report_type: str, mapping_version: str) -> ReportAdapter:
        try:
            return self._adapters[(report_type, mapping_version)]
        except KeyError as exc:
            raise ValueError("report_adapter_not_found") from exc

    def list(self) -> list[ReportAdapter]:
        return [self._adapters[key] for key in sorted(self._adapters)]


def _frozen_aliases(
    value: Mapping[str, Mapping[str, str]] | None = None,
) -> Mapping[str, Mapping[str, str]]:
    return MappingProxyType(
        {
            field: MappingProxyType(dict(aliases))
            for field, aliases in (value or {}).items()
        }
    )


def _adapter(
    *,
    report_type: str,
    domain: ReportDomain,
    grain: str,
    amount_unit: str,
    row_model: type[BaseModel],
    identity_fields: tuple[str, ...],
    field_aliases: Mapping[str, str],
    value_aliases: Mapping[str, Mapping[str, str]] | None = None,
    formats: frozenset[ReportFileFormat] = frozenset(ReportFileFormat),
) -> ReportAdapter:
    fields = row_model.model_fields
    policy = ReportFieldPolicy(
        report_type=report_type,
        mapping_version="generic-cn-v1",
        field_aliases=dict(field_aliases),
        allowed_fields=frozenset(fields),
        required_fields=frozenset(
            field_name
            for field_name, field in fields.items()
            if field.is_required()
        ),
    )
    return ReportAdapter(
        policy=policy,
        domain=domain,
        grain=grain,
        amount_unit=amount_unit,
        source_timezone="request",
        row_model=row_model,
        identity_fields=identity_fields,
        formats=formats,
        value_aliases=_frozen_aliases(value_aliases),
    )


_CATALOG_STATUS = {
    "在售": "active",
    "已上架": "active",
    "下架": "inactive",
    "已下架": "inactive",
    "草稿": "draft",
    "删除": "deleted",
}
_ORDER_STATUS = {
    "待付款": "created",
    "已付款": "paid",
    "备货中": "fulfilling",
    "已发货": "shipped",
    "已签收": "delivered",
    "已关闭": "closed",
    "已取消": "canceled",
}
_PAYMENT_STATUS = {
    "未付款": "unpaid",
    "已付款": "paid",
    "部分退款": "partially_refunded",
    "已退款": "refunded",
    "已关闭": "closed",
}
_LOGISTICS_STATUS = {
    "待揽收": "pending",
    "已揽收": "collected",
    "运输中": "in_transit",
    "已签收": "delivered",
    "异常": "exception",
}
_AFTER_SALE_STATUS = {
    "已申请": "requested",
    "审核中": "reviewing",
    "已同意": "approved",
    "已拒绝": "rejected",
    "退货中": "returning",
    "已完成": "completed",
    "已取消": "canceled",
}
_AFTER_SALE_TYPE = {
    "仅退款": "refund",
    "退货退款": "return_refund",
    "换货": "exchange",
    "维修": "repair",
    "投诉": "complaint",
}
_CAMPAIGN_STATUS = {"投放中": "active", "已暂停": "paused", "已结束": "ended"}


REPORT_ADAPTERS = ReportAdapterRegistry()

for _definition in (
    _adapter(
        report_type="catalog_snapshot",
        domain=ReportDomain.CATALOG,
        grain="store_sku_snapshot",
        amount_unit="currency_major_unit",
        row_model=CatalogSnapshotRow,
        identity_fields=("store_id", "sku_id"),
        field_aliases={
            "店铺ID": "store_id",
            "商品ID": "item_id",
            "宝贝ID": "item_id",
            "平台SKU": "sku_id",
            "SKU ID": "sku_id",
            "商品标题": "title",
            "商品状态": "status",
            "销售价": "sale_price",
            "币种": "currency",
            "商家编码": "merchant_code",
        },
        value_aliases={"status": _CATALOG_STATUS},
    ),
    _adapter(
        report_type="inventory_snapshot",
        domain=ReportDomain.INVENTORY,
        grain="warehouse_sku_snapshot",
        amount_unit="quantity",
        row_model=InventorySnapshotRow,
        identity_fields=("store_id", "warehouse_id", "sku_id"),
        field_aliases={
            "店铺ID": "store_id",
            "仓库ID": "warehouse_id",
            "平台SKU": "sku_id",
            "现有库存": "on_hand",
            "占用库存": "reserved",
            "在途库存": "inbound",
            "日均销量": "average_daily_sales",
        },
    ),
    _adapter(
        report_type="order_snapshot",
        domain=ReportDomain.ORDERS,
        grain="order_line_snapshot",
        amount_unit="currency_major_unit",
        row_model=OrderSnapshotRow,
        identity_fields=("store_id", "order_id", "line_id"),
        field_aliases={
            "店铺ID": "store_id",
            "订单号": "order_id",
            "订单状态": "order_status",
            "支付状态": "payment_status",
            "币种": "currency",
            "订单金额": "total_amount",
            "下单时间": "placed_at",
            "子订单号": "line_id",
            "平台SKU": "sku_id",
            "商品标题": "title",
            "数量": "quantity",
            "单价": "unit_price",
        },
        value_aliases={
            "order_status": _ORDER_STATUS,
            "payment_status": _PAYMENT_STATUS,
        },
    ),
    _adapter(
        report_type="fulfillment_snapshot",
        domain=ReportDomain.FULFILLMENT,
        grain="order_fulfillment_snapshot",
        amount_unit="not_applicable",
        row_model=FulfillmentSnapshotRow,
        identity_fields=("store_id", "order_id"),
        field_aliases={
            "店铺ID": "store_id",
            "订单号": "order_id",
            "物流公司": "carrier",
            "脱敏运单号": "tracking_no_masked",
            "物流状态": "logistics_status",
            "最新物流事件": "last_event",
            "物流更新时间": "last_event_at",
        },
        value_aliases={"logistics_status": _LOGISTICS_STATUS},
    ),
    _adapter(
        report_type="operations_daily",
        domain=ReportDomain.OPERATIONS,
        grain="store_channel_day",
        amount_unit="CNY_major_unit",
        row_model=OperationsDailyRow,
        identity_fields=("store_id", "metric_date", "channel"),
        field_aliases={
            "店铺ID": "store_id",
            "日期": "metric_date",
            "渠道": "channel",
            "访客数": "visitors",
            "订单数": "orders",
            "销售额": "sales_amount",
            "推广花费": "ad_spend",
            "币种": "currency",
        },
        formats=frozenset({ReportFileFormat.CSV}),
    ),
    _adapter(
        report_type="marketing_daily",
        domain=ReportDomain.MARKETING,
        grain="campaign_day",
        amount_unit="currency_major_unit",
        row_model=MarketingDailyRow,
        identity_fields=("store_id", "campaign_id", "metric_date"),
        field_aliases={
            "店铺ID": "store_id",
            "计划ID": "campaign_id",
            "日期": "metric_date",
            "计划名称": "campaign_name",
            "渠道": "channel",
            "投放目标": "objective",
            "计划状态": "status",
            "花费": "spend",
            "归因收入": "attributed_revenue",
            "归因订单": "attributed_orders",
            "曝光": "impressions",
            "点击": "clicks",
            "币种": "currency",
        },
        value_aliases={"status": _CAMPAIGN_STATUS},
    ),
    _adapter(
        report_type="refund_snapshot",
        domain=ReportDomain.REFUNDS,
        grain="order_after_sale_case",
        amount_unit="order_currency_major_unit",
        row_model=RefundSnapshotRow,
        identity_fields=("store_id", "order_id", "case_id"),
        field_aliases={
            "店铺ID": "store_id",
            "订单号": "order_id",
            "售后单号": "case_id",
            "售后类型": "case_type",
            "售后状态": "status",
            "申请金额": "requested_amount",
            "同意金额": "approved_amount",
            "原因编码": "reason_code",
            "申请时间": "opened_at",
            "更新时间": "updated_at",
        },
        value_aliases={
            "case_type": _AFTER_SALE_TYPE,
            "status": _AFTER_SALE_STATUS,
        },
    ),
    _adapter(
        report_type="settlement_statement",
        domain=ReportDomain.FINANCE,
        grain="store_statement_period",
        amount_unit="currency_major_unit",
        row_model=SettlementStatementRow,
        identity_fields=("store_id", "statement_key"),
        field_aliases={
            "店铺ID": "store_id",
            "结算单号": "statement_key",
            "开始日期": "period_start",
            "结束日期": "period_end",
            "销售总额": "gross_sales",
            "退款金额": "refund_amount",
            "平台费用": "fee_amount",
            "结算金额": "settlement_amount",
            "币种": "currency",
        },
    ),
):
    REPORT_ADAPTERS.register(_definition)


__all__ = [
    "REPORT_ADAPTERS",
    "CatalogSnapshotRow",
    "FulfillmentSnapshotRow",
    "InventorySnapshotRow",
    "MarketingDailyRow",
    "OperationsDailyRow",
    "OrderSnapshotRow",
    "RefundSnapshotRow",
    "ReportAdapter",
    "ReportAdapterRegistry",
    "ReportDomain",
    "ReportFileFormat",
    "ReportImportJob",
    "ReportImportRequest",
    "SettlementStatementRow",
]
