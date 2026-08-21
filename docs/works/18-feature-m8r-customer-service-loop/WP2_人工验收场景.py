from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

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
from ecommerce_agent.customer_service_facts import CustomerServiceFactsService
from ecommerce_agent.database import Database
from ecommerce_agent.tools import ToolExecutionContext, ToolRegistry


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _catalog(
    db: Database,
    *,
    sku_id: str,
    title: str,
    source_time: datetime,
    connector_id: str = "virtual_taobao",
    source_id: str | None = None,
) -> None:
    CatalogService(db).upsert(
        "tenant-a",
        CatalogItemUpsert(
            connector_id=connector_id,
            store_id="store-a",
            item_id=f"ITEM-{sku_id}",
            sku_id=sku_id,
            title=title,
            status="active",
            sale_price=Decimal("129.00"),
            currency="CNY",
            attributes={"internal_note": "不能进入客服投影"},
            source_updated_at=source_time,
            source_id=source_id or f"virtual:catalog:{sku_id}",
        ),
    )


def _inventory(db: Database, *, sku_id: str, source_time: datetime) -> None:
    InventoryService(db).upsert(
        "tenant-a",
        InventoryBalanceUpsert(
            connector_id="virtual_taobao",
            store_id="store-a",
            warehouse_id="INTERNAL-WAREHOUSE",
            sku_id=sku_id,
            on_hand=Decimal("8"),
            reserved=Decimal("3"),
            inbound=Decimal("2"),
            average_daily_sales=Decimal("1"),
            source_updated_at=source_time,
            source_id=f"virtual:inventory:{sku_id}",
        ),
    )


def _order(db: Database, *, source_time: datetime, delivered: bool) -> None:
    OrderService(db).upsert(
        "tenant-a",
        OrderUpsert(
            connector_id="virtual_taobao",
            store_id="store-a",
            order_id="ORDER-1",
            order_status="delivered" if delivered else "shipped",
            payment_status="paid",
            currency="CNY",
            total_amount=Decimal("129.00"),
            placed_at=source_time - timedelta(days=1),
            buyer_ref_hash="customer-secret-hash-0000000001",
            lines=[
                OrderLineInput(
                    line_id="INTERNAL-LINE-1",
                    sku_id="SKU-1",
                    title="恒温水壶",
                    quantity=1,
                    unit_price=Decimal("129.00"),
                )
            ],
            logistics=LogisticsSnapshotInput(
                carrier="测试快递",
                tracking_no_masked="TRACK****0001",
                status="delivered" if delivered else "in_transit",
                last_event="已签收" if delivered else "运输中，联系电话 13800138000",
                last_event_at=source_time - timedelta(minutes=10),
            ),
            after_sales=[
                AfterSaleCaseInput(
                    case_id="INTERNAL-CASE-1",
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
            source_id="virtual:order:ORDER-1",
        ),
    )


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id="tenant-a",
        client_id="manual-client",
        session_id="manual-session",
        trace_id="manual-trace",
        trusted_context={
            "authorized": True,
            "shop_id": "store-a",
            "order_id": "ORDER-1",
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--tester", default="谢良璇")
    parser.add_argument("--auto-confirm", action="store_true")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    evidence_dir = Path(args.evidence_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    db = Database(data_dir / "wp2-manual.sqlite3")
    db.initialize()
    connectors = ConnectorRegistry()
    connectors.register(VirtualTaobaoConnector())
    facts = CustomerServiceFactsService(db, connectors=connectors)
    registry = ToolRegistry()
    facts.register_agent_tools(registry)
    now = datetime.now(UTC).replace(microsecond=0)
    transcript: list[str] = []
    observations: list[dict[str, Any]] = []

    def emit(value: str = "") -> None:
        print(value)
        transcript.append(value)

    def show(title: str, value: Any) -> None:
        emit("=" * 76)
        emit(title)
        emit(_json(value))

    def confirm(identifier: str, expected: str) -> None:
        emit(f"人工观察重点：{expected}")
        if args.auto_confirm:
            answer = "Y"
            emit("开发侧演练自动确认：Y（不能替代谢良璇正式人工验收）")
        else:
            answer = input("实际结果是否符合预期？请输入 Y 或 N：").strip().upper()
            transcript.append(f"人工确认：{answer}")
        observations.append(
            {"id": identifier, "expected": expected, "confirmed": answer == "Y"}
        )

    try:
        _catalog(db, sku_id="SKU-1", title="恒温水壶", source_time=now)
        _inventory(db, sku_id="SKU-1", source_time=now)
        _catalog(db, sku_id="SKU-NO-STOCK", title="无库存样例商品", source_time=now)
        old = now - timedelta(days=5)
        _catalog(db, sku_id="SKU-STALE", title="旧快照商品", source_time=old)
        _inventory(db, sku_id="SKU-STALE", source_time=old)
        _catalog(
            db,
            sku_id="SKU-UNKNOWN",
            title="未知来源商品",
            source_time=now,
            connector_id="unknown-connector",
            source_id="uncontrolled:catalog:unknown",
        )
        _order(db, source_time=now - timedelta(hours=1), delivered=False)

        sales = facts.sales_projection(
            "tenant-a", store_id="store-a", sku_id="SKU-1", observed_at=now
        )
        show("第 1 步：销售事实最小投影", sales)
        assert sales["facts"]["product"]["sale_price"] == "129.00"
        assert sales["facts"]["inventory"]["available_quantity"] == "5.00"
        assert sales["facts"]["inventory"]["inbound_quantity"] == "2.00"
        confirm("sales", "售价 129.00、可用库存 5.00、在途 2.00，且没有仓库号或 attributes。")

        missing = facts.sales_projection(
            "tenant-a",
            store_id="store-a",
            sku_id="SKU-NO-STOCK",
            observed_at=now,
        )
        show("第 2 步：缺失事实不能补零", missing)
        assert missing["facts"]["inventory"]["state"] == "missing"
        assert missing["facts"]["inventory"]["available_quantity"] is None
        confirm("missing", "商品存在，但库存为 missing/null，而不是库存 0。")

        stale = facts.sales_projection(
            "tenant-a", store_id="store-a", sku_id="SKU-STALE", observed_at=now
        )
        show("第 3 步：过期快照不能冒充当前", stale)
        assert stale["freshness"]["status"] == "stale"
        assert stale["freshness"]["usable_as_current"] is False
        confirm("freshness", "结果显示 stale、usable_as_current=false，并保留 data_as_of。")

        unknown = facts.sales_projection(
            "tenant-a", store_id="store-a", sku_id="SKU-UNKNOWN", observed_at=now
        )
        show("第 4 步：来源 Gate", {"virtual": sales["source_provenance"], "unknown": unknown})
        assert sales["source_provenance"]["source_type"] == "virtual"
        assert unknown["state"] == "blocked" and unknown["facts"] == {}
        confirm("provenance", "虚拟来源明确标记 virtual；未知来源被 blocked，facts 为空。")

        context = _context()
        gate_results: dict[str, str] = {}
        for key, arguments in {
            "wrong_order": {"store_id": "store-a", "order_id": "ORDER-2"},
            "wrong_store": {"store_id": "store-b", "order_id": "ORDER-1"},
        }.items():
            try:
                registry.validate_selection(
                    name="get_customer_after_sales_facts",
                    arguments=arguments,
                    requested_mode="observe",
                    context=context,
                )
            except ValueError as exc:
                gate_results[key] = str(exc)
        show("第 5 步：可信订单号和店铺号双绑定", gate_results)
        assert "order_scope_mismatch" in gate_results["wrong_order"]
        assert "store_scope_mismatch" in gate_results["wrong_store"]
        confirm("scope", "错订单号和错店铺号分别在工具执行前被明确阻断。")

        spec, arguments = registry.validate_selection(
            name="get_customer_after_sales_facts",
            arguments={"store_id": "store-a", "order_id": "ORDER-1"},
            requested_mode="observe",
            context=context,
        )
        order_result = registry.execute(spec=spec, arguments=arguments, context=context)
        show("第 6 步：订单、物流、退款事实与隐私投影", order_result.output)
        serialized = _json(order_result.output)
        for forbidden in (
            "buyer_ref_hash",
            "tracking_no_masked",
            "INTERNAL-LINE-1",
            "INTERNAL-CASE-1",
            "13800138000",
        ):
            assert forbidden not in serialized
        confirm("privacy", "订单、物流和退款事实可见；买家 hash、运单号、内部编号和手机号不可见。")

        _order(db, source_time=now, delivered=True)
        corrected = facts.after_sales_projection(
            "tenant-a",
            store_id="store-a",
            order_id="ORDER-1",
            include_history=True,
            observed_at=now,
        )
        show("第 7 步：事实更正后的历史与 current", corrected)
        assert [item["version"] for item in corrected["history"]] == [1, 2]
        assert corrected["history"][0]["freshness"]["status"] == "superseded"
        assert corrected["history"][1]["current"] is True
        assert corrected["facts"]["order"]["order_status"] == "delivered"
        confirm("history", "版本 1 仍可读但为 superseded；版本 2 为 current 且订单已 delivered。")

        isolated = facts.sales_projection(
            "tenant-b", store_id="store-a", sku_id="SKU-1", observed_at=now
        )
        show("第 8 步：租户隔离", isolated)
        assert isolated["state"] == "missing"
        assert isolated["facts"]["product"]["title"] is None
        confirm("tenant", "tenant-b 得到 missing，不能看到 tenant-a 的商品名称或价格。")

        automatic_status = "passed"
    except Exception as exc:
        automatic_status = "failed"
        emit(f"自动契约检查失败：{type(exc).__name__}: {exc}")
    finally:
        registry.close()

    completed_at = datetime.now().astimezone().isoformat()
    human_passed = bool(observations) and all(item["confirmed"] for item in observations)
    final_status = (
        "human_accepted"
        if automatic_status == "passed" and human_passed and not args.auto_confirm
        else "developer_rehearsal_passed"
        if automatic_status == "passed" and human_passed
        else "failed"
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    transcript_path = evidence_dir / f"{args.tester}_WP2人工验收过程_{stamp}.txt"
    result_path = evidence_dir / f"{args.tester}_WP2人工验收结果_{stamp}.json"
    result = {
        "tester": args.tester,
        "work_package": "M8-R-WP2",
        "completed_at": completed_at,
        "data_dir": str(data_dir),
        "external_model_called": False,
        "confirmation_mode": "auto" if args.auto_confirm else "human",
        "automatic_contract_checks": automatic_status,
        "observations": observations,
        "human_observations_passed": human_passed and not args.auto_confirm,
        "final_status": final_status,
        "transcript": str(transcript_path),
    }
    transcript_path.write_text("\n".join(transcript) + "\n", encoding="utf-8")
    result_path.write_text(_json(result) + "\n", encoding="utf-8")
    emit("=" * 76)
    emit("验收结束")
    emit(_json(result))
    emit(f"过程记录：{transcript_path}")
    emit(f"结果文件：{result_path}")
    if args.auto_confirm:
        emit("本次是开发侧演练，不能替代谢良璇正式人工验收。")
    return 0 if automatic_status == "passed" and human_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
