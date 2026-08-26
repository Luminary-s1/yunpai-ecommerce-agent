"""M9-R WP2 Demo 隔离测试：Demo 数据不进入默认经营视图。

对齐验收标准：条目 3（真实/Demo 查询范围物理隔离）、条目 12（Demo 隔离不进入默认视图）。

WP5 验收修正：桥接从真实持久化位置取证据（metric buckets 的 connector_id），
证据状态映射（virtual→demo / operational→actual / 缺失→missing）由确定性函数
_evidence_state / _source_type_from_connector 给出。operational 查询层据此过滤 demo。
"""
from __future__ import annotations

from ecommerce_agent.product_diagnosis.bridge import (
    _evidence_state,
    _source_type_from_connector,
)
from ecommerce_agent.readonly_data.contracts import EvidenceState


def test_virtual_connector_maps_to_demo() -> None:
    """virtual_taobao connector → demo（Demo 隔离：不进 operational 默认视图）。"""
    assert _source_type_from_connector("virtual_taobao") == "virtual"
    assert _evidence_state("virtual") is EvidenceState.DEMO


def test_operational_connector_maps_to_actual() -> None:
    """真实 connector（非 virtual_*）→ actual（可进 operational）。"""
    assert _source_type_from_connector("taobao_official") == "operational"
    assert _evidence_state("operational") is EvidenceState.ACTUAL


def test_unknown_or_missing_source_is_missing() -> None:
    """未知/缺失来源 → missing（不冒充真实，不静默）。"""
    assert _evidence_state(None) is EvidenceState.MISSING
    assert _evidence_state("unknown") is EvidenceState.MISSING
    assert _evidence_state("mixed") is EvidenceState.MISSING


def test_none_connector_is_unknown() -> None:
    """无 connector → source_type None → missing。"""
    assert _source_type_from_connector(None) is None
    assert _evidence_state(None) is EvidenceState.MISSING
