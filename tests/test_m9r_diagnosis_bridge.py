"""M9-R WP2 桥接层测试：EvidenceBridge 统一只读证据查询。

对齐验收标准：条目 1（桥接 revision/experiment/analysis）、条目 2（freshness/provenance）。
"""
from __future__ import annotations

import pytest

from ecommerce_agent.product_diagnosis.bridge import PROVENANCE_PATHS, EvidenceBridge
from ecommerce_agent.traffic_lab.service import TrafficLabService


def _bridge_with_views() -> tuple[EvidenceBridge, dict[str, dict]]:
    """构造一个用假视图的 bridge（不连真 DB——测桥接逻辑本身）。"""
    bridge = EvidenceBridge(service=None)  # type: ignore[arg-type]  # 测试用假 service

    class _FakeService:
        def get_revision(self, tenant_id, revision_id):
            return {
                "revision_id": revision_id,
                "evidence_json": {
                    "source_provenance": {
                        "source_type": "virtual", "virtual": True,
                        "completeness": "complete", "basis": "demo",
                    }
                },
                "freshness": {"usable_as_current": True},
                "data_as_of": "2026-08-17",
            }

        def get_experiment(self, tenant_id, experiment_id):
            return {
                "experiment_id": experiment_id,
                "evidence_json": {
                    "source_provenance": {
                        "source_type": "actual", "virtual": False,
                        "completeness": "complete", "basis": "report",
                    }
                },
                "freshness": {"usable_as_current": True},
                "status": "running",
                "data_as_of": "2026-08-17",
            }

        def list_analysis_runs(self, tenant_id, experiment_id, *, limit=100):
            return [{"analysis_run_id": "run-1", "statistical_facts": {"effect": 0.1}}]

    bridge.service = _FakeService()  # type: ignore[assignment]
    return bridge, {}


def test_provenance_paths_are_defined() -> None:
    """4 个持久化位置已定义（闫哥 8/18 确认）。"""
    assert PROVENANCE_PATHS["traffic"] == ("evidence_json", "source_provenance")
    assert PROVENANCE_PATHS["demand"] == ("lineage_json", "source_provenance")
    assert PROVENANCE_PATHS["forecast"] == ("candidate_models_json", "source_provenance")
    assert PROVENANCE_PATHS["plan"] == ("forecast_evidence_json", "source_provenance")


def test_revision_view_extracts_demo_provenance() -> None:
    bridge, _ = _bridge_with_views()
    view = bridge.get_revision_view("t1", "rev-1")
    assert view["evidence_state"] == "demo"  # virtual → demo
    assert view["source_provenance"]["source_type"] == "virtual"
    assert view["freshness"]["usable_as_current"] is True


def test_experiment_view_extracts_actual_provenance() -> None:
    bridge, _ = _bridge_with_views()
    view = bridge.get_experiment_view("t1", "exp-1")
    assert view["evidence_state"] == "actual"
    assert view["status"] == "running"


def test_analysis_runs_view_bridges_statistical_facts() -> None:
    bridge, _ = _bridge_with_views()
    runs = bridge.list_analysis_runs_view("t1", "exp-1")
    assert runs[0]["analysis_run_id"] == "run-1"
    assert runs[0]["evidence_state"] == "actual"
