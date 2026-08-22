"""M9-R WP4 商品经营工作台包。

公开 API 边界：
- 页面：WorkbenchPages（SKU 下钻数据组装，扩展现有 /admin）
- 机制 Eval：MechanismEvalRunner / EvalResult（复用冻结场景 + 确定性诊断）
- 冻结场景：FrozenScene / FROZEN_SCENES
- 边界说明：BOUNDARY_NOTES / STATE_BADGES / DEMO_LABEL / state_badge
"""
from .boundaries import BOUNDARY_NOTES, DEMO_LABEL, STATE_BADGES, state_badge
from .eval import EvalResult, MechanismEvalRunner
from .pages import WorkbenchPages
from .scenes import FROZEN_SCENES, FrozenScene

__all__ = [
    "BOUNDARY_NOTES",
    "DEMO_LABEL",
    "EvalResult",
    "FROZEN_SCENES",
    "FrozenScene",
    "MechanismEvalRunner",
    "STATE_BADGES",
    "WorkbenchPages",
    "state_badge",
]
