from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Any, Iterable, Mapping, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..business.source_versioning import canonical_source_time
from ..storage_refs import validate_controlled_storage_ref
from ..text_utils import redact_sensitive


_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
_CANONICAL_FIELD_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.]*$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?<!\d)1[3-9]\d(?:[\s-]*\d){8}(?!\d)"),
    re.compile(
        r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+"
        r"@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}(?![A-Za-z0-9.-])"
    ),
    re.compile(
        r"(?:收货地址|收件地址|配送地址|寄送地址|联系地址|客户地址|顾客地址|"
        r"买家地址|地址|shipping address|delivery address|customer address)"
        r"(?:\s*[:：=]\s*|\s+)\S+",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:顾客姓名|客户姓名|买家姓名|收货人姓名|收件人姓名|联系人姓名|姓名|"
        r"customer name|buyer name|recipient name|contact name)"
        r"(?:\s*[:：=]\s*|\s+)\S+",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:邮政编码|邮编|postal code|postcode|zip code)"
        r"(?:\s*[:：=]\s*|\s+)\d{5,10}",
        re.IGNORECASE,
    ),
)
_T = TypeVar("_T")


class _FrozenDict(dict[str, _T]):
    __slots__ = ()

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("readonly_mapping_is_immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable

    def __ior__(self, _other: object) -> Self:
        raise TypeError("readonly_mapping_is_immutable")

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> Self:
        return self


class SourceKind(StrEnum):
    ACTUAL = "actual"
    MANUAL = "manual"
    DEMO = "demo"


class EvidenceState(StrEnum):
    ACTUAL = "actual"
    MANUAL = "manual"
    DEMO = "demo"
    MISSING = "missing"


class DataScope(StrEnum):
    OPERATIONAL = "operational"
    DEMO = "demo"
    ALL = "all"


class ReferenceKind(StrEnum):
    RAW_FILE = "raw_file"
    SOURCE_RECEIPT = "source_receipt"


class RowDisposition(StrEnum):
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


class QualityStatus(StrEnum):
    PASSED = "passed"
    PARTIAL = "partial"
    FAILED = "failed"


def _normalized_field_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and character not in {"_", "-"}
    )


SENSITIVE_FIELD_NAMES = frozenset(
    _normalized_field_name(value)
    for value in {
        "customer_name",
        "buyer_name",
        "buyer_nick",
        "recipient_name",
        "receiver_name",
        "consignee_name",
        "contact_name",
        "full_name",
        "phone",
        "phone_number",
        "mobile",
        "mobile_number",
        "customer_phone",
        "buyer_phone",
        "recipient_phone",
        "receiver_phone",
        "contact_phone",
        "telephone",
        "identity_number",
        "identity_card",
        "identity_card_number",
        "id_number",
        "id_card",
        "national_id",
        "passport",
        "passport_number",
        "email",
        "email_address",
        "customer_email",
        "buyer_email",
        "recipient_email",
        "receiver_email",
        "contact_email",
        "postal_code",
        "zip_code",
        "postcode",
        "shipping_postal_code",
        "delivery_postal_code",
        "bank_card",
        "bank_card_number",
        "address",
        "full_address",
        "shipping_address",
        "delivery_address",
        "recipient_address",
        "receiver_address",
        "customer_address",
        "detailed_address",
        "顾客姓名",
        "客户姓名",
        "买家姓名",
        "买家昵称",
        "收货人",
        "收件人",
        "联系人",
        "姓名",
        "手机号",
        "手机号码",
        "电话",
        "联系电话",
        "收货电话",
        "买家电话",
        "地址",
        "收货地址",
        "详细地址",
        "联系地址",
        "收货省",
        "收货市",
        "收货区",
        "旺旺名",
        "身份证",
        "身份证号",
        "身份证号码",
        "证件号",
        "证件号码",
        "护照",
        "护照号",
        "护照号码",
        "邮箱",
        "电子邮箱",
        "邮件地址",
        "邮政编码",
        "邮编",
        "银行卡",
        "银行卡号",
    }
)


def _require_safe_code(value: str, *, error: str, max_length: int = 256) -> str:
    if value != value.strip() or not value or len(value) > max_length or not _CODE_PATTERN.fullmatch(value):
        raise ValueError(error)
    return value


def _require_safe_label(value: str, *, error: str, max_length: int = 256) -> str:
    if value != value.strip() or not value or len(value) > max_length:
        raise ValueError(error)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(error)
    return value


def _normalize_digest(value: str, *, error: str) -> str:
    normalized = value.lower()
    if not _DIGEST_PATTERN.fullmatch(normalized):
        raise ValueError(error)
    return normalized


def _require_aware(value: datetime) -> datetime:
    canonical_source_time(value)
    return value


def content_digest(content: bytes | bytearray | memoryview) -> str:
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise TypeError("readonly_content_must_be_bytes")
    return hashlib.sha256(bytes(content)).hexdigest()


def schema_fingerprint(field_names: Iterable[str]) -> str:
    normalized = [unicodedata.normalize("NFKC", name).strip().casefold() for name in field_names]
    if not normalized or any(not name for name in normalized):
        raise ValueError("readonly_schema_fields_required")
    if len(set(normalized)) != len(normalized):
        raise ValueError("duplicate_readonly_schema_field")
    encoded = json.dumps(sorted(normalized), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _require_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("readonly_nested_or_nonfinite_value_not_allowed")


def _contains_sensitive_value(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value)
    if redact_sensitive(normalized)[1]:
        return True
    return any(pattern.search(normalized) is not None for pattern in _SENSITIVE_VALUE_PATTERNS)


class ReportFieldPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_type: str = Field(min_length=1, max_length=128)
    mapping_version: str = Field(min_length=1, max_length=128)
    field_aliases: dict[str, str] = Field(default_factory=dict)
    allowed_fields: frozenset[str] = Field(min_length=1)
    required_fields: frozenset[str] = Field(default_factory=frozenset)
    sensitive_fields: frozenset[str] = Field(default_factory=frozenset)

    @field_validator("report_type")
    @classmethod
    def validate_report_type(cls, value: str) -> str:
        return _require_safe_code(value, error="invalid_report_type", max_length=128)

    @field_validator("mapping_version")
    @classmethod
    def validate_mapping_version(cls, value: str) -> str:
        return _require_safe_code(value, error="invalid_mapping_version", max_length=128)

    @field_validator("allowed_fields", "required_fields")
    @classmethod
    def validate_canonical_fields(cls, value: frozenset[str]) -> frozenset[str]:
        if any(not _CANONICAL_FIELD_PATTERN.fullmatch(field) for field in value):
            raise ValueError("invalid_canonical_field")
        return value

    @field_validator("sensitive_fields")
    @classmethod
    def validate_sensitive_fields(cls, value: frozenset[str]) -> frozenset[str]:
        for field in value:
            _require_safe_label(field, error="invalid_sensitive_field")
        return value

    @field_validator("field_aliases")
    @classmethod
    def validate_aliases(cls, value: dict[str, str]) -> dict[str, str]:
        normalized_aliases: set[str] = set()
        for alias, target in value.items():
            _require_safe_label(alias, error="invalid_report_field_alias")
            if not _CANONICAL_FIELD_PATTERN.fullmatch(target):
                raise ValueError("invalid_canonical_field")
            normalized = _normalized_field_name(alias)
            if not normalized:
                raise ValueError("invalid_report_field_alias")
            if normalized in normalized_aliases:
                raise ValueError("duplicate_report_field_alias")
            normalized_aliases.add(normalized)
        return _FrozenDict(value)

    @model_validator(mode="after")
    def validate_field_sets(self) -> Self:
        if not self.required_fields <= self.allowed_fields:
            raise ValueError("required_fields_not_allowlisted")
        if set(self.field_aliases.values()) - set(self.allowed_fields):
            raise ValueError("alias_target_not_allowlisted")
        sensitive = SENSITIVE_FIELD_NAMES | {
            _normalized_field_name(field) for field in self.sensitive_fields
        }
        if any(_normalized_field_name(field) in sensitive for field in self.allowed_fields):
            raise ValueError("sensitive_field_cannot_be_allowlisted")
        if any(_normalized_field_name(alias) in sensitive for alias in self.field_aliases):
            raise ValueError("sensitive_field_cannot_be_aliased")
        canonical_by_normalized: dict[str, str] = {}
        for field in sorted(self.allowed_fields):
            normalized = _normalized_field_name(field)
            existing = canonical_by_normalized.get(normalized)
            if existing is not None and existing != field:
                raise ValueError("ambiguous_canonical_field")
            canonical_by_normalized[normalized] = field
        for alias, target in self.field_aliases.items():
            canonical = canonical_by_normalized.get(_normalized_field_name(alias))
            if canonical is not None and canonical != target:
                raise ValueError("ambiguous_report_field_alias")
        return self


class SanitizedReportRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_type: str
    mapping_version: str
    payload: dict[str, Any]
    sensitive_fields_removed: int = Field(ge=0)
    sensitive_values_removed: int = Field(ge=0)
    non_allowlisted_fields_removed: int = Field(ge=0)

    @field_validator("payload")
    @classmethod
    def freeze_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _FrozenDict({key: _require_scalar(item) for key, item in value.items()})

    def downstream_payload(self) -> dict[str, Any]:
        return dict(self.payload)

    def log_projection(self) -> dict[str, Any]:
        accepted_fields = sorted(self.payload)
        return {
            "report_type": self.report_type,
            "mapping_version": self.mapping_version,
            "accepted_fields": accepted_fields,
            "accepted_field_count": len(accepted_fields),
            "sensitive_fields_removed": self.sensitive_fields_removed,
            "sensitive_values_removed": self.sensitive_values_removed,
            "non_allowlisted_fields_removed": self.non_allowlisted_fields_removed,
        }


def sanitize_report_row(
    policy: ReportFieldPolicy, raw_row: Mapping[str, Any]
) -> SanitizedReportRow:
    alias_lookup = {
        _normalized_field_name(alias): target for alias, target in policy.field_aliases.items()
    }
    canonical_lookup = {
        _normalized_field_name(field): field for field in policy.allowed_fields
    }
    sensitive = SENSITIVE_FIELD_NAMES | {
        _normalized_field_name(field) for field in policy.sensitive_fields
    }
    payload: dict[str, Any] = {}
    seen_canonical_fields: set[str] = set()
    sensitive_removed = 0
    sensitive_values_removed = 0
    non_allowlisted_removed = 0
    for raw_field, raw_value in raw_row.items():
        if not isinstance(raw_field, str):
            raise ValueError("readonly_report_field_name_must_be_text")
        normalized = _normalized_field_name(raw_field)
        if normalized in sensitive:
            sensitive_removed += 1
            continue
        canonical = alias_lookup.get(normalized) or canonical_lookup.get(normalized)
        if canonical is None:
            non_allowlisted_removed += 1
            continue
        if canonical in seen_canonical_fields:
            raise ValueError("duplicate_canonical_field")
        seen_canonical_fields.add(canonical)
        scalar = _require_scalar(raw_value)
        if isinstance(scalar, str) and _contains_sensitive_value(scalar):
            sensitive_values_removed += 1
            continue
        payload[canonical] = scalar
    missing = sorted(policy.required_fields - set(payload))
    if missing:
        raise ValueError(f"required_report_fields_missing:{','.join(missing)}")
    return SanitizedReportRow(
        report_type=policy.report_type,
        mapping_version=policy.mapping_version,
        payload=payload,
        sensitive_fields_removed=sensitive_removed,
        sensitive_values_removed=sensitive_values_removed,
        non_allowlisted_fields_removed=non_allowlisted_removed,
    )


class ReportContractRegistry:
    def __init__(self) -> None:
        self._contracts: dict[tuple[str, str], ReportFieldPolicy] = {}

    def register(self, policy: ReportFieldPolicy) -> None:
        key = (policy.report_type, policy.mapping_version)
        if key in self._contracts:
            raise ValueError("duplicate_report_contract")
        self._contracts[key] = policy

    def get(self, report_type: str, mapping_version: str) -> ReportFieldPolicy:
        try:
            return self._contracts[(report_type, mapping_version)]
        except KeyError as exc:
            raise ValueError("report_contract_not_found") from exc

    def list(self) -> list[ReportFieldPolicy]:
        return [self._contracts[key] for key in sorted(self._contracts)]


REPORT_CONTRACTS = ReportContractRegistry()


class ImportReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ReferenceKind
    reference: str = Field(min_length=1, max_length=2048)
    content_digest: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("content_digest")
    @classmethod
    def normalize_content_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_digest(value, error="invalid_content_digest")

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        if self.kind is ReferenceKind.RAW_FILE:
            if self.content_digest is None:
                raise ValueError("raw_file_digest_required")
            validate_controlled_storage_ref(
                self.reference,
                required_subpath="readonly-imports",
            )
        else:
            _require_safe_code(
                self.reference, error="source_receipt_reference_not_approved", max_length=512
            )
        return self


class ImportManifestInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    store_id: str = Field(min_length=1, max_length=128)
    source_kind: SourceKind
    source_system: str = Field(min_length=1, max_length=128)
    report_type: str = Field(min_length=1, max_length=128)
    report_period: str = Field(min_length=1, max_length=256)
    exported_at: datetime
    schema_fingerprint: str = Field(min_length=64, max_length=64)
    content_digest: str = Field(min_length=64, max_length=64)
    mapping_version: str = Field(min_length=1, max_length=128)
    parsed_rows: int = Field(ge=1)
    data_as_of: datetime | None = None
    references: tuple[ImportReference, ...] = Field(min_length=1, max_length=20)

    @field_validator("store_id", "source_system", "report_type", "mapping_version")
    @classmethod
    def validate_codes(cls, value: str) -> str:
        return _require_safe_code(value, error="invalid_import_manifest_code", max_length=128)

    @field_validator("report_period")
    @classmethod
    def validate_report_period(cls, value: str) -> str:
        return _require_safe_label(value, error="invalid_report_period")

    @field_validator("exported_at", "data_as_of")
    @classmethod
    def require_aware_times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value)

    @field_validator("schema_fingerprint")
    @classmethod
    def normalize_schema_fingerprint(cls, value: str) -> str:
        return _normalize_digest(value, error="invalid_schema_fingerprint")

    @field_validator("content_digest")
    @classmethod
    def normalize_manifest_digest(cls, value: str) -> str:
        return _normalize_digest(value, error="invalid_content_digest")

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if self.data_as_of is not None and self.data_as_of > self.exported_at:
            raise ValueError("data_as_of_after_export")
        raw_files = [
            reference for reference in self.references if reference.kind is ReferenceKind.RAW_FILE
        ]
        if not raw_files:
            raise ValueError("raw_file_reference_required")
        if any(reference.content_digest != self.content_digest for reference in raw_files):
            raise ValueError("raw_file_digest_mismatch")
        keys = [(reference.kind.value, reference.reference) for reference in self.references]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate_import_reference")
        return self


class RowIsolationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_number: int = Field(ge=1)
    disposition: RowDisposition
    reason: str = Field(min_length=1, max_length=128)
    field_keys: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    raw_row_digest: str = Field(min_length=64, max_length=64)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return _require_safe_code(value, error="invalid_row_issue_reason", max_length=128)

    @field_validator("field_keys")
    @classmethod
    def validate_field_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _CANONICAL_FIELD_PATTERN.fullmatch(field) for field in value):
            raise ValueError("invalid_row_issue_field")
        if len(value) != len(set(value)):
            raise ValueError("duplicate_row_issue_field")
        return tuple(sorted(value))

    @field_validator("raw_row_digest")
    @classmethod
    def normalize_raw_row_digest(cls, value: str) -> str:
        return _normalize_digest(value, error="invalid_raw_row_digest")


class DataQualitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: QualityStatus
    total_rows: int = Field(ge=1)
    accepted_rows: int = Field(ge=0)
    quarantined_rows: int = Field(ge=0)
    rejected_rows: int = Field(ge=0)
    issue_counts: dict[str, int] = Field(default_factory=dict)

    @field_validator("issue_counts")
    @classmethod
    def freeze_issue_counts(cls, value: dict[str, int]) -> dict[str, int]:
        return _FrozenDict(dict(sorted(value.items())))

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.total_rows != self.accepted_rows + self.quarantined_rows + self.rejected_rows:
            raise ValueError("data_quality_row_count_mismatch")
        if any(count <= 0 for count in self.issue_counts.values()):
            raise ValueError("invalid_quality_issue_count")
        return self


class FieldEvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    store_id: str = Field(min_length=1, max_length=128)
    field_key: str = Field(min_length=1, max_length=128)
    scope: str = Field(min_length=1, max_length=256)
    evidence_state: EvidenceState
    reason: str = Field(min_length=1, max_length=128)
    data_as_of: datetime | None = None
    source_reference: str | None = Field(default=None, max_length=2048)
    import_id: str | None = Field(default=None, max_length=128)

    @field_validator("store_id", "field_key", "scope", "reason")
    @classmethod
    def validate_evidence_codes(cls, value: str) -> str:
        return _require_safe_code(value, error="invalid_field_evidence_code")

    @field_validator("data_as_of")
    @classmethod
    def require_aware_data_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value)

    @field_validator("source_reference")
    @classmethod
    def validate_source_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_safe_code(
            value, error="source_reference_not_approved", max_length=512
        )

    @model_validator(mode="after")
    def validate_evidence_source(self) -> Self:
        if self.evidence_state is EvidenceState.MISSING:
            if self.import_id is not None or self.data_as_of is not None or self.source_reference is not None:
                raise ValueError("missing_evidence_cannot_reference_import")
        elif self.import_id is None:
            raise ValueError("evidence_import_required")
        return self


def project_evidenced_value(state: EvidenceState, value: _T | None) -> _T | None:
    state = EvidenceState(state)
    if state is EvidenceState.MISSING:
        if value is not None:
            raise ValueError("missing_evidence_must_not_have_value")
        return None
    if value is None:
        raise ValueError("evidenced_value_required")
    return value
