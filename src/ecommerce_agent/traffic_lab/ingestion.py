from __future__ import annotations

import csv
import io
import json
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..business_calendar import StoreBusinessCalendarService
from ..business.source_versioning import payload_digest
from ..connectors import PullRecord
from ..database import Database
from .models import CreativeAssetCreate, ListingRevisionCreate, TrafficMetricBucketUpsert
from .service import TrafficLabService


MetricImportFormat = Literal["csv", "json"]
MAX_TRAFFIC_IMPORT_ROWS = 2_000

_GRANULARITY_ALIASES = {
    "hour": "hour",
    "hourly": "hour",
    "小时": "hour",
    "小时级": "hour",
    "day": "day",
    "daily": "day",
    "日": "day",
    "日级": "day",
    "天": "day",
}
_CANONICAL_COLUMNS = {
    "listing_revision_id",
    "store_id",
    "item_id",
    "sku_id",
    "metric_start",
    "metric_end",
    "bucket_granularity",
    "traffic_source",
    "impressions",
    "clicks",
    "visitors",
    "favorites",
    "cart_adds",
    "orders",
    "sales_amount",
    "ad_spend",
    "search_impressions",
    "recommend_impressions",
    "data_as_of",
    "source_id",
}
_COLUMN_ALIASES = {
    **{name.casefold(): name for name in _CANONICAL_COLUMNS},
    "revision_id": "listing_revision_id",
    "来源记录id": "source_id",
    "数据id": "source_id",
    "店铺id": "store_id",
    "商品id": "item_id",
    "sku": "sku_id",
    "指标开始": "metric_start",
    "指标结束": "metric_end",
    "统计时间": "metric_start",
    "粒度": "bucket_granularity",
    "流量来源": "traffic_source",
    "曝光": "impressions",
    "曝光量": "impressions",
    "点击": "clicks",
    "点击量": "clicks",
    "访客": "visitors",
    "访客数": "visitors",
    "收藏": "favorites",
    "加购": "cart_adds",
    "订单": "orders",
    "订单数": "orders",
    "销售额": "sales_amount",
    "广告花费": "ad_spend",
    "搜索曝光": "search_impressions",
    "推荐曝光": "recommend_impressions",
    "数据截至": "data_as_of",
}


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_required")
    return value


class _ListingRevisionResourcePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=128)
    item_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    revision_no: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=500)
    sale_price: Decimal
    attributes: dict[str, Any] = Field(default_factory=dict)
    active_from: datetime
    active_to: datetime | None = None
    source_receipt_id: str = Field(min_length=1, max_length=256)
    applied_at: datetime
    asset: CreativeAssetCreate

    @field_validator("active_from", "active_to", "applied_at")
    @classmethod
    def require_aware_times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)


class TrafficLabIngestionService:
    def __init__(
        self,
        db: Database,
        *,
        business_calendars: StoreBusinessCalendarService | None = None,
    ):
        self.db = db
        self.domain = TrafficLabService(
            db,
            business_calendars=business_calendars,
        )

    def import_metrics(
        self,
        tenant_id: str,
        *,
        connector_id: str,
        source_format: MetricImportFormat,
        content: str,
        source_timezone: str = "Asia/Shanghai",
    ) -> dict[str, Any]:
        rows = self._rows_from_content(source_format, content)
        if len(rows) > MAX_TRAFFIC_IMPORT_ROWS:
            raise ValueError("traffic_import_too_large")
        if not rows:
            raise ValueError("traffic_import_empty")
        zone = self._source_zone(source_timezone)
        accepted: list[dict[str, Any]] = []
        quarantined: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        granularities: Counter[str] = Counter()
        applied = 0
        idempotent = 0
        for row_number, raw in enumerate(rows, start=1):
            try:
                value, reason_code = self._normalize_metric_row(
                    tenant_id,
                    connector_id=connector_id,
                    raw=raw,
                    source_zone=zone,
                )
                if reason_code is None:
                    result = self.domain.ingest_metric_bucket(tenant_id, value)
                else:
                    result = self.domain.quarantine_metric_bucket(
                        tenant_id, value, reason_code=reason_code
                    )
            except (ValidationError, ValueError) as exc:
                rejected.append({"row": row_number, "reason": self._row_error(exc)})
                continue
            granularities[value.bucket_granularity] += 1
            applied += int(result["write_status"] == "applied")
            idempotent += int(result["write_status"] == "idempotent")
            if result["disposition"] == "accepted":
                accepted.append(result)
            else:
                quarantined.append(result)
        return {
            "connector_id": connector_id,
            "source_format": source_format,
            "source_timezone": source_timezone,
            "total_rows": len(rows),
            "accepted_rows": len(accepted),
            "quarantined_rows": len(quarantined),
            "rejected_rows": len(rejected),
            "applied": applied,
            "idempotent": idempotent,
            "granularity_counts": {
                key: granularities[key]
                for key in ("hour", "day")
                if granularities[key]
            },
            "records": accepted,
            "quarantined": quarantined,
            "rejected": rejected,
        }

    def ingest_listing_revision_record(
        self,
        tenant_id: str,
        *,
        connector_id: str,
        record: PullRecord,
    ) -> dict[str, Any]:
        payload = _ListingRevisionResourcePayload.model_validate(record.payload)
        source_updated_at = self._connector_time(record.source_version)
        asset = self.domain.register_asset(tenant_id, payload.asset)
        revision = self.domain.create_revision(
            tenant_id,
            ListingRevisionCreate(
                connector_id=connector_id,
                store_id=payload.store_id,
                item_id=payload.item_id,
                sku_id=payload.sku_id,
                revision_no=payload.revision_no,
                title=payload.title,
                main_image_asset_id=asset["asset_id"],
                sale_price=payload.sale_price,
                attributes=payload.attributes,
                active_from=payload.active_from,
                active_to=payload.active_to,
                source_updated_at=source_updated_at,
            ),
        )
        receipt = {
            "receipt_id": payload.source_receipt_id,
            "status": "confirmed",
            "connector_id": connector_id,
            "source_id": record.source_id,
            "source_version": source_updated_at.astimezone(UTC).isoformat(),
            "revision_id": revision["id"],
            "revision_no": revision["revision_no"],
            "applied_at": payload.applied_at.astimezone(UTC).isoformat(),
        }
        return {
            "write_status": revision["write_status"],
            "asset_write_status": asset["write_status"],
            "revision": revision,
            "receipt": receipt,
        }

    def ingest_metric_record(
        self,
        tenant_id: str,
        *,
        connector_id: str,
        record: PullRecord,
    ) -> dict[str, Any]:
        raw = dict(record.payload)
        raw["source_id"] = record.source_id
        raw.setdefault("data_as_of", record.source_version)
        value, reason_code = self._normalize_metric_row(
            tenant_id,
            connector_id=connector_id,
            raw=raw,
            source_zone=UTC,
        )
        if reason_code is not None:
            return self.domain.quarantine_metric_bucket(
                tenant_id, value, reason_code=reason_code
            )
        return self.domain.ingest_metric_bucket(tenant_id, value)

    def _normalize_metric_row(
        self,
        tenant_id: str,
        *,
        connector_id: str,
        raw: dict[str, Any],
        source_zone: tzinfo,
    ) -> tuple[TrafficMetricBucketUpsert, str | None]:
        normalized = self._canonical_columns(raw)
        granularity_raw = self._required(normalized, "bucket_granularity")
        granularity = _GRANULARITY_ALIASES.get(str(granularity_raw).casefold())
        if granularity is None:
            raise ValueError("bucket_granularity_invalid")
        metric_start = self._parse_time(
            self._required(normalized, "metric_start"), source_zone
        )
        if granularity == "hour":
            if metric_start.minute or metric_start.second or metric_start.microsecond:
                raise ValueError("hour_bucket_not_aligned")
            expected_end = metric_start + timedelta(hours=1)
        else:
            if (
                metric_start.hour
                or metric_start.minute
                or metric_start.second
                or metric_start.microsecond
            ):
                raise ValueError("day_bucket_not_aligned")
            expected_end = metric_start + timedelta(days=1)
        raw_end = normalized.get("metric_end")
        metric_end = (
            expected_end
            if raw_end in (None, "")
            else self._parse_time(raw_end, source_zone)
        )
        if metric_end.astimezone(UTC) != expected_end.astimezone(UTC):
            raise ValueError("metric_window_granularity_mismatch")
        raw_data_as_of = normalized.get("data_as_of")
        data_as_of = (
            metric_end
            if raw_data_as_of in (None, "")
            else self._parse_time(raw_data_as_of, source_zone)
        )
        if data_as_of.astimezone(UTC) < metric_end.astimezone(UTC):
            raise ValueError("data_as_of_before_metric_end")

        source_id = normalized.get("source_id")
        if source_id in (None, ""):
            source_id = self._derived_source_id(
                connector_id=connector_id,
                store_id=normalized.get("store_id"),
                item_id=normalized.get("item_id"),
                sku_id=normalized.get("sku_id"),
                metric_start=metric_start,
                bucket_granularity=granularity,
                traffic_source=normalized.get("traffic_source"),
            )
        value = TrafficMetricBucketUpsert.model_validate(
            {
                "listing_revision_id": self._optional(normalized, "listing_revision_id"),
                "connector_id": connector_id,
                "store_id": self._optional(normalized, "store_id"),
                "item_id": self._optional(normalized, "item_id"),
                "sku_id": self._optional(normalized, "sku_id"),
                "metric_start": metric_start,
                "metric_end": metric_end,
                "bucket_granularity": granularity,
                "traffic_source": self._required(normalized, "traffic_source"),
                "impressions": self._required(normalized, "impressions"),
                "clicks": self._required(normalized, "clicks"),
                "visitors": self._default(
                    normalized, "visitors", normalized.get("clicks")
                ),
                "favorites": self._default(normalized, "favorites", 0),
                "cart_adds": self._default(normalized, "cart_adds", 0),
                "orders": self._default(normalized, "orders", 0),
                "sales_amount": self._default(normalized, "sales_amount", "0"),
                "ad_spend": self._default(normalized, "ad_spend", "0"),
                "search_impressions": self._default(
                    normalized, "search_impressions", 0
                ),
                "recommend_impressions": self._default(
                    normalized, "recommend_impressions", 0
                ),
                "data_as_of": data_as_of,
                "source_id": str(source_id).strip(),
            }
        )
        if value.listing_revision_id is not None:
            self._validate_explicit_revision_identity(tenant_id, value)
            return value, None
        revision_id, reason_code = self._resolve_revision(tenant_id, value)
        if revision_id is not None:
            value = value.model_copy(update={"listing_revision_id": revision_id})
        return value, reason_code

    def _resolve_revision(
        self, tenant_id: str, value: TrafficMetricBucketUpsert
    ) -> tuple[str | None, str | None]:
        identity = (value.connector_id, value.store_id, value.item_id, value.sku_id)
        if any(item is None for item in identity):
            return None, "listing_revision_missing"
        metric_start = value.metric_start.astimezone(UTC).isoformat()
        metric_end = value.metric_end.astimezone(UTC).isoformat()
        with self.db.connect() as conn:
            revisions = conn.execute(
                """
                SELECT id, active_from, active_to FROM listing_revisions
                WHERE tenant_id=? AND connector_id=? AND store_id=?
                  AND item_id=? AND sku_id=?
                ORDER BY revision_no ASC
                """,
                (tenant_id, *identity),
            ).fetchall()
        if not revisions:
            return None, "listing_revision_not_found"
        matches = [
            row
            for row in revisions
            if str(row["active_from"]) <= metric_start
            and (row["active_to"] is None or str(row["active_to"]) >= metric_end)
        ]
        if not matches:
            return None, "metric_outside_revision_window"
        if len(matches) > 1:
            return None, "listing_revision_ambiguous"
        return str(matches[0]["id"]), None

    def _validate_explicit_revision_identity(
        self, tenant_id: str, value: TrafficMetricBucketUpsert
    ) -> None:
        with self.db.connect() as conn:
            revision = conn.execute(
                """
                SELECT connector_id, store_id, item_id, sku_id
                FROM listing_revisions WHERE tenant_id=? AND id=?
                """,
                (tenant_id, value.listing_revision_id),
            ).fetchone()
        if revision is None:
            return
        for field in ("connector_id", "store_id", "item_id", "sku_id"):
            supplied = getattr(value, field)
            if supplied is not None and supplied != str(revision[field]):
                raise ValueError("listing_revision_identity_mismatch")

    @staticmethod
    def _rows_from_content(
        source_format: MetricImportFormat, content: str
    ) -> list[dict[str, Any]]:
        if source_format == "csv":
            reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
            if not reader.fieldnames:
                raise ValueError("traffic_csv_header_missing")
            return [dict(row) for row in reader]
        if source_format != "json":
            raise ValueError("traffic_import_format_invalid")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("traffic_json_invalid") from exc
        if isinstance(payload, dict):
            payload = payload.get("records")
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise ValueError("traffic_json_records_missing")
        return payload

    @staticmethod
    def _canonical_columns(raw: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for raw_key, raw_value in raw.items():
            key = str(raw_key).lstrip("\ufeff").strip().casefold()
            field = _COLUMN_ALIASES.get(key)
            if field is None:
                continue
            value = raw_value.strip() if isinstance(raw_value, str) else raw_value
            result[field] = value
        return result

    @staticmethod
    def _parse_time(value: Any, source_zone: tzinfo) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, time.min)
        else:
            text = str(value).strip()
            if not text:
                raise ValueError("datetime_required")
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("datetime_invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=source_zone)
        return parsed

    @staticmethod
    def _connector_time(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("connector_source_version_invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("connector_source_version_timezone_required")
        return parsed

    @staticmethod
    def _source_zone(value: str) -> ZoneInfo:
        try:
            return ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("source_timezone_invalid") from exc

    @staticmethod
    def _derived_source_id(
        *,
        connector_id: str,
        store_id: Any,
        item_id: Any,
        sku_id: Any,
        metric_start: datetime,
        bucket_granularity: str,
        traffic_source: Any,
    ) -> str:
        digest = payload_digest(
            {
                "connector_id": connector_id,
                "store_id": store_id,
                "item_id": item_id,
                "sku_id": sku_id,
                "metric_start": metric_start.astimezone(UTC).isoformat(),
                "bucket_granularity": bucket_granularity,
                "traffic_source": traffic_source,
            }
        )
        return f"traffic-import-{digest}"

    @staticmethod
    def _required(values: dict[str, Any], field: str) -> Any:
        value = values.get(field)
        if value in (None, ""):
            raise ValueError(f"missing_field:{field}")
        return value

    @staticmethod
    def _optional(values: dict[str, Any], field: str) -> Any | None:
        value = values.get(field)
        return None if value in (None, "") else value

    @staticmethod
    def _default(values: dict[str, Any], field: str, default: Any) -> Any:
        value = values.get(field)
        return default if value in (None, "") else value

    @staticmethod
    def _row_error(exc: Exception) -> str:
        if isinstance(exc, ValidationError):
            first = exc.errors()[0]
            location = ".".join(str(item) for item in first["loc"]) or "row"
            return f"{location}:{first['msg']}"
        return str(exc)
