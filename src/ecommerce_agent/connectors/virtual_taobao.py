from __future__ import annotations

import hashlib
import hmac
import json
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from .base import (
    ConnectionCheck,
    ConnectorCapabilities,
    ExternalAction,
    ExternalResult,
    PullBatch,
    PullRecord,
    PullRequest,
    VerificationResult,
    VerifiedEvent,
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class VirtualTaobaoConnector:
    """Deterministic local connector used until an authorized platform is available."""

    CONNECTOR_ID = "virtual_taobao"

    def __init__(self, signing_secret: str = "yunpai-virtual-taobao") -> None:
        self._signing_secret = signing_secret.encode("utf-8")
        self._lock = threading.Lock()
        self._executions: dict[str, ExternalResult] = {}
        self._records = self._build_records()

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            connector_id=self.CONNECTOR_ID,
            display_name="淘宝虚拟接口",
            capability_version="1.1",
            virtual=True,
            resources=["catalog", "orders", "inventory", "competitor_price"],
            modes=["read", "write", "webhook", "polling"],
            actions=["send_customer_message", "update_safety_stock_buffer"],
            supports_dry_run=True,
            supports_idempotency=True,
            supports_readback=True,
            required_permissions=["virtual.local"],
        )

    def test_connection(self) -> ConnectionCheck:
        return ConnectionCheck(
            ok=True,
            connector_id=self.CONNECTOR_ID,
            mode="virtual",
            detail="本地虚拟接口可用；不会访问淘宝或任何外部系统",
        )

    def pull(self, request: PullRequest) -> PullBatch:
        if request.resource not in self.capabilities().resources:
            raise ValueError(f"resource not supported: {request.resource}")
        offset = int(request.cursor or "0")
        records = self._records[request.resource]
        selected = records[offset : offset + request.limit]
        next_offset = offset + len(selected)
        has_more = next_offset < len(records)
        return PullBatch(
            connector_id=self.CONNECTOR_ID,
            resource=request.resource,
            records=[PullRecord.model_validate(item) for item in selected],
            next_cursor=str(next_offset) if has_more else None,
            has_more=has_more,
            data_as_of=max((item["occurred_at"] for item in selected), default=_utc_now()),
        )

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> VerifiedEvent:
        supplied = headers.get("x-virtual-signature", "")
        expected = hmac.new(self._signing_secret, body, hashlib.sha256).hexdigest()
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise ValueError("virtual webhook signature verification failed")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("virtual webhook body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("virtual webhook payload must be an object")
        return VerifiedEvent(
            verified=True,
            event_id=str(payload.get("event_id") or f"virtual-event-{uuid.uuid4().hex}"),
            event_type=str(payload.get("event_type") or "virtual.updated"),
            resource=str(payload.get("resource") or "unknown"),
            payload=payload,
        )

    def execute(self, action: ExternalAction) -> ExternalResult:
        if action.action not in self.capabilities().actions:
            raise ValueError(f"action not supported: {action.action}")
        with self._lock:
            previous = self._executions.get(action.idempotency_key)
            if previous is not None:
                return previous
            request_id = f"virtual-request-{uuid.uuid4().hex}"
            result = ExternalResult(
                status="accepted" if action.dry_run else "succeeded",
                external_request_id=request_id,
                output={
                    "virtual": True,
                    "dry_run": action.dry_run,
                    "action": action.action,
                    "receipt": f"virtual-receipt:{action.idempotency_key}",
                },
            )
            self._executions[action.idempotency_key] = result
            return result

    def verify(self, action: ExternalAction, result: ExternalResult) -> VerificationResult:
        receipt = str(result.output.get("receipt") or "")
        verified = (
            result.status in {"accepted", "succeeded"}
            and result.output.get("virtual") is True
            and receipt == f"virtual-receipt:{action.idempotency_key}"
        )
        return VerificationResult(
            verified=verified,
            status=result.status if verified else "uncertain",
            detail="虚拟回执一致" if verified else "虚拟回执无法验证",
        )

    @staticmethod
    def _build_records() -> dict[str, list[dict[str, Any]]]:
        observed_at = "2026-07-21T08:00:00+00:00"
        return {
            "catalog": [
                {
                    "source_id": "virtual-catalog-001",
                    "source_version": observed_at,
                    "occurred_at": observed_at,
                    "payload": {
                        "store_id": "virtual-shop-001",
                        "item_id": "YP-SPU-001",
                        "sku_id": "YP-SKU-001",
                        "title": "云湃智能客服一体机标准版",
                        "status": "active",
                        "sale_price": "109.00",
                        "currency": "CNY",
                        "attributes": {"edition": "standard", "warranty_months": 12},
                    },
                },
                {
                    "source_id": "virtual-catalog-002",
                    "source_version": observed_at,
                    "occurred_at": observed_at,
                    "payload": {
                        "store_id": "virtual-shop-001",
                        "item_id": "YP-SPU-001",
                        "sku_id": "YP-SKU-002",
                        "title": "云湃智能客服一体机专业版",
                        "status": "active",
                        "sale_price": "199.00",
                        "currency": "CNY",
                        "attributes": {"edition": "professional", "warranty_months": 24},
                    },
                },
            ],
            "orders": [
                {
                    "source_id": "virtual-order-001",
                    "source_version": observed_at,
                    "occurred_at": observed_at,
                    "payload": {
                        "store_id": "virtual-shop-001",
                        "order_id": "VIRTUAL-ORDER-001",
                        "order_status": "shipped",
                        "payment_status": "paid",
                        "currency": "CNY",
                        "total_amount": "109.00",
                        "placed_at": "2026-07-20T02:30:00+00:00",
                        "buyer_ref_hash": "7de91145e578e3fe8e6b14559c2a43d7",
                        "lines": [{
                            "line_id": "line-001",
                            "sku_id": "YP-SKU-001",
                            "title": "云湃智能客服一体机标准版",
                            "quantity": 1,
                            "unit_price": "109.00"
                        }],
                        "logistics": {
                            "carrier": "圆通",
                            "tracking_no_masked": "YT****8899",
                            "status": "in_transit",
                            "last_event": "快件已离开杭州转运中心",
                            "last_event_at": "2026-07-21T07:30:00+00:00"
                        },
                        "after_sales": []
                    },
                },
                {
                    "source_id": "virtual-order-002",
                    "source_version": observed_at,
                    "occurred_at": observed_at,
                    "payload": {
                        "store_id": "virtual-shop-001",
                        "order_id": "VIRTUAL-ORDER-002",
                        "order_status": "delivered",
                        "payment_status": "partially_refunded",
                        "currency": "CNY",
                        "total_amount": "199.00",
                        "placed_at": "2026-07-18T04:00:00+00:00",
                        "buyer_ref_hash": "64fd42180bc184f8db14354676cf4f73",
                        "lines": [{
                            "line_id": "line-002",
                            "sku_id": "YP-SKU-002",
                            "title": "云湃智能客服一体机专业版",
                            "quantity": 1,
                            "unit_price": "199.00"
                        }],
                        "logistics": {
                            "carrier": "顺丰",
                            "tracking_no_masked": "SF****6677",
                            "status": "delivered",
                            "last_event": "已签收",
                            "last_event_at": "2026-07-20T09:00:00+00:00"
                        },
                        "after_sales": [{
                            "case_id": "VIRTUAL-AS-001",
                            "case_type": "refund",
                            "status": "reviewing",
                            "requested_amount": "20.00",
                            "approved_amount": "0",
                            "reason_code": "price_protection",
                            "opened_at": "2026-07-21T06:00:00+00:00",
                            "updated_at": "2026-07-21T07:00:00+00:00"
                        }]
                    },
                },
            ],
            "inventory": [
                {
                    "source_id": "virtual-inventory-001",
                    "source_version": observed_at,
                    "occurred_at": observed_at,
                    "payload": {
                        "store_id": "virtual-shop-001",
                        "warehouse_id": "WH-HZ",
                        "sku_id": "YP-SKU-001",
                        "on_hand": "24",
                        "reserved": "5",
                        "inbound": "20",
                        "average_daily_sales": "4.5",
                    },
                },
                {
                    "source_id": "virtual-inventory-002",
                    "source_version": observed_at,
                    "occurred_at": observed_at,
                    "payload": {
                        "store_id": "virtual-shop-001",
                        "warehouse_id": "WH-HZ",
                        "sku_id": "YP-SKU-002",
                        "on_hand": "180",
                        "reserved": "4",
                        "inbound": "0",
                        "average_daily_sales": "0",
                    },
                },
            ],
            "competitor_price": [
                {
                    "source_id": "virtual-competitor-001",
                    "source_version": observed_at,
                    "occurred_at": observed_at,
                    "payload": {
                        "subject_sku": "YP-SKU-001",
                        "competitor_name": "授权样本店 A",
                        "competitor_sku": "SAMPLE-A-001",
                        "subject_price": "109.00",
                        "competitor_price": "119.00",
                        "currency": "CNY",
                        "source_type": "virtual",
                        "source_ref": "virtual://taobao/competitor/001",
                        "is_estimate": True,
                    },
                },
                {
                    "source_id": "virtual-competitor-002",
                    "source_version": observed_at,
                    "occurred_at": observed_at,
                    "payload": {
                        "subject_sku": "YP-SKU-001",
                        "competitor_name": "授权样本店 B",
                        "competitor_sku": "SAMPLE-B-001",
                        "subject_price": "109.00",
                        "competitor_price": "99.00",
                        "currency": "CNY",
                        "source_type": "virtual",
                        "source_ref": "virtual://taobao/competitor/002",
                        "is_estimate": True,
                    },
                },
            ],
        }
