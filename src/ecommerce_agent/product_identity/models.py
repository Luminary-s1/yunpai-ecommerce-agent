from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..readonly_data.contracts import DataScope, SourceKind


PRODUCT_IDENTITY_POLICY_VERSION = "product-identity-v1"
MAX_RECONCILIATION_ROWS = 50_000

_SAFE_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")


class MappingEventType(StrEnum):
    CONFIRMED = "confirmed"
    REVOKED = "revoked"


class MatchEvidence(StrEnum):
    """Stable evidence keys emitted with every reconciliation row."""

    CONFIRMED_MAPPING = "confirmed_mapping"
    REVOKED_MAPPING = "revoked_mapping"
    SKU_ID_EXACT = "sku_id_exact"
    ITEM_ID_EXACT = "item_id_exact"
    MERCHANT_CODE_EXACT = "merchant_code_exact"
    TITLE_EXACT = "title_exact"
    CONFLICTING_SIGNALS = "conflicting_signals"
    MANUAL_CONFIRMATION_REQUIRED = "manual_confirmation_required"
    NO_CANDIDATE = "no_candidate"
    INVALID_OBSERVATION = "invalid_observation"
    STORE_SCOPE_CONFLICT = "store_scope_conflict"


class ReconciliationStatus(StrEnum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    UNMAPPED = "unmapped"
    REJECTED = "rejected"


class ObservationDomain(StrEnum):
    CATALOG = "catalog"
    INVENTORY = "inventory"
    ORDER = "order"
    UNKNOWN = "unknown"


def _safe_code(value: str, *, error: str, max_length: int = 256) -> str:
    if (
        not value
        or value != value.strip()
        or len(value) > max_length
        or _SAFE_CODE.fullmatch(value) is None
    ):
        raise ValueError(error)
    return value


def _trimmed_label(value: str, *, error: str, max_length: int) -> str:
    if not value or value != value.strip() or len(value) > max_length:
        raise ValueError(error)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(error)
    return value


class CanonicalProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    store_id: str = Field(min_length=1, max_length=128)
    internal_part_number: str = Field(min_length=1, max_length=128)
    merchant_code: str | None = Field(default=None, min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    source_kind: SourceKind
    source_reference: str = Field(min_length=1, max_length=512)

    @field_validator("store_id", "internal_part_number", "merchant_code")
    @classmethod
    def validate_identity_labels(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _trimmed_label(value, error="invalid_product_identity_label", max_length=128)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _trimmed_label(value, error="invalid_canonical_product_title", max_length=500)

    @field_validator("source_reference")
    @classmethod
    def validate_source_reference(cls, value: str) -> str:
        return _safe_code(
            value,
            error="invalid_product_source_reference",
            max_length=512,
        )


class MappingDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    store_id: str = Field(min_length=1, max_length=128)
    connector_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    item_id: str | None = Field(default=None, min_length=1, max_length=128)
    merchant_code: str | None = Field(default=None, min_length=1, max_length=128)
    canonical_product_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=0)
    decision_key: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=128)
    actor_ref: str = Field(min_length=1, max_length=128)
    source_import_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator(
        "store_id",
        "connector_id",
        "sku_id",
        "item_id",
        "merchant_code",
        "canonical_product_id",
    )
    @classmethod
    def validate_identity_labels(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _trimmed_label(value, error="invalid_mapping_identity", max_length=128)

    @field_validator("decision_key", "reason", "actor_ref", "source_import_id")
    @classmethod
    def validate_audit_codes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _safe_code(value, error="invalid_mapping_audit_code", max_length=128)


class MappingRevocationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    store_id: str = Field(min_length=1, max_length=128)
    connector_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=1)
    decision_key: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=128)
    actor_ref: str = Field(min_length=1, max_length=128)

    @field_validator("store_id", "connector_id", "sku_id")
    @classmethod
    def validate_identity_labels(cls, value: str) -> str:
        return _trimmed_label(value, error="invalid_mapping_identity", max_length=128)

    @field_validator("decision_key", "reason", "actor_ref")
    @classmethod
    def validate_audit_codes(cls, value: str) -> str:
        return _safe_code(value, error="invalid_mapping_audit_code", max_length=128)


class ProductReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    store_id: str = Field(min_length=1, max_length=128)
    scope: DataScope = DataScope.OPERATIONAL
    # Keep malformed rows in the batch so reconciliation can persist a
    # rejected terminal row instead of failing the whole report at validation.
    observations: tuple[Any, ...] = Field(
        min_length=1,
        max_length=MAX_RECONCILIATION_ROWS,
    )

    @field_validator("store_id")
    @classmethod
    def validate_store_id(cls, value: str) -> str:
        return _trimmed_label(value, error="invalid_mapping_identity", max_length=128)


class ProductIdentityObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_domain: ObservationDomain
    source_reference: str = Field(min_length=1, max_length=512)
    store_id: str = Field(min_length=1, max_length=128)
    connector_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    item_id: str | None = Field(default=None, min_length=1, max_length=128)
    merchant_code: str | None = Field(default=None, min_length=1, max_length=128)
    title: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("store_id", "connector_id", "sku_id", "item_id", "merchant_code")
    @classmethod
    def validate_identity_labels(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _trimmed_label(value, error="invalid_mapping_identity", max_length=128)

    @field_validator("source_reference")
    @classmethod
    def validate_source_reference(cls, value: str) -> str:
        return _safe_code(
            value,
            error="invalid_observation_source_reference",
            max_length=512,
        )

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _trimmed_label(value, error="invalid_observation_title", max_length=500)


__all__ = [
    "MAX_RECONCILIATION_ROWS",
    "PRODUCT_IDENTITY_POLICY_VERSION",
    "CanonicalProductCreate",
    "MappingDecisionInput",
    "MappingEventType",
    "MatchEvidence",
    "MappingRevocationInput",
    "ObservationDomain",
    "ProductIdentityObservation",
    "ProductReconciliationRequest",
    "ReconciliationStatus",
]
