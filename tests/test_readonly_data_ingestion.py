from __future__ import annotations

import csv
import io
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ecommerce_agent.database import Database
from ecommerce_agent.readonly_data import (
    REPORT_ADAPTERS,
    REPORT_CONTRACTS,
    DataScope,
    ReadonlyDataService,
    ReadonlyReportIngestionService,
    ReportFileFormat,
    ReportImportJob,
    ReportImportRequest,
    SourceKind,
    content_digest,
)
from ecommerce_agent.business import (
    CatalogService,
    FinanceService,
    InventoryService,
    OrderService,
    OrderUpsert,
)
from ecommerce_agent.business.orders import OrderLineInput
from ecommerce_agent.business.source_versioning import SourceVersionError


EXPORTED_AT = datetime(2026, 8, 18, 4, 0, tzinfo=UTC)
DATA_AS_OF = EXPORTED_AT - timedelta(hours=1)


ADAPTER_CASES = {
    "catalog_snapshot": {
        "row": {
            "store_id": "store-a",
            "item_id": "ITEM-1",
            "sku_id": "SKU-1",
            "title": "恒温水壶",
            "status": "active",
            "sale_price": "129.00",
            "currency": "CNY",
            "merchant_code": "PART-001",
        },
        "missing": "sku_id",
        "invalid": ("sale_price", "not-money"),
    },
    "inventory_snapshot": {
        "row": {
            "store_id": "store-a",
            "warehouse_id": "WH-1",
            "sku_id": "SKU-1",
            "on_hand": "8",
            "reserved": "1",
            "inbound": "2",
            "average_daily_sales": "1.5",
        },
        "missing": "warehouse_id",
        "invalid": ("on_hand", "-1"),
    },
    "order_snapshot": {
        "row": {
            "store_id": "store-a",
            "order_id": "ORDER-1",
            "order_status": "paid",
            "payment_status": "paid",
            "currency": "CNY",
            "total_amount": "129.00",
            "placed_at": "2026-08-18T09:00:00",
            "line_id": "LINE-1",
            "sku_id": "SKU-1",
            "title": "恒温水壶",
            "quantity": "1",
            "unit_price": "129.00",
        },
        "missing": "order_id",
        "invalid": ("quantity", "0"),
    },
    "fulfillment_snapshot": {
        "row": {
            "store_id": "store-a",
            "order_id": "ORDER-1",
            "carrier": "SF",
            "tracking_no_masked": "SF****1234",
            "logistics_status": "in_transit",
            "last_event": "已到达中转站",
            "last_event_at": "2026-08-18T10:00:00",
        },
        "missing": "tracking_no_masked",
        "invalid": ("logistics_status", "teleported"),
    },
    "operations_daily": {
        "row": {
            "store_id": "store-a",
            "metric_date": "2026-08-18",
            "channel": "search",
            "visitors": "100",
            "orders": "10",
            "sales_amount": "1290.00",
            "ad_spend": "100.00",
            "currency": "CNY",
        },
        "missing": "metric_date",
        "invalid": ("visitors", "not-an-int"),
    },
    "marketing_daily": {
        "row": {
            "store_id": "store-a",
            "campaign_id": "CAMPAIGN-1",
            "metric_date": "2026-08-18",
            "campaign_name": "夏日推广",
            "channel": "search",
            "objective": "conversion",
            "status": "active",
            "spend": "100.00",
            "attributed_revenue": "500.00",
            "attributed_orders": "5",
            "impressions": "1000",
            "clicks": "100",
            "currency": "CNY",
        },
        "missing": "campaign_id",
        "invalid": ("impressions", "-1"),
    },
    "refund_snapshot": {
        "row": {
            "store_id": "store-a",
            "order_id": "ORDER-1",
            "case_id": "CASE-1",
            "case_type": "refund",
            "status": "approved",
            "requested_amount": "129.00",
            "approved_amount": "129.00",
            "reason_code": "quality_issue",
            "opened_at": "2026-08-18T10:10:00",
            "updated_at": "2026-08-18T10:20:00",
        },
        "missing": "case_id",
        "invalid": ("approved_amount", "-1"),
    },
    "settlement_statement": {
        "row": {
            "store_id": "store-a",
            "statement_key": "STMT-1",
            "period_start": "2026-08-01",
            "period_end": "2026-08-18",
            "gross_sales": "1000.00",
            "refund_amount": "100.00",
            "fee_amount": "50.00",
            "settlement_amount": "850.00",
            "currency": "CNY",
        },
        "missing": "statement_key",
        "invalid": ("period_start", "not-a-date"),
    },
}


def _request(
    content: bytes,
    *,
    report_type: str,
    mapping_version: str = "generic-cn-v1",
    file_format: ReportFileFormat = ReportFileFormat.CSV,
    exported_at: datetime = EXPORTED_AT,
    data_as_of: datetime = DATA_AS_OF,
    sheet_name: str | None = None,
    source_kind: SourceKind = SourceKind.ACTUAL,
) -> ReportImportRequest:
    digest = content_digest(content)
    suffix = file_format.value
    return ReportImportRequest(
        store_id="store-a",
        source_kind=source_kind,
        source_system="controlled_export",
        report_type=report_type,
        mapping_version=mapping_version,
        report_period="2026-08-18",
        exported_at=exported_at,
        data_as_of=data_as_of,
        file_format=file_format,
        storage_ref=f"objects/readonly-imports/{digest}.{suffix}",
        source_timezone="Asia/Shanghai",
        sheet_name=sheet_name,
    )


def _csv(rows: list[dict[str, str]], *, headers: list[str] | None = None) -> bytes:
    selected_headers = headers or list(rows[0])
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=selected_headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def _seed_parent_order(db: Database) -> None:
    OrderService(db).upsert(
        "tenant-a",
        OrderUpsert(
            connector_id="controlled_export",
            store_id="store-a",
            order_id="ORDER-1",
            order_status="paid",
            payment_status="paid",
            currency="CNY",
            total_amount=Decimal("129.00"),
            placed_at=DATA_AS_OF - timedelta(hours=2),
            lines=[
                OrderLineInput(
                    line_id="LINE-1",
                    sku_id="SKU-1",
                    title="恒温水壶",
                    quantity=1,
                    unit_price=Decimal("129.00"),
                )
            ],
            source_updated_at=DATA_AS_OF - timedelta(hours=1),
            source_id="fixture:parent-order",
        ),
    )


def _service_for_case(tmp_path, report_type: str, scenario: str):
    db = Database(tmp_path / f"wp2-{report_type}-{scenario}.sqlite3")
    db.initialize()
    if report_type in {"fulfillment_snapshot", "refund_snapshot"}:
        _seed_parent_order(db)
    return db, ReadonlyReportIngestionService(db)


def _xlsx(rows: list[list[str]], *, formula_cell: str | None = None) -> bytes:
    def cell(reference: str, value: str) -> str:
        if formula_cell == reference:
            return f'<c r="{reference}"><f>1+1</f><v>2</v></c>'
        return (
            f'<c r="{reference}" t="inlineStr"><is><t>'
            f"{value}"
            "</t></is></c>"
        )

    sheet_rows: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(row, start=1):
            column = chr(ord("A") + column_index - 1)
            cells.append(cell(f"{column}{row_index}", value))
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
              <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
              <Default Extension="xml" ContentType="application/xml"/>
              <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
              <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
            </Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
            </Relationships>""",
        )
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
              xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets><sheet name="库存" sheetId="1" r:id="rId1"/></sheets>
            </workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
            </Relationships>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>"""
            + "".join(sheet_rows)
            + "</sheetData></worksheet>",
        )
    return payload.getvalue()


def test_wp2_registry_is_authoritative_and_declares_grain_unit_timezone() -> None:
    required = {
        "catalog_snapshot",
        "inventory_snapshot",
        "order_snapshot",
        "fulfillment_snapshot",
        "operations_daily",
        "marketing_daily",
        "refund_snapshot",
        "settlement_statement",
    }

    registered = {adapter.report_type for adapter in REPORT_ADAPTERS.list()}

    assert required <= registered
    for report_type in required:
        adapter = REPORT_ADAPTERS.get(report_type, "generic-cn-v1")
        assert adapter.grain
        assert adapter.amount_unit
        assert adapter.source_timezone == "request"
        assert adapter.policy.allowed_fields == frozenset(adapter.row_model.model_fields)
        assert REPORT_CONTRACTS.get(report_type, "generic-cn-v1") is adapter.policy


def test_catalog_csv_import_is_private_scoped_traceable_and_replay_safe(tmp_path) -> None:
    content = (
        "store_id,item_id,sku_id,title,status,sale_price,currency,merchant_code,手机号\n"
        "store-a,ITEM-1,SKU-1,恒温水壶,active,129.00,CNY,PART-001,13800138000\n"
        "store-a,ITEM-2,SKU-2,滤芯,active,not-money,CNY,PART-002,\n"
        "store-b,ITEM-3,SKU-3,跨店商品,active,59.00,CNY,PART-003,\n"
    ).encode()
    db = Database(tmp_path / "wp2-catalog.sqlite3")
    db.initialize()
    service = ReadonlyReportIngestionService(db)
    request = _request(content, report_type="catalog_snapshot")

    first = service.ingest("tenant-a", request, content)
    repeated = service.ingest("tenant-a", request, content)

    assert first["status"] == "partial"
    assert first["manifest"]["quality"] == {
        "status": "partial",
        "total_rows": 3,
        "accepted_rows": 1,
        "quarantined_rows": 1,
        "rejected_rows": 1,
        "issue_counts": {
            "cross_store_row": 1,
            "invalid_report_row": 1,
        },
    }
    assert first["domain_writes"] == {"applied": 1, "idempotent": 0}
    assert repeated["replayed"] is True
    assert repeated["manifest"]["import_id"] == first["manifest"]["import_id"]
    assert "13800138000" not in repr(first)

    items = CatalogService(db).list_items("tenant-a", store_id="store-a")
    assert [item["sku_id"] for item in items] == ["SKU-1"]
    assert items[0]["attributes"]["merchant_code"] == "PART-001"
    assert CatalogService(db).list_items("tenant-a", store_id="store-b") == []
    issues = ReadonlyDataService(db).list_row_issues(
        "tenant-a", import_id=first["manifest"]["import_id"]
    )
    assert [(item["row_number"], item["reason"]) for item in issues] == [
        (2, "invalid_report_row"),
        (3, "cross_store_row"),
    ]


def test_chinese_headers_and_enum_labels_normalize_through_the_registry(tmp_path) -> None:
    content = (
        "店铺ID,商品ID,平台SKU,商品标题,商品状态,销售价,币种,商家编码\n"
        "store-a,ITEM-1,SKU-1,恒温水壶,在售,129.00,CNY,PART-001\n"
    ).encode()
    db = Database(tmp_path / "wp2-chinese-aliases.sqlite3")
    db.initialize()

    result = ReadonlyReportIngestionService(db).ingest(
        "tenant-a",
        _request(content, report_type="catalog_snapshot"),
        content,
    )

    assert result["status"] == "passed"
    item = CatalogService(db).list_items(
        "tenant-a", store_id="store-a"
    )[0]
    assert item["status"] == "active"
    assert item["attributes"]["merchant_code"] == "PART-001"


def test_xlsx_inventory_import_uses_selected_sheet_and_never_executes_formulas(
    tmp_path,
) -> None:
    rows = [
        [
            "store_id",
            "warehouse_id",
            "sku_id",
            "on_hand",
            "reserved",
            "inbound",
            "average_daily_sales",
        ],
        ["store-a", "WH-1", "SKU-2", "8", "1", "0", "1.5"],
        ["store-a", "WH-1", "SKU-1", "5", "0", "2", "1"],
    ]
    content = _xlsx(rows)
    db = Database(tmp_path / "wp2-inventory-xlsx.sqlite3")
    db.initialize()
    service = ReadonlyReportIngestionService(db)

    result = service.ingest(
        "tenant-a",
        _request(
            content,
            report_type="inventory_snapshot",
            file_format=ReportFileFormat.XLSX,
            sheet_name="库存",
        ),
        content,
    )

    assert result["status"] == "passed"
    assert result["trace"] == {
        "grain": "warehouse_sku_snapshot",
        "amount_unit": "quantity",
        "source_timezone": "Asia/Shanghai",
        "sheet_name": "库存",
    }
    assert [
        item["sku_id"]
        for item in InventoryService(db).list_balances(
            "tenant-a", store_id="store-a"
        )
    ] == ["SKU-1", "SKU-2"]

    formula_content = _xlsx(rows, formula_cell="D2")
    with pytest.raises(ValueError, match="xlsx_formula_forbidden"):
        service.ingest(
            "tenant-a",
            _request(
                formula_content,
                report_type="inventory_snapshot",
                file_format=ReportFileFormat.XLSX,
                exported_at=EXPORTED_AT + timedelta(minutes=1),
                data_as_of=DATA_AS_OF + timedelta(minutes=1),
                sheet_name="库存",
            ),
            formula_content,
        )


def test_xlsx_excel_serial_dates_are_normalized_by_the_adapter(tmp_path) -> None:
    content = _xlsx(
        [
            [
                "store_id",
                "statement_key",
                "period_start",
                "period_end",
                "gross_sales",
                "refund_amount",
                "fee_amount",
                "settlement_amount",
                "currency",
            ],
            [
                "store-a",
                "STMT-1",
                "46235",
                "46252",
                "1000.00",
                "100.00",
                "50.00",
                "850.00",
                "CNY",
            ],
        ]
    )
    db = Database(tmp_path / "wp2-xlsx-date-serial.sqlite3")
    db.initialize()

    result = ReadonlyReportIngestionService(db).ingest(
        "tenant-a",
        _request(
            content,
            report_type="settlement_statement",
            file_format=ReportFileFormat.XLSX,
            sheet_name="库存",
        ),
        content,
    )

    assert result["status"] == "passed"
    statement = FinanceService(db).list_statements(
        "tenant-a", store_id="store-a"
    )[0]
    assert statement["period_start"] == "2026-08-01"
    assert statement["period_end"] == "2026-08-18"


def test_batch_preserves_success_when_another_domain_fails(tmp_path) -> None:
    catalog = (
        "store_id,item_id,sku_id,title,status,sale_price,currency,merchant_code\n"
        "store-a,ITEM-1,SKU-1,恒温水壶,active,129.00,CNY,PART-001\n"
    ).encode()
    inventory = (
        "store_id,warehouse_id,sku_id,on_hand,reserved,inbound,average_daily_sales\n"
        "store-a,WH-1,SKU-1,-1,0,0,1\n"
    ).encode()
    db = Database(tmp_path / "wp2-batch.sqlite3")
    db.initialize()
    service = ReadonlyReportIngestionService(db)

    result = service.ingest_batch(
        "tenant-a",
        [
            ReportImportJob(
                request=_request(catalog, report_type="catalog_snapshot"),
                content=catalog,
            ),
            ReportImportJob(
                request=_request(inventory, report_type="inventory_snapshot"),
                content=inventory,
            ),
        ],
    )

    assert result["status"] == "partial"
    assert result["reports_succeeded"] == 1
    assert result["reports_failed"] == 1
    assert [item["status"] for item in result["reports"]] == ["passed", "failed"]
    assert [
        item["sku_id"]
        for item in CatalogService(db).list_items("tenant-a", store_id="store-a")
    ] == ["SKU-1"]
    assert InventoryService(db).list_balances("tenant-a", store_id="store-a") == []


def test_demo_import_receipt_keeps_its_issues_without_polluting_operational_scope(
    tmp_path,
) -> None:
    row = dict(ADAPTER_CASES["inventory_snapshot"]["row"])
    row["on_hand"] = "-1"
    content = _csv([row])
    db = Database(tmp_path / "wp2-demo-issue-scope.sqlite3")
    db.initialize()

    result = ReadonlyReportIngestionService(db).ingest(
        "tenant-a",
        _request(
            content,
            report_type="inventory_snapshot",
            source_kind=SourceKind.DEMO,
        ),
        content,
    )

    assert [issue["reason"] for issue in result["issues"]] == [
        "invalid_report_row"
    ]
    readonly = ReadonlyDataService(db)
    assert readonly.list_row_issues("tenant-a") == []
    assert len(readonly.list_row_issues("tenant-a", scope=DataScope.ALL)) == 1


def test_order_fulfillment_and_refund_reports_share_the_public_order_truth(tmp_path) -> None:
    orders = (
        "store_id,order_id,order_status,payment_status,currency,total_amount,placed_at,"
        "line_id,sku_id,title,quantity,unit_price\n"
        "store-a,ORDER-1,paid,paid,CNY,258.00,2026-08-18T09:00:00,"
        "LINE-1,SKU-1,恒温水壶,2,129.00\n"
    ).encode()
    fulfillment = (
        "store_id,order_id,carrier,tracking_no_masked,logistics_status,last_event,last_event_at\n"
        "store-a,ORDER-1,SF,SF****1234,in_transit,已到达中转站,2026-08-18T11:00:00\n"
    ).encode()
    refund = (
        "store_id,order_id,case_id,case_type,status,requested_amount,approved_amount,"
        "reason_code,opened_at,updated_at\n"
        "store-a,ORDER-1,CASE-1,refund,approved,129.00,129.00,quality_issue,"
        "2026-08-18T11:10:00,2026-08-18T11:20:00\n"
    ).encode()
    db = Database(tmp_path / "wp2-orders.sqlite3")
    db.initialize()
    ingestion = ReadonlyReportIngestionService(db)

    order_result = ingestion.ingest(
        "tenant-a", _request(orders, report_type="order_snapshot"), orders
    )
    fulfillment_result = ingestion.ingest(
        "tenant-a",
        _request(
            fulfillment,
            report_type="fulfillment_snapshot",
            exported_at=EXPORTED_AT + timedelta(hours=1),
            data_as_of=DATA_AS_OF + timedelta(hours=1),
        ),
        fulfillment,
    )
    refund_result = ingestion.ingest(
        "tenant-a",
        _request(
            refund,
            report_type="refund_snapshot",
            exported_at=EXPORTED_AT + timedelta(hours=2),
            data_as_of=DATA_AS_OF + timedelta(hours=2),
        ),
        refund,
    )

    assert order_result["status"] == "passed"
    assert fulfillment_result["status"] == "passed"
    assert refund_result["status"] == "passed"
    order = OrderService(db).list_orders(
        "tenant-a", store_id="store-a", order_id="ORDER-1"
    )[0]
    assert [line["line_id"] for line in order["lines"]] == ["LINE-1"]
    assert order["logistics"]["status"] == "in_transit"
    assert [case["case_id"] for case in order["after_sales"]] == ["CASE-1"]
    assert order["after_sales"][0]["approved_amount"] == "129.00"


def test_newer_order_snapshot_preserves_fulfillment_and_refund_facts(tmp_path) -> None:
    db = Database(tmp_path / "wp2-order-child-preservation.sqlite3")
    db.initialize()
    ingestion = ReadonlyReportIngestionService(db)
    order_row = dict(ADAPTER_CASES["order_snapshot"]["row"])
    fulfillment = _csv([dict(ADAPTER_CASES["fulfillment_snapshot"]["row"])])
    refund = _csv([dict(ADAPTER_CASES["refund_snapshot"]["row"])])

    order_content = _csv([order_row])
    ingestion.ingest(
        "tenant-a",
        _request(order_content, report_type="order_snapshot"),
        order_content,
    )
    ingestion.ingest(
        "tenant-a",
        _request(
            fulfillment,
            report_type="fulfillment_snapshot",
            exported_at=EXPORTED_AT + timedelta(hours=1),
            data_as_of=DATA_AS_OF + timedelta(hours=1),
        ),
        fulfillment,
    )
    ingestion.ingest(
        "tenant-a",
        _request(
            refund,
            report_type="refund_snapshot",
            exported_at=EXPORTED_AT + timedelta(hours=2),
            data_as_of=DATA_AS_OF + timedelta(hours=2),
        ),
        refund,
    )

    order_row["order_status"] = "fulfilling"
    newer_order = _csv([order_row])
    result = ingestion.ingest(
        "tenant-a",
        _request(
            newer_order,
            report_type="order_snapshot",
            exported_at=EXPORTED_AT + timedelta(hours=3),
            data_as_of=DATA_AS_OF + timedelta(hours=3),
        ),
        newer_order,
    )

    assert result["status"] == "passed"
    order = OrderService(db).list_orders(
        "tenant-a", store_id="store-a", order_id="ORDER-1"
    )[0]
    assert order["order_status"] == "fulfilling"
    assert order["logistics"]["status"] == "in_transit"
    assert [case["case_id"] for case in order["after_sales"]] == ["CASE-1"]


@pytest.mark.parametrize("report_type", sorted(ADAPTER_CASES))
@pytest.mark.parametrize(
    "scenario",
    ["normal", "missing", "invalid_type", "duplicate", "out_of_order", "cross_store"],
)
def test_every_registered_adapter_has_the_wp2_acceptance_matrix(
    tmp_path,
    report_type: str,
    scenario: str,
) -> None:
    case = ADAPTER_CASES[report_type]
    row = dict(case["row"])
    db, service = _service_for_case(tmp_path, report_type, scenario)

    if scenario == "missing":
        missing = str(case["missing"])
        content = _csv([row], headers=[field for field in row if field != missing])
    elif scenario == "invalid_type":
        field, invalid = case["invalid"]
        row[str(field)] = str(invalid)
        content = _csv([row])
    elif scenario == "duplicate":
        content = _csv([row, row])
    elif scenario == "cross_store":
        row["store_id"] = "store-b"
        content = _csv([row])
    else:
        content = _csv([row])

    if scenario == "out_of_order":
        first = service.ingest(
            "tenant-a",
            _request(
                content,
                report_type=report_type,
                exported_at=EXPORTED_AT + timedelta(minutes=10),
                data_as_of=DATA_AS_OF + timedelta(minutes=10),
            ),
            content,
        )
        assert first["status"] == "passed"
        with pytest.raises(SourceVersionError, match="stale_source_version"):
            service.ingest(
                "tenant-a",
                _request(content, report_type=report_type),
                content,
            )
        assert len(ReadonlyDataService(db).list_imports("tenant-a")) == 1
        return

    result = service.ingest(
        "tenant-a",
        _request(content, report_type=report_type),
        content,
    )
    if scenario == "normal":
        assert result["status"] == "passed"
        assert result["manifest"]["quality"]["accepted_rows"] == 1
        assert result["domain_writes"] == {"applied": 1, "idempotent": 0}
    elif scenario in {"missing", "invalid_type"}:
        assert result["status"] == "failed"
        assert result["manifest"]["quality"]["rejected_rows"] == 1
        assert [issue["reason"] for issue in result["issues"]] == [
            "invalid_report_row"
        ]
    elif scenario == "duplicate":
        assert result["status"] == "failed"
        assert result["manifest"]["quality"]["quarantined_rows"] == 2
        assert [issue["reason"] for issue in result["issues"]] == [
            "duplicate_report_identity",
            "duplicate_report_identity",
        ]
    else:
        assert scenario == "cross_store"
        assert result["status"] == "failed"
        assert result["manifest"]["quality"]["quarantined_rows"] == 1
        assert [issue["reason"] for issue in result["issues"]] == [
            "cross_store_row"
        ]


def test_same_export_version_conflict_is_rejected_before_domain_mutation(tmp_path) -> None:
    first_content = _csv([dict(ADAPTER_CASES["catalog_snapshot"]["row"])])
    conflicting_row = dict(ADAPTER_CASES["catalog_snapshot"]["row"])
    conflicting_row.update(item_id="ITEM-2", sku_id="SKU-2", merchant_code="PART-002")
    conflicting_content = _csv([conflicting_row])
    db = Database(tmp_path / "wp2-preflight-conflict.sqlite3")
    db.initialize()
    service = ReadonlyReportIngestionService(db)

    service.ingest(
        "tenant-a",
        _request(first_content, report_type="catalog_snapshot"),
        first_content,
    )
    with pytest.raises(SourceVersionError, match="source_version_conflict"):
        service.ingest(
            "tenant-a",
            _request(conflicting_content, report_type="catalog_snapshot"),
            conflicting_content,
        )

    assert [
        item["sku_id"]
        for item in CatalogService(db).list_items("tenant-a", store_id="store-a")
    ] == ["SKU-1"]
    assert len(ReadonlyDataService(db).list_imports("tenant-a")) == 1
