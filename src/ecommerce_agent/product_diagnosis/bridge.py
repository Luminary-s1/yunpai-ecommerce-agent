"""M9-R WP2 M5-R 证据桥接层：统一只读查询（WP5 验收修复版）。

边界声明：
- 输入：tenant_id + 查询参数（experiment_id / revision_id 等）。
- 输出：统一只读证据视图 dict（含 evidence_state / source_provenance / freshness /
  data_as_of / quality_gate 结构）。
- 副作用：零——纯只读，调用 M5-R TrafficLabService 读接口，不写库、不网络写。
- 失败暴露：M5-R 未找到 → 抛 TrafficLabError（透传）；无证据 → 返回显式 missing 视图。
- 确定性：freshness 用 M5-R 固化的 analysis_input_freshness / evidence_freshness；
  provenance 读 traffic_analysis_runs.evidence_json 里的 source_provenance。
- 复用边界：本层只做「读 + 组装视图」，不重写统计（统计在 TrafficAnalysisEngine）。

真实证据位置（WP5 验收修正）：
- revision 的证据不在 revision 行顶层，而在 traffic_metric_buckets（有 data_as_of）。
- experiment / analysis 的证据在 traffic_analysis_runs.evidence_json
  （含 quality_gate / source_provenance / input_snapshot）。
"""
from __future__ import annotations

from typing import Any, Mapping

from ecommerce_agent.connectors.provenance import read_source_provenance
from ecommerce_agent.readonly_data.contracts import (
    EvidenceState,
    evidence_state_from_source_type,
    source_type_from_connector,
)
from ecommerce_agent.traffic_lab.freshness import analysis_input_freshness
from ecommerce_agent.traffic_lab.service import TrafficLabError, TrafficLabService

from .gates import GateEngine, GateResult


def _provenance_from(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """从 analysis run 的 evidence 读取 source_provenance（确定性：缺失显式 unknown）。"""
    return read_source_provenance(
        evidence.get("source_provenance"), missing_basis="traffic_analysis_run"
    )


def _evidence_state(source_type: str | None) -> EvidenceState:
    """provenance.source_type → evidence_state（权威实现在 readonly_data.contracts）。"""
    return evidence_state_from_source_type(source_type)


def _source_type_from_connector(connector_id: str | None) -> str | None:
    """按 connector_id 推导来源类型（权威实现在 readonly_data.contracts）。"""
    return source_type_from_connector(connector_id)


class EvidenceBridge:
    """统一只读证据查询：桥接 M5-R TrafficLabService + freshness/provenance。

    边界声明：
    - 构造：TrafficLabService 实例（调用方传入，测试用 tmp_path DB）。
    - 方法均为纯读，无副作用。
    - 复用边界：不新建实验框架、不重写统计、不改 M5-R 代码。
    """

    def __init__(self, service: TrafficLabService) -> None:
        self.service = service

    def get_revision_view(self, tenant_id: str, revision_id: str) -> dict[str, Any]:
        """revision 统一视图：证据取该 revision 的 metric buckets（真实持久化位置）。"""
        try:
            row = self.service.get_revision(tenant_id, revision_id)
        except TrafficLabError:
            return {"evidence_state": EvidenceState.MISSING.value,
                    "reason": "traffic_revision_not_found"}
        buckets = self.service.list_metric_buckets(
            tenant_id, listing_revision_id=revision_id
        )
        if not buckets:
            return {
                "revision_id": revision_id,
                "evidence_state": EvidenceState.MISSING.value,
                "reason": "traffic_metric_evidence_not_found",
                "source_provenance": None,
                "freshness": None,
                "data_as_of": None,
                "quality_gate": None,
            }
        # 最新 bucket 的 data_as_of 作为 revision 的数据时间；来源按 bucket 的 connector
        latest = buckets[0]
        source_type = _source_type_from_connector(latest.get("connector_id"))
        provenance = {
            "policy_version": "source-provenance-v1",
            "source_type": source_type,
            "virtual": source_type == "virtual",
            "connectors": (
                [
                    {
                        "connector_id": str(latest["connector_id"]),
                        "capability_version": None,
                        "virtual": source_type == "virtual",
                    }
                ]
                if latest.get("connector_id")
                else []
            ),
            "completeness": "complete",
            "basis": "traffic_metric_bucket",
        }
        # 证据审查 #11：provenance 过校验器后再输出（防下游 read_source_provenance
        # 抛 SourceProvenanceError）
        provenance = read_source_provenance(
            provenance, missing_basis="traffic_metric_bucket"
        )
        return {
            "revision_id": revision_id,
            "evidence_state": _evidence_state(source_type).value,
            "source_provenance": provenance,
            "freshness": _bucket_freshness(buckets, row),
            "data_as_of": latest.get("data_as_of"),
            "quality_gate": self._revision_quality_gate(
                tenant_id, row["store_id"], row["sku_id"], revision_id
            ),
            "bucket_count": len(buckets),
        }

    def latest_revision_view(
        self, tenant_id: str, *, store_id: str, sku_id: str,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        """SKU 最新 revision 的统一证据视图（门禁生产消费者入口）。

        无 revision → 显式 missing 视图（不抛，缺数据是合法状态）。
        item_id 参与过滤：同店同 SKU 不同 item 的 revision 不得串读
        （WP1 metrics 的 item 隔离延伸到 WP2/门禁链路）。
        """
        revisions = self.service.list_revisions(
            tenant_id, store_id=store_id, sku_id=sku_id, item_id=item_id, limit=1
        )
        if not revisions:
            return {
                "store_id": store_id,
                "sku_id": sku_id,
                "evidence_state": EvidenceState.MISSING.value,
                "reason": "traffic_revision_not_found",
                "source_provenance": None,
                "freshness": None,
                "data_as_of": None,
                "quality_gate": None,
            }
        return self.get_revision_view(tenant_id, str(revisions[0]["id"]))

    def _revision_quality_gate(
        self, tenant_id: str, store_id: str, sku_id: str, revision_id: str
    ) -> Any:
        """revision 的 quality_gate：引用它的最新 experiment analysis run 的 gate。

        无引用 → None（门禁拒绝，不编造——「无合格实验不编造」）。确定性：只读、
        复用 M5-R listing_traffic_insights 聚合，不重算统计。
        """
        insights = self.service.listing_traffic_insights(
            tenant_id, sku_id, store_id=store_id, limit=50
        )
        for insight in insights.get("insights", []):
            experiment = insight.get("experiment", {})
            if (
                str(experiment.get("control_revision_id")) == revision_id
                or str(experiment.get("treatment_revision_id")) == revision_id
            ):
                analysis = insight.get("analysis", {})
                evidence = analysis.get("evidence")
                if isinstance(evidence, Mapping):
                    return evidence.get("quality_gate")
        return None

    def get_experiment_view(self, tenant_id: str, experiment_id: str) -> dict[str, Any]:
        """experiment 统一视图：证据取最新 analysis run 的 evidence_json。"""
        try:
            row = self.service.get_experiment(tenant_id, experiment_id)
        except TrafficLabError:
            return {"evidence_state": EvidenceState.MISSING.value,
                    "reason": "traffic_experiment_not_found"}
        runs = self.service.list_analysis_runs(tenant_id, experiment_id, limit=1)
        if not runs:
            return {
                "experiment_id": experiment_id,
                "evidence_state": EvidenceState.MISSING.value,
                "reason": "traffic_analysis_evidence_not_found",
                "source_provenance": None,
                "freshness": None,
                "data_as_of": None,
                "status": row.get("status"),
            }
        run = runs[0]
        evidence = run["evidence"] if isinstance(run.get("evidence"), Mapping) else {}
        provenance = _provenance_from(evidence)
        # B2 修正：freshness 用 analysis_input_freshness（基于 input_snapshot 与当前库
        # 比对），而非 evidence 顶层（不存在）；快照缺失 → stale（fail-closed）。
        freshness = analysis_input_freshness(
            self.service.db,
            tenant_id,
            experiment_id,
            dict(evidence),
            analysis_run_id=str(run.get("analysis_run_id")),
        )
        return {
            "experiment_id": experiment_id,
            "evidence_state": _evidence_state(provenance.get("source_type")).value,
            "source_provenance": provenance,
            "freshness": freshness,
            "data_as_of": evidence.get("data_as_of"),
            "status": row.get("status"),
            "quality_gate": evidence.get("quality_gate"),
        }

    def list_analysis_runs_view(
        self, tenant_id: str, experiment_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """experiment 的分析运行列表（统计事实桥接，不重算）。"""
        rows = self.service.list_analysis_runs(tenant_id, experiment_id, limit=limit)
        views: list[dict[str, Any]] = []
        for row in rows:
            evidence = row["evidence"] if isinstance(row.get("evidence"), Mapping) else {}
            provenance = _provenance_from(evidence)
            quality_gate = evidence.get("quality_gate")
            # B2 修正：freshness 用 analysis_input_freshness，而非 evidence 顶层（不存在）
            freshness = analysis_input_freshness(
                self.service.db,
                tenant_id,
                experiment_id,
                dict(evidence),
                analysis_run_id=str(row.get("analysis_run_id")),
            )
            views.append(
                {
                    "analysis_run_id": row.get("analysis_run_id"),
                    "evidence_state": _evidence_state(
                        provenance.get("source_type")
                    ).value,
                    "source_provenance": provenance,
                    "statistical_facts": {
                        "effect_estimate": row.get("effect_estimate"),
                        "confidence_interval": row.get("confidence_interval"),
                        "sample_size": row.get("sample_size"),
                        "quality_gate": quality_gate,
                    },
                    "freshness": freshness,
                }
            )
        return views

    def run_gates(
        self,
        view: Mapping[str, Any],
        model_output: Mapping[str, Any] | None = None,
    ) -> tuple[bool, list[GateResult]]:
        """确定性门禁组合：evidence + freshness + quality_gate（证据三关）。

        model_output 可选：提供时追加越权输出 Gate（模型不得改 effect/区间/样本量/Gate）。
        越权检查作用于模型输出而非证据视图（视图含 quality_gate/effect_estimate 等合法键）。
        """
        engine = GateEngine()
        all_passed, results = engine.run_all(view)
        if model_output is not None:
            forbidden_result = engine.check_no_forbidden_output(model_output)
            results.append(forbidden_result)
            all_passed = all_passed and forbidden_result.passed
        return all_passed, results


def _bucket_freshness(
    buckets: list[dict[str, Any]], revision: Mapping[str, Any]
) -> dict[str, Any]:
    """确定性 freshness：bucket 在 revision 窗口内且至少 1 桶 → current，否则 stale。

    不依赖墙钟：窗口由 revision 的 active_from/active_to 决定。
    """
    active_from = revision.get("active_from")
    active_to = revision.get("active_to")
    if not buckets:
        return {"status": "stale", "usable_as_current": False,
                "reason_codes": ["traffic_metric_evidence_not_found"]}
    if active_from is None:
        return {"status": "stale", "usable_as_current": False,
                "reason_codes": ["revision_window_missing"]}
    out_of_window = []
    for bucket in buckets:
        start = bucket.get("metric_start")
        if start is None:
            out_of_window.append(bucket.get("id") or "?")
        elif active_to is not None and start > active_to:
            out_of_window.append(bucket.get("id") or "?")
    if out_of_window:
        return {"status": "stale", "usable_as_current": False,
                "reason_codes": [f"metric_bucket_out_of_window:{out_of_window[0]}"]}
    return {"status": "current", "usable_as_current": True, "reason_codes": []}


__all__ = [
    "EvidenceBridge",
]
