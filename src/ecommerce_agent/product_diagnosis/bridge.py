"""M9-R WP2 M5-R 证据桥接层：统一只读查询。

边界声明：
- 输入：tenant_id + 查询参数（experiment_id / revision_id / sku_id 等）。
- 输出：统一只读证据视图 dict（含 evidence_state / granularity / data_as_of /
  source_provenance / freshness 结构）。
- 副作用：零——纯只读，调用 M5-R TrafficLabService 读接口，不写库、不网络写。
- 失败暴露：M5-R 未找到 → 抛 TrafficLabError（透传）；无证据 → 返回显式 missing 视图。
- 确定性：freshness 判定用 evidence-freshness-v1 结构（usable_as_current），
  不依赖时间源；provenance 读闫哥确认的 4 个持久化位置。

复用边界：本层只做「读 + 组装视图」，不重写统计（统计在 TrafficAnalysisEngine）。
"""
from __future__ import annotations

from typing import Any, Mapping

from ecommerce_agent.traffic_lab.service import TrafficLabError, TrafficLabService
from ecommerce_agent.readonly_data.contracts import EvidenceState


def _provenance_from(row: Mapping[str, Any], path: tuple[str, ...]) -> dict[str, Any] | None:
    """按持久化位置读取 source_provenance（确定性：路径不存在 → None）。"""
    value: Any = row
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value if isinstance(value, Mapping) else None


# 闫哥 8/18 确认的 4 个持久化位置
PROVENANCE_PATHS: dict[str, tuple[str, ...]] = {
    "traffic": ("evidence_json", "source_provenance"),
    "demand": ("lineage_json", "source_provenance"),
    "forecast": ("candidate_models_json", "source_provenance"),
    "plan": ("forecast_evidence_json", "source_provenance"),
}


def _evidence_state(source_type: str | None) -> EvidenceState:
    """provenance.source_type → evidence_state（virtual→demo，否则按 source_kind）。"""
    if source_type == "virtual":
        return EvidenceState.DEMO
    if source_type is None:
        return EvidenceState.MISSING
    return EvidenceState.ACTUAL


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
        """revision 统一视图（含 freshness + provenance 结构，尽力提取缺失标 missing）。"""
        try:
            row = self.service.get_revision(tenant_id, revision_id)
        except TrafficLabError:
            return {"evidence_state": EvidenceState.MISSING.value,
                    "reason": "traffic_revision_not_found"}
        provenance = _provenance_from(row, PROVENANCE_PATHS["traffic"])
        return {
            "revision_id": revision_id,
            "evidence_state": _evidence_state(
                provenance.get("source_type") if provenance else None).value,
            "source_provenance": provenance,
            "freshness": row.get("freshness"),
            "data_as_of": row.get("data_as_of"),
        }

    def get_experiment_view(self, tenant_id: str, experiment_id: str) -> dict[str, Any]:
        """experiment 统一视图。"""
        try:
            row = self.service.get_experiment(tenant_id, experiment_id)
        except TrafficLabError:
            return {"evidence_state": EvidenceState.MISSING.value,
                    "reason": "traffic_experiment_not_found"}
        provenance = _provenance_from(row, PROVENANCE_PATHS["traffic"])
        return {
            "experiment_id": experiment_id,
            "evidence_state": _evidence_state(
                provenance.get("source_type") if provenance else None).value,
            "source_provenance": provenance,
            "freshness": row.get("freshness"),
            "status": row.get("status"),
            "data_as_of": row.get("data_as_of"),
        }

    def list_analysis_runs_view(
        self, tenant_id: str, experiment_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """experiment 的分析运行列表（统计事实桥接，不重算）。"""
        rows = self.service.list_analysis_runs(tenant_id, experiment_id, limit=limit)
        return [
            {
                "analysis_run_id": row.get("analysis_run_id"),
                "evidence_state": EvidenceState.ACTUAL.value,
                "statistical_facts": row.get("statistical_facts") or row,
                "freshness": row.get("freshness"),
            }
            for row in rows
        ]


__all__ = [
    "EvidenceBridge",
    "PROVENANCE_PATHS",
]
