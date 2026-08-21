from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from ecommerce_agent.business.catalog import CatalogItemUpsert, CatalogService
from ecommerce_agent.business.inventory import InventoryBalanceUpsert, InventoryService
from ecommerce_agent.business.orders import (
    AfterSaleCaseInput,
    LogisticsSnapshotInput,
    OrderLineInput,
    OrderService,
    OrderUpsert,
)
from ecommerce_agent.connectors import ConnectorRegistry, VirtualTaobaoConnector
from ecommerce_agent.customer_service_facts import (
    CUSTOMER_SERVICE_FIELD_WHITELISTS,
    CustomerAfterSalesFactsInput,
    CustomerServiceFactsService,
    CustomerSalesFactsInput,
)
from ecommerce_agent.database import Database
from ecommerce_agent.product_identity import (
    CanonicalProductCreate,
    MappingDecisionInput,
)
from ecommerce_agent.readonly_data import (
    ImportManifestInput,
    ImportReference,
    ReadonlyDataService,
    ReferenceKind,
    SourceKind,
)
from ecommerce_agent.tools import ToolExecutionContext, ToolRegistry


NOW = datetime(2026, 8, 20, 4, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (CustomerSalesFactsInput, {"store_id": " ", "sku_id": "SKU-1"}),
        (CustomerSalesFactsInput, {"store_id": "store-a", "sku_id": "\t"}),
        (
            CustomerAfterSalesFactsInput,
            {"store_id": " ", "order_id": "ORDER-1"},
        ),
        (
            CustomerAfterSalesFactsInput,
            {"store_id": "store-a", "order_id": "\n"},
        ),
    ],
)
def test_fact_tool_inputs_reject_blank_scope_values(
    model: type[BaseModel], payload: dict[str, str]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


class SecondaryVirtualTaobaoConnector(VirtualTaobaoConnector):
    CONNECTOR_ID = "virtual_taobao_secondary"


def _service(tmp_path) -> tuple[CustomerServiceFactsService, Database]:
    db = Database(tmp_path / "m8r-wp2.sqlite3")
    db.initialize()
    connectors = ConnectorRegistry()
    connectors.register(VirtualTaobaoConnector())
    return CustomerServiceFactsService(db, connectors=connectors), db


def _catalog(db: Database, *, source_time: datetime = NOW) -> None:
    CatalogService(db).upsert(
        "tenant-a",
        CatalogItemUpsert(
            connector_id="virtual_taobao",
            store_id="store-a",
            item_id="ITEM-1",
            sku_id="SKU-1",
            title="恒温水壶",
            status="active",
            sale_price=Decimal("129.00"),
            currency="CNY",
            attributes={"merchant_code": "SECRET-MERCHANT", "capacity": "1.5L"},
            source_updated_at=source_time,
            source_id="virtual:catalog:1",
        ),
    )


def _inventory(db: Database, *, source_time: datetime = NOW) -> None:
    InventoryService(db).upsert(
        "tenant-a",
        InventoryBalanceUpsert(
            connector_id="virtual_taobao",
            store_id="store-a",
            warehouse_id="SECRET-WAREHOUSE",
            sku_id="SKU-1",
            on_hand=Decimal("8"),
            reserved=Decimal("3"),
            inbound=Decimal("2"),
            average_daily_sales=Decimal("1"),
            source_updated_at=source_time,
            source_id="virtual:inventory:1",
        ),
    )


def _secondary_inventory(db: Database, *, source_time: datetime = NOW) -> None:
    InventoryService(db).upsert(
        "tenant-a",
        InventoryBalanceUpsert(
            connector_id=SecondaryVirtualTaobaoConnector.CONNECTOR_ID,
            store_id="store-a",
            warehouse_id="SECONDARY-WAREHOUSE",
            sku_id="SKU-1",
            on_hand=Decimal("8"),
            reserved=Decimal("3"),
            inbound=Decimal("2"),
            average_daily_sales=Decimal("1"),
            source_updated_at=source_time,
            source_id="virtual:secondary-inventory:1",
        ),
    )


def _order(
    db: Database,
    *,
    source_time: datetime = NOW,
    status: str = "shipped",
    logistics_status: str = "in_transit",
    connector_id: str = "virtual_taobao",
    source_id: str = "virtual:order:1",
) -> None:
    OrderService(db).upsert(
        "tenant-a",
        OrderUpsert(
            connector_id=connector_id,
            store_id="store-a",
            order_id="ORDER-1",
            order_status=status,
            payment_status="paid",
            currency="CNY",
            total_amount=Decimal("129.00"),
            placed_at=NOW - timedelta(days=1),
            buyer_ref_hash="customer-secret-hash-0000000001",
            lines=[
                OrderLineInput(
                    line_id="SECRET-LINE-1",
                    sku_id="SKU-1",
                    title="恒温水壶",
                    quantity=1,
                    unit_price=Decimal("129.00"),
                )
            ],
            logistics=LogisticsSnapshotInput(
                carrier="测试快递",
                tracking_no_masked="TRACK****0001",
                status=logistics_status,
                last_event="运输中，联系电话 13800138000",
                last_event_at=source_time - timedelta(hours=1),
            ),
            after_sales=[
                AfterSaleCaseInput(
                    case_id="SECRET-CASE-1",
                    case_type="refund",
                    status="reviewing",
                    requested_amount=Decimal("20.00"),
                    approved_amount=Decimal("0"),
                    reason_code="price_protection",
                    opened_at=source_time - timedelta(hours=2),
                    updated_at=source_time - timedelta(hours=1),
                )
            ],
            source_updated_at=source_time,
            source_id=source_id,
        ),
    )


def _context(**trusted_changes) -> ToolExecutionContext:
    trusted = {"authorized": True, "shop_id": "store-a", "order_id": "ORDER-1"}
    trusted.update(trusted_changes)
    return ToolExecutionContext(
        tenant_id="tenant-a",
        client_id="client-a",
        session_id="session-a",
        trace_id="trace-a",
        trusted_context=trusted,
    )


def _all_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _all_keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _all_keys(item)}
    return set()


def test_sales_projection_is_store_scoped_minimal_and_traceable(tmp_path) -> None:
    service, db = _service(tmp_path)
    _catalog(db)
    _inventory(db)

    result = service.sales_projection(
        "tenant-a", store_id="store-a", sku_id="SKU-1", observed_at=NOW
    )

    assert result["state"] == "available"
    assert result["scope"] == {
        "tenant_id": "tenant-a",
        "store_id": "store-a",
        "sku_id": "SKU-1",
    }
    assert result["facts"]["product"] == {
        "state": "available",
        "sku_id": "SKU-1",
        "title": "恒温水壶",
        "status": "active",
        "sale_price": "129.00",
        "currency": "CNY",
    }
    assert result["facts"]["inventory"]["available_quantity"] == "5.00"
    assert result["facts"]["inventory"]["inbound_quantity"] == "2.00"
    assert result["freshness"]["usable_as_current"] is True
    assert result["source_provenance"]["source_type"] == "virtual"
    assert result["product_identity"] == {
        "state": "unmatched",
        "canonical_product_id": None,
    }
    assert not {
        "attributes",
        "warehouse_id",
        "source_id",
        "payload_hash",
    } & _all_keys(result)


def test_sales_missing_inventory_remains_missing_instead_of_zero(tmp_path) -> None:
    service, db = _service(tmp_path)
    _catalog(db)

    result = service.sales_projection(
        "tenant-a", store_id="store-a", sku_id="SKU-1", observed_at=NOW
    )

    assert result["state"] == "partial"
    assert result["facts"]["inventory"] == {
        "state": "missing",
        "available_quantity": None,
        "inbound_quantity": None,
    }
    assert "inventory_snapshot_missing" in result["missing"]


def test_stale_sales_snapshot_cannot_claim_current(tmp_path) -> None:
    service, db = _service(tmp_path)
    old = NOW - timedelta(days=5)
    _catalog(db, source_time=old)
    _inventory(db, source_time=old)

    result = service.sales_projection(
        "tenant-a", store_id="store-a", sku_id="SKU-1", observed_at=NOW
    )

    assert result["state"] == "available"
    assert result["data_as_of"] == old.isoformat()
    assert result["freshness"]["status"] == "stale"
    assert result["freshness"]["usable_as_current"] is False
    assert "snapshot_age_exceeded" in result["freshness"]["reason_codes"]


@pytest.mark.parametrize(
    ("source_kind", "expected_type"),
    [(SourceKind.ACTUAL, "operational"), (SourceKind.DEMO, "virtual")],
)
def test_readonly_manifest_is_authoritative_for_source_provenance(
    tmp_path, source_kind: SourceKind, expected_type: str
) -> None:
    service, db = _service(tmp_path)
    digest = "d" * 64
    ReadonlyDataService(db).record_import(
        "tenant-a",
        ImportManifestInput(
            store_id="store-a",
            source_kind=source_kind,
            source_system="controlled_export",
            report_type="catalog_snapshot",
            report_period="2026-08-20",
            exported_at=NOW,
            schema_fingerprint="a" * 64,
            content_digest=digest,
            mapping_version="catalog-cn-v1",
            parsed_rows=1,
            data_as_of=NOW,
            references=(
                ImportReference(
                    kind=ReferenceKind.RAW_FILE,
                    reference=f"objects/readonly-imports/{digest}.csv",
                    content_digest=digest,
                ),
            ),
        ),
    )
    CatalogService(db).upsert(
        "tenant-a",
        CatalogItemUpsert(
            connector_id="controlled_export",
            store_id="store-a",
            item_id="ITEM-1",
            sku_id="SKU-1",
            title="恒温水壶",
            status="active",
            sale_price=Decimal("129.00"),
            currency="CNY",
            source_updated_at=NOW,
            source_id=f"readonly:catalog_snapshot:{digest[:24]}:row-1",
        ),
    )

    result = service.sales_projection(
        "tenant-a", store_id="store-a", sku_id="SKU-1", observed_at=NOW
    )

    assert result["state"] == "partial"
    assert result["source_provenance"]["source_type"] == expected_type
    assert result["source_provenance"]["completeness"] == "complete"


def test_only_confirmed_product_mapping_becomes_canonical(tmp_path) -> None:
    service, db = _service(tmp_path)
    _catalog(db)
    _inventory(db)
    product = service.identity.register_product(
        "tenant-a",
        CanonicalProductCreate(
            store_id="store-a",
            internal_part_number="PART-1",
            merchant_code="MERCHANT-1",
            title="恒温水壶",
            source_kind=SourceKind.DEMO,
            source_reference="demo:product-master:v1",
        ),
    )
    service.identity.confirm_mapping(
        "tenant-a",
        MappingDecisionInput(
            store_id="store-a",
            connector_id="virtual_taobao",
            sku_id="SKU-1",
            item_id="ITEM-1",
            merchant_code="MERCHANT-1",
            canonical_product_id=product["canonical_product_id"],
            expected_version=0,
            decision_key="decision:sku-1:v1",
            reason="manual_identity_verified",
            actor_ref="operator:sha256:test",
        ),
    )

    result = service.sales_projection(
        "tenant-a", store_id="store-a", sku_id="SKU-1", observed_at=NOW
    )

    assert result["product_identity"]["state"] == "confirmed"
    assert result["product_identity"]["canonical_product_id"] == product[
        "canonical_product_id"
    ]


def test_every_fact_source_must_share_the_confirmed_product_identity(tmp_path) -> None:
    service, db = _service(tmp_path)
    service.provenance.registry.register(SecondaryVirtualTaobaoConnector())
    _catalog(db)
    _secondary_inventory(db)
    product = service.identity.register_product(
        "tenant-a",
        CanonicalProductCreate(
            store_id="store-a",
            internal_part_number="PART-1",
            merchant_code="MERCHANT-1",
            title="恒温水壶",
            source_kind=SourceKind.DEMO,
            source_reference="demo:product-master:v1",
        ),
    )
    service.identity.confirm_mapping(
        "tenant-a",
        MappingDecisionInput(
            store_id="store-a",
            connector_id="virtual_taobao",
            sku_id="SKU-1",
            item_id="ITEM-1",
            merchant_code="MERCHANT-1",
            canonical_product_id=product["canonical_product_id"],
            expected_version=0,
            decision_key="decision:sku-1:v1",
            reason="manual_identity_verified",
            actor_ref="operator:sha256:test",
        ),
    )

    result = service.sales_projection(
        "tenant-a", store_id="store-a", sku_id="SKU-1", observed_at=NOW
    )

    assert result["state"] == "available"
    assert result["product_identity"] == {
        "state": "unmatched",
        "canonical_product_id": None,
    }


def test_after_sales_projection_requires_trusted_order_and_removes_pii(tmp_path) -> None:
    service, db = _service(tmp_path)
    _order(db)
    registry = ToolRegistry()
    service.register_agent_tools(registry)
    try:
        with pytest.raises(ValueError, match="order_scope_mismatch"):
            registry.validate_selection(
                name="get_customer_after_sales_facts",
                arguments={"store_id": "store-a", "order_id": "ORDER-2"},
                requested_mode="observe",
                context=_context(),
            )
        with pytest.raises(ValueError, match="store_scope_mismatch"):
            registry.validate_selection(
                name="get_customer_after_sales_facts",
                arguments={"store_id": "store-b", "order_id": "ORDER-1"},
                requested_mode="observe",
                context=_context(),
            )

        spec, arguments = registry.validate_selection(
            name="get_customer_after_sales_facts",
            arguments={"store_id": "store-a", "order_id": "ORDER-1"},
            requested_mode="observe",
            context=_context(),
        )
        result = registry.execute(spec=spec, arguments=arguments, context=_context())
    finally:
        registry.close()

    assert result.status == "success"
    assert result.postcondition_met is True
    assert result.output["facts"]["order"]["order_id"] == "ORDER-1"
    assert result.output["facts"]["logistics"]["status"] == "in_transit"
    assert result.output["facts"]["after_sales"][0]["case_type"] == "refund"
    assert "13800138000" not in json.dumps(result.output, ensure_ascii=False)
    assert not {
        "buyer_ref_hash",
        "tracking_no_masked",
        "line_id",
        "case_id",
        "source_id",
        "payload_hash",
    } & _all_keys(result.output)


def test_customer_service_field_whitelists_match_the_public_projection(tmp_path) -> None:
    service, db = _service(tmp_path)
    _catalog(db)
    _inventory(db)
    _order(db)

    sales = service.sales_projection(
        "tenant-a", store_id="store-a", sku_id="SKU-1", observed_at=NOW
    )
    after_sales = service.after_sales_projection(
        "tenant-a", store_id="store-a", order_id="ORDER-1", observed_at=NOW
    )

    assert set(sales["facts"]["product"]) == {
        "state",
        *CUSTOMER_SERVICE_FIELD_WHITELISTS["product"],
    }
    assert set(sales["facts"]["inventory"]) == {
        "state",
        *CUSTOMER_SERVICE_FIELD_WHITELISTS["inventory"],
    }
    assert set(after_sales["facts"]["order"]) == {
        "state",
        *CUSTOMER_SERVICE_FIELD_WHITELISTS["order"],
    }
    assert set(after_sales["facts"]["order"]["lines"][0]) == set(
        CUSTOMER_SERVICE_FIELD_WHITELISTS["order_line"]
    )
    assert set(after_sales["facts"]["logistics"]) == {
        "state",
        *CUSTOMER_SERVICE_FIELD_WHITELISTS["logistics"],
    }
    assert set(after_sales["facts"]["after_sales"][0]) == set(
        CUSTOMER_SERVICE_FIELD_WHITELISTS["after_sale"]
    )


def test_after_sales_history_keeps_correction_but_only_latest_is_current(tmp_path) -> None:
    service, db = _service(tmp_path)
    _order(db, source_time=NOW - timedelta(hours=2))
    _order(
        db,
        source_time=NOW,
        status="delivered",
        logistics_status="delivered",
    )

    result = service.after_sales_projection(
        "tenant-a",
        store_id="store-a",
        order_id="ORDER-1",
        include_history=True,
        observed_at=NOW,
    )

    assert result["facts"]["order"]["order_status"] == "delivered"
    assert [item["version"] for item in result["history"]] == [1, 2]
    assert result["history"][0]["current"] is False
    assert result["history"][0]["freshness"]["status"] == "superseded"
    assert result["history"][0]["freshness"]["usable_as_current"] is False
    assert result["history"][1]["current"] is True
    assert result["history"][1]["freshness"]["status"] == "current"


def test_unknown_source_provenance_fails_closed_without_fact_leak(tmp_path) -> None:
    service, db = _service(tmp_path)
    CatalogService(db).upsert(
        "tenant-a",
        CatalogItemUpsert(
            connector_id="unknown-connector",
            store_id="store-a",
            item_id="ITEM-1",
            sku_id="SKU-1",
            title="恒温水壶",
            status="active",
            sale_price=Decimal("129.00"),
            currency="CNY",
            source_updated_at=NOW,
            source_id="uncontrolled:catalog:1",
        ),
    )

    result = service.sales_projection(
        "tenant-a", store_id="store-a", sku_id="SKU-1", observed_at=NOW
    )

    assert result["state"] == "blocked"
    assert result["reason"] == "source_provenance_unknown"
    assert result["facts"] == {}


def test_blocked_after_sales_projection_keeps_an_empty_history_shape(tmp_path) -> None:
    service, db = _service(tmp_path)
    _order(db, connector_id="unknown-connector", source_id="uncontrolled:order:1")

    result = service.after_sales_projection(
        "tenant-a", store_id="store-a", order_id="ORDER-1", observed_at=NOW
    )

    assert result["state"] == "blocked"
    assert result["facts"] == {}
    assert result["history"] == []


def test_customer_service_tools_are_additive_and_require_trusted_store(tmp_path) -> None:
    service, _db = _service(tmp_path)
    registry = ToolRegistry()
    service.register_agent_tools(registry)
    try:
        names = {item["name"] for item in registry.catalog_for_model()}
        assert "get_customer_sales_facts" in names
        assert "get_customer_after_sales_facts" in names
        with pytest.raises(ValueError, match="trusted_context_missing:shop_id"):
            registry.validate_selection(
                name="get_customer_sales_facts",
                arguments={"store_id": "store-a", "sku_id": "SKU-1"},
                requested_mode="observe",
                context=_context(shop_id=""),
            )
        with pytest.raises(ValueError, match="store_scope_mismatch"):
            registry.validate_selection(
                name="get_customer_sales_facts",
                arguments={"store_id": "store-b", "sku_id": "SKU-1"},
                requested_mode="observe",
                context=_context(),
            )
    finally:
        registry.close()
