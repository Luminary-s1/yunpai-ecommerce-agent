"""M9-R WP4 边界说明文字 + 试算字样规范。

边界声明：
- 页面展示给运营的边界说明（B1/B2/B4），不只代码侧验证。
- 四态徽标颜色规范 + demo「试算」字样。
- 副作用：零——纯常量/文案。
"""
from __future__ import annotations


# 硬边界页面说明（B1/B2/B4 用页面文字标注给运营看）
BOUNDARY_NOTES: dict[str, str] = {
    "B1": "本系统默认不改标题/主图。仅当证据表明标题错误或关键词陈旧时，才会给出「人工复核」建议。",
    "B2": "所有建议均为 draft 状态，需人工批准后生效。批准不触发任何平台动作。",
    "B4": "本模块为零平台写动作模块——仅输出建议，不自动改价/换图/报名/调广告。",
}

# 四态徽标规范（evidence_state → 颜色 + 中文标签）
# 对齐显示原则：每个数字带四态标识（真实数据/人工配置/演示参数/缺失）
STATE_BADGES: dict[str, dict[str, str]] = {
    "actual": {"label": "真实数据", "color": "green"},
    "manual": {"label": "人工配置", "color": "blue"},
    "demo": {"label": "演示参数", "color": "orange"},
    "missing": {"label": "缺失", "color": "gray"},
}

# demo 数据必须渲染「试算」字样（对齐 PDF 显示原则）
DEMO_LABEL: str = "试算"


def state_badge(evidence_state: str) -> dict[str, str]:
    """取状态徽标；未知状态 → 抛（不静默返回默认，防止漏标注）。"""
    if evidence_state not in STATE_BADGES:
        raise ValueError(f"unknown_evidence_state_badge:{evidence_state}")
    return STATE_BADGES[evidence_state]


__all__ = [
    "BOUNDARY_NOTES",
    "DEMO_LABEL",
    "STATE_BADGES",
    "state_badge",
]
