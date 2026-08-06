from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..database import Database, utc_now
from .source_versioning import payload_digest


OpsSourceFormat = Literal["csv", "json", "form"]
CopyStyle = Literal[
    "formal",
    "playful",
    "urgent",
    "premium",
    "concise",
    "xiaohongshu",
    "livestream",
    "product_detail",
    "wechat_moments",
]
CopyLength = Literal["short", "medium", "long"]

MAX_IMPORT_ROWS = 2000

COPY_STYLE_LABELS: dict[str, str] = {
    "formal": "专业详实",
    "playful": "活泼种草",
    "urgent": "促销紧迫",
    "premium": "高端质感",
    "concise": "简洁卖点",
    "xiaohongshu": "小红书种草",
    "livestream": "直播话术",
    "product_detail": "详情页文案",
    "wechat_moments": "朋友圈推广",
}

COPY_LENGTH_RANGES: dict[CopyLength, tuple[int, int]] = {
    "short": (20, 60),
    "medium": (61, 120),
    "long": (121, 200),
}
COPY_SAFETY_CLOSER = "商品规格、价格与活动以详情页为准。"

# 每个场景都有独立结构约束，避免只替换一个风格名称却生成同构文案。
COPY_STYLE_PROMPTS: dict[str, str] = {
    "formal": "采用专业说明结构：先给事实结论，再展开卖点，最后给出核对提示。",
    "playful": "采用轻松种草结构：口语化开场、生活化感受、轻量行动提示。",
    "urgent": "采用活动提醒结构：先说明活动主题，再列核心卖点和活动边界。",
    "premium": "采用质感叙事结构：突出设计取向、使用体验与克制的选购建议。",
    "concise": "采用极简信息结构：一行核心卖点、一行补充事实、一行核对提示。",
    "xiaohongshu": "采用体验分享式种草笔记：场景痛点、亲历口吻、分点感受和话题收束。",
    "livestream": "采用直播口播节奏：抓注意力、逐点讲解、互动承接和合规下单提醒。",
    "product_detail": "采用详情页卖点分层：核心卖点、使用场景、适用人群和参数核对提示。",
    "wechat_moments": "采用朋友圈熟人分享结构：自然推荐理由、生活场景和克制的行动邀请。",
}

# 广告法高风险绝对化用语；命中的文案必须人工复核后才能进入任何发布流程。
RISK_TERMS: tuple[str, ...] = (
    "最",
    "第一",
    "顶级",
    "绝对",
    "全网最低",
    "100%",
    "国家级",
)

# 中文表头到规范字段的映射，兼容运营团队直接导出的中文 CSV。
COLUMN_ALIASES: dict[str, str] = {
    "record_date": "record_date",
    "日期": "record_date",
    "channel": "channel",
    "渠道": "channel",
    "visitors": "visitors",
    "访客数": "visitors",
    "orders": "orders",
    "订单数": "orders",
    "sales_amount": "sales_amount",
    "销售额": "sales_amount",
    "ad_spend": "ad_spend",
    "推广花费": "ad_spend",
}


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _ratio(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


class OpsOperationRecordUpsert(BaseModel):
    """One traceable operations-day fact. It never triggers spend or pricing changes."""

    model_config = ConfigDict(extra="forbid")

    dataset_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    store_id: str = Field(min_length=1, max_length=128)
    record_date: date
    channel: str = Field(min_length=1, max_length=64)
    visitors: int = Field(ge=0, le=2_000_000_000)
    orders: int = Field(ge=0, le=2_000_000_000)
    sales_amount: Decimal = Field(ge=0)
    ad_spend: Decimal = Field(default=Decimal("0"), ge=0)
    source_format: OpsSourceFormat = "form"
    source_id: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_funnel(self) -> "OpsOperationRecordUpsert":
        if self.orders > self.visitors:
            raise ValueError("ops_orders_exceed_visitors")
        return self


class CopywritingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=128)
    product_name: str = Field(min_length=1, max_length=120)
    selling_points: list[str] = Field(min_length=1, max_length=6)
    price: Decimal | None = Field(default=None, ge=0)
    target_audience: str | None = Field(default=None, max_length=64)
    styles: list[CopyStyle] = Field(min_length=1, max_length=5)
    variants_per_style: int = Field(default=1, ge=1, le=3)
    length: CopyLength = "medium"

    @field_validator("selling_points")
    @classmethod
    def normalize_selling_points(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item or len(item) > 60 for item in normalized):
            raise ValueError("invalid_selling_point")
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate_selling_point")
        return normalized

    @field_validator("styles")
    @classmethod
    def unique_styles(cls, value: list[CopyStyle]) -> list[CopyStyle]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate_copy_style")
        return value

    @model_validator(mode="after")
    def small_batch_only(self) -> "CopywritingRequest":
        if len(self.styles) * self.variants_per_style > 9:
            raise ValueError("copy_batch_too_large")
        return self


class CopywritingRegenerateRequest(CopywritingRequest):
    edited_copy: str = Field(min_length=1, max_length=1000)

    @field_validator("edited_copy")
    @classmethod
    def normalize_edited_copy(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("edited_copy_empty")
        return normalized

    @model_validator(mode="after")
    def edited_copy_fits_requested_length(self) -> "CopywritingRegenerateRequest":
        revision = (
            self.edited_copy
            if self.edited_copy.endswith(("。", "！", "？", "!", "?"))
            else f"{self.edited_copy}。"
        )
        maximum = COPY_LENGTH_RANGES[self.length][1]
        if len(revision) + len(COPY_SAFETY_CLOSER) > maximum:
            raise ValueError("copy_revision_too_long_for_length")
        return self


class OpsReportQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_key: str | None = Field(default=None, max_length=128)
    store_id: str | None = Field(default=None, max_length=128)
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def dates_are_ordered(self) -> "OpsReportQuery":
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("ops_report_date_range_invalid")
        return self


class OpsAssistantService:
    """M5 运营辅助：数据解析、文案生成与分析报告。

    模型只负责把结构化数据转成可读文字；所有数值由固化代码计算，
    模型不可用时自动降级到确定性模板，输出始终标记生成方式。
    """

    def __init__(self, db: Database):
        self.db = db
        self._model: Any | None = None

    def attach_model(self, gateway: Any) -> None:
        self._model = gateway

    # ------------------------------------------------------------------
    # 运营数据解析
    # ------------------------------------------------------------------

    def parse_dataset(
        self,
        tenant_id: str,
        *,
        dataset_key: str,
        store_id: str,
        source_format: Literal["csv", "json"],
        content: str,
    ) -> dict[str, Any]:
        if source_format == "csv":
            raw_rows = self._rows_from_csv(content)
        else:
            raw_rows = self._rows_from_json(content)
        if len(raw_rows) > MAX_IMPORT_ROWS:
            raise ValueError("ops_dataset_too_large")
        if not raw_rows:
            raise ValueError("ops_dataset_empty")
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        applied = 0
        idempotent = 0
        for index, raw in enumerate(raw_rows, start=1):
            try:
                value = OpsOperationRecordUpsert(
                    dataset_key=dataset_key,
                    store_id=store_id,
                    source_format=source_format,
                    **self._normalize_row(raw),
                )
            except (ValidationError, ValueError) as exc:
                rejected.append({"row": index, "reason": self._row_error(exc)})
                continue
            result = self.upsert_record(tenant_id, value)
            applied += int(result["write_status"] == "applied")
            idempotent += int(result["write_status"] == "idempotent")
            accepted.append(result)
        return {
            "dataset_key": dataset_key,
            "store_id": store_id,
            "source_format": source_format,
            "total_rows": len(raw_rows),
            "accepted_rows": len(accepted),
            "rejected_rows": len(rejected),
            "applied": applied,
            "idempotent": idempotent,
            "records": accepted,
            "rejected": rejected,
        }

    def upsert_record(self, tenant_id: str, value: OpsOperationRecordUpsert) -> dict[str, Any]:
        payload = value.model_dump(mode="json")
        payload_hash = payload_digest(payload)
        now = utc_now()
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT id, payload_hash, version FROM ops_operation_records
                WHERE tenant_id=? AND dataset_key=? AND record_date=? AND channel=?
                """,
                (tenant_id, value.dataset_key, value.record_date.isoformat(), value.channel),
            ).fetchone()
            record_id = str(existing["id"]) if existing else f"ops-{uuid.uuid4().hex}"
            if existing is not None and str(existing["payload_hash"]) == payload_hash:
                write_status = "idempotent"
            if write_status == "applied":
                version = int(existing["version"]) + 1 if existing else 1
                conn.execute(
                    """
                    INSERT INTO ops_operation_records(
                        id, tenant_id, dataset_key, store_id, record_date, channel,
                        visitors, orders, sales_amount, ad_spend, source_format,
                        source_id, payload_hash, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, dataset_key, record_date, channel)
                    DO UPDATE SET store_id=excluded.store_id, visitors=excluded.visitors,
                        orders=excluded.orders, sales_amount=excluded.sales_amount,
                        ad_spend=excluded.ad_spend, source_format=excluded.source_format,
                        source_id=excluded.source_id, payload_hash=excluded.payload_hash,
                        version=excluded.version, updated_at=excluded.updated_at
                    """,
                    (
                        record_id,
                        tenant_id,
                        value.dataset_key,
                        value.store_id,
                        value.record_date.isoformat(),
                        value.channel,
                        value.visitors,
                        value.orders,
                        _money(value.sales_amount),
                        _money(value.ad_spend),
                        value.source_format,
                        value.source_id,
                        payload_hash,
                        version,
                        now,
                        now,
                    ),
                )
        result = self._record_by_id(tenant_id, record_id)
        result["write_status"] = write_status
        return result

    def list_records(
        self,
        tenant_id: str,
        *,
        dataset_key: str | None = None,
        store_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = 500,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if dataset_key:
            conditions.append("dataset_key=?")
            params.append(dataset_key)
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        if start_date:
            conditions.append("record_date>=?")
            params.append(start_date.isoformat())
        if end_date:
            conditions.append("record_date<=?")
            params.append(end_date.isoformat())
        limit_clause = ""
        if limit is not None:
            params.append(limit)
            limit_clause = "LIMIT ?"
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM ops_operation_records WHERE {' AND '.join(conditions)}
                ORDER BY record_date DESC, channel ASC {limit_clause}
                """,
                tuple(params),
            ).fetchall()
        return [self._record_view(dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # 营销文案生成
    # ------------------------------------------------------------------

    def generate_copy(self, tenant_id: str, request: CopywritingRequest) -> dict[str, Any]:
        return self._generate_copy(request)

    def regenerate_copy(
        self, tenant_id: str, request: CopywritingRegenerateRequest
    ) -> dict[str, Any]:
        return self._generate_copy(request, revision_text=request.edited_copy)

    def _generate_copy(
        self,
        request: CopywritingRequest,
        *,
        revision_text: str | None = None,
    ) -> dict[str, Any]:
        variants: list[dict[str, Any]] = []
        source_text = " ".join(
            item
            for item in (
                request.product_name,
                *request.selling_points,
                _money(request.price) if request.price is not None else None,
            )
            if item
        )
        source_risk_terms = sorted({term for term in RISK_TERMS if term in source_text})
        for style in request.styles:
            for index in range(request.variants_per_style):
                generated = self._model_copy_variant(
                    request, style, index, revision_text=revision_text
                )
                generator = "model"
                if generated is None:
                    generated = self._template_copy_variant(
                        request, style, index, revision_text=revision_text
                    )
                    generator = "template_fallback" if self._model else "template"
                title, body = generated
                text = f"{title}{body}"
                rendered_risk_terms = sorted({term for term in RISK_TERMS if term in text})
                risk_terms = sorted(set(rendered_risk_terms) | set(source_risk_terms))
                variants.append(
                    {
                        "variant_id": f"copy-{style}-{index + 1}",
                        "style": style,
                        "style_label": COPY_STYLE_LABELS[style],
                        "title": title,
                        "body": body,
                        "char_count": len(body),
                        "risk_terms": risk_terms,
                        "rendered_risk_terms": rendered_risk_terms,
                        "source_risk_terms": source_risk_terms,
                        "needs_review": bool(risk_terms),
                        "generator": generator,
                        "publication_allowed": False,
                    }
                )
        return {
            "store_id": request.store_id,
            "product_name": request.product_name,
            "selling_points": request.selling_points,
            "price": _money(request.price) if request.price is not None else None,
            "target_audience": request.target_audience,
            "requested_styles": list(request.styles),
            "variants_per_style": request.variants_per_style,
            "length": request.length,
            "length_range": list(COPY_LENGTH_RANGES[request.length]),
            "batch_size": len(variants),
            "variants": variants,
            "revision_applied": revision_text is not None,
            "revision_source": revision_text,
            "publication_allowed": False,
            "action_boundary": "仅生成候选文案；发布前必须人工审核卖点与价格主张。",
        }

    # ------------------------------------------------------------------
    # 运营分析报告
    # ------------------------------------------------------------------

    def analysis_report(self, tenant_id: str, query: OpsReportQuery) -> dict[str, Any]:
        rows = self.list_records(
            tenant_id,
            dataset_key=query.dataset_key,
            store_id=query.store_id,
            start_date=query.start_date,
            end_date=query.end_date,
            limit=None,
        )
        rows = sorted(rows, key=lambda item: (item["record_date"], item["channel"]))
        totals = self._totals(rows)
        first_half, second_half = self._split_halves(rows)
        trends = self._trends(self._totals(first_half), self._totals(second_half))
        channels = self._channel_breakdown(rows)
        findings = self._findings(rows, totals, trends, channels)
        summary = self._summary_lines(rows, totals, trends)
        narrative, narrative_generator = self._model_narrative(totals, trends, findings)
        source_formats = sorted({str(item["source_format"]) for item in rows})
        return {
            "period": {
                "dataset_key": query.dataset_key,
                "store_id": query.store_id,
                "start_date": rows[0]["record_date"] if rows else None,
                "end_date": rows[-1]["record_date"] if rows else None,
            },
            "totals": totals,
            "trends": trends,
            "channels": channels,
            "findings": findings,
            "summary": summary,
            "narrative": narrative,
            "narrative_generator": narrative_generator,
            "data_quality": {
                "record_count": len(rows),
                "source_formats": source_formats,
                "numbers_computed_by_code": True,
            },
            "action_boundary": "仅输出数据解读与优化建议；不执行预算、价格或库存变更。",
        }

    # ------------------------------------------------------------------
    # 解析辅助
    # ------------------------------------------------------------------

    @classmethod
    def _rows_from_csv(cls, content: str) -> list[dict[str, Any]]:
        reader = csv.DictReader(io.StringIO(content.strip()))
        if not reader.fieldnames:
            raise ValueError("ops_csv_header_missing")
        return [dict(row) for row in reader]

    @staticmethod
    def _rows_from_json(content: str) -> list[dict[str, Any]]:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("ops_json_invalid") from exc
        if isinstance(payload, dict):
            payload = payload.get("records")
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ValueError("ops_json_records_missing")
        return payload

    @classmethod
    def _normalize_row(cls, raw: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, value in raw.items():
            field = COLUMN_ALIASES.get(str(key).strip())
            if field is None:
                continue
            if isinstance(value, str):
                value = value.strip()
            normalized[field] = value
        missing = {"record_date", "channel", "visitors", "orders", "sales_amount"} - set(
            key for key, value in normalized.items() if value not in (None, "")
        )
        if missing:
            raise ValueError(f"missing_fields:{','.join(sorted(missing))}")
        normalized.setdefault("ad_spend", "0")
        if normalized["ad_spend"] in (None, ""):
            normalized["ad_spend"] = "0"
        return normalized

    @staticmethod
    def _row_error(exc: Exception) -> str:
        if isinstance(exc, ValidationError):
            first = exc.errors()[0]
            location = ".".join(str(item) for item in first["loc"]) or "row"
            detail = first["msg"] if first["type"] == "value_error" else first["type"]
            return f"{location}:{detail}"
        return str(exc)

    def _record_by_id(self, tenant_id: str, record_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM ops_operation_records WHERE tenant_id=? AND id=?",
                (tenant_id, record_id),
            ).fetchone()
        if row is None:
            raise ValueError("ops_record_not_found")
        return self._record_view(dict(row))

    @staticmethod
    def _record_view(row: dict[str, Any]) -> dict[str, Any]:
        visitors = int(row["visitors"])
        orders = int(row["orders"])
        sales = Decimal(str(row["sales_amount"]))
        spend = Decimal(str(row["ad_spend"]))
        return {
            "id": row["id"],
            "dataset_key": row["dataset_key"],
            "store_id": row["store_id"],
            "record_date": row["record_date"],
            "channel": row["channel"],
            "visitors": visitors,
            "orders": orders,
            "sales_amount": _money(sales),
            "ad_spend": _money(spend),
            "conversion_rate": _ratio(Decimal(orders) / Decimal(visitors)) if visitors else None,
            "source_format": row["source_format"],
            "source_id": row["source_id"],
            "version": int(row["version"]),
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------
    # 文案模板引擎（确定性降级路径）
    # ------------------------------------------------------------------

    def _template_copy_variant(
        self,
        request: CopywritingRequest,
        style: str,
        index: int,
        *,
        revision_text: str | None = None,
    ) -> tuple[str, str]:
        points = request.selling_points
        lead = points[index % len(points)]
        rest = "、".join(item for item in points if item != lead) or lead
        name = request.product_name
        audience = request.target_audience or "认真生活的你"
        price = _money(request.price) if request.price is not None else None
        price_line = f"到手价 {price} 元，" if price else ""
        if style == "formal":
            title = f"{name}：{lead}，为日常使用而设计"
            body = (
                f"{name}围绕{lead}打造，兼顾{rest}。"
                f"{price_line}参数与承诺以商品详情页为准，欢迎对比后选择。"
            )
        elif style == "playful":
            title = f"被{name}圈粉了！{lead}真的很可"
            body = (
                f"谁懂啊，{lead}这一点直接戳中{audience}～"
                f"还有{rest}加分。{price_line}冲之前记得看清规格哦。"
            )
        elif style == "urgent":
            title = f"限时上新：{name}，{lead}"
            body = (
                f"本期活动聚焦{lead}，同时带来{rest}。"
                f"{price_line}活动以店铺页面公示时间为准，售完即止。"
            )
        elif style == "premium":
            title = f"{name}·{lead}的进阶之选"
            body = (
                f"以{lead}为核心，辅以{rest}，为{audience}提供更从容的体验。"
                f"{price_line}细节以实物与详情页信息为准。"
            )
        elif style == "concise":
            title = f"{name} | {lead}"
            body = f"{lead}；{rest}。{price_line}详情页可查完整参数。"
        elif style == "xiaohongshu":
            title = f"种草笔记｜{name}的{lead}体验"
            body = (
                f"最近给{audience}挖到一个实用选择：{lead}很贴近日常场景，"
                f"{rest}也值得留意。{price_line}入手前记得核对详情页规格。"
            )
        elif style == "livestream":
            title = f"直播口播｜{name}重点看{lead}"
            body = (
                f"朋友们看这里，{name}先看{lead}，再看{rest}。"
                f"{price_line}需要的朋友先对照详情页参数和活动规则，再决定是否下单。"
            )
        elif style == "product_detail":
            title = f"核心卖点｜{name}"
            body = (
                f"【核心卖点】{lead}。【补充亮点】{rest}。"
                f"【适用人群】{audience}。【购买提示】{price_line}完整规格以详情页为准。"
            )
        else:  # wechat_moments
            title = f"朋友圈分享｜最近留意到{name}"
            body = (
                f"今天想把{name}分享给{audience}：{lead}是我首先留意的点，"
                f"{rest}也很实用。{price_line}感兴趣可以先去详情页看看完整信息。"
            )
        return title, self._fit_copy_length(
            body, request, style=style, index=index, revision_text=revision_text
        )

    @staticmethod
    def _fit_copy_length(
        body: str,
        request: CopywritingRequest,
        *,
        style: str,
        index: int,
        revision_text: str | None = None,
    ) -> str:
        minimum, maximum = COPY_LENGTH_RANGES[request.length]
        revision = OpsAssistantService._revision_sentence(revision_text)
        if request.length == "short":
            return OpsAssistantService._compact_copy_body(
                request, style, index, revision, minimum=minimum, maximum=maximum
            )
        fitted = f"{revision}{body}" if revision else body
        if len(fitted) > maximum:
            return OpsAssistantService._compact_copy_body(
                request, style, index, revision, minimum=minimum, maximum=maximum
            )
        audience = request.target_audience or "有相关需要的人"
        points = "、".join(request.selling_points)
        additions = (
            f"可以结合{audience}的实际使用场景，重点比较{points}。",
            "文案只呈现已提供的商品信息，不替代页面中的完整参数、库存和活动说明。",
            "选购前建议再次核对商品详情页，确认规格、价格与适用条件符合当前需要。",
        )
        for addition in additions:
            if len(fitted) >= minimum:
                break
            if len(fitted) + len(addition) <= maximum:
                fitted += addition
        if len(fitted) < minimum:
            return OpsAssistantService._compact_copy_body(
                request, style, index, revision, minimum=minimum, maximum=maximum
            )
        return fitted

    @staticmethod
    def _revision_sentence(revision_text: str | None) -> str:
        if not revision_text:
            return ""
        normalized = revision_text.strip()
        if not normalized:
            return ""
        return normalized if normalized.endswith(("。", "！", "？", "!", "?")) else f"{normalized}。"

    @staticmethod
    def _compact_copy_body(
        request: CopywritingRequest,
        style: str,
        index: int,
        revision: str,
        *,
        minimum: int,
        maximum: int,
    ) -> str:
        audience = request.target_audience or "有相关需要的人"
        lead = request.selling_points[index % len(request.selling_points)]
        style_lead_prefixes = {
            "formal": ("围绕", "重点看", "优先了解"),
            "playful": ("种草点是", "心动点是", "加分点是"),
            "urgent": ("活动重点看", "下单前先看", "限时关注"),
            "premium": ("质感亮点是", "从容体验来自", "进阶感来自"),
            "concise": ("核心卖点：", "重点亮点：", "产品亮点："),
            "xiaohongshu": ("种草点是", "心动点是", "笔记重点是"),
            "livestream": ("直播重点讲", "镜头先看", "上播先讲"),
            "product_detail": ("【核心卖点】", "【重点亮点】", "【产品亮点】"),
            "wechat_moments": ("想分享的点是", "今天推荐的点是", "值得说的点是"),
        }
        style_lead = f"{style_lead_prefixes[style][index % 3]}{lead}。"
        # 卖点本身允许最长 60 字，无法在短档完整容纳时绝不截断它；
        # 改用可区分且不新增商品主张的风格化提示，避免批量正文退化为同一条。
        compact_style_markers = {
            "formal": ("专业选购参考。", "重点信息提示。", "使用前先核对。"),
            "playful": ("先看这条小提示。", "换个角度看看。", "下单前多核对。"),
            "urgent": ("活动信息请核对。", "下单前再确认。", "限时信息看详情。"),
            "premium": ("细节值得留意。", "从容做个比较。", "选择前看看参数。"),
            "concise": ("选购重点提示。", "参数核对提示。", "页面信息提示。"),
            "xiaohongshu": ("这条先记下来。", "换个角度看。", "入手前先核对。"),
            "livestream": ("镜头前先提示。", "下单前先确认。", "直播信息看详情。"),
            "product_detail": ("【选购提示】。", "【参数说明】。", "【页面信息】。"),
            "wechat_moments": ("今天先说个提示。", "下单前先看看。", "分享前先核对。"),
        }
        if len(style_lead) + len(COPY_SAFETY_CLOSER) > maximum:
            style_lead = compact_style_markers[style][index % 3]
        price = _money(request.price) if request.price is not None else None
        units = [
            revision,
            style_lead,
            f"商品：{request.product_name}。",
            f"到手价 {price} 元。" if price else "",
            f"适合{audience}的日常使用场景。",
            "下单前请核对商品参数和活动规则。",
            "完整卖点、规格与服务承诺以商品详情页展示为准。",
            "文案仅供参考，实际库存与价格请以页面实时信息为准。",
            "如需进一步比较，请结合自身使用场景查看详情页完整说明。",
        ]
        fitted = ""
        for unit in units:
            if not unit:
                continue
            if len(fitted) + len(unit) + len(COPY_SAFETY_CLOSER) <= maximum:
                fitted += unit
        fitted += COPY_SAFETY_CLOSER
        if len(fitted) < minimum:
            raise ValueError("copy_length_range_unreachable")
        return fitted

    def _model_copy_variant(
        self,
        request: CopywritingRequest,
        style: str,
        index: int,
        *,
        revision_text: str | None = None,
    ) -> tuple[str, str] | None:
        if self._model is None:
            return None
        minimum, maximum = COPY_LENGTH_RANGES[request.length]
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是电商运营文案助手。基于给定商品信息生成一条中文营销文案，"
                    "禁止编造未提供的卖点、价格或功效，禁止使用绝对化用语。"
                    f"{COPY_STYLE_PROMPTS[style]}"
                    f"正文必须为 {minimum}–{maximum} 个字符。"
                    '只返回 JSON：{"title": "...", "body": "..."}。'
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task_type": "ops_copywriting",
                        "product_name": request.product_name,
                        "selling_points": request.selling_points,
                        "price": _money(request.price) if request.price is not None else None,
                        "target_audience": request.target_audience,
                        "style": COPY_STYLE_LABELS[style],
                        "variant_index": index + 1,
                        "length": request.length,
                        "edited_copy": revision_text,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            payload = self._model.generate_json(prompt)
        except Exception:
            return None
        title = str(payload.get("title") or "").strip()
        body = str(payload.get("body") or "").strip()
        if not title or not body:
            return None
        if not minimum <= len(body) <= maximum:
            return None
        return title[:60], body

    # ------------------------------------------------------------------
    # 报告计算（全部数值由固化代码产出）
    # ------------------------------------------------------------------

    @staticmethod
    def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
        visitors = sum(int(item["visitors"]) for item in rows)
        orders = sum(int(item["orders"]) for item in rows)
        sales = sum((Decimal(item["sales_amount"]) for item in rows), Decimal("0"))
        spend = sum((Decimal(item["ad_spend"]) for item in rows), Decimal("0"))
        return {
            "visitors": visitors,
            "orders": orders,
            "sales_amount": _money(sales),
            "ad_spend": _money(spend),
            "conversion_rate": _ratio(Decimal(orders) / Decimal(visitors)) if visitors else None,
            "average_order_value": _money(sales / Decimal(orders)) if orders else None,
            "roi": _ratio(sales / spend) if spend else None,
        }

    @staticmethod
    def _split_halves(
        rows: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        dates = sorted({item["record_date"] for item in rows})
        if len(dates) < 2:
            return [], []
        cut = dates[len(dates) // 2]
        first = [item for item in rows if item["record_date"] < cut]
        second = [item for item in rows if item["record_date"] >= cut]
        return first, second

    @classmethod
    def _trends(cls, first: dict[str, Any], second: dict[str, Any]) -> list[dict[str, Any]]:
        trends: list[dict[str, Any]] = []
        for metric, label in (
            ("visitors", "访客数"),
            ("orders", "订单数"),
            ("sales_amount", "销售额"),
            ("ad_spend", "推广花费"),
        ):
            try:
                before = Decimal(str(first.get(metric) or "0"))
                after = Decimal(str(second.get(metric) or "0"))
            except InvalidOperation:
                continue
            if before == 0 and after == 0:
                continue
            change = (after - before) / before if before else None
            if change is None:
                direction = "up"
            elif abs(change) < Decimal("0.05"):
                direction = "flat"
            else:
                direction = "up" if change > 0 else "down"
            trends.append(
                {
                    "metric": metric,
                    "label": label,
                    "first_half": str(first.get(metric) or "0"),
                    "second_half": str(second.get(metric) or "0"),
                    "change_pct": (
                        str((change * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
                        if change is not None
                        else None
                    ),
                    "direction": direction,
                }
            )
        return trends

    @staticmethod
    def _channel_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for item in rows:
            bucket = grouped.setdefault(
                item["channel"],
                {"visitors": 0, "orders": 0, "sales": Decimal("0"), "spend": Decimal("0")},
            )
            bucket["visitors"] += int(item["visitors"])
            bucket["orders"] += int(item["orders"])
            bucket["sales"] += Decimal(item["sales_amount"])
            bucket["spend"] += Decimal(item["ad_spend"])
        breakdown = []
        for channel, bucket in grouped.items():
            breakdown.append(
                {
                    "channel": channel,
                    "visitors": bucket["visitors"],
                    "orders": bucket["orders"],
                    "sales_amount": _money(bucket["sales"]),
                    "ad_spend": _money(bucket["spend"]),
                    "conversion_rate": (
                        _ratio(Decimal(bucket["orders"]) / Decimal(bucket["visitors"]))
                        if bucket["visitors"]
                        else None
                    ),
                }
            )
        breakdown.sort(key=lambda item: Decimal(item["sales_amount"]), reverse=True)
        return breakdown

    @staticmethod
    def _findings(
        rows: list[dict[str, Any]],
        totals: dict[str, Any],
        trends: list[dict[str, Any]],
        channels: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        if not rows:
            return [
                {
                    "code": "no_data",
                    "severity": "info",
                    "recommendation": "尚无运营数据；请先通过 CSV/JSON 上传或表单录入。",
                }
            ]
        by_metric = {item["metric"]: item for item in trends}
        sales = by_metric.get("sales_amount")
        spend = by_metric.get("ad_spend")
        orders = by_metric.get("orders")
        visitors = by_metric.get("visitors")
        if sales and sales["direction"] == "down":
            findings.append(
                {
                    "code": "sales_declining",
                    "severity": "high",
                    "recommendation": "后半段销售额下滑，建议排查价格、评价与竞品动作，再决定是否调整活动。",
                    "evidence": sales,
                }
            )
        if sales and sales["direction"] == "up":
            findings.append(
                {
                    "code": "sales_growing",
                    "severity": "info",
                    "recommendation": "销售额呈上升趋势，建议提前确认库存与发货能力，避免断货。",
                    "evidence": sales,
                }
            )
        if spend and sales and spend["direction"] == "up" and sales["direction"] != "up":
            findings.append(
                {
                    "code": "spend_up_sales_flat",
                    "severity": "high",
                    "recommendation": "推广花费上升但销售额未同步增长，建议人工复核投放计划与素材，不自动调整预算。",
                    "evidence": {"spend": spend, "sales": sales},
                }
            )
        if visitors and orders and visitors["direction"] == "up" and orders["direction"] == "down":
            findings.append(
                {
                    "code": "conversion_declining",
                    "severity": "medium",
                    "recommendation": "访客上升而订单下降，转化率恶化，建议检查详情页、SKU 价格与客服话术。",
                    "evidence": {"visitors": visitors, "orders": orders},
                }
            )
        if totals["roi"] is not None and Decimal(totals["roi"]) < Decimal("1"):
            findings.append(
                {
                    "code": "roi_below_break_even",
                    "severity": "medium",
                    "recommendation": f"整体投产比 {totals['roi']} 低于 1，推广处于亏损区间，建议人工评估投放必要性。",
                }
            )
        overall_conversion = totals["conversion_rate"]
        if overall_conversion is not None:
            for channel in channels:
                if channel["conversion_rate"] is None:
                    continue
                if Decimal(channel["conversion_rate"]) < Decimal(overall_conversion) / 2:
                    findings.append(
                        {
                            "code": "channel_conversion_low",
                            "severity": "low",
                            "recommendation": (
                                f"渠道「{channel['channel']}」转化率 {channel['conversion_rate']} "
                                f"显著低于整体 {overall_conversion}，建议核对流量质量与承接页。"
                            ),
                            "evidence": channel,
                        }
                    )
        if len(rows) < 4:
            findings.append(
                {
                    "code": "insufficient_sample",
                    "severity": "info",
                    "recommendation": "样本少于 4 条，趋势结论仅供参考，建议积累更多数据后复核。",
                }
            )
        if not findings:
            findings.append(
                {
                    "code": "within_expectation",
                    "severity": "info",
                    "recommendation": "当前样本未触发风险规则；仍需人工确认统计口径。",
                }
            )
        return findings

    @staticmethod
    def _summary_lines(
        rows: list[dict[str, Any]],
        totals: dict[str, Any],
        trends: list[dict[str, Any]],
    ) -> list[str]:
        if not rows:
            return ["统计范围内没有运营数据。"]
        days = len({item["record_date"] for item in rows})
        lines = [
            (
                f"统计周期覆盖 {days} 天、{len(rows)} 条渠道日记录："
                f"访客 {totals['visitors']}，订单 {totals['orders']}，"
                f"销售额 {totals['sales_amount']} 元，推广花费 {totals['ad_spend']} 元。"
            )
        ]
        if totals["conversion_rate"] is not None:
            aov = totals["average_order_value"] or "-"
            lines.append(
                f"整体转化率 {totals['conversion_rate']}，客单价 {aov} 元"
                + (f"，投产比 {totals['roi']}。" if totals["roi"] is not None else "。")
            )
        for trend in trends:
            if trend["direction"] == "flat" or trend["change_pct"] is None:
                continue
            word = "上升" if trend["direction"] == "up" else "下降"
            lines.append(
                f"{trend['label']}后半段较前半段{word} {trend['change_pct']}%"
                f"（{trend['first_half']} → {trend['second_half']}）。"
            )
        return lines

    def _model_narrative(
        self,
        totals: dict[str, Any],
        trends: list[dict[str, Any]],
        findings: list[dict[str, Any]],
    ) -> tuple[str | None, str]:
        if self._model is None:
            return None, "disabled"
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是电商运营分析助手。基于给定的既定统计结果撰写一段不超过 150 字的中文解读，"
                    "不得修改或编造任何数值，不得给出预算、价格等执行指令。只返回纯文本。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task_type": "ops_report_narrative",
                        "totals": totals,
                        "trends": trends,
                        "finding_codes": [item["code"] for item in findings],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            text = str(self._model.generate(prompt)).strip()
        except Exception:
            return None, "fallback_summary_only"
        return (text[:600] or None), "model"
