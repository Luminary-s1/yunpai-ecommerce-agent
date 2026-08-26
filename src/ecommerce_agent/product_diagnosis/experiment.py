"""M9-R WP2 受控实验双路径封装。

边界声明：
- Demo 路径：run_demo_experiment(...) 走 VirtualStoreSimulation（隔离虚拟数据）。
- 真实路径：create_real_experiment(...) 走 TrafficLabService（**本阶段预留 + blocked**）。
- 两个公开方法分别代表 Demo 与真实路径，不自动选择。
- 副作用：Demo 路径内部会运行 VirtualStoreSimulation（隔离），真实路径当前不写库。
- 失败暴露：Demo 未用 confirm_virtual → 构造即抛；真实路径未开通 → 抛明确错误。

复用边界：Demo 走 simulation.py（VirtualStoreSimulation 已核实有 confirm_virtual 强制
隔离）；真实走 TrafficLabService.create_experiment（已核实 TrafficExperimentCreate
无 scope/demo 字段 → 真实实验绝不能用于 Demo，防污染真实实验表）。
"""
from __future__ import annotations

from typing import Any

from ecommerce_agent.simulation import VirtualStoreSimulation


class ExperimentNotAvailableError(RuntimeError):
    """真实实验路径未开通时抛出（blocked 语义）。"""


class ExperimentGateway:
    """受控实验入口（双路径强制显式声明）。

    用法：
      gateway.run_demo_experiment(...)          # Demo 路径（本阶段唯一可用）
      gateway.create_real_experiment(...)       # 真实路径（预留，抛 NotAvailable）
    """

    def __init__(self, simulation: VirtualStoreSimulation | None = None) -> None:
        self.simulation = simulation

    def run_demo_experiment(
        self,
        *,
        tenant_id: str,
        actor: str,
        confirm_virtual: bool = False,
        include_customer_service: bool = True,
    ) -> dict[str, Any]:
        """Demo 实验：走 VirtualStoreSimulation.run（已核实签名），强制 confirm_virtual。

        失败暴露：confirm_virtual 非 True → 抛 ValueError（防 Demo 冒充真实）。
        """
        if not confirm_virtual:
            raise ValueError("demo_experiment_requires_confirm_virtual")
        if self.simulation is None:
            raise ValueError("simulation_service_not_configured")
        # 走 VirtualStoreSimulation.run（隔离虚拟数据 + source_type="virtual"）
        return self.simulation.run(
            tenant_id=tenant_id,
            actor=actor,
            include_customer_service=include_customer_service,
        )

    def create_real_experiment(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """真实实验路径：本阶段预留，未开通 → 抛 ExperimentNotAvailableError。

        解锁条件：真实 SKU 级流量 + revision 窗口证据齐备后，此处接入
        TrafficLabService.create_experiment（需显式传真实 revision/窗口）。
        """
        raise ExperimentNotAvailableError(
            "real_experiment_not_available: requires real sku_traffic + revision_window"
        )


__all__ = [
    "ExperimentGateway",
    "ExperimentNotAvailableError",
]
