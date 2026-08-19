from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any, Mapping

from ..business.source_versioning import canonical_source_time
from ..database import Database
from ..product_identity import ProductIdentityService
from ..readonly_data import DataScope, REPORT_ADAPTERS, ReadonlyDataService
from .policy import (
    READINESS_GAP_REQUIREMENTS,
    READINESS_POLICY_VERSION,
    READINESS_REPORT_POLICIES,
)


_IDENTITY_DOMAIN_KEYS = {
    "catalog": "catalog",
    "inventory": "inventory",
    "orders": "order",
}


class ReadonlyReadinessService:
    """Project WP1-WP3 evidence into one read-only management contract."""

    policy_version = READINESS_POLICY_VERSION

    def __init__(self, db: Database):
        self.db = db
        self.readonly = ReadonlyDataService(db)
        self.identity = ProductIdentityService(db)

    def project(
        self,
        tenant_id: str,
        *,
        store_id: str,
        scope: DataScope = DataScope.OPERATIONAL,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        data_scope = DataScope(scope)
        projection_time = as_of or datetime.now(UTC)
        projection_time_text = canonical_source_time(projection_time)
        projection_time = datetime.fromisoformat(projection_time_text)
        manifests = self.readonly.list_imports(
            tenant_id,
            store_id=store_id,
            scope=data_scope,
            limit=1000,
        )
        by_report: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for manifest in manifests:
            by_report[str(manifest["report_type"])].append(manifest)

        product_identity = self._product_identity(
            tenant_id,
            store_id=store_id,
            scope=data_scope,
        )
        domains = [
            self._domain_projection(
                report_type,
                by_report.get(report_type, []),
                as_of=projection_time,
                identity_by_domain=product_identity["by_domain"],
                projection_truncated=len(manifests) == 1000,
            )
            for report_type in sorted(READINESS_REPORT_POLICIES)
        ]
        gaps = self._gap_projection(
            tenant_id,
            store_id=store_id,
            scope=data_scope,
        )
        counts = Counter(str(item["status"]) for item in domains)
        domain_counts = {
            status: counts[status] for status in ("ready", "attention", "missing")
        }
        available_domains = len(domains) - domain_counts["missing"]
        open_gap_count = sum(bool(item["open"]) for item in gaps)
        if available_domains == 0:
            status = "missing"
        elif (
            domain_counts == {"ready": len(domains), "attention": 0, "missing": 0}
            and open_gap_count == 0
            and product_identity["status"] in {"matched", "not_applicable"}
        ):
            status = "ready"
        else:
            status = "attention"
        public_identity = dict(product_identity)
        public_identity.pop("by_domain")
        return {
            "policy_version": self.policy_version,
            "store_id": store_id,
            "scope": data_scope.value,
            "as_of": projection_time_text,
            "summary": {
                "status": status,
                "domain_counts": domain_counts,
                "available_domains": available_domains,
                "total_domains": len(domains),
                "open_gap_count": open_gap_count,
            },
            "domains": domains,
            "gaps": gaps,
            "product_identity": public_identity,
            "trace": {
                "manifest_count": len(manifests),
                "projection_limit": 1000,
                "projection_truncated": len(manifests) == 1000,
            },
            "boundaries": {
                "read_only": True,
                "model_used": False,
                "platform_write_performed": False,
                "scope_rule": (
                    "operational includes actual/manual and excludes demo; "
                    "demo includes only demo; all is explicit"
                ),
            },
        }

    def _domain_projection(
        self,
        report_type: str,
        manifests: list[dict[str, Any]],
        *,
        as_of: datetime,
        identity_by_domain: Mapping[str, dict[str, Any]],
        projection_truncated: bool,
    ) -> dict[str, Any]:
        adapter = REPORT_ADAPTERS.get(
            report_type,
            next(
                item.mapping_version
                for item in REPORT_ADAPTERS.list()
                if item.report_type == report_type
            ),
        )
        policy = READINESS_REPORT_POLICIES[report_type]
        identity_key = _IDENTITY_DOMAIN_KEYS.get(adapter.domain.value)
        mapping = (
            dict(identity_by_domain.get(identity_key, {"status": "not_observed", "status_counts": {}}))
            if identity_key is not None
            else {"status": "not_applicable", "status_counts": {}}
        )
        base = {
            "report_type": report_type,
            "domain": adapter.domain.value,
            "grain": adapter.grain,
            "amount_unit": adapter.amount_unit,
            "mapping_version": adapter.mapping_version,
            "mapping": mapping,
        }
        if not manifests:
            return {
                **base,
                "status": "missing",
                "source": {"kind": "missing", "kinds": [], "systems": []},
                "coverage": {
                    "basis": "data_as_of_or_exported_at",
                    "start": None,
                    "end": None,
                    "report_periods": [],
                },
                "watermark": None,
                "freshness": {
                    "policy_version": self.policy_version,
                    "status": "missing",
                    "max_age_hours": policy.max_age_hours,
                    "age_hours": None,
                },
                "quality": None,
                "trace": {
                    "import_ids": [],
                    "import_count": 0,
                    "projection_truncated": projection_truncated,
                },
            }

        observations = [
            (self._manifest_time(manifest), manifest) for manifest in manifests
        ]
        observations.sort(
            key=lambda item: (
                item[0],
                self._parse_time(str(item[1]["exported_at"])),
                str(item[1]["import_id"]),
            )
        )
        latest_time, latest = observations[-1]
        age_hours = (as_of - latest_time).total_seconds() / 3600
        if age_hours < 0:
            freshness_status = "future"
        elif age_hours <= policy.max_age_hours:
            freshness_status = "fresh"
        else:
            freshness_status = "stale"
        source_kinds = sorted({str(item["source_kind"]) for item in manifests})
        source_kind = source_kinds[0] if len(source_kinds) == 1 else "mixed"
        quality = dict(latest["quality"])
        quality_status = str(quality["status"])
        status = "ready"
        if (
            freshness_status != "fresh"
            or quality_status != "passed"
            or mapping["status"] in {"attention", "not_observed"}
        ):
            status = "attention"
        return {
            **base,
            "status": status,
            "source": {
                "kind": source_kind,
                "kinds": source_kinds,
                "systems": sorted(
                    {str(item["source_system"]) for item in manifests}
                ),
            },
            "coverage": {
                "basis": "data_as_of_or_exported_at",
                "start": canonical_source_time(observations[0][0]),
                "end": canonical_source_time(observations[-1][0]),
                "report_periods": sorted(
                    {str(item["report_period"]) for item in manifests}
                ),
            },
            "watermark": {
                "import_id": str(latest["import_id"]),
                "data_as_of": latest["data_as_of"],
                "exported_at": str(latest["exported_at"]),
            },
            "freshness": {
                "policy_version": self.policy_version,
                "status": freshness_status,
                "max_age_hours": policy.max_age_hours,
                "age_hours": round(age_hours, 3),
            },
            "quality": {
                "import_id": str(latest["import_id"]),
                **quality,
            },
            "trace": {
                "import_ids": sorted(str(item["import_id"]) for item in manifests),
                "import_count": len(manifests),
                "projection_truncated": projection_truncated,
            },
        }

    def _product_identity(
        self,
        tenant_id: str,
        *,
        store_id: str,
        scope: DataScope,
    ) -> dict[str, Any]:
        mappings = self.identity.list_mappings(
            tenant_id,
            store_id=store_id,
            scope=scope,
            latest_only=True,
            limit=1000,
        )
        runs = self.identity.list_reconciliations(
            tenant_id,
            store_id=store_id,
            scope=scope,
            limit=1,
        )
        if not runs:
            return {
                "status": "not_run",
                "run_id": None,
                "scope": scope.value,
                "status_counts": {
                    "matched": 0,
                    "ambiguous": 0,
                    "unmapped": 0,
                    "rejected": 0,
                },
                "mapping_event_count": len(mappings),
                "mapping_events_truncated": len(mappings) == 1000,
                "by_domain": {},
            }
        run = self.identity.get_reconciliation(tenant_id, str(runs[0]["run_id"]))
        by_domain_counts: dict[str, Counter[str]] = defaultdict(Counter)
        for row in run["rows"]:
            by_domain_counts[str(row["source_domain"])][
                str(row["terminal_status"])
            ] += 1
        by_domain: dict[str, dict[str, Any]] = {}
        for domain, counts in sorted(by_domain_counts.items()):
            status_counts = {
                status: counts[status]
                for status in ("matched", "ambiguous", "unmapped", "rejected")
            }
            total = sum(status_counts.values())
            by_domain[domain] = {
                "status": (
                    "matched" if total and status_counts["matched"] == total else "attention"
                ),
                "status_counts": status_counts,
            }
        status_counts = dict(run["status_counts"])
        status = (
            "matched"
            if run["total_rows"] and status_counts["matched"] == run["total_rows"]
            else "attention"
        )
        return {
            "status": status,
            "run_id": str(run["run_id"]),
            "scope": str(run["scope"]),
            "policy_version": str(run["policy_version"]),
            "created_at": str(run["created_at"]),
            "total_rows": int(run["total_rows"]),
            "status_counts": status_counts,
            "mapping_event_count": len(mappings),
            "mapping_events_truncated": len(mappings) == 1000,
            "by_domain": by_domain,
        }

    def _gap_projection(
        self,
        tenant_id: str,
        *,
        store_id: str,
        scope: DataScope,
    ) -> list[dict[str, Any]]:
        evidence = self.readonly.list_field_evidence(
            tenant_id,
            store_id=store_id,
            scope=scope,
            limit=1000,
        )
        by_field = {str(item["field_key"]): item for item in evidence}
        result: list[dict[str, Any]] = []
        for field_key, requirement in READINESS_GAP_REQUIREMENTS.items():
            item = by_field.get(field_key)
            state = str(item["evidence_state"]) if item is not None else "missing"
            result.append(
                {
                    "field_key": field_key,
                    "label": requirement.label,
                    "open": state == "missing",
                    "evidence_state": state,
                    "reason": (
                        str(item["reason"]) if item is not None else requirement.reason
                    ),
                    "impact": requirement.impact,
                    "data_as_of": item["data_as_of"] if item is not None else None,
                    "trace": {
                        "evidence_id": item["evidence_id"] if item is not None else None,
                        "import_id": item["import_id"] if item is not None else None,
                    },
                }
            )
        return result

    @classmethod
    def _manifest_time(cls, manifest: Mapping[str, Any]) -> datetime:
        return cls._parse_time(
            str(manifest["data_as_of"] or manifest["exported_at"])
        )

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("readonly_manifest_time_timezone_required")
        return parsed.astimezone(UTC)


__all__ = ["ReadonlyReadinessService"]
