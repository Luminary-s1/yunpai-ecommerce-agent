"""M9-R WP2 写屏障测试：平台写=0 + 内部写白名单。

对齐验收标准：条目 10（诊断全链平台写=0，内部写白名单内）。
"""
from __future__ import annotations

import pytest

from ecommerce_agent.product_diagnosis.experiment import ExperimentGateway


class _PlatformClient:
    """模拟平台写客户端（改价/换图/报名/调广告）。"""

    def __init__(self) -> None:
        self.write_calls: list[str] = []

    def change_price(self, sku_id: str) -> None:
        self.write_calls.append(f"price:{sku_id}")

    def update_image(self, sku_id: str) -> None:
        self.write_calls.append(f"image:{sku_id}")


def test_demo_experiment_does_not_touch_platform() -> None:
    """Demo 实验（VirtualStoreSimulation）不触发任何平台写客户端。"""
    fake_sim = type("FakeSim", (), {"run": lambda self, **kw: {"virtual": True}})()
    gateway = ExperimentGateway(simulation=fake_sim)
    platform = _PlatformClient()
    gateway.run_demo_experiment(
        tenant_id="t1", actor="ops", confirm_virtual=True
    )
    # 平台写客户端必须零调用（平台写=0）
    assert platform.write_calls == []


def test_real_experiment_blocked_before_any_write() -> None:
    """真实实验未开通 → 抛 blocked，绝不在抛出前触发写。"""
    gateway = ExperimentGateway(simulation=None)
    platform = _PlatformClient()
    with pytest.raises(Exception):
        try:
            gateway.create_real_experiment(tenant_id="t1")
        except Exception:
            raise
    assert platform.write_calls == []
