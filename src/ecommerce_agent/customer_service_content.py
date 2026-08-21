from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .customer_service_contracts import (
    CUSTOMER_SERVICE_FIELD_POLICY,
    CUSTOMER_SERVICE_KEYWORD_CATEGORY,
    CUSTOMER_SERVICE_MAPPING_VERSION,
    CUSTOMER_SERVICE_REPORT_TYPE,
    CUSTOMER_SERVICE_SCRIPT_CATEGORY,
    CUSTOMER_SERVICE_SOURCE_PREFIX,
    canonical_customer_service_field_name,
)
from .database import Database
from .knowledge_management import (
    KnowledgeCreateRequest,
    KnowledgeManagementService,
    KnowledgeReviseRequest,
)
from .rag import KnowledgeBase
from .readonly_data import (
    ImportManifestInput,
    ReadonlyDataService,
    RowDisposition,
    RowIsolationIssue,
    content_digest,
    sanitize_report_row,
)
from .text_utils import normalize_text


class CustomerServiceContentType(StrEnum):
    SCRIPT = "script"
    KEYWORD = "keyword"


class CustomerServiceContentImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: ImportManifestInput
    rows: tuple[dict[str, Any], ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_row_count(self) -> "CustomerServiceContentImportRequest":
        if self.manifest.report_type != CUSTOMER_SERVICE_REPORT_TYPE:
            raise ValueError("customer_service_report_type_required")
        if self.manifest.mapping_version != CUSTOMER_SERVICE_MAPPING_VERSION:
            raise ValueError("unsupported_customer_service_mapping_version")
        if self.manifest.parsed_rows != len(self.rows):
            raise ValueError("customer_service_row_count_mismatch")
        return self


class CustomerServiceContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(min_length=1, max_length=4000)
    store_id: str = Field(min_length=1, max_length=128)
    sku_id: str | None = Field(default=None, max_length=128)
    scenario: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    now: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("now")
    @classmethod
    def require_aware_now(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("customer_service_context_now_must_be_aware")
        return value


class CustomerServiceContentService:
    """M8-R WP1 adapter over M7-R imports and the existing knowledge lifecycle."""

    def __init__(
        self,
        *,
        db: Database,
        readonly_data: ReadonlyDataService,
        knowledge: KnowledgeBase,
        lifecycle: KnowledgeManagementService,
    ) -> None:
        self.db = db
        self.readonly_data = readonly_data
        self.knowledge = knowledge
        self.lifecycle = lifecycle

    @staticmethod
    def canonical_raw_row(row: dict[str, Any]) -> bytes:
        return json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")

    def import_content(
        self,
        tenant_id: str,
        request: CustomerServiceContentImportRequest,
        *,
        actor: str,
    ) -> dict[str, Any]:
        parsed: list[dict[str, Any]] = []
        issues: list[RowIsolationIssue] = []
        sanitization = {
            "non_allowlisted_fields_removed": 0,
            "sensitive_fields_removed": 0,
            "sensitive_values_removed": 0,
        }
        for position, row in enumerate(request.rows, start=1):
            raw_digest = content_digest(self.canonical_raw_row(row))
            hidden_fields = self._hidden_fields(row.get("hidden_fields", ()))
            business_row = {
                field: value for field, value in row.items() if field != "hidden_fields"
            }
            try:
                sanitized = sanitize_report_row(
                    CUSTOMER_SERVICE_FIELD_POLICY,
                    business_row,
                )
                sanitization["non_allowlisted_fields_removed"] += (
                    sanitized.non_allowlisted_fields_removed
                )
                sanitization["sensitive_fields_removed"] += (
                    sanitized.sensitive_fields_removed
                )
                sanitization["sensitive_values_removed"] += (
                    sanitized.sensitive_values_removed
                )
                parsed.append(
                    self._parse_row(
                        sanitized.downstream_payload(),
                        default_row_number=position,
                        manifest_store_id=request.manifest.store_id,
                        raw_row_digest=raw_digest,
                        hidden_fields=hidden_fields,
                    )
                )
            except ValueError as exc:
                if not isinstance(exc, _RowRejected):
                    exc = _RowRejected(
                        position,
                        str(exc).partition(":")[0] or "customer_service_row_sanitization_failed",
                        (),
                    )
                issues.append(
                    RowIsolationIssue(
                        row_number=position,
                        disposition=exc.disposition,
                        reason=exc.reason,
                        field_keys=tuple(exc.field_keys),
                        raw_row_digest=raw_digest,
                    )
                )

        import_result = self.readonly_data.record_import(
            tenant_id,
            request.manifest,
            row_issues=issues,
        )
        candidates = [
            self._upsert_candidate(
                tenant_id,
                import_id=import_result["import_id"],
                value=value,
                actor=actor,
            )
            for value in parsed
        ]
        return {
            "import": import_result,
            "candidates": candidates,
            "sanitization": sanitization,
            "inert_external_content": True,
        }

    def get_trace(self, tenant_id: str, item_id: str) -> dict[str, Any]:
        item = self.lifecycle.get_item(tenant_id, item_id)
        if item is None or not str(item["source"]).startswith(
            CUSTOMER_SERVICE_SOURCE_PREFIX
        ):
            raise ValueError("customer_service_content_not_found")
        metadata = self._source_metadata(str(item["source"]))
        imported = self.readonly_data.get_import(tenant_id, metadata["import_id"])
        raw_reference = next(
            reference["reference"]
            for reference in imported["references"]
            if reference["kind"] == "raw_file"
        )
        return {
            "item_id": item["id"],
            "knowledge_key": item["knowledge_key"],
            "content_type": metadata["content_type"],
            "import_id": metadata["import_id"],
            "row_number": metadata["row_number"],
            "raw_row_digest": metadata["raw_row_digest"],
            "source_reference": raw_reference,
            "normalized_question": item["question"],
            "approved_answer": item["answer"],
            "status": item["status"],
            "review_status": item["review_status"],
            "approved_by": item["approved_by"],
            "effective_from": item["effective_from"],
            "effective_to": item["effective_to"],
            "executable_content_processed": False,
        }

    def build_context(
        self,
        tenant_id: str,
        request: CustomerServiceContextRequest,
    ) -> dict[str, Any]:
        question = normalize_text(request.question)
        now = request.now.astimezone(UTC).isoformat()
        scope_sql, scope_params = self._scope_sql(request.store_id, request.sku_id)
        scenario_sql = ""
        scenario_params: list[Any] = []
        if request.scenario is not None:
            scenario_sql = "AND intent=?"
            scenario_params.append(self._intent(request.scenario))
        specificity_sql = ""
        specificity_params: list[Any] = []
        if request.sku_id is not None:
            specificity_sql = "CASE WHEN sku_id=? THEN 0 ELSE 1 END,"
            specificity_params.append(request.sku_id)
        with self.db.connect() as conn:
            scripts = conn.execute(
                f"""
                SELECT id, knowledge_key, question, answer, source, version, intent,
                       risk_level, store_id, sku_id, approved_by, effective_from,
                       effective_to, record_version
                FROM knowledge
                WHERE tenant_id=? AND category=? AND status='active'
                  AND review_status='approved' AND approved_by IS NOT NULL
                  AND effective_from <= ? AND (effective_to IS NULL OR effective_to > ?)
                  {scope_sql} {scenario_sql}
                ORDER BY {specificity_sql} version DESC, updated_at DESC
                """,
                (
                    tenant_id,
                    CUSTOMER_SERVICE_SCRIPT_CATEGORY,
                    now,
                    now,
                    *scope_params,
                    *scenario_params,
                    *specificity_params,
                ),
            ).fetchall()
            keyword_rows = conn.execute(
                f"""
                SELECT id, keywords, intent, risk_level, source, version, store_id, sku_id
                FROM knowledge
                WHERE tenant_id=? AND category=? AND status='active'
                  AND review_status='approved' AND approved_by IS NOT NULL
                  AND effective_from <= ? AND (effective_to IS NULL OR effective_to > ?)
                  {scope_sql} {scenario_sql}
                ORDER BY {specificity_sql} version DESC, updated_at DESC
                """,
                (
                    tenant_id,
                    CUSTOMER_SERVICE_KEYWORD_CATEGORY,
                    now,
                    now,
                    *scope_params,
                    *scenario_params,
                    *specificity_params,
                ),
            ).fetchall()

        script_items = [dict(row) for row in scripts]
        exact = []
        if request.scenario is not None:
            exact = [
                item
                for item in script_items
                if normalize_text(item["question"]) == question
            ]
        keyword_signals = []
        lowered_question = question.lower()
        for row in keyword_rows:
            keyword = normalize_text(str(row["keywords"]))
            if keyword and keyword.lower() in lowered_question:
                keyword_signals.append(
                    {
                        "knowledge_id": row["id"],
                        "keyword": keyword,
                        "scenario": self._scenario(str(row["intent"])),
                        "risk_level": row["risk_level"],
                        "authority": "advisory_only",
                        "source": row["source"],
                        "version": row["version"],
                    }
                )
        exact_item = exact[0] if exact else None
        return {
            "normalized_question": question,
            "scripts": script_items,
            "keyword_signals": keyword_signals,
            "exact_approved_answer": exact_item,
            "fast_path_eligible": bool(
                exact_item
                and str(exact_item["source"]).startswith(
                    CUSTOMER_SERVICE_SOURCE_PREFIX
                )
            ),
            "fast_path_rule": "human_approved_immutable_exact_normalized_match_only",
            "keyword_authority": "advisory_only",
            "exclusions": {
                "unapproved": True,
                "retired": True,
                "expired": True,
                "cross_store": True,
            },
        }

    def _parse_row(
        self,
        row: dict[str, Any],
        *,
        default_row_number: int,
        manifest_store_id: str,
        raw_row_digest: str,
        hidden_fields: set[str],
    ) -> dict[str, Any]:
        row_number = self._row_number(row.get("row_number"), default_row_number)
        try:
            content_type = CustomerServiceContentType(str(row.get("content_type", "")))
        except ValueError as exc:
            raise _RowRejected(row_number, "unsupported_content_type", ("content_type",)) from exc
        required = (
            {"question", "answer"}
            if content_type is CustomerServiceContentType.SCRIPT
            else {"keyword"}
        )
        hidden_required = sorted(required & hidden_fields)
        if hidden_required:
            raise _RowRejected(
                row_number,
                "hidden_required_field",
                tuple(hidden_required),
                RowDisposition.QUARANTINED,
            )
        store_id = normalize_text(str(row.get("store_id") or manifest_store_id))
        if store_id != manifest_store_id:
            raise _RowRejected(row_number, "row_store_scope_conflict", ("store_id",))
        scenario = normalize_text(str(row.get("scenario", "")))
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", scenario):
            raise _RowRejected(row_number, "scenario_required", ("scenario",))
        risk_level = normalize_text(str(row.get("risk_level") or "low")).lower()
        if risk_level not in {"low", "medium", "high"}:
            raise _RowRejected(row_number, "invalid_risk_level", ("risk_level",))
        effective_from = self._optional_datetime(
            row.get("effective_from"), "effective_from", row_number
        )
        effective_to = self._optional_datetime(
            row.get("effective_to"), "effective_to", row_number
        )
        if effective_from and effective_to and effective_to <= effective_from:
            raise _RowRejected(
                row_number,
                "invalid_effective_period",
                ("effective_from", "effective_to"),
            )
        sku_id = normalize_text(str(row.get("sku_id", ""))) or None
        if content_type is CustomerServiceContentType.SCRIPT:
            question = normalize_text(str(row.get("question", "")))
            answer = normalize_text(str(row.get("answer", "")))
            if not (2 <= len(question) <= 500) or not (2 <= len(answer) <= 2000):
                raise _RowRejected(
                    row_number,
                    "script_question_answer_required",
                    tuple(sorted(required)),
                )
            keyword = normalize_text(str(row.get("keyword", "")))
            if len(keyword) > 500:
                raise _RowRejected(row_number, "keyword_too_long", ("keyword",))
        else:
            keyword = normalize_text(str(row.get("keyword", "")))
            if not (1 <= len(keyword) <= 500):
                raise _RowRejected(row_number, "keyword_required", ("keyword",))
            question = f"signal:{keyword}"
            answer = "Non-authoritative customer-service context signal only."
        return {
            "row_number": row_number,
            "content_type": content_type.value,
            "scenario": scenario,
            "question": question,
            "answer": answer,
            "keyword": keyword,
            "risk_level": risk_level,
            "store_id": store_id,
            "sku_id": sku_id,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "raw_row_digest": raw_row_digest,
        }

    def _upsert_candidate(
        self,
        tenant_id: str,
        *,
        import_id: str,
        value: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        knowledge_key = self._knowledge_key(value)
        source = self._source(
            import_id=import_id,
            row_number=value["row_number"],
            raw_row_digest=value["raw_row_digest"],
            content_type=value["content_type"],
        )
        with self.db.connect() as conn:
            existing_source = conn.execute(
                """
                SELECT id FROM knowledge
                WHERE tenant_id=? AND knowledge_key=? AND source=?
                ORDER BY version DESC LIMIT 1
                """,
                (tenant_id, knowledge_key, source),
            ).fetchone()
            latest = conn.execute(
                """
                SELECT id, record_version FROM knowledge
                WHERE tenant_id=? AND knowledge_key=?
                ORDER BY version DESC, updated_at DESC LIMIT 1
                """,
                (tenant_id, knowledge_key),
            ).fetchone()
        if existing_source is not None:
            item = self.lifecycle.get_item(tenant_id, str(existing_source["id"]))
            if item is None:
                raise ValueError("customer_service_candidate_not_found")
            return item
        category = (
            CUSTOMER_SERVICE_SCRIPT_CATEGORY
            if value["content_type"] == CustomerServiceContentType.SCRIPT.value
            else CUSTOMER_SERVICE_KEYWORD_CATEGORY
        )
        if latest is not None:
            return self.lifecycle.revise(
                tenant_id,
                str(latest["id"]),
                KnowledgeReviseRequest(
                    expected_record_version=int(latest["record_version"]),
                    question=value["question"],
                    answer=value["answer"],
                    keywords=value["keyword"],
                    source=source,
                    effective_from=value["effective_from"],
                    effective_to=value["effective_to"],
                ),
                actor,
            )
        layer = "product" if value["sku_id"] else "store"
        return self.lifecycle.create(
            tenant_id,
            KnowledgeCreateRequest(
                category=category,
                intent=self._intent(value["scenario"]),
                question=value["question"],
                answer=value["answer"],
                keywords=value["keyword"],
                risk_level=value["risk_level"],
                source=source,
                layer=layer,
                store_id=value["store_id"],
                sku_id=value["sku_id"],
                effective_from=value["effective_from"],
                effective_to=value["effective_to"],
            ),
            actor,
            knowledge_key=knowledge_key,
        )

    @staticmethod
    def _scope_sql(store_id: str, sku_id: str | None) -> tuple[str, list[Any]]:
        if sku_id is None:
            return "AND store_id=? AND sku_id IS NULL", [store_id]
        return "AND store_id=? AND (sku_id IS NULL OR sku_id=?)", [store_id, sku_id]

    @staticmethod
    def _intent(scenario: str) -> str:
        return f"m8r.customer_service.{scenario}"

    @staticmethod
    def _scenario(intent: str) -> str:
        return intent.removeprefix("m8r.customer_service.")

    @staticmethod
    def _knowledge_key(value: dict[str, Any]) -> str:
        identity = "|".join(
            (
                value["content_type"],
                value["store_id"],
                value["sku_id"] or "*",
                value["scenario"],
                value["question"],
            )
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        return f"m8r-customer-service:{value['content_type']}:{digest}"

    @staticmethod
    def _source(
        *,
        import_id: str,
        row_number: int,
        raw_row_digest: str,
        content_type: str,
    ) -> str:
        payload = json.dumps(
            {
                "content_type": content_type,
                "import_id": import_id,
                "raw_row_digest": raw_row_digest,
                "row_number": row_number,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"{CUSTOMER_SERVICE_SOURCE_PREFIX}{payload}"

    @staticmethod
    def _source_metadata(source: str) -> dict[str, Any]:
        try:
            value = json.loads(source.removeprefix(CUSTOMER_SERVICE_SOURCE_PREFIX))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_customer_service_source_trace") from exc
        required = {"content_type", "import_id", "raw_row_digest", "row_number"}
        if not isinstance(value, dict) or not required.issubset(value):
            raise ValueError("invalid_customer_service_source_trace")
        return value

    @staticmethod
    def _row_number(value: Any, default: int) -> int:
        try:
            row_number = int(value if value is not None else default)
        except (TypeError, ValueError) as exc:
            raise _RowRejected(default, "invalid_row_number", ("row_number",)) from exc
        if row_number < 1:
            raise _RowRejected(default, "invalid_row_number", ("row_number",))
        return row_number

    @staticmethod
    def _hidden_fields(value: Any) -> set[str]:
        if not isinstance(value, (list, tuple, set, frozenset)):
            return set()
        return {
            canonical
            for field in value
            if str(field).strip()
            if (canonical := canonical_customer_service_field_name(str(field))) is not None
        }

    @staticmethod
    def _optional_datetime(value: Any, field_name: str, row_number: int) -> datetime | None:
        if value in (None, ""):
            return None
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise _RowRejected(
                row_number, "invalid_effective_datetime", (field_name,)
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise _RowRejected(
                row_number, "effective_datetime_must_be_aware", (field_name,)
            )
        return parsed.astimezone(UTC)


class _RowRejected(ValueError):
    def __init__(
        self,
        row_number: int,
        reason: str,
        field_keys: tuple[str, ...],
        disposition: RowDisposition = RowDisposition.REJECTED,
    ) -> None:
        super().__init__(reason)
        self.row_number = row_number
        self.reason = reason
        self.field_keys = field_keys
        self.disposition = disposition
