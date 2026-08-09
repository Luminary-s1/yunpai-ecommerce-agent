from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Self
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


BucketGranularity = Literal["hour", "day"]
ExperimentAssignment = Literal["control", "treatment"]
ExperimentType = Literal["aa", "platform_ab", "switchback", "difference_in_differences"]
_APPROVED_OBJECT_STORAGE_SCHEMES = frozenset({"cos", "oss", "s3"})
_LOCAL_OBJECT_PREFIX = "objects"


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_required")
    return value


class CreativeAssetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    mime_type: str = Field(min_length=1, max_length=128, pattern=r"^image/")
    width: int = Field(gt=0, le=100_000)
    height: int = Field(gt=0, le=100_000)
    storage_ref: str = Field(min_length=1, max_length=2048)
    source_ref: str | None = Field(default=None, max_length=2048)
    feature_schema_version: str = Field(min_length=1, max_length=128)

    @field_validator("sha256")
    @classmethod
    def normalize_sha256(cls, value: str) -> str:
        return value.lower()

    @field_validator("storage_ref")
    @classmethod
    def reject_embedded_credentials(cls, value: str) -> str:
        parsed = urlsplit(value)
        sensitive_keys = {
            "access_key",
            "access_token",
            "credential",
            "secret",
            "signature",
            "token",
            "x-amz-credential",
            "x-amz-signature",
        }
        query_keys = {key.lower() for key, _value in parse_qsl(parsed.query)}
        if parsed.username is not None or parsed.password is not None or query_keys & sensitive_keys:
            raise ValueError("storage_ref_credentials_forbidden")
        if value != value.strip() or parsed.query or parsed.fragment:
            raise ValueError("storage_ref_not_approved")
        if parsed.scheme:
            if (
                parsed.scheme.lower() not in _APPROVED_OBJECT_STORAGE_SCHEMES
                or not parsed.netloc
                or not parsed.path.strip("/")
            ):
                raise ValueError("storage_ref_not_approved")
            return value
        parts = value.split("/")
        if (
            parsed.netloc
            or "\\" in value
            or len(parts) < 2
            or parts[0] != _LOCAL_OBJECT_PREFIX
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("storage_ref_not_approved")
        return value


class ListingRevisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1, max_length=128)
    store_id: str = Field(min_length=1, max_length=128)
    item_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    revision_no: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=500)
    main_image_asset_id: str = Field(min_length=1, max_length=128)
    sale_price: Decimal = Field(ge=0)
    attributes: dict[str, Any] = Field(default_factory=dict)
    active_from: datetime
    active_to: datetime | None = None
    source_updated_at: datetime

    @field_validator("active_from", "active_to", "source_updated_at")
    @classmethod
    def require_aware_times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value)

    @model_validator(mode="after")
    def validate_active_window(self) -> Self:
        if self.active_to is not None and self.active_to <= self.active_from:
            raise ValueError("revision_active_window_invalid")
        return self


class TrafficMetricBucketUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    listing_revision_id: str | None = Field(default=None, min_length=1, max_length=128)
    metric_start: datetime
    metric_end: datetime
    bucket_granularity: BucketGranularity
    traffic_source: str = Field(min_length=1, max_length=128)
    impressions: int = Field(ge=0)
    clicks: int = Field(ge=0)
    visitors: int = Field(ge=0)
    favorites: int = Field(ge=0)
    cart_adds: int = Field(ge=0)
    orders: int = Field(ge=0)
    sales_amount: Decimal = Field(ge=0)
    ad_spend: Decimal = Field(ge=0)
    search_impressions: int = Field(ge=0)
    recommend_impressions: int = Field(ge=0)
    data_as_of: datetime
    source_id: str = Field(min_length=1, max_length=256)

    @field_validator("metric_start", "metric_end", "data_as_of")
    @classmethod
    def require_aware_times(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def validate_bucket(self) -> Self:
        if self.metric_end <= self.metric_start:
            raise ValueError("metric_window_invalid")
        if self.clicks > self.impressions:
            raise ValueError("clicks_cannot_exceed_impressions")
        return self


class TrafficExperimentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    experiment_type: ExperimentType
    primary_metric: str = Field(min_length=1, max_length=128)
    started_at: datetime
    ended_at: datetime | None = None
    control_revision_id: str = Field(min_length=1, max_length=128)
    treatment_revision_id: str = Field(min_length=1, max_length=128)
    minimum_exposure: int = Field(ge=0)
    washout_window: int = Field(ge=0, description="Washout duration in minutes")
    analysis_policy_version: str = Field(min_length=1, max_length=128)

    @field_validator("started_at", "ended_at")
    @classmethod
    def require_aware_times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value)

    @model_validator(mode="after")
    def validate_experiment_window(self) -> Self:
        if self.ended_at is not None and self.ended_at <= self.started_at:
            raise ValueError("experiment_window_invalid")
        return self


class TrafficExperimentTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "running", "completed", "paused", "invalid"]
    ended_at: datetime | None = None
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("ended_at")
    @classmethod
    def require_aware_end(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value)


class TrafficExperimentWindowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    listing_revision_id: str = Field(min_length=1, max_length=128)
    window_start: datetime
    window_end: datetime
    assignment: ExperimentAssignment
    washout: bool = False
    source_receipt_id: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("window_start", "window_end")
    @classmethod
    def require_aware_times(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.window_end <= self.window_start:
            raise ValueError("experiment_window_invalid")
        return self


class TrafficAnalysisRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str = Field(min_length=1, max_length=128)
    data_window: dict[str, Any]
    sample_size: dict[str, Any]
    effect_estimate: dict[str, Any]
    confidence_interval: dict[str, Any]
    evidence: dict[str, Any]
    counter_evidence: dict[str, Any]
    hypotheses: dict[str, Any]
    model_provider: str | None = Field(default=None, max_length=128)
    model_name: str | None = Field(default=None, max_length=256)
    prompt_version: str | None = Field(default=None, max_length=128)
    analysis_code_version: str = Field(min_length=1, max_length=128)
