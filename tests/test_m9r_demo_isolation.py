"""M9-R WP2 Demo 隔离测试：Demo 数据不进入默认经营视图。

对齐验收标准：条目 3（真实/Demo 查询范围物理隔离）、条目 12（Demo 隔离不进入默认视图）。
"""
from __future__ import annotations

from ecommerce_agent.product_diagnosis.bridge import EvidenceBridge


def test_demo_view_does_not_enter_operational_scope() -> None:
    """Demo 数据（virtual→demo）不应出现在 operational 默认视图。

    确定性：bridge 的 get_revision_view 对 virtual 源返回 evidence_state="demo"，
    operational 查询层应过滤 demo（本测试锁 bridge 侧标记正确）。
    """
    bridge = EvidenceBridge(service=None)  # type: ignore[arg-type]

    class _FakeService:
        def get_revision(self, tenant_id, revision_id):
            return {
                "evidence_json": {
                    "source_provenance": {
                        "source_type": "virtual", "virtual": True,
                    }
                },
                "freshness": {"usable_as_current": True},
            }

    bridge.service = _FakeService()  # type: ignore[assignment]
    view = bridge.get_revision_view("t1", "rev-demo")
    # Demo 源必须被标记为 demo（operational 查询层据此过滤）
    assert view["evidence_state"] == "demo"


def test_real_view_marks_actual() -> None:
    """真实源（source_type=actual）→ evidence_state=actual，可进 operational。"""
    bridge = EvidenceBridge(service=None)  # type: ignore[arg-type]

    class _FakeService:
        def get_revision(self, tenant_id, revision_id):
            return {
                "evidence_json": {
                    "source_provenance": {
                        "source_type": "actual", "virtual": False,
                    }
                },
                "freshness": {"usable_as_current": True},
            }

    bridge.service = _FakeService()  # type: ignore[assignment]
    view = bridge.get_revision_view("t1", "rev-real")
    assert view["evidence_state"] == "actual"
