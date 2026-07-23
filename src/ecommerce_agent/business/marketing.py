from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..database import Database, utc_now
from .source_versioning import canonical_source_time, decide_write, payload_digest


MarketingSourceType = Literal["virtual", "file_import"]
CampaignStatus = Literal["active", "paused", "ended"]
ContentType = Literal["product_copy", "campaign_copy", "social_post"]


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class MarketingPerformanceUpsert(BaseModel):
    """One traceable campaign-day observation. It never performs a bid or budget change."""

    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1, max_length=128)
    store_id: str = Field(min_length=1, max_length=128)
    campaign_id: str = Field(min_length=1, max_length=128)
    metric_date: date
    campaign_name: str = Field(min_length=1, max_length=256)
    channel: str = Field(min_length=1, max_length=64)
    objective: str = Field(min_length=1, max_length=64)
    status: CampaignStatus
    spend: Decimal = Field(ge=0)
    attributed_revenue: Decimal = Field(ge=0)
    attributed_orders: int = Field(ge=0, le=10_000_000)
    impressions: int = Field(ge=0, le=2_000_000_000)
    clicks: int = Field(ge=0, le=2_000_000_000)
    source_type: MarketingSourceType
    source_updated_at: datetime
    source_id: str | None = Field(default=None, max_length=256)

    @field_validator("source_updated_at")
    @classmethod
    def require_aware_source_time(cls, value: datetime) -> datetime:
        canonical_source_time(value)
        return value

    @model_validator(mode="after")
    def validate_funnel(self) -> "MarketingPerformanceUpsert":
        if self.clicks > self.impressions:
            raise ValueError("marketing_clicks_exceed_impressions")
        return self


class ContentDraftUpsert(BaseModel):
    """A non-publishable content draft with mechanical, intentionally limited fact checks."""

    model_config = ConfigDict(extra="forbid")

    draft_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    store_id: str = Field(min_length=1, max_length=128)
    content_type: ContentType
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10_000)
    sku_ids: list[str] = Field(min_length=1, max_length=20)
    declared_prices: dict[str, Decimal] = Field(default_factory=dict)
    source_type: Literal["manual", "virtual"] = "manual"
    source_id: str | None = Field(default=None, max_length=256)
    expected_record_version: int = Field(default=0, ge=0)

    @field_validator("sku_ids")
    @classmethod
    def unique_sku_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item or len(item) > 128 for item in normalized):
            raise ValueError("invalid_content_sku_id")
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate_content_sku_id")
        return normalized

    @model_validator(mode="after")
    def prices_reference_declared_skus(self) -> "ContentDraftUpsert":
        if set(self.declared_prices) - set(self.sku_ids):
            raise ValueError("declared_price_sku_not_referenced")
        return self


class MarketingDiagnosisQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str | None = Field(default=None, max_length=128)
    start_date: date | None = None
    end_date: date | None = None
    min_roas: Decimal = Field(default=Decimal("2.00"), ge=0, le=1000)
    min_ctr: Decimal = Field(default=Decimal("0.0100"), ge=0, le=1)

    @model_validator(mode="after")
    def dates_are_ordered(self) -> "MarketingDiagnosisQuery":
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("marketing_date_range_invalid")
        return self


class MarketingService:
    def __init__(self, db: Database):
        self.db = db

    def upsert_performance(
        self, tenant_id: str, value: MarketingPerformanceUpsert
    ) -> dict[str, Any]:
        payload = value.model_dump(mode="json")
        source_time = canonical_source_time(value.source_updated_at)
        payload["source_updated_at"] = source_time
        payload_hash = payload_digest(payload)
        now = utc_now()
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT id, source_updated_at, payload_hash, version
                FROM marketing_campaign_metrics
                WHERE tenant_id=? AND connector_id=? AND store_id=?
                  AND campaign_id=? AND metric_date=?
                """,
                (
                    tenant_id,
                    value.connector_id,
                    value.store_id,
                    value.campaign_id,
                    value.metric_date.isoformat(),
                ),
            ).fetchone()
            metric_id = str(existing["id"]) if existing else f"marketing-{uuid.uuid4().hex}"
            if existing is not None:
                decision = decide_write(
                    existing_source_time=str(existing["source_updated_at"]),
                    existing_payload_hash=str(existing["payload_hash"]),
                    incoming_source_time=source_time,
                    incoming_payload_hash=payload_hash,
                )
                if decision == "idempotent":
                    write_status = "idempotent"
            if write_status == "applied":
                version = int(existing["version"]) + 1 if existing else 1
                conn.execute(
                    """
                    INSERT INTO marketing_campaign_metrics(
                        id, tenant_id, connector_id, store_id, campaign_id, metric_date,
                        campaign_name, channel, objective, status, spend, attributed_revenue,
                        attributed_orders, impressions, clicks, source_type, source_id,
                        source_updated_at, payload_hash, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, connector_id, store_id, campaign_id, metric_date)
                    DO UPDATE SET campaign_name=excluded.campaign_name, channel=excluded.channel,
                        objective=excluded.objective, status=excluded.status, spend=excluded.spend,
                        attributed_revenue=excluded.attributed_revenue,
                        attributed_orders=excluded.attributed_orders,
                        impressions=excluded.impressions, clicks=excluded.clicks,
                        source_type=excluded.source_type, source_id=excluded.source_id,
                        source_updated_at=excluded.source_updated_at,
                        payload_hash=excluded.payload_hash, version=excluded.version,
                        updated_at=excluded.updated_at
                    """,
                    (
                        metric_id,
                        tenant_id,
                        value.connector_id,
                        value.store_id,
                        value.campaign_id,
                        value.metric_date.isoformat(),
                        value.campaign_name,
                        value.channel,
                        value.objective,
                        value.status,
                        _money(value.spend),
                        _money(value.attributed_revenue),
                        value.attributed_orders,
                        value.impressions,
                        value.clicks,
                        value.source_type,
                        value.source_id,
                        source_time,
                        payload_hash,
                        version,
                        now,
                        now,
                    ),
                )
        result = self._performance_by_id(tenant_id, metric_id)
        result["write_status"] = write_status
        return result

    def list_performance(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        if start_date:
            conditions.append("metric_date>=?")
            params.append(start_date.isoformat())
        if end_date:
            conditions.append("metric_date<=?")
            params.append(end_date.isoformat())
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM marketing_campaign_metrics
                WHERE {' AND '.join(conditions)}
                ORDER BY metric_date DESC, campaign_name ASC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._performance_view(dict(row)) for row in rows]

    def diagnose(self, tenant_id: str, query: MarketingDiagnosisQuery) -> dict[str, Any]:
        rows = self.list_performance(
            tenant_id,
            store_id=query.store_id,
            start_date=query.start_date,
            end_date=query.end_date,
        )
        totals = {
            "spend": sum((Decimal(item["spend"]) for item in rows), Decimal("0")),
            "revenue": sum((Decimal(item["attributed_revenue"]) for item in rows), Decimal("0")),
            "orders": sum((int(item["attributed_orders"]) for item in rows), 0),
            "impressions": sum((int(item["impressions"]) for item in rows), 0),
            "clicks": sum((int(item["clicks"]) for item in rows), 0),
        }
        roas = totals["revenue"] / totals["spend"] if totals["spend"] else None
        ctr = (
            Decimal(totals["clicks"]) / Decimal(totals["impressions"])
            if totals["impressions"]
            else None
        )
        findings: list[dict[str, Any]] = []
        for item in rows:
            item_spend = Decimal(item["spend"])
            item_revenue = Decimal(item["attributed_revenue"])
            item_roas = item_revenue / item_spend if item_spend else None
            item_ctr = (
                Decimal(item["clicks"]) / Decimal(item["impressions"])
                if item["impressions"]
                else None
            )
            if item_spend > 0 and item["attributed_orders"] == 0:
                findings.append(
                    self._finding(item, "high_spend_no_orders", "high", "停止或调整投放前需人工审批")
                )
            elif item_roas is not None and item_roas < query.min_roas:
                findings.append(
                    self._finding(item, "roas_below_target", "medium", "建议复核素材、受众和归因窗口，不自动改预算")
                )
            if item_ctr is not None and item["impressions"] >= 100 and item_ctr < query.min_ctr:
                findings.append(
                    self._finding(item, "ctr_below_target", "low", "建议生成内容草稿并进行人工事实审核")
                )
        if rows and not findings:
            findings.append(
                {
                    "code": "within_threshold",
                    "severity": "info",
                    "recommendation": "当前样本未触发阈值；仍需人工确认归因与预算口径。",
                }
            )
        source_types = sorted({str(item["source_type"]) for item in rows})
        return {
            "period": {
                "store_id": query.store_id,
                "start_date": query.start_date.isoformat() if query.start_date else None,
                "end_date": query.end_date.isoformat() if query.end_date else None,
            },
            "totals": {
                "spend": _money(totals["spend"]),
                "attributed_revenue": _money(totals["revenue"]),
                "attributed_orders": totals["orders"],
                "impressions": totals["impressions"],
                "clicks": totals["clicks"],
                "roas": _money(roas) if roas is not None else None,
                "ctr": str(ctr.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)) if ctr is not None else None,
            },
            "thresholds": {"min_roas": _money(query.min_roas), "min_ctr": str(query.min_ctr)},
            "findings": findings,
            "data_quality": {
                "record_count": len(rows),
                "source_types": source_types,
                "virtual_only": bool(rows) and source_types == ["virtual"],
                "attribution_is_not_financial_revenue": True,
            },
            "action_boundary": "仅生成诊断和内容建议；不执行竞价、预算修改或内容发布。",
        }

    def save_content_draft(self, tenant_id: str, value: ContentDraftUpsert) -> dict[str, Any]:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT id, version FROM marketing_content_drafts
                WHERE tenant_id=? AND draft_key=?
                """,
                (tenant_id, value.draft_key),
            ).fetchone()
            if existing is None and value.expected_record_version != 0:
                raise ValueError("content_draft_not_found")
            if existing is not None and value.expected_record_version != int(existing["version"]):
                raise ValueError("content_draft_version_conflict")
            draft_id = str(existing["id"]) if existing else f"content-{uuid.uuid4().hex}"
            version = int(existing["version"]) + 1 if existing else 1
            fact_check = self._fact_check(conn, tenant_id, value)
            conn.execute(
                """
                INSERT INTO marketing_content_drafts(
                    id, tenant_id, draft_key, store_id, content_type, title, body,
                    sku_ids_json, declared_prices_json, fact_check_json, status,
                    source_type, source_id, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, draft_key) DO UPDATE SET
                    store_id=excluded.store_id, content_type=excluded.content_type,
                    title=excluded.title, body=excluded.body, sku_ids_json=excluded.sku_ids_json,
                    declared_prices_json=excluded.declared_prices_json,
                    fact_check_json=excluded.fact_check_json, status='draft',
                    source_type=excluded.source_type, source_id=excluded.source_id,
                    version=excluded.version, updated_at=excluded.updated_at
                """,
                (
                    draft_id,
                    tenant_id,
                    value.draft_key,
                    value.store_id,
                    value.content_type,
                    value.title,
                    value.body,
                    json.dumps(value.sku_ids, ensure_ascii=False),
                    json.dumps({key: _money(item) for key, item in value.declared_prices.items()}, ensure_ascii=False, sort_keys=True),
                    json.dumps(fact_check, ensure_ascii=False, sort_keys=True),
                    value.source_type,
                    value.source_id,
                    version,
                    now,
                    now,
                ),
            )
        return self._draft_by_id(tenant_id, draft_id)

    def list_content_drafts(
        self, tenant_id: str, *, store_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM marketing_content_drafts WHERE {' AND '.join(conditions)}
                ORDER BY updated_at DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._draft_view(dict(row)) for row in rows]

    @staticmethod
    def _finding(
        item: dict[str, Any], code: str, severity: str, recommendation: str
    ) -> dict[str, Any]:
        return {
            "campaign_id": item["campaign_id"],
            "campaign_name": item["campaign_name"],
            "code": code,
            "severity": severity,
            "recommendation": recommendation,
            "evidence": {
                "metric_date": item["metric_date"],
                "spend": item["spend"],
                "attributed_revenue": item["attributed_revenue"],
                "attributed_orders": item["attributed_orders"],
                "source_id": item["source_id"],
                "source_type": item["source_type"],
            },
        }

    @staticmethod
    def _fact_check(conn: Any, tenant_id: str, value: ContentDraftUpsert) -> dict[str, Any]:
        placeholders = ",".join("?" for _ in value.sku_ids)
        rows = conn.execute(
            f"""
            SELECT sku_id, title, sale_price, status FROM catalog_items
            WHERE tenant_id=? AND store_id=? AND sku_id IN ({placeholders})
            """,
            (tenant_id, value.store_id, *value.sku_ids),
        ).fetchall()
        by_sku = {str(row["sku_id"]): dict(row) for row in rows}
        missing = sorted(set(value.sku_ids) - set(by_sku))
        inactive = sorted(
            sku for sku, item in by_sku.items() if str(item["status"]) != "active"
        )
        price_mismatches = []
        for sku, declared in value.declared_prices.items():
            item = by_sku.get(sku)
            if item is not None and Decimal(str(item["sale_price"])) != declared:
                price_mismatches.append(
                    {"sku_id": sku, "declared": _money(declared), "catalog": str(item["sale_price"])}
                )
        passed = not missing and not inactive and not price_mismatches
        return {
            "status": "limited_passed" if passed else "needs_review",
            "passed": passed,
            "checked_skus": sorted(by_sku),
            "missing_skus": missing,
            "inactive_skus": inactive,
            "price_mismatches": price_mismatches,
            "machine_checked_fields": ["referenced_sku", "catalog_status", "declared_price"],
            "unverified_body_claims": True,
            "publication_allowed": False,
            "required_next_step": "人工审核后才能进入任何外部发布流程。",
        }

    def _performance_by_id(self, tenant_id: str, metric_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM marketing_campaign_metrics WHERE tenant_id=? AND id=?",
                (tenant_id, metric_id),
            ).fetchone()
        if row is None:
            raise ValueError("marketing_metric_not_found")
        return self._performance_view(dict(row))

    def _draft_by_id(self, tenant_id: str, draft_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM marketing_content_drafts WHERE tenant_id=? AND id=?",
                (tenant_id, draft_id),
            ).fetchone()
        if row is None:
            raise ValueError("content_draft_not_found")
        return self._draft_view(dict(row))

    @staticmethod
    def _performance_view(row: dict[str, Any]) -> dict[str, Any]:
        spend = Decimal(str(row["spend"]))
        revenue = Decimal(str(row["attributed_revenue"]))
        return {
            "id": row["id"],
            "connector_id": row["connector_id"],
            "store_id": row["store_id"],
            "campaign_id": row["campaign_id"],
            "metric_date": row["metric_date"],
            "campaign_name": row["campaign_name"],
            "channel": row["channel"],
            "objective": row["objective"],
            "status": row["status"],
            "spend": _money(spend),
            "attributed_revenue": _money(revenue),
            "attributed_orders": int(row["attributed_orders"]),
            "impressions": int(row["impressions"]),
            "clicks": int(row["clicks"]),
            "roas": _money(revenue / spend) if spend else None,
            "source_type": row["source_type"],
            "source_id": row["source_id"],
            "source_updated_at": row["source_updated_at"],
            "version": int(row["version"]),
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _draft_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "draft_key": row["draft_key"],
            "store_id": row["store_id"],
            "content_type": row["content_type"],
            "title": row["title"],
            "body": row["body"],
            "sku_ids": json.loads(row["sku_ids_json"]),
            "declared_prices": json.loads(row["declared_prices_json"]),
            "fact_check": json.loads(row["fact_check_json"]),
            "status": row["status"],
            "source_type": row["source_type"],
            "source_id": row["source_id"],
            "record_version": int(row["version"]),
            "updated_at": row["updated_at"],
            "publication_allowed": False,
        }
