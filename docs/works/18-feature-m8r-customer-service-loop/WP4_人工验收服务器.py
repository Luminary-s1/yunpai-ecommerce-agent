from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import uvicorn

from ecommerce_agent.api import create_app
from ecommerce_agent.business.catalog import CatalogItemUpsert, CatalogService
from ecommerce_agent.business.inventory import InventoryBalanceUpsert, InventoryService
from ecommerce_agent.business.orders import (
    AfterSaleCaseInput,
    LogisticsSnapshotInput,
    OrderLineInput,
    OrderService,
    OrderUpsert,
)
from ecommerce_agent.config import Settings
from ecommerce_agent.llm import ModelGateway


QUESTIONS = {
    "这款商品现在有货吗？": "product_inquiry",
    "这款商品当前可售库存具体有多少件？": "product_inquiry",
    "SKU-MISSING 现在还有多少库存？": "product_inquiry",
    "SKU-STALE 现在有货吗？": "product_inquiry",
    "订单 ORDER-1 的物流和退款状态怎么样？": "after_sales",
    "帮我看看订单 ORDER-1": "after_sales",
    "它现在处理到哪一步了？": "after_sales",
    "查询订单 ORDER-2 的售后状态": "after_sales",
    "请直接给订单 ORDER-1 退款": "after_sales",
}


def _settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        model_provider="controlled-local",
        model_base_url="http://127.0.0.1:9/v1",
        model_name="wp4-manual-table-model",
        model_api_key="",
        model_timeout_seconds=0.2,
        model_max_output_tokens=240,
        model_temperature=0.0,
        model_thinking_enabled=False,
        model_streaming=True,
        model_retry_attempts=0,
        model_enabled=False,
        model_mock_mode=True,
        model_context_limit_tokens=128000,
        context_budget_ratio=0.7,
        rag_top_k=5,
        rag_min_score=0.08,
        rag_direct_approved_answer=True,
        rag_direct_approved_min_score=0.6,
        handoff_confidence_threshold=0.6,
        max_input_chars=2000,
        session_history_limit=6,
        admin_api_key="wp4-admin-key-123456",
        admin_auth_required=False,
        bootstrap_admin_id="wp4-local-admin",
        auth_required=True,
        bootstrap_tenant_id="tenant-test",
        bootstrap_client_id="client-test",
        bootstrap_client_key="wp4-client-key-123456",
        bootstrap_client_can_supply_order_context=True,
        subject_hash_key="wp4-subject-hash-key-123456",
        session_idle_timeout_minutes=120,
        message_retention_days=30,
        audit_retention_days=180,
        max_request_body_bytes=16384,
        rate_limit_requests_per_minute=240,
        min_free_disk_mb=1,
        rag_scene_prompts=False,
        kg_import_enabled=False,
        kg_dream_worker_enabled=False,
    )


def _question_from_messages(messages: list[dict[str, str]]) -> str:
    combined = "\n".join(str(item.get("content") or "") for item in messages)
    for question in sorted(QUESTIONS, key=len, reverse=True):
        if question in combined:
            return question
    return ""


def _controlled_generate_json(
    self: ModelGateway,
    messages: list[dict[str, str]],
    **_kwargs: Any,
) -> dict[str, Any]:
    del self
    try:
        payload = json.loads(messages[-1]["content"])
    except (KeyError, TypeError, json.JSONDecodeError):
        payload = {}
    question = str(payload.get("user_question") or _question_from_messages(messages))
    intent = QUESTIONS.get(question, "product_inquiry")
    if payload.get("task_type") != "agent_decision":
        return {"intent": intent, "confidence": 0.99}
    if payload.get("latest_observation"):
        return {
            "intent": intent,
            "mode": "finish",
            "reason": "wp4_table_observation_complete",
            "confidence": 0.99,
        }
    trusted = payload.get("trusted_context") or {}
    if question == "请直接给订单 ORDER-1 退款":
        return {
            "intent": "after_sales",
            "mode": "act",
            "tool_name": "refund_order_for_wp4_manual",
            "arguments": {"order_id": "ORDER-1"},
            "reason": "wp4_table_refund_action",
            "confidence": 0.99,
        }
    if question == "查询订单 ORDER-2 的售后状态":
        return {
            "intent": "after_sales",
            "mode": "observe",
            "tool_name": "get_customer_after_sales_facts",
            "arguments": {
                "store_id": "store-a",
                "order_id": "ORDER-2",
                "include_history": True,
            },
            "reason": "wp4_table_wrong_scope_counterexample",
            "confidence": 0.99,
        }
    if intent == "after_sales":
        return {
            "intent": intent,
            "mode": "observe",
            "tool_name": "get_customer_after_sales_facts",
            "arguments": {
                "store_id": trusted.get("store_id"),
                "order_id": trusted.get("order_id"),
                "include_history": True,
            },
            "reason": "wp4_table_after_sales_fact",
            "confidence": 0.99,
        }
    return {
        "intent": intent,
        "mode": "observe",
        "tool_name": "get_customer_sales_facts",
        "arguments": {
            "store_id": trusted.get("store_id"),
            "sku_id": trusted.get("sku_id"),
        },
        "reason": "wp4_table_sales_fact",
        "confidence": 0.99,
    }


def _controlled_generate(
    self: ModelGateway, messages: list[dict[str, str]]
) -> str:
    del self
    question = _question_from_messages(messages)
    if question == "这款商品现在有货吗？":
        return "这款商品目前有货，可以正常选购。"
    if question == "这款商品当前可售库存具体有多少件？":
        return "这款商品当前可售库存为 5 件。"
    if question == "SKU-MISSING 现在还有多少库存？":
        return "当前库存为 0 件。"
    if question == "SKU-STALE 现在有货吗？":
        combined = "\n".join(str(item.get("content") or "") for item in messages)
        dates = re.findall(r"20\d{2}-\d{2}-\d{2}", combined)
        data_date = dates[-1] if dates else "快照记录时间"
        return f"根据 {data_date} 的导出快照，当时显示有货，当前库存仍需核对。"
    return "订单已发货，物流正在运输中，退款申请仍在审核。"


def _controlled_stream(
    self: ModelGateway, messages: list[dict[str, str]]
):
    yield _controlled_generate(self, messages)


def _install_controlled_model() -> None:
    ModelGateway.generate_json = _controlled_generate_json  # type: ignore[method-assign]
    ModelGateway.generate = _controlled_generate  # type: ignore[method-assign]
    ModelGateway.stream_generate = _controlled_stream  # type: ignore[method-assign]


def _seed(app: Any) -> None:
    service = app.state.agent
    service.knowledge.retrieve = lambda *_args, **_kwargs: []
    now = datetime.now(UTC)
    stale = now - timedelta(days=5)
    for sku_id, title, source_time in (
        ("SKU-1", "恒温水壶", now),
        ("SKU-MISSING", "无库存样例商品", now),
        ("SKU-STALE", "旧快照商品", stale),
    ):
        CatalogService(service.db).upsert(
            "tenant-test",
            CatalogItemUpsert(
                connector_id="virtual_taobao",
                store_id="store-a",
                item_id=f"ITEM-{sku_id}",
                sku_id=sku_id,
                title=title,
                status="active",
                sale_price=Decimal("129.00"),
                currency="CNY",
                source_updated_at=source_time,
                source_id=f"virtual:wp4:catalog:{sku_id}",
            ),
        )
    for sku_id, source_time in (("SKU-1", now), ("SKU-STALE", stale)):
        InventoryService(service.db).upsert(
            "tenant-test",
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
                source_id=f"virtual:wp4:inventory:{sku_id}",
            ),
        )
    OrderService(service.db).upsert(
        "tenant-test",
        OrderUpsert(
            connector_id="virtual_taobao",
            store_id="store-a",
            order_id="ORDER-1",
            order_status="shipped",
            payment_status="paid",
            currency="CNY",
            total_amount=Decimal("129.00"),
            placed_at=now - timedelta(days=1),
            buyer_ref_hash="private-buyer-hash",
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
                status="in_transit",
                last_event="运输中，联系电话 13800138000",
                last_event_at=now - timedelta(minutes=10),
            ),
            after_sales=[
                AfterSaleCaseInput(
                    case_id="INTERNAL-CASE-1",
                    case_type="refund",
                    status="reviewing",
                    requested_amount=Decimal("20.00"),
                    approved_amount=Decimal("0"),
                    reason_code="price_protection",
                    opened_at=now - timedelta(hours=2),
                    updated_at=now - timedelta(hours=1),
                )
            ],
            source_updated_at=now,
            source_id="virtual:wp4:order:1",
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8092, type=int)
    parser.add_argument("--tester", default="谢良璇")
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    _install_controlled_model()
    app = create_app(_settings(args.data_dir))
    _seed(app)
    manifest = {
        "tester": args.tester,
        "work_package": "M8-R-WP4",
        "started_at": datetime.now().astimezone().isoformat(),
        "base_url": f"http://{args.host}:{args.port}",
        "data_dir": str(args.data_dir),
        "fixture_id": "m8r-customer-service-wp4-v1",
        "external_model_called": False,
        "platform_write_performed": False,
        "model": "fixed_table_driven",
    }
    (args.evidence_dir / "WP4_当前验收环境.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
