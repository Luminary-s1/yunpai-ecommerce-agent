from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, ValidationError

from ..business.catalog import CatalogService
from ..business.inventory import InventoryService
from ..business.orders import OrderService
from ..business.source_versioning import payload_digest
from ..database import Database, utc_now
from ..readonly_data import source_manifest_key
from ..readonly_data.contracts import DataScope, SourceKind
from .models import (
    MAX_RECONCILIATION_ROWS,
    PRODUCT_IDENTITY_POLICY_VERSION,
    CanonicalProductCreate,
    MappingDecisionInput,
    MappingEventType,
    MatchEvidence,
    MappingRevocationInput,
    ObservationDomain,
    ProductIdentityObservation,
    ProductReconciliationRequest,
    ReconciliationStatus,
)


_STATUS_ORDER = tuple(status.value for status in ReconciliationStatus)


class ProductIdentityService:
    """Tenant/store-scoped canonical product mapping and immutable reconciliation."""

    policy_version = PRODUCT_IDENTITY_POLICY_VERSION

    def __init__(self, db: Database):
        self.db = db
        self.catalog = CatalogService(db)
        self.inventory = InventoryService(db)
        self.orders = OrderService(db)

    def register_product(
        self,
        tenant_id: str,
        value: CanonicalProductCreate,
    ) -> dict[str, Any]:
        tenant_id = self._tenant_id(tenant_id)
        normalized_title = self._normalized_title(value.title)
        product_payload = {
            "tenant_id": tenant_id,
            **value.model_dump(mode="json"),
            "normalized_title": normalized_title,
            "policy_version": self.policy_version,
        }
        product_hash = payload_digest(product_payload)
        canonical_product_id = self._stable_id(
            "canonical",
            tenant_id,
            value.store_id,
            value.internal_part_number,
        )
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT canonical_product_id, payload_hash
                FROM readonly_canonical_products
                WHERE tenant_id=? AND store_id=? AND internal_part_number=?
                """,
                (tenant_id, value.store_id, value.internal_part_number),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != product_hash:
                    raise ValueError("canonical_product_conflict")
                canonical_product_id = str(existing["canonical_product_id"])
                write_status = "idempotent"
            else:
                conn.execute(
                    """
                    INSERT INTO readonly_canonical_products(
                        canonical_product_id, tenant_id, store_id,
                        internal_part_number, merchant_code, title,
                        normalized_title, source_kind, source_reference,
                        policy_version, payload_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        canonical_product_id,
                        tenant_id,
                        value.store_id,
                        value.internal_part_number,
                        value.merchant_code,
                        value.title,
                        normalized_title,
                        value.source_kind.value,
                        value.source_reference,
                        self.policy_version,
                        product_hash,
                        utc_now(),
                    ),
                )
        result = self._get_product(
            tenant_id,
            store_id=value.store_id,
            canonical_product_id=canonical_product_id,
        )
        result["write_status"] = write_status
        return result

    def list_products(
        self,
        tenant_id: str,
        *,
        store_id: str,
        scope: DataScope = DataScope.OPERATIONAL,
    ) -> list[dict[str, Any]]:
        tenant_id = self._tenant_id(tenant_id)
        scope = DataScope(scope)
        scope_sql, scope_params = self._scope_condition(scope)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM readonly_canonical_products
                WHERE tenant_id=? AND store_id=? AND {scope_sql}
                ORDER BY internal_part_number, canonical_product_id
                """,
                (tenant_id, store_id, *scope_params),
            ).fetchall()
        return [self._product_view(dict(row)) for row in rows]

    def get_product(
        self,
        tenant_id: str,
        *,
        store_id: str,
        canonical_product_id: str,
    ) -> dict[str, Any]:
        """Read one canonical product under the explicit tenant/store scope."""
        return self._get_product(
            self._tenant_id(tenant_id),
            store_id=store_id,
            canonical_product_id=canonical_product_id,
        )

    def get_latest_mapping(
        self,
        tenant_id: str,
        *,
        store_id: str,
        connector_id: str,
        sku_id: str,
    ) -> dict[str, Any] | None:
        """Return the latest immutable event; ``event_type=revoked`` is not active."""
        tenant_id = self._tenant_id(tenant_id)
        with self.db.connect() as conn:
            row = self._current_mapping_row(
                conn,
                tenant_id,
                store_id=store_id,
                connector_id=connector_id,
                sku_id=sku_id,
            )
        return None if row is None else self._mapping_view(dict(row))

    def confirm_mapping(
        self,
        tenant_id: str,
        value: MappingDecisionInput,
    ) -> dict[str, Any]:
        tenant_id = self._tenant_id(tenant_id)
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replayed = self._decision_replay(
                conn,
                tenant_id,
                store_id=value.store_id,
                decision_key=value.decision_key,
                event_type=MappingEventType.CONFIRMED,
                value=value,
            )
            if replayed is not None:
                result = self._mapping_view(dict(replayed))
                result["write_status"] = "idempotent"
                return result
            product = conn.execute(
                """
                SELECT canonical_product_id FROM readonly_canonical_products
                WHERE tenant_id=? AND store_id=? AND canonical_product_id=?
                """,
                (tenant_id, value.store_id, value.canonical_product_id),
            ).fetchone()
            if product is None:
                raise ValueError("canonical_product_scope_mismatch")
            if value.source_import_id is not None:
                manifest = conn.execute(
                    """
                    SELECT import_id FROM readonly_import_manifests
                    WHERE tenant_id=? AND store_id=? AND import_id=?
                    """,
                    (tenant_id, value.store_id, value.source_import_id),
                ).fetchone()
                if manifest is None:
                    raise ValueError("mapping_source_import_scope_mismatch")
            current = self._current_mapping_row(
                conn,
                tenant_id,
                store_id=value.store_id,
                connector_id=value.connector_id,
                sku_id=value.sku_id,
            )
            current_version = int(current["mapping_version"]) if current else 0
            if value.expected_version != current_version:
                raise ValueError("mapping_version_conflict")
            mapping_version = current_version + 1
            supersedes = str(current["event_id"]) if current else None
            event_payload = self._confirmed_event_payload(
                tenant_id,
                value,
                mapping_version=mapping_version,
                supersedes_event_id=supersedes,
            )
            event_id = self._stable_id(
                "mapping-event",
                tenant_id,
                value.store_id,
                value.decision_key,
            )
            self._insert_mapping_event(
                conn,
                event_id=event_id,
                payload=event_payload,
            )
            row = conn.execute(
                "SELECT * FROM readonly_product_mapping_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        result = self._mapping_view(dict(row))
        result["write_status"] = "applied"
        return result

    def revoke_mapping(
        self,
        tenant_id: str,
        value: MappingRevocationInput,
    ) -> dict[str, Any]:
        tenant_id = self._tenant_id(tenant_id)
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replayed = self._decision_replay(
                conn,
                tenant_id,
                store_id=value.store_id,
                decision_key=value.decision_key,
                event_type=MappingEventType.REVOKED,
                value=value,
            )
            if replayed is not None:
                result = self._mapping_view(dict(replayed))
                result["write_status"] = "idempotent"
                return result
            current = self._current_mapping_row(
                conn,
                tenant_id,
                store_id=value.store_id,
                connector_id=value.connector_id,
                sku_id=value.sku_id,
            )
            if current is None or str(current["event_type"]) != MappingEventType.CONFIRMED:
                raise ValueError("mapping_not_active")
            current_version = int(current["mapping_version"])
            if value.expected_version != current_version:
                raise ValueError("mapping_version_conflict")
            event_payload = self._revoked_event_payload(
                tenant_id,
                value,
                current=dict(current),
            )
            event_id = self._stable_id(
                "mapping-event",
                tenant_id,
                value.store_id,
                value.decision_key,
            )
            self._insert_mapping_event(
                conn,
                event_id=event_id,
                payload=event_payload,
            )
            row = conn.execute(
                "SELECT * FROM readonly_product_mapping_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        result = self._mapping_view(dict(row))
        result["write_status"] = "applied"
        return result

    def mapping_history(
        self,
        tenant_id: str,
        *,
        store_id: str,
        connector_id: str,
        sku_id: str,
    ) -> list[dict[str, Any]]:
        tenant_id = self._tenant_id(tenant_id)
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM readonly_product_mapping_events
                WHERE tenant_id=? AND store_id=? AND connector_id=? AND sku_id=?
                ORDER BY mapping_version, event_id
                """,
                (tenant_id, store_id, connector_id, sku_id),
            ).fetchall()
        return [self._mapping_view(dict(row)) for row in rows]

    def list_mappings(
        self,
        tenant_id: str,
        *,
        store_id: str,
        scope: DataScope = DataScope.OPERATIONAL,
        latest_only: bool = True,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List immutable mapping events without weakening product source scope."""
        tenant_id = self._tenant_id(tenant_id)
        scope_sql, scope_params = self._scope_condition(
            DataScope(scope), alias="p"
        )
        latest_sql = ""
        if latest_only:
            latest_sql = """
              AND e.mapping_version=(
                SELECT MAX(latest.mapping_version)
                FROM readonly_product_mapping_events AS latest
                WHERE latest.tenant_id=e.tenant_id
                  AND latest.store_id=e.store_id
                  AND latest.connector_id=e.connector_id
                  AND latest.sku_id=e.sku_id
              )
            """
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT e.*, p.source_kind AS canonical_source_kind,
                       (
                         SELECT MAX(current.mapping_version)
                         FROM readonly_product_mapping_events AS current
                         WHERE current.tenant_id=e.tenant_id
                           AND current.store_id=e.store_id
                           AND current.connector_id=e.connector_id
                           AND current.sku_id=e.sku_id
                       ) AS latest_mapping_version
                FROM readonly_product_mapping_events AS e
                JOIN readonly_canonical_products AS p
                  ON p.tenant_id=e.tenant_id
                 AND p.store_id=e.store_id
                 AND p.canonical_product_id=e.canonical_product_id
                WHERE e.tenant_id=? AND e.store_id=? AND {scope_sql}
                {latest_sql}
                ORDER BY e.created_at DESC, e.event_id DESC
                LIMIT ?
                """,
                (
                    tenant_id,
                    store_id,
                    *scope_params,
                    self._bounded_read_limit(limit),
                ),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            row_value = dict(row)
            item = self._mapping_view(row_value)
            item["source_kind"] = str(row_value["canonical_source_kind"])
            item["active"] = (
                item["event_type"] == MappingEventType.CONFIRMED.value
                and item["mapping_version"]
                == int(row_value["latest_mapping_version"])
            )
            result.append(item)
        return result

    def reconcile(
        self,
        tenant_id: str,
        request: ProductReconciliationRequest,
    ) -> dict[str, Any]:
        tenant_id = self._tenant_id(tenant_id)
        request = request.model_copy(update={"scope": DataScope(request.scope)})
        parsed_rows = self._parse_observations(request)
        products = self.list_products(
            tenant_id,
            store_id=request.store_id,
            scope=request.scope,
        )
        product_by_id = {
            str(product["canonical_product_id"]): product for product in products
        }
        by_merchant: dict[str, list[str]] = {}
        by_title: dict[str, list[str]] = {}
        for product in products:
            product_id = str(product["canonical_product_id"])
            merchant_code = product["merchant_code"]
            if merchant_code is not None:
                by_merchant.setdefault(str(merchant_code), []).append(product_id)
            by_title.setdefault(str(product["normalized_title"]), []).append(product_id)
        for index in (by_merchant, by_title):
            for key in index:
                index[key].sort()

        current_mappings = self._current_mapping_snapshot(
            tenant_id,
            store_id=request.store_id,
            scope=request.scope,
        )
        current_by_identity = {
            (str(row["connector_id"]), str(row["sku_id"])): row
            for row in current_mappings
        }
        result_rows: list[dict[str, Any]] = []
        for row_number, raw_digest, observation, rejection_reason in parsed_rows:
            if rejection_reason is not None or observation is None:
                result_rows.append(
                    self._terminal_row(
                        row_number=row_number,
                        raw_digest=raw_digest,
                        terminal_status=ReconciliationStatus.REJECTED,
                        reason=rejection_reason or "invalid_product_observation",
                        evidence_keys=(MatchEvidence.INVALID_OBSERVATION,),
                    )
                )
                continue
            if observation.store_id != request.store_id:
                result_rows.append(
                    self._terminal_row(
                        row_number=row_number,
                        raw_digest=raw_digest,
                        observation=observation,
                        terminal_status=ReconciliationStatus.REJECTED,
                        reason="cross_store_observation",
                        evidence_keys=(MatchEvidence.STORE_SCOPE_CONFLICT,),
                    )
                )
                continue
            current = current_by_identity.get(
                (observation.connector_id, observation.sku_id)
            )
            result_rows.append(
                self._reconcile_observation(
                    row_number=row_number,
                    raw_digest=raw_digest,
                    observation=observation,
                    current=current,
                    product_by_id=product_by_id,
                    by_merchant=by_merchant,
                    by_title=by_title,
                )
            )

        input_digest = payload_digest(
            {
                "store_id": request.store_id,
                "scope": request.scope.value,
                "row_digests": [row["input_digest"] for row in result_rows],
            }
        )
        mapping_snapshot_digest = payload_digest(
            {
                "products": [
                    {
                        "canonical_product_id": product["canonical_product_id"],
                        "payload_hash": product["payload_hash"],
                    }
                    for product in products
                ],
                "mappings": [
                    {
                        "event_id": row["event_id"],
                        "payload_hash": row["payload_hash"],
                    }
                    for row in current_mappings
                ],
            }
        )
        counts = Counter(str(row["terminal_status"]) for row in result_rows)
        status_counts = {status: counts[status] for status in _STATUS_ORDER}
        run_payload = {
            "tenant_id": tenant_id,
            "store_id": request.store_id,
            "data_scope": request.scope.value,
            "policy_version": self.policy_version,
            "input_digest": input_digest,
            "mapping_snapshot_digest": mapping_snapshot_digest,
            "total_rows": len(result_rows),
            "status_counts": status_counts,
            "rows": result_rows,
        }
        run_hash = payload_digest(run_payload)
        run_id = self._stable_id(
            "product-reconciliation",
            tenant_id,
            request.store_id,
            self.policy_version,
            request.scope.value,
            input_digest,
            mapping_snapshot_digest,
        )
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT payload_hash FROM readonly_product_reconciliation_runs
                WHERE tenant_id=? AND run_id=?
                """,
                (tenant_id, run_id),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != run_hash:
                    raise ValueError("product_reconciliation_replay_conflict")
                write_status = "idempotent"
            else:
                created_at = utc_now()
                conn.execute(
                    """
                    INSERT INTO readonly_product_reconciliation_runs(
                        run_id, tenant_id, store_id, data_scope, policy_version,
                        input_digest, mapping_snapshot_digest, total_rows,
                        matched_rows, ambiguous_rows, unmapped_rows, rejected_rows,
                        payload_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        tenant_id,
                        request.store_id,
                        request.scope.value,
                        self.policy_version,
                        input_digest,
                        mapping_snapshot_digest,
                        len(result_rows),
                        status_counts[ReconciliationStatus.MATCHED],
                        status_counts[ReconciliationStatus.AMBIGUOUS],
                        status_counts[ReconciliationStatus.UNMAPPED],
                        status_counts[ReconciliationStatus.REJECTED],
                        run_hash,
                        created_at,
                    ),
                )
                conn.executemany(
                    """
                    INSERT INTO readonly_product_reconciliation_rows(
                        row_id, run_id, tenant_id, store_id, row_number,
                        source_domain, source_reference, connector_id, sku_id,
                        item_id, merchant_code, terminal_status,
                        canonical_product_id, internal_part_number, reason,
                        candidate_product_ids_json, evidence_keys_json,
                        input_digest, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            self._stable_id("reconciliation-row", run_id, row["row_number"]),
                            run_id,
                            tenant_id,
                            request.store_id,
                            row["row_number"],
                            row["source_domain"],
                            row["source_reference"],
                            row["connector_id"],
                            row["sku_id"],
                            row["item_id"],
                            row["merchant_code"],
                            row["terminal_status"],
                            row["canonical_product_id"],
                            row["internal_part_number"],
                            row["reason"],
                            json.dumps(
                                row["candidate_product_ids"],
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            json.dumps(
                                row["evidence_keys"],
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            row["input_digest"],
                            created_at,
                        )
                        for row in result_rows
                    ],
                )
        result = self.get_reconciliation(tenant_id, run_id)
        result["write_status"] = write_status
        return result

    def reconcile_domain(
        self,
        tenant_id: str,
        *,
        store_id: str,
        scope: DataScope = DataScope.OPERATIONAL,
    ) -> dict[str, Any]:
        tenant_id = self._tenant_id(tenant_id)
        scope = DataScope(scope)
        provenance = self._domain_source_provenance(tenant_id, store_id=store_id)
        catalog_items = [
            item
            for item in self.catalog.list_items(
                tenant_id,
                store_id=store_id,
                limit=MAX_RECONCILIATION_ROWS + 1,
            )
            if self._source_in_scope(item.get("source_id"), scope, provenance)
        ]
        inventory_balances = [
            balance
            for balance in self.inventory.list_balances(tenant_id, store_id=store_id)
            if self._source_in_scope(balance.get("source_id"), scope, provenance)
        ]
        domain_orders = [
            order
            for order in self.orders.demand_source_orders(
                tenant_id,
                store_id=store_id,
            )
            if self._source_in_scope(order.get("source_id"), scope, provenance)
        ]
        if len(catalog_items) > MAX_RECONCILIATION_ROWS:
            raise ValueError("product_reconciliation_row_limit_exceeded")
        catalog_by_identity = {
            (str(item["connector_id"]), str(item["sku_id"])): item
            for item in catalog_items
        }
        observations: list[dict[str, Any]] = []
        for item in catalog_items:
            observations.append(
                self._domain_observation(
                    source_domain=ObservationDomain.CATALOG,
                    source_reference=f"catalog:{self._reference_token(item['id'])}",
                    item=item,
                )
            )
        for balance in inventory_balances:
            catalog = catalog_by_identity.get(
                (str(balance["connector_id"]), str(balance["sku_id"]))
            )
            observations.append(
                self._domain_observation(
                    source_domain=ObservationDomain.INVENTORY,
                    source_reference=(
                        f"inventory:{self._reference_token(balance['id'])}"
                    ),
                    item=balance,
                    catalog=catalog,
                )
            )
        for order in domain_orders:
            for line in order["lines"]:
                catalog = catalog_by_identity.get(
                    (str(order["connector_id"]), str(line["sku_id"]))
                )
                observations.append(
                    self._domain_observation(
                        source_domain=ObservationDomain.ORDER,
                        source_reference=(
                            f"order:{self._reference_token(order['id'])}:"
                            f"{self._reference_token(line['line_id'])}"
                        ),
                        item={
                            "store_id": order["store_id"],
                            "connector_id": order["connector_id"],
                            "sku_id": line["sku_id"],
                            "title": line["title"],
                        },
                        catalog=catalog,
                    )
                )
        if not observations:
            raise ValueError("product_reconciliation_source_empty")
        if len(observations) > MAX_RECONCILIATION_ROWS:
            raise ValueError("product_reconciliation_row_limit_exceeded")
        observations.sort(
            key=lambda row: (
                str(row["source_domain"]),
                str(row["source_reference"]),
            )
        )
        return self.reconcile(
            tenant_id,
            ProductReconciliationRequest(
                store_id=store_id,
                scope=scope,
                observations=tuple(observations),
            ),
        )

    def _domain_source_provenance(
        self,
        tenant_id: str,
        *,
        store_id: str,
    ) -> dict[tuple[str, str], frozenset[str]]:
        """Index WP2 source ids to the WP1 manifest source kind.

        WP2 domain tables predate a source-kind column.  Their readonly source
        ids contain the report type and the first 24 digest characters, so the
        manifest remains the authority for operational/demo filtering.  Older
        hand-seeded domain facts are treated as unknown/manual for backwards
        compatibility; malformed readonly ids fail closed for scoped views.
        """
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT report_type, substr(content_digest, 1, 24) AS digest_prefix,
                       source_kind
                FROM readonly_import_manifests
                WHERE tenant_id=? AND store_id=?
                """,
                (tenant_id, store_id),
            ).fetchall()
        result: dict[tuple[str, str], set[str]] = {}
        for row in rows:
            result.setdefault(
                (str(row["report_type"]), str(row["digest_prefix"])), set()
            ).add(str(row["source_kind"]))
        return {key: frozenset(values) for key, values in result.items()}

    @classmethod
    def _source_in_scope(
        cls,
        source_id: Any,
        scope: DataScope,
        provenance: Mapping[tuple[str, str], frozenset[str]],
    ) -> bool:
        if scope is DataScope.ALL:
            return True
        source_kind = cls._source_kind_for_id(source_id, provenance)
        if source_kind == SourceKind.DEMO.value:
            return scope is DataScope.DEMO
        if source_kind == "unresolved_readonly":
            return False
        return scope is DataScope.OPERATIONAL

    @staticmethod
    def _source_kind_for_id(
        source_id: Any,
        provenance: Mapping[tuple[str, str], frozenset[str]],
    ) -> str:
        if source_id is None or not str(source_id):
            return "unknown"
        value = str(source_id)
        manifest_key = source_manifest_key(value)
        if manifest_key is not None:
            kinds = provenance.get(manifest_key, frozenset())
            if len(kinds) == 1:
                return next(iter(kinds))
            return "unresolved_readonly"
        if value.startswith("readonly:"):
            return "unresolved_readonly"
        return "unknown"

    @staticmethod
    def _reference_token(value: Any) -> str:
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]

    def get_reconciliation(self, tenant_id: str, run_id: str) -> dict[str, Any]:
        tenant_id = self._tenant_id(tenant_id)
        with self.db.connect() as conn:
            run = conn.execute(
                """
                SELECT * FROM readonly_product_reconciliation_runs
                WHERE tenant_id=? AND run_id=?
                """,
                (tenant_id, run_id),
            ).fetchone()
            if run is None:
                raise ValueError("product_reconciliation_not_found")
            rows = conn.execute(
                """
                SELECT * FROM readonly_product_reconciliation_rows
                WHERE tenant_id=? AND run_id=?
                ORDER BY row_number
                """,
                (tenant_id, run_id),
            ).fetchall()
        run_value = dict(run)
        self._require_policy(str(run_value["policy_version"]))
        total_rows = int(run_value["total_rows"])
        status_counts = {
            "matched": int(run_value["matched_rows"]),
            "ambiguous": int(run_value["ambiguous_rows"]),
            "unmapped": int(run_value["unmapped_rows"]),
            "rejected": int(run_value["rejected_rows"]),
        }
        row_values = [dict(row) for row in rows]
        if len(row_values) != total_rows or [
            int(row["row_number"]) for row in row_values
        ] != list(range(1, total_rows + 1)):
            raise ValueError("product_reconciliation_rows_incomplete")
        actual_counts = Counter(str(row["terminal_status"]) for row in row_values)
        if {status: actual_counts[status] for status in _STATUS_ORDER} != status_counts:
            raise ValueError("product_reconciliation_counts_inconsistent")
        return {
            "run_id": run_value["run_id"],
            "store_id": run_value["store_id"],
            "scope": run_value["data_scope"],
            "policy_version": run_value["policy_version"],
            "input_digest": run_value["input_digest"],
            "mapping_snapshot_digest": run_value["mapping_snapshot_digest"],
            "total_rows": total_rows,
            "status_counts": status_counts,
            "rows": [self._reconciliation_row_view(row) for row in row_values],
            "created_at": run_value["created_at"],
        }

    def list_reconciliations(
        self,
        tenant_id: str,
        *,
        store_id: str,
        scope: DataScope = DataScope.OPERATIONAL,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List reconciliation summaries for the exact data scope requested."""
        tenant_id = self._tenant_id(tenant_id)
        data_scope = DataScope(scope)
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM readonly_product_reconciliation_runs
                WHERE tenant_id=? AND store_id=? AND data_scope=?
                ORDER BY created_at DESC, run_id DESC
                LIMIT ?
                """,
                (
                    tenant_id,
                    store_id,
                    data_scope.value,
                    self._bounded_read_limit(limit),
                ),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            self._require_policy(str(value["policy_version"]))
            result.append(
                {
                    "run_id": str(value["run_id"]),
                    "store_id": str(value["store_id"]),
                    "scope": str(value["data_scope"]),
                    "policy_version": str(value["policy_version"]),
                    "input_digest": str(value["input_digest"]),
                    "mapping_snapshot_digest": str(
                        value["mapping_snapshot_digest"]
                    ),
                    "total_rows": int(value["total_rows"]),
                    "status_counts": {
                        "matched": int(value["matched_rows"]),
                        "ambiguous": int(value["ambiguous_rows"]),
                        "unmapped": int(value["unmapped_rows"]),
                        "rejected": int(value["rejected_rows"]),
                    },
                    "created_at": str(value["created_at"]),
                }
            )
        return result

    def _reconcile_observation(
        self,
        *,
        row_number: int,
        raw_digest: str,
        observation: ProductIdentityObservation,
        current: Mapping[str, Any] | None,
        product_by_id: Mapping[str, Mapping[str, Any]],
        by_merchant: Mapping[str, Sequence[str]],
        by_title: Mapping[str, Sequence[str]],
    ) -> dict[str, Any]:
        if current is not None:
            if str(current["event_type"]) == MappingEventType.REVOKED:
                return self._terminal_row(
                    row_number=row_number,
                    raw_digest=raw_digest,
                    observation=observation,
                    terminal_status=ReconciliationStatus.UNMAPPED,
                    reason="mapping_revoked",
                    evidence_keys=(
                        MatchEvidence.REVOKED_MAPPING,
                        MatchEvidence.SKU_ID_EXACT,
                    ),
                )
            if self._mapping_evidence_conflicts(current, observation):
                candidates = self._candidate_ids(
                    observation,
                    by_merchant=by_merchant,
                    by_title=by_title,
                )
                candidates.add(str(current["canonical_product_id"]))
                return self._terminal_row(
                    row_number=row_number,
                    raw_digest=raw_digest,
                    observation=observation,
                    terminal_status=ReconciliationStatus.AMBIGUOUS,
                    reason="confirmed_mapping_evidence_conflict",
                    candidate_product_ids=sorted(candidates),
                    evidence_keys=(
                        MatchEvidence.CONFIRMED_MAPPING,
                        MatchEvidence.SKU_ID_EXACT,
                        *self._candidate_signal_evidence(observation),
                        MatchEvidence.CONFLICTING_SIGNALS,
                    ),
                )
            product_id = str(current["canonical_product_id"])
            product = product_by_id.get(product_id)
            if product is not None:
                return self._terminal_row(
                    row_number=row_number,
                    raw_digest=raw_digest,
                    observation=observation,
                    terminal_status=ReconciliationStatus.MATCHED,
                    reason="manual_mapping_confirmed",
                    canonical_product_id=product_id,
                    internal_part_number=str(product["internal_part_number"]),
                    candidate_product_ids=(product_id,),
                    evidence_keys=self._confirmed_evidence(current, observation),
                )

        merchant_candidates = set(
            by_merchant.get(observation.merchant_code or "", ())
        )
        title_candidates = set(
            by_title.get(self._normalized_title(observation.title), ())
            if observation.title is not None
            else ()
        )
        candidates = merchant_candidates | title_candidates
        if len(candidates) > 1:
            reason = (
                "conflicting_identity_signals"
                if merchant_candidates
                and title_candidates
                and merchant_candidates != title_candidates
                else "identity_candidate_ambiguous"
            )
            return self._terminal_row(
                row_number=row_number,
                raw_digest=raw_digest,
                observation=observation,
                terminal_status=ReconciliationStatus.AMBIGUOUS,
                reason=reason,
                candidate_product_ids=sorted(candidates),
                evidence_keys=(
                    *self._candidate_signal_evidence(observation),
                    MatchEvidence.CONFLICTING_SIGNALS,
                ),
            )
        if candidates:
            return self._terminal_row(
                row_number=row_number,
                raw_digest=raw_digest,
                observation=observation,
                terminal_status=ReconciliationStatus.UNMAPPED,
                reason="manual_confirmation_required",
                candidate_product_ids=sorted(candidates),
                evidence_keys=(
                    *self._candidate_signal_evidence(observation),
                    MatchEvidence.MANUAL_CONFIRMATION_REQUIRED,
                ),
            )
        return self._terminal_row(
            row_number=row_number,
            raw_digest=raw_digest,
            observation=observation,
            terminal_status=ReconciliationStatus.UNMAPPED,
            reason="mapping_candidate_not_found",
            evidence_keys=(MatchEvidence.NO_CANDIDATE,),
        )

    @staticmethod
    def _mapping_evidence_conflicts(
        current: Mapping[str, Any], observation: ProductIdentityObservation
    ) -> bool:
        return any(
            expected is not None
            and observed is not None
            and str(expected) != str(observed)
            for expected, observed in (
                (current["item_id"], observation.item_id),
                (current["merchant_code"], observation.merchant_code),
            )
        )

    def _parse_observations(
        self, request: ProductReconciliationRequest
    ) -> list[tuple[int, str, ProductIdentityObservation | None, str | None]]:
        result = []
        for row_number, raw in enumerate(request.observations, start=1):
            raw_digest = self._raw_digest(raw)
            try:
                observation = ProductIdentityObservation.model_validate(raw)
            except ValidationError:
                result.append(
                    (row_number, raw_digest, None, "invalid_product_observation")
                )
                continue
            result.append((row_number, raw_digest, observation, None))
        return result

    def _current_mapping_snapshot(
        self,
        tenant_id: str,
        *,
        store_id: str,
        scope: DataScope,
    ) -> list[dict[str, Any]]:
        scope_sql, scope_params = self._scope_condition(scope, alias="p")
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT e.* FROM readonly_product_mapping_events AS e
                JOIN readonly_canonical_products AS p
                  ON p.tenant_id=e.tenant_id
                 AND p.store_id=e.store_id
                 AND p.canonical_product_id=e.canonical_product_id
                WHERE e.tenant_id=? AND e.store_id=? AND {scope_sql}
                  AND e.mapping_version=(
                    SELECT MAX(latest.mapping_version)
                    FROM readonly_product_mapping_events AS latest
                    WHERE latest.tenant_id=e.tenant_id
                      AND latest.store_id=e.store_id
                      AND latest.connector_id=e.connector_id
                      AND latest.sku_id=e.sku_id
                  )
                ORDER BY e.connector_id, e.sku_id
                """,
                (tenant_id, store_id, *scope_params),
            ).fetchall()
        return [self._mapping_row(dict(row)) for row in rows]

    @staticmethod
    def _current_mapping_row(
        conn: Any,
        tenant_id: str,
        *,
        store_id: str,
        connector_id: str,
        sku_id: str,
    ) -> Any | None:
        return conn.execute(
            """
            SELECT * FROM readonly_product_mapping_events
            WHERE tenant_id=? AND store_id=? AND connector_id=? AND sku_id=?
            ORDER BY mapping_version DESC, event_id DESC
            LIMIT 1
            """,
            (tenant_id, store_id, connector_id, sku_id),
        ).fetchone()

    def _decision_replay(
        self,
        conn: Any,
        tenant_id: str,
        *,
        store_id: str,
        decision_key: str,
        event_type: MappingEventType,
        value: MappingDecisionInput | MappingRevocationInput,
    ) -> Any | None:
        existing = conn.execute(
            """
            SELECT * FROM readonly_product_mapping_events
            WHERE tenant_id=? AND store_id=? AND decision_key=?
            """,
            (tenant_id, store_id, decision_key),
        ).fetchone()
        if existing is None:
            return None
        row = dict(existing)
        if event_type is MappingEventType.CONFIRMED:
            assert isinstance(value, MappingDecisionInput)
            expected = self._confirmed_event_payload(
                tenant_id,
                value,
                mapping_version=int(row["mapping_version"]),
                supersedes_event_id=row["supersedes_event_id"],
            )
        else:
            assert isinstance(value, MappingRevocationInput)
            expected = self._revoked_event_payload(
                tenant_id,
                value,
                current={
                    **row,
                    "mapping_version": int(row["mapping_version"]) - 1,
                    "event_id": row["supersedes_event_id"],
                },
            )
        if (
            str(row["event_type"]) != event_type.value
            or str(row["payload_hash"]) != payload_digest(expected)
        ):
            raise ValueError("mapping_decision_key_conflict")
        return existing

    def _confirmed_event_payload(
        self,
        tenant_id: str,
        value: MappingDecisionInput,
        *,
        mapping_version: int,
        supersedes_event_id: str | None,
    ) -> dict[str, Any]:
        return {
            "tenant_id": tenant_id,
            "store_id": value.store_id,
            "connector_id": value.connector_id,
            "sku_id": value.sku_id,
            "mapping_version": mapping_version,
            "expected_version": value.expected_version,
            "event_type": MappingEventType.CONFIRMED.value,
            "canonical_product_id": value.canonical_product_id,
            "item_id": value.item_id,
            "merchant_code": value.merchant_code,
            "decision_key": value.decision_key,
            "reason": value.reason,
            "actor_ref": value.actor_ref,
            "source_import_id": value.source_import_id,
            "supersedes_event_id": supersedes_event_id,
            "policy_version": self.policy_version,
        }

    def _revoked_event_payload(
        self,
        tenant_id: str,
        value: MappingRevocationInput,
        *,
        current: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "tenant_id": tenant_id,
            "store_id": value.store_id,
            "connector_id": value.connector_id,
            "sku_id": value.sku_id,
            "mapping_version": int(current["mapping_version"]) + 1,
            "expected_version": value.expected_version,
            "event_type": MappingEventType.REVOKED.value,
            "canonical_product_id": str(current["canonical_product_id"]),
            "item_id": current["item_id"],
            "merchant_code": current["merchant_code"],
            "decision_key": value.decision_key,
            "reason": value.reason,
            "actor_ref": value.actor_ref,
            "source_import_id": current["source_import_id"],
            "supersedes_event_id": str(current["event_id"]),
            "policy_version": self.policy_version,
        }

    @staticmethod
    def _insert_mapping_event(
        conn: Any,
        *,
        event_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO readonly_product_mapping_events(
                event_id, tenant_id, store_id, connector_id, sku_id,
                mapping_version, expected_version, event_type,
                canonical_product_id, item_id,
                merchant_code, decision_key, reason, actor_ref,
                source_import_id, supersedes_event_id, policy_version,
                payload_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                payload["tenant_id"],
                payload["store_id"],
                payload["connector_id"],
                payload["sku_id"],
                payload["mapping_version"],
                payload["expected_version"],
                payload["event_type"],
                payload["canonical_product_id"],
                payload["item_id"],
                payload["merchant_code"],
                payload["decision_key"],
                payload["reason"],
                payload["actor_ref"],
                payload["source_import_id"],
                payload["supersedes_event_id"],
                payload["policy_version"],
                payload_digest(payload),
                utc_now(),
            ),
        )

    def _get_product(
        self,
        tenant_id: str,
        *,
        store_id: str,
        canonical_product_id: str,
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM readonly_canonical_products
                WHERE tenant_id=? AND store_id=? AND canonical_product_id=?
                """,
                (tenant_id, store_id, canonical_product_id),
            ).fetchone()
        if row is None:
            raise ValueError("canonical_product_not_found")
        return self._product_view(dict(row))

    def _product_view(self, row: dict[str, Any]) -> dict[str, Any]:
        self._require_policy(str(row["policy_version"]))
        return {
            key: row[key]
            for key in (
                "canonical_product_id",
                "store_id",
                "internal_part_number",
                "merchant_code",
                "title",
                "normalized_title",
                "source_kind",
                "source_reference",
                "policy_version",
                "payload_hash",
                "created_at",
            )
        }

    def _mapping_row(self, row: dict[str, Any]) -> dict[str, Any]:
        self._require_policy(str(row["policy_version"]))
        return row

    def _mapping_view(self, row: dict[str, Any]) -> dict[str, Any]:
        self._require_policy(str(row["policy_version"]))
        return {
            key: row[key]
            for key in (
                "event_id",
                "store_id",
                "connector_id",
                "sku_id",
                "mapping_version",
                "expected_version",
                "event_type",
                "canonical_product_id",
                "item_id",
                "merchant_code",
                "decision_key",
                "reason",
                "actor_ref",
                "source_import_id",
                "supersedes_event_id",
                "policy_version",
                "payload_hash",
                "created_at",
            )
        }

    @staticmethod
    def _reconciliation_row_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "row_number": int(row["row_number"]),
            "source_domain": row["source_domain"],
            "source_reference": row["source_reference"],
            "connector_id": row["connector_id"],
            "sku_id": row["sku_id"],
            "item_id": row["item_id"],
            "merchant_code": row["merchant_code"],
            "terminal_status": row["terminal_status"],
            "canonical_product_id": row["canonical_product_id"],
            "internal_part_number": row["internal_part_number"],
            "reason": row["reason"],
            "candidate_product_ids": json.loads(row["candidate_product_ids_json"]),
            "evidence_keys": json.loads(row["evidence_keys_json"]),
            "input_digest": row["input_digest"],
        }

    @staticmethod
    def _terminal_row(
        *,
        row_number: int,
        raw_digest: str,
        terminal_status: ReconciliationStatus,
        reason: str,
        observation: ProductIdentityObservation | None = None,
        canonical_product_id: str | None = None,
        internal_part_number: str | None = None,
        candidate_product_ids: Iterable[str] = (),
        evidence_keys: Iterable[MatchEvidence] = (),
    ) -> dict[str, Any]:
        return {
            "row_number": row_number,
            "source_domain": (
                observation.source_domain.value
                if observation is not None
                else ObservationDomain.UNKNOWN.value
            ),
            "source_reference": (
                observation.source_reference if observation is not None else None
            ),
            "connector_id": observation.connector_id if observation is not None else None,
            "sku_id": observation.sku_id if observation is not None else None,
            "item_id": observation.item_id if observation is not None else None,
            "merchant_code": observation.merchant_code if observation is not None else None,
            "terminal_status": terminal_status.value,
            "canonical_product_id": canonical_product_id,
            "internal_part_number": internal_part_number,
            "reason": reason,
            "candidate_product_ids": sorted(set(candidate_product_ids)),
            "evidence_keys": ProductIdentityService._evidence_values(evidence_keys),
            "input_digest": raw_digest,
        }

    @staticmethod
    def _evidence_values(values: Iterable[MatchEvidence]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = MatchEvidence(value).value
            if normalized not in seen:
                result.append(normalized)
                seen.add(normalized)
        return result

    @staticmethod
    def _candidate_signal_evidence(
        observation: ProductIdentityObservation,
    ) -> tuple[MatchEvidence, ...]:
        values: list[MatchEvidence] = []
        if observation.merchant_code is not None:
            values.append(MatchEvidence.MERCHANT_CODE_EXACT)
        if observation.title is not None:
            values.append(MatchEvidence.TITLE_EXACT)
        return tuple(values)

    @staticmethod
    def _confirmed_evidence(
        current: Mapping[str, Any], observation: ProductIdentityObservation
    ) -> tuple[MatchEvidence, ...]:
        values: list[MatchEvidence] = [
            MatchEvidence.CONFIRMED_MAPPING,
            MatchEvidence.SKU_ID_EXACT,
        ]
        if (
            current["item_id"] is not None
            and observation.item_id is not None
            and str(current["item_id"]) == observation.item_id
        ):
            values.append(MatchEvidence.ITEM_ID_EXACT)
        if (
            current["merchant_code"] is not None
            and observation.merchant_code is not None
            and str(current["merchant_code"]) == observation.merchant_code
        ):
            values.append(MatchEvidence.MERCHANT_CODE_EXACT)
        return tuple(values)

    @staticmethod
    def _candidate_ids(
        observation: ProductIdentityObservation,
        *,
        by_merchant: Mapping[str, Sequence[str]],
        by_title: Mapping[str, Sequence[str]],
    ) -> set[str]:
        candidates = set(by_merchant.get(observation.merchant_code or "", ()))
        if observation.title is not None:
            candidates.update(
                by_title.get(ProductIdentityService._normalized_title(observation.title), ())
            )
        return candidates

    @staticmethod
    def _domain_observation(
        *,
        source_domain: ObservationDomain,
        source_reference: str,
        item: Mapping[str, Any],
        catalog: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        catalog_value = catalog or item
        attributes = catalog_value.get("attributes")
        merchant_code = (
            attributes.get("merchant_code")
            if isinstance(attributes, Mapping)
            and isinstance(attributes.get("merchant_code"), str)
            else None
        )
        return {
            "source_domain": source_domain.value,
            "source_reference": source_reference,
            "store_id": item["store_id"],
            "connector_id": item["connector_id"],
            "sku_id": item["sku_id"],
            "item_id": catalog_value.get("item_id"),
            "merchant_code": merchant_code,
            "title": catalog_value.get("title") or item.get("title"),
        }

    @staticmethod
    def _scope_condition(
        scope: DataScope,
        *,
        alias: str = "readonly_canonical_products",
    ) -> tuple[str, tuple[str, ...]]:
        scope = DataScope(scope)
        if scope is DataScope.OPERATIONAL:
            return f"{alias}.source_kind<>'demo'", ()
        if scope is DataScope.DEMO:
            return f"{alias}.source_kind='demo'", ()
        return "1=1", ()

    @staticmethod
    def _normalized_title(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())

    @classmethod
    def _raw_digest(cls, value: Any) -> str:
        encoded = json.dumps(
            cls._json_safe(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, BaseModel):
            return cls._json_safe(value.model_dump(mode="json"))
        if isinstance(value, Mapping):
            return {
                str(key): cls._json_safe(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (list, tuple)):
            return [cls._json_safe(item) for item in value]
        return {"unsupported_type": type(value).__name__}

    @staticmethod
    def _stable_id(prefix: str, *parts: Any) -> str:
        encoded = json.dumps(
            [str(part) for part in parts],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:32]}"

    @staticmethod
    def _tenant_id(value: str) -> str:
        if not isinstance(value, str) or not value or value != value.strip() or len(value) > 128:
            raise ValueError("invalid_product_identity_tenant")
        return value

    @staticmethod
    def _bounded_read_limit(value: int) -> int:
        if value < 1 or value > 1000:
            raise ValueError("product_identity_query_limit_invalid")
        return value

    def _require_policy(self, value: str) -> None:
        if value != self.policy_version:
            raise ValueError("unsupported_product_identity_policy")


__all__ = ["ProductIdentityService"]
