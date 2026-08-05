"""M6 竞品对比分析引擎（F-312）——分析层服务骨架。

本服务是分析层（工作包 4）的入口：在既有 F-304 数据层之上做多维度对比分析，
不重复实现数据层能力，也不修改 competitive.py（零回归约束）。

P1 范围（本文件当前实现）：
- 服务骨架 CompetitiveReportService
- D-025 门禁：只消费已批准同款匹配绑定的价格证据
- 价格区间对比：基于已批准数据计算多竞品价格分布

后续 P2/P3 在此文件扩展：情感占比、口碑得分、卖点差异、定位建议、
维度门控、Prompt 组装。P4/P5 在报告服务（另文件或本文件）扩展渲染与导出。

边界（对齐 M6_WORKBENCH 工作包 4）：
- 数值由代码计算，模型只写解读文字
- 只输出建议，不执行改价/下架/投放
- 不保存评论者身份或原始评论
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..database import Database
from .competitive import CompetitiveIntelligenceService


class CompetitiveReportService:
    """竞品对比分析引擎。消费已批准竞品事实，输出可量化对比结论。"""

    #: 分析引擎允许消费的价格证据数量上限（防越权拉全量，与数据层口径一致）
    MAX_PRICE_EVIDENCE = 5000

    def __init__(self, db: Database) -> None:
        self.db = db
        # 依赖注入数据层服务，不直接触库 —— 复用其 D-025 门禁与既有算术。
        self._intelligence = CompetitiveIntelligenceService(db)

    def analyze(
        self,
        tenant_id: str,
        subject_sku: str,
        *,
        store_id: str | None = None,
    ) -> dict[str, Any]:
        """对自有商品执行竞品价格区间对比分析。

        输入只允许已批准 match 绑定的价格证据（D-025 门禁由数据层
        analyze_prices 的 actionable 标记保证：未批准数据 actionable=False，
        本方法只对其做质量复核统计，绝不进入对比区间）。

        返回结构对齐任务文档「先表格后文字」的报告前身：
        - data_as_of / summary：分析口径
        - price_bands：价格区间分布（核心交付）
        - actionable_only：已批准数据才进入的对比结论
        - guardrail：门禁声明
        """
        raw = self._intelligence.analyze_prices(
            tenant_id, subject_sku, store_id=store_id
        )
        entries = raw["observations"]
        actionable = [item for item in entries if item["actionable"]]

        price_bands = self._build_price_bands(actionable)
        return {
            "subject_sku": subject_sku,
            "store_id": store_id,
            "data_as_of": raw["data_as_of"],
            "summary": {
                "total_observations": len(entries),
                "approved_observations": len(actionable),
                "blocked_by_gate": len(entries) - len(actionable),
                "price_evidence_cap": self.MAX_PRICE_EVIDENCE,
                "guardrail": "分析只使用已批准同款匹配的价格证据（D-025）",
            },
            "price_bands": price_bands,
            "recommendations": raw["recommendations"],
            "content_review_insights": raw["content_review_insights"],
            "signals": raw["signals"],
            "observations": actionable,
        }

    def _build_price_bands(
        self, actionable: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """从已批准价格证据计算区间分布。

        区间语义：
        - band == "our_price_lower"  —— 自有价格更低（竞品价 > 自有价）
        - band == "same_price"       —— 价格一致（含 ±0 算术差）
        - band == "our_price_higher" —— 自有价格更高（竞品价 < 自有价）

        每个竞品只取最新一条证据；差异百分比为纯算术（gap / 自有价）。
        """
        bands: dict[str, list[dict[str, Any]]] = {
            "our_price_lower": [],
            "same_price": [],
            "our_price_higher": [],
        }
        for item in actionable:
            bands.setdefault(item["position"], []).append(
                {
                    "competitor_name": item["competitor_name"],
                    "competitor_sku": item["competitor_sku"],
                    "competitor_price": item["competitor_price"],
                    "subject_price": item["subject_price"],
                    "gap_amount": item["gap_amount"],
                    "gap_percent": item["gap_percent"],
                    "observed_at": item["observed_at"],
                }
            )

        result: list[dict[str, Any]] = []
        for band in ("our_price_lower", "same_price", "our_price_higher"):
            members = bands[band]
            gaps = [Decimal(item["gap_percent"]) for item in members]
            result.append(
                {
                    "band": band,
                    "competitor_count": len(members),
                    "share_percent": self._share(len(members), len(actionable)),
                    "average_gap_percent": self._decimal(
                        sum(gaps) / len(gaps)
                    )
                    if gaps
                    else None,
                    "min_gap_percent": self._decimal(min(gaps)) if gaps else None,
                    "max_gap_percent": self._decimal(max(gaps)) if gaps else None,
                    "members": members,
                }
            )
        return result

    @staticmethod
    def _share(part: int, total: int) -> Decimal | None:
        if total <= 0:
            return None
        return (Decimal(part) / Decimal(total) * Decimal("100")).quantize(
            Decimal("0.01")
        )

    @staticmethod
    def _decimal(value: Decimal | None) -> str | None:
        if value is None:
            return None
        return format(value, "f")
