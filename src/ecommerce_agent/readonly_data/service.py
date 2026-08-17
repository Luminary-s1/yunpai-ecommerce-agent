from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import datetime
from typing import Any, Iterable

from ..business.source_versioning import canonical_source_time, decide_write, payload_digest
from ..database import Database, utc_now
from .contracts import (
    DataQualitySummary,
    DataScope,
    EvidenceState,
    FieldEvidenceInput,
    ImportManifestInput,
    QualityStatus,
    RowDisposition,
    RowIsolationIssue,
    SourceKind,
)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _bounded_limit(value: int) -> int:
    if value < 1 or value > 1000:
        raise ValueError("readonly_query_limit_invalid")
    return value


class ReadonlyDataService:
    def __init__(self, db: Database):
        self.db = db

    def record_import(
        self,
        tenant_id: str,
        value: ImportManifestInput,
        *,
        row_issues: Iterable[RowIsolationIssue] = (),
    ) -> dict[str, Any]:
        tenant_id = self._tenant_id(tenant_id)
        issues = sorted(row_issues, key=lambda issue: issue.row_number)
        quality = self._quality(value, issues)
        manifest_payload = value.model_dump(mode="json")
        exported_at = canonical_source_time(value.exported_at)
        data_as_of = (
            canonical_source_time(value.data_as_of) if value.data_as_of is not None else None
        )
        manifest_payload["exported_at"] = exported_at
        manifest_payload["data_as_of"] = data_as_of
        manifest_payload["references"] = sorted(
            manifest_payload["references"],
            key=lambda reference: (reference["kind"], reference["reference"]),
        )
        issue_payloads = [issue.model_dump(mode="json") for issue in issues]
        contract_hash = payload_digest(
            {
                "manifest": manifest_payload,
                "quality": quality.model_dump(mode="json"),
                "row_issues": issue_payloads,
            }
        )
        imported_at = utc_now()
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT import_id, exported_at, payload_hash
                FROM readonly_import_manifests
                WHERE tenant_id=? AND store_id=? AND source_kind=?
                  AND source_system=? AND report_type=? AND report_period=?
                  AND mapping_version=?
                ORDER BY exported_at DESC, import_id DESC
                LIMIT 1
                """,
                (
                    tenant_id,
                    value.store_id,
                    value.source_kind.value,
                    value.source_system,
                    value.report_type,
                    value.report_period,
                    value.mapping_version,
                ),
            ).fetchone()
            if existing is not None:
                decision = decide_write(
                    existing_source_time=str(existing["exported_at"]),
                    existing_payload_hash=str(existing["payload_hash"]),
                    incoming_source_time=exported_at,
                    incoming_payload_hash=contract_hash,
                )
                if decision == "idempotent":
                    import_id = str(existing["import_id"])
                    write_status = "idempotent"
                else:
                    import_id = f"import-{uuid.uuid4().hex}"
            else:
                import_id = f"import-{uuid.uuid4().hex}"
            if write_status == "applied":
                conn.execute(
                    """
                    INSERT INTO readonly_import_manifests(
                        import_id, tenant_id, store_id, source_kind, source_system,
                        report_type, report_period, exported_at, imported_at,
                        schema_fingerprint, content_digest, mapping_version,
                        accepted_rows, quarantined_rows, rejected_rows, data_as_of,
                        references_json, quality_json, payload_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        import_id,
                        tenant_id,
                        value.store_id,
                        value.source_kind.value,
                        value.source_system,
                        value.report_type,
                        value.report_period,
                        exported_at,
                        imported_at,
                        value.schema_fingerprint,
                        value.content_digest,
                        value.mapping_version,
                        quality.accepted_rows,
                        quality.quarantined_rows,
                        quality.rejected_rows,
                        data_as_of,
                        _json_dump(manifest_payload["references"]),
                        _json_dump(quality.model_dump(mode="json")),
                        contract_hash,
                    ),
                )
                for issue in issues:
                    conn.execute(
                        """
                        INSERT INTO readonly_import_row_issues(
                            issue_id, import_id, tenant_id, store_id, row_number,
                            disposition, reason, field_keys_json, raw_row_digest, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"import-issue-{uuid.uuid4().hex}",
                            import_id,
                            tenant_id,
                            value.store_id,
                            issue.row_number,
                            issue.disposition.value,
                            issue.reason,
                            _json_dump(list(issue.field_keys)),
                            issue.raw_row_digest,
                            imported_at,
                        ),
                    )
        result = self.get_import(tenant_id, import_id)
        result["write_status"] = write_status
        return result

    def get_import(self, tenant_id: str, import_id: str) -> dict[str, Any]:
        tenant_id = self._tenant_id(tenant_id)
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM readonly_import_manifests WHERE tenant_id=? AND import_id=?",
                (tenant_id, import_id),
            ).fetchone()
        if row is None:
            raise ValueError("readonly_import_not_found")
        return self._manifest_view(dict(row))

    def list_imports(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        scope: DataScope = DataScope.OPERATIONAL,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [self._tenant_id(tenant_id)]
        if store_id is not None:
            conditions.append("store_id=?")
            params.append(store_id)
        scope_sql, scope_params = self._scope_condition(scope)
        if scope_sql:
            conditions.append(scope_sql)
            params.extend(scope_params)
        params.append(_bounded_limit(limit))
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM readonly_import_manifests
                WHERE {' AND '.join(conditions)}
                ORDER BY exported_at DESC, import_id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._manifest_view(dict(row)) for row in rows]

    def list_row_issues(
        self,
        tenant_id: str,
        *,
        import_id: str | None = None,
        store_id: str | None = None,
        scope: DataScope = DataScope.OPERATIONAL,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        conditions = ["i.tenant_id=?"]
        params: list[Any] = [self._tenant_id(tenant_id)]
        if import_id is not None:
            conditions.append("i.import_id=?")
            params.append(import_id)
        if store_id is not None:
            conditions.append("i.store_id=?")
            params.append(store_id)
        scope_sql, scope_params = self._scope_condition(scope, alias="m")
        if scope_sql:
            conditions.append(scope_sql)
            params.extend(scope_params)
        params.append(_bounded_limit(limit))
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT i.* FROM readonly_import_row_issues AS i
                JOIN readonly_import_manifests AS m ON m.import_id=i.import_id
                WHERE {' AND '.join(conditions)}
                ORDER BY i.row_number ASC, i.issue_id ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._row_issue_view(dict(row)) for row in rows]

    def record_field_evidence(
        self, tenant_id: str, value: FieldEvidenceInput
    ) -> dict[str, Any]:
        tenant_id = self._tenant_id(tenant_id)
        data_as_of = (
            canonical_source_time(value.data_as_of) if value.data_as_of is not None else None
        )
        source_reference = value.source_reference
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if value.import_id is not None:
                manifest = conn.execute(
                    """
                    SELECT store_id, source_kind, exported_at, data_as_of, references_json
                    FROM readonly_import_manifests WHERE tenant_id=? AND import_id=?
                    """,
                    (tenant_id, value.import_id),
                ).fetchone()
                if manifest is None:
                    raise ValueError("readonly_import_not_found")
                if str(manifest["store_id"]) != value.store_id:
                    raise ValueError("readonly_import_store_mismatch")
                if str(manifest["source_kind"]) != value.evidence_state.value:
                    raise ValueError("field_evidence_source_mismatch")
                manifest_data_as_of = str(
                    manifest["data_as_of"] or manifest["exported_at"]
                )
                if data_as_of is None:
                    data_as_of = manifest_data_as_of
                elif datetime.fromisoformat(data_as_of) > datetime.fromisoformat(
                    manifest_data_as_of
                ):
                    raise ValueError("field_evidence_data_as_of_after_manifest")
                if source_reference is not None:
                    manifest_references = {
                        reference["reference"]
                        for reference in json.loads(str(manifest["references_json"]))
                    }
                    if source_reference not in manifest_references:
                        raise ValueError("field_evidence_reference_mismatch")
            evidence_payload = {
                "store_id": value.store_id,
                "field_key": value.field_key,
                "scope": value.scope,
                "evidence_state": value.evidence_state.value,
                "reason": value.reason,
                "data_as_of": data_as_of,
                "source_reference": source_reference,
                "import_id": value.import_id,
            }
            evidence_hash = payload_digest(evidence_payload)
            existing = conn.execute(
                """
                SELECT * FROM readonly_field_evidence
                WHERE tenant_id=? AND store_id=? AND field_key=? AND scope=?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (tenant_id, value.store_id, value.field_key, value.scope),
            ).fetchone()
            write_status = (
                "idempotent"
                if existing is not None and str(existing["payload_hash"]) == evidence_hash
                else "applied"
            )
            if write_status == "applied":
                evidence_id = f"field-evidence-{uuid.uuid4().hex}"
                created_at = utc_now()
                conn.execute(
                    """
                    INSERT INTO readonly_field_evidence(
                        evidence_id, tenant_id, store_id, field_key, scope, evidence_state,
                        reason, data_as_of, source_reference, import_id, payload_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence_id,
                        tenant_id,
                        value.store_id,
                        value.field_key,
                        value.scope,
                        value.evidence_state.value,
                        value.reason,
                        data_as_of,
                        source_reference,
                        value.import_id,
                        evidence_hash,
                        created_at,
                    ),
                )
                existing = conn.execute(
                    "SELECT * FROM readonly_field_evidence WHERE evidence_id=?",
                    (evidence_id,),
                ).fetchone()
        result = self._field_evidence_view(dict(existing))
        result["write_status"] = write_status
        return result

    def list_field_evidence(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        scope: DataScope = DataScope.OPERATIONAL,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [self._tenant_id(tenant_id)]
        if store_id is not None:
            conditions.append("store_id=?")
            params.append(store_id)
        data_scope = DataScope(scope)
        if data_scope is DataScope.OPERATIONAL:
            conditions.append("evidence_state IN (?, ?, ?)")
            params.extend(
                [
                    EvidenceState.ACTUAL.value,
                    EvidenceState.MANUAL.value,
                    EvidenceState.MISSING.value,
                ]
            )
        elif data_scope is DataScope.DEMO:
            conditions.append("evidence_state=?")
            params.append(EvidenceState.DEMO.value)
        params.append(_bounded_limit(limit))
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY tenant_id, store_id, field_key, scope
                        ORDER BY rowid DESC
                    ) AS evidence_rank
                    FROM readonly_field_evidence
                    WHERE {' AND '.join(conditions)}
                )
                SELECT * FROM ranked WHERE evidence_rank=1
                ORDER BY store_id ASC, field_key ASC, scope ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._field_evidence_view(dict(row)) for row in rows]

    @staticmethod
    def _tenant_id(value: str) -> str:
        if value != value.strip() or not value or len(value) > 128:
            raise ValueError("invalid_tenant_id")
        return value

    @staticmethod
    def _quality(
        value: ImportManifestInput, issues: list[RowIsolationIssue]
    ) -> DataQualitySummary:
        row_numbers = [issue.row_number for issue in issues]
        if len(row_numbers) != len(set(row_numbers)):
            raise ValueError("duplicate_import_row_issue")
        total_rows = value.parsed_rows
        if row_numbers and max(row_numbers) > total_rows:
            raise ValueError("row_issue_outside_import")
        dispositions = Counter(issue.disposition for issue in issues)
        quarantined_rows = dispositions[RowDisposition.QUARANTINED]
        rejected_rows = dispositions[RowDisposition.REJECTED]
        accepted_rows = total_rows - quarantined_rows - rejected_rows
        issue_counts = Counter(issue.reason for issue in issues)
        if not issues:
            status = QualityStatus.PASSED
        elif accepted_rows:
            status = QualityStatus.PARTIAL
        else:
            status = QualityStatus.FAILED
        return DataQualitySummary(
            status=status,
            total_rows=total_rows,
            accepted_rows=accepted_rows,
            quarantined_rows=quarantined_rows,
            rejected_rows=rejected_rows,
            issue_counts=dict(sorted(issue_counts.items())),
        )

    @staticmethod
    def _scope_condition(
        scope: DataScope, *, alias: str | None = None
    ) -> tuple[str, list[str]]:
        data_scope = DataScope(scope)
        prefix = f"{alias}." if alias else ""
        if data_scope is DataScope.OPERATIONAL:
            return f"{prefix}source_kind IN (?, ?)", [
                SourceKind.ACTUAL.value,
                SourceKind.MANUAL.value,
            ]
        if data_scope is DataScope.DEMO:
            return f"{prefix}source_kind=?", [SourceKind.DEMO.value]
        return "", []

    @staticmethod
    def _manifest_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "import_id": str(row["import_id"]),
            "tenant_id": str(row["tenant_id"]),
            "store_id": str(row["store_id"]),
            "source_kind": str(row["source_kind"]),
            "source_system": str(row["source_system"]),
            "report_type": str(row["report_type"]),
            "report_period": str(row["report_period"]),
            "exported_at": str(row["exported_at"]),
            "imported_at": str(row["imported_at"]),
            "schema_fingerprint": str(row["schema_fingerprint"]),
            "content_digest": str(row["content_digest"]),
            "mapping_version": str(row["mapping_version"]),
            "accepted_rows": int(row["accepted_rows"]),
            "quarantined_rows": int(row["quarantined_rows"]),
            "rejected_rows": int(row["rejected_rows"]),
            "data_as_of": str(row["data_as_of"]) if row["data_as_of"] else None,
            "references": json.loads(str(row["references_json"])),
            "quality": json.loads(str(row["quality_json"])),
        }

    @staticmethod
    def _row_issue_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "issue_id": str(row["issue_id"]),
            "import_id": str(row["import_id"]),
            "tenant_id": str(row["tenant_id"]),
            "store_id": str(row["store_id"]),
            "row_number": int(row["row_number"]),
            "disposition": str(row["disposition"]),
            "reason": str(row["reason"]),
            "field_keys": json.loads(str(row["field_keys_json"])),
            "raw_row_digest": str(row["raw_row_digest"]),
            "created_at": str(row["created_at"]),
        }

    @staticmethod
    def _field_evidence_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "evidence_id": str(row["evidence_id"]),
            "tenant_id": str(row["tenant_id"]),
            "store_id": str(row["store_id"]),
            "field_key": str(row["field_key"]),
            "scope": str(row["scope"]),
            "evidence_state": str(row["evidence_state"]),
            "reason": str(row["reason"]),
            "data_as_of": str(row["data_as_of"]) if row["data_as_of"] else None,
            "source_reference": (
                str(row["source_reference"]) if row["source_reference"] else None
            ),
            "import_id": str(row["import_id"]) if row["import_id"] else None,
            "created_at": str(row["created_at"]),
        }
