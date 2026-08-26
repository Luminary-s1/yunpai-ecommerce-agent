"""M9-R WP2 受控实验双路径测试。

对齐验收标准：条目 11（受控实验入口 Demo 路径）、条目 9（真实缺 SKU 流量 → blocked）。
"""
from __future__ import annotations

import pytest

from ecommerce_agent.product_diagnosis.experiment import (
    ExperimentGateway,
    ExperimentNotAvailableError,
)


class _FakeSimulation:
    """假 VirtualStoreSimulation：run 记录被调用。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, *, tenant_id: str, actor: str, include_customer_service: bool = True):
        self.calls.append({"tenant_id": tenant_id, "actor": actor})
        return {"virtual": True, "run_id": "sim-1"}


def test_demo_experiment_requires_confirm_virtual() -> None:
    """Demo 路径强制 confirm_virtual=True（防冒充真实）。"""
    gateway = ExperimentGateway(simulation=_FakeSimulation())
    with pytest.raises(ValueError, match="confirm_virtual"):
        gateway.run_demo_experiment(tenant_id="t1", actor="ops")


def test_demo_experiment_runs_with_confirm() -> None:
    """confirm_virtual=True → 走 VirtualStoreSimulation.run。"""
    fake = _FakeSimulation()
    gateway = ExperimentGateway(simulation=fake)
    result = gateway.run_demo_experiment(
        tenant_id="t1", actor="ops", confirm_virtual=True
    )
    assert result["virtual"] is True
    assert fake.calls == [{"tenant_id": "t1", "actor": "ops"}]


def test_demo_experiment_requires_simulation_service() -> None:
    gateway = ExperimentGateway(simulation=None)
    with pytest.raises(ValueError, match="simulation_service_not_configured"):
        gateway.run_demo_experiment(tenant_id="t1", actor="ops", confirm_virtual=True)


def test_real_experiment_is_blocked() -> None:
    """真实路径本阶段不开通 → 抛 ExperimentNotAvailableError（blocked 语义）。"""
    gateway = ExperimentGateway(simulation=None)
    with pytest.raises(ExperimentNotAvailableError):
        gateway.create_real_experiment(tenant_id="t1")
