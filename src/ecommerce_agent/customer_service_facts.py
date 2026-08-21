from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .business.catalog import CatalogService
from .business.inventory import InventoryService
from .business.orders import OrderService
from .connectors import (
    SOURCE_PROVENANCE_VERSION,
    ConnectorRegistry,
    SourceProvenanceResolver,
    merge_source_provenance,
    unknown_source_provenance,
)
from .database import Database
from .evidence_freshness import evidence_freshness
from .product_identity import ProductIdentityService
from .readonly_data import DataScope, ReadonlyDataService, source_manifest_key
from .readonly_readiness.policy import READINESS_REPORT_POLICIES
from .text_utils import redact_sensitive
from .tools import ToolExecutionContext, ToolRegistry, ToolResult, ToolSpec


CUSTOMER_SERVICE_FACTS_VERSION = "customer-service-facts-v1"
CUSTOMER_SERVICE_FIELD_WHITELISTS = MappingProxyType(
    {
        "product": ("sku_id", "title", "status", "sale_price", "currency"),
        "inventory": ("available_quantity", "inbound_quantity"),
        "order": (
            "order_id",
            "order_status",
            "payment_status",
            "currency",
            "total_amount",
            "placed_at",
            "lines",
        ),
        "order_line": ("sku_id", "title", "quantity", "unit_price"),
        "logistics": ("carrier", "status", "last_event", "last_event_at"),
        "after_sale": (
            "case_type",
            "status",
            "requested_amount",
            "approved_amount",
            "reason_code",
            "opened_at",
            "updated_at",
        ),
    }
)


class CustomerSalesFactsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    store_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)


class CustomerAfterSalesFactsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    store_id: str = Field(min_length=1, max_length=128)
    order_id: str = Field(min_length=1, max_length=128)
    include_history: bool = True


class CustomerServiceFactsService:
    """Customer-safe projections over existing read-only domain services."""

    def __init__(self, db: Database, *, connectors: ConnectorRegistry) -> None:
        self.db = db
        self.catalog = CatalogService(db)
        self.inventory = InventoryService(db)
        self.orders = OrderService(db)
        self.identity = ProductIdentityService(db)
        self.readonly = ReadonlyDataService(db)
        self.provenance = SourceProvenanceResolver(connectors)

    def register_agent_tools(self, registry: ToolRegistry) -> None:
        if registry.get("get_customer_sales_facts") is None:
            registry.register(
                ToolSpec(
                    name="get_customer_sales_facts",
                    description=(
                        "读取可信店铺范围内的客服商品、价格和库存事实；"
                        "结果包含缺失、来源和新鲜度状态"
                    ),
                    kind="read",
                    input_model=CustomerSalesFactsInput,
                    handler=self._sales_tool,
                    required_context_fields=("authorized", "shop_id"),
                    policy=self._store_scope_policy,
                    metadata={"domain": "customer_service_sales", "risk_level": "L0"},
                )
            )
        if registry.get("get_customer_after_sales_facts") is None:
            registry.register(
                ToolSpec(
                    name="get_customer_after_sales_facts",
                    description=(
                        "读取已由可信上游同时绑定订单号和店铺号的订单、物流、退款与售后事实；"
                        "结果不包含顾客身份或追踪号码"
                    ),
                    kind="read",
                    input_model=CustomerAfterSalesFactsInput,
                    handler=self._after_sales_tool,
                    required_context_fields=("authorized", "order_id", "shop_id"),
                    policy=self._order_scope_policy,
                    metadata={"domain": "customer_service_after_sales", "risk_level": "L1"},
                )
            )

    def sales_projection(
        self,
        tenant_id: str,
        *,
        store_id: str,
        sku_id: str,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        now = self._aware_time(observed_at or datetime.now(UTC))
        items = self.catalog.list_items(
            tenant_id,
            store_id=store_id,
            sku_id=sku_id,
            status="active",
            limit=2,
        )
        balances = self.inventory.list_balances(
            tenant_id,
            store_id=store_id,
            sku_id=sku_id,
        )
        scope = {"tenant_id": tenant_id, "store_id": store_id, "sku_id": sku_id}
        missing: list[str] = []
        if len(items) > 1:
            return self._blocked(scope, "catalog_identity_ambiguous")
        if not items:
            missing.append("catalog_snapshot_missing")
        if not balances:
            missing.append("inventory_snapshot_missing")
        if not items and not balances:
            return {
                "contract_version": CUSTOMER_SERVICE_FACTS_VERSION,
                "domain": "sales",
                "state": "missing",
                "scope": scope,
                "facts": {
                    "product": self._missing_product(sku_id),
                    "inventory": self._missing_inventory(),
                },
                "missing": missing,
                "data_as_of": None,
                "freshness": None,
                "source_provenance": unknown_source_provenance(
                    basis="customer_service_sales:no_evidence"
                ),
                "evidence": [],
            }

        source_rows = [*items, *balances]
        provenance = self._source_provenance(
            tenant_id,
            store_id=store_id,
            rows=source_rows,
            basis="customer_service_sales",
        )
        if provenance["source_type"] == "unknown":
            return self._blocked(
                scope,
                "source_provenance_unknown",
                source_provenance=provenance,
            )

        facts = {
            "product": (
                {
                    "state": "available",
                    "sku_id": items[0]["sku_id"],
                    "title": self._safe_text(items[0]["title"]),
                    "status": items[0]["status"],
                    "sale_price": items[0]["sale_price"],
                    "currency": items[0]["currency"],
                }
                if items
                else self._missing_product(sku_id)
            ),
            "inventory": self._inventory_projection(balances),
        }
        identity = self._identity_projection(tenant_id, store_id=store_id, rows=source_rows)
        if identity["state"] == "conflict":
            return self._blocked(
                scope,
                "product_identity_conflict",
                source_provenance=provenance,
            )
        observations = [
            self._observation("catalog_snapshot", item) for item in items
        ] + [self._observation("inventory_snapshot", item) for item in balances]
        freshness, data_as_of = self._combined_freshness(
            observations,
            observed_at=now,
            current_ref={"store_id": store_id, "sku_id": sku_id},
        )
        return {
            "contract_version": CUSTOMER_SERVICE_FACTS_VERSION,
            "domain": "sales",
            "state": "partial" if missing else "available",
            "scope": scope,
            "facts": facts,
            "product_identity": identity,
            "missing": missing,
            "data_as_of": data_as_of,
            "freshness": freshness,
            "source_provenance": provenance,
            "evidence": [self._evidence_view(item) for item in observations],
        }

    def after_sales_projection(
        self,
        tenant_id: str,
        *,
        store_id: str,
        order_id: str,
        include_history: bool = True,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        now = self._aware_time(observed_at or datetime.now(UTC))
        scope = {
            "tenant_id": tenant_id,
            "store_id": store_id,
            "order_id": order_id,
        }
        orders = self.orders.list_orders(
            tenant_id,
            store_id=store_id,
            order_id=order_id,
            limit=2,
        )
        if not orders:
            return {
                "contract_version": CUSTOMER_SERVICE_FACTS_VERSION,
                "domain": "after_sales",
                "state": "missing",
                "scope": scope,
                "facts": {},
                "missing": ["order_snapshot_missing"],
                "data_as_of": None,
                "freshness": None,
                "source_provenance": unknown_source_provenance(
                    basis="customer_service_after_sales:no_evidence"
                ),
                "evidence": [],
                "history": [],
            }
        if len(orders) > 1:
            return self._blocked(scope, "order_identity_ambiguous")
        order = orders[0]
        provenance = self._source_provenance(
            tenant_id,
            store_id=store_id,
            rows=[order],
            basis="customer_service_after_sales",
        )
        if provenance["source_type"] == "unknown":
            return self._blocked(
                scope,
                "source_provenance_unknown",
                source_provenance=provenance,
            )

        observations = self._order_observations(order)
        freshness, data_as_of = self._combined_freshness(
            observations,
            observed_at=now,
            current_ref={"store_id": store_id, "order_id": order_id, "version": order["version"]},
        )
        missing = [] if order["logistics"] is not None else ["fulfillment_snapshot_missing"]
        history = (
            self._history_projection(
                tenant_id,
                store_id=store_id,
                order_id=order_id,
                current_version=int(order["version"]),
                observed_at=now,
            )
            if include_history
            else []
        )
        return {
            "contract_version": CUSTOMER_SERVICE_FACTS_VERSION,
            "domain": "after_sales",
            "state": "partial" if missing else "available",
            "scope": scope,
            "facts": self._order_facts(order),
            "missing": missing,
            "data_as_of": data_as_of,
            "freshness": freshness,
            "source_provenance": provenance,
            "evidence": [self._evidence_view(item) for item in observations],
            "history": history,
        }

    @staticmethod
    def _missing_product(sku_id: str) -> dict[str, Any]:
        return {
            "state": "missing",
            "sku_id": sku_id,
            "title": None,
            "status": None,
            "sale_price": None,
            "currency": None,
        }

    @staticmethod
    def _missing_inventory() -> dict[str, Any]:
        return {
            "state": "missing",
            "available_quantity": None,
            "inbound_quantity": None,
        }

    @classmethod
    def _inventory_projection(cls, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return cls._missing_inventory()
        available = sum(
            max(Decimal("0"), Decimal(str(row["on_hand"])) - Decimal(str(row["reserved"])))
            for row in rows
        )
        inbound = sum(Decimal(str(row["inbound"])) for row in rows)
        return {
            "state": "available",
            "available_quantity": cls._decimal(available),
            "inbound_quantity": cls._decimal(inbound),
        }

    def _identity_projection(
        self,
        tenant_id: str,
        *,
        store_id: str,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        mappings: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (str(row["connector_id"]), str(row["sku_id"]))
            if key in seen:
                continue
            seen.add(key)
            mapping = self.identity.get_latest_mapping(
                tenant_id,
                store_id=store_id,
                connector_id=key[0],
                sku_id=key[1],
            )
            if mapping is not None and mapping["event_type"] == "confirmed":
                mappings.append(mapping)
        canonical_ids = {str(item["canonical_product_id"]) for item in mappings}
        if len(canonical_ids) > 1:
            return {"state": "conflict", "canonical_product_id": None}
        if not canonical_ids or len(mappings) != len(seen):
            return {"state": "unmatched", "canonical_product_id": None}
        mapping = max(mappings, key=lambda item: int(item["mapping_version"]))
        return {
            "state": "confirmed",
            "canonical_product_id": next(iter(canonical_ids)),
            "mapping_version": int(mapping["mapping_version"]),
            "policy_version": mapping["policy_version"],
        }

    @staticmethod
    def _order_facts(order: dict[str, Any]) -> dict[str, Any]:
        logistics = order["logistics"]
        return {
            "order": {
                "state": "available",
                "order_id": order["order_id"],
                "order_status": order["order_status"],
                "payment_status": order["payment_status"],
                "currency": order["currency"],
                "total_amount": order["total_amount"],
                "placed_at": order["placed_at"],
                "lines": [
                    {
                        "sku_id": line["sku_id"],
                        "title": CustomerServiceFactsService._safe_text(line["title"]),
                        "quantity": line["quantity"],
                        "unit_price": line["unit_price"],
                    }
                    for line in order["lines"]
                ],
            },
            "logistics": (
                {
                    "state": "available",
                    "carrier": CustomerServiceFactsService._safe_text(
                        logistics["carrier"]
                    ),
                    "status": logistics["status"],
                    "last_event": CustomerServiceFactsService._safe_text(
                        logistics["last_event"]
                    ),
                    "last_event_at": logistics["last_event_at"],
                }
                if logistics is not None
                else {
                    "state": "missing",
                    "carrier": None,
                    "status": None,
                    "last_event": None,
                    "last_event_at": None,
                }
            ),
            "after_sales": [
                {
                    "case_type": item["case_type"],
                    "status": item["status"],
                    "requested_amount": item["requested_amount"],
                    "approved_amount": item["approved_amount"],
                    "reason_code": CustomerServiceFactsService._safe_text(
                        item["reason_code"]
                    ),
                    "opened_at": item["opened_at"],
                    "updated_at": item["updated_at"],
                }
                for item in order["after_sales"]
            ],
        }

    def _history_projection(
        self,
        tenant_id: str,
        *,
        store_id: str,
        order_id: str,
        current_version: int,
        observed_at: datetime,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for event in self.orders.history(tenant_id, order_id, store_id=store_id):
            version = int(event["version"])
            snapshot = event["snapshot"]
            evidence_ref = {
                "evidence_id": self._reference(
                    "order_history", order_id, version, event["source_updated_at"]
                ),
                "version": version,
                "data_as_of": event["source_updated_at"],
            }
            current_ref = {"order_id": order_id, "version": current_version}
            if version != current_version:
                freshness = evidence_freshness(
                    status="superseded",
                    reason_codes=["newer_order_version_available"],
                    evidence_ref=evidence_ref,
                    current_ref=current_ref,
                )
            else:
                freshness, _ = self._combined_freshness(
                    self._order_observations(snapshot, version=version),
                    observed_at=observed_at,
                    current_ref=current_ref,
                )
            result.append(
                {
                    "version": version,
                    "current": version == current_version,
                    "data_as_of": event["source_updated_at"],
                    "freshness": freshness,
                    "facts": self._order_facts(snapshot),
                }
            )
        return result

    def _source_provenance(
        self,
        tenant_id: str,
        *,
        store_id: str,
        rows: list[dict[str, Any]],
        basis: str,
    ) -> dict[str, Any]:
        values = [
            self._row_provenance(
                tenant_id,
                store_id=store_id,
                connector_id=str(row.get("connector_id") or ""),
                source_id=row.get("source_id"),
                basis=basis,
            )
            for row in rows
        ]
        return merge_source_provenance(values, basis=basis)

    def _row_provenance(
        self,
        tenant_id: str,
        *,
        store_id: str,
        connector_id: str,
        source_id: Any,
        basis: str,
    ) -> dict[str, Any]:
        manifest_key = source_manifest_key(str(source_id) if source_id is not None else None)
        if manifest_key is not None:
            manifests = self.readonly.list_imports(
                tenant_id,
                store_id=store_id,
                scope=DataScope.ALL,
                limit=1000,
            )
            matching = [
                item
                for item in manifests
                if item["report_type"] == manifest_key[0]
                and str(item["content_digest"]).startswith(manifest_key[1])
            ]
            kinds = {str(item["source_kind"]) for item in matching}
            if len(matching) != 1 or len(kinds) != 1:
                return unknown_source_provenance(basis=f"{basis}:manifest_unresolved")
            manifest = matching[0]
            virtual = manifest["source_kind"] == "demo"
            return {
                "policy_version": SOURCE_PROVENANCE_VERSION,
                "source_type": "virtual" if virtual else "operational",
                "virtual": virtual,
                "connectors": [
                    {
                        "connector_id": str(manifest["source_system"]),
                        "capability_version": str(manifest["mapping_version"]),
                        "virtual": virtual,
                    }
                ],
                "completeness": "complete",
                "basis": f"{basis}:readonly_import_manifest",
            }
        if not source_id:
            return unknown_source_provenance(basis=f"{basis}:source_id_missing")
        return self.provenance.freeze([connector_id], basis=f"{basis}:connector")

    @classmethod
    def _combined_freshness(
        cls,
        observations: list[dict[str, Any]],
        *,
        observed_at: datetime,
        current_ref: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        times = [cls._aware_time(item["data_as_of"]) for item in observations]
        data_as_of = min(times)
        future = [
            item
            for item, source_time in zip(observations, times, strict=True)
            if source_time > observed_at
        ]
        stale = [
            item
            for item, source_time in zip(observations, times, strict=True)
            if source_time <= observed_at
            and (observed_at - source_time).total_seconds()
            > READINESS_REPORT_POLICIES[item["report_type"]].max_age_hours * 3600
        ]
        reasons = []
        if future:
            reasons.append("snapshot_time_in_future")
            reasons.extend(f"{item['report_type']}_time_in_future" for item in future)
        if stale:
            reasons.append("snapshot_age_exceeded")
            reasons.extend(
                f"{item['report_type']}_age_exceeded" for item in stale
            )
        evidence_ref = {
            "evidence_ids": [item["reference"] for item in observations],
            "data_as_of": data_as_of.isoformat(),
        }
        max_age = min(
            READINESS_REPORT_POLICIES[item["report_type"]].max_age_hours
            for item in observations
        )
        return (
            evidence_freshness(
                status="stale" if stale or future else "current",
                reason_codes=reasons,
                evidence_ref=evidence_ref,
                current_ref=current_ref,
                max_age_hours=max_age,
            ),
            data_as_of.isoformat(),
        )

    @classmethod
    def _observation(cls, report_type: str, row: dict[str, Any]) -> dict[str, Any]:
        data_as_of = str(row["source_updated_at"])
        return {
            "report_type": report_type,
            "data_as_of": data_as_of,
            "version": int(row["version"]),
            "reference": cls._reference(
                report_type,
                row.get("store_id"),
                row.get("sku_id") or row.get("order_id"),
                row["version"],
                data_as_of,
            ),
        }

    @classmethod
    def _order_observations(
        cls,
        order: dict[str, Any],
        *,
        version: int | None = None,
    ) -> list[dict[str, Any]]:
        value = order if version is None else {**order, "version": version}
        observations = [cls._observation("order_snapshot", value)]
        if order.get("logistics") is not None:
            observations.append(cls._observation("fulfillment_snapshot", value))
        if order.get("after_sales"):
            observations.append(cls._observation("refund_snapshot", value))
        return observations

    @staticmethod
    def _evidence_view(observation: dict[str, Any]) -> dict[str, Any]:
        return {
            "evidence_id": observation["reference"],
            "report_type": observation["report_type"],
            "data_as_of": observation["data_as_of"],
            "version": observation["version"],
            "current": True,
        }

    @staticmethod
    def _blocked(
        scope: dict[str, Any],
        reason: str,
        *,
        source_provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = {
            "contract_version": CUSTOMER_SERVICE_FACTS_VERSION,
            "domain": "after_sales" if "order_id" in scope else "sales",
            "state": "blocked",
            "reason": reason,
            "scope": scope,
            "facts": {},
            "missing": [],
            "data_as_of": None,
            "freshness": None,
            "source_provenance": source_provenance
            or unknown_source_provenance(basis=f"customer_service:{reason}"),
            "evidence": [],
        }
        if "order_id" in scope:
            result["history"] = []
        return result

    @staticmethod
    def _store_scope_policy(
        arguments: BaseModel, context: ToolExecutionContext
    ) -> str | None:
        value = CustomerSalesFactsInput.model_validate(arguments.model_dump())
        if value.store_id != str(context.trusted_context.get("shop_id") or ""):
            return "store_scope_mismatch"
        return None

    @staticmethod
    def _order_scope_policy(
        arguments: BaseModel, context: ToolExecutionContext
    ) -> str | None:
        value = CustomerAfterSalesFactsInput.model_validate(arguments.model_dump())
        if value.order_id != str(context.trusted_context.get("order_id") or ""):
            return "order_scope_mismatch"
        if value.store_id != str(context.trusted_context.get("shop_id") or ""):
            return "store_scope_mismatch"
        return None

    def _sales_tool(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        value = CustomerSalesFactsInput.model_validate(arguments.model_dump())
        return ToolResult(
            status="success",
            output=self.sales_projection(
                context.tenant_id,
                store_id=value.store_id,
                sku_id=value.sku_id,
            ),
        )

    def _after_sales_tool(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        value = CustomerAfterSalesFactsInput.model_validate(arguments.model_dump())
        return ToolResult(
            status="success",
            output=self.after_sales_projection(
                context.tenant_id,
                store_id=value.store_id,
                order_id=value.order_id,
                include_history=value.include_history,
            ),
        )

    @staticmethod
    def _aware_time(value: datetime | str) -> datetime:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("customer_service_fact_time_timezone_required")
        return parsed.astimezone(UTC)

    @staticmethod
    def _decimal(value: Decimal) -> str:
        return format(value.quantize(Decimal("0.01")), "f")

    @staticmethod
    def _safe_text(value: Any) -> Any:
        if value is None:
            return None
        return redact_sensitive(str(value))[0]

    @staticmethod
    def _reference(*parts: Any) -> str:
        digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8"))
        return f"cs-fact-{digest.hexdigest()[:24]}"


__all__ = [
    "CUSTOMER_SERVICE_FIELD_WHITELISTS",
    "CUSTOMER_SERVICE_FACTS_VERSION",
    "CustomerAfterSalesFactsInput",
    "CustomerSalesFactsInput",
    "CustomerServiceFactsService",
]
