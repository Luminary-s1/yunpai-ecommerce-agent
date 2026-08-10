# 项目进度记录规则

> 负责人工作台网页是执行状态的唯一来源。本文件不再保存任务状态、百分比、工时、
> 日期、人员动态、日报、周报、分支或 PR 进展。

## 当前任务

- 本文件只提供任务入口，不复制实时状态。M4、M5-R、M6-R 的任务书统一位于 `docs/tasks/`，负责人、排期和执行状态在负责人工作台网页维护。
- 本地虚拟展示数据的能力范围以 `PROJECT_FEATURES.md` 的 F-121 / F-310 为准，验收结果以 `PROJECT_ACCEPTANCE.md` 的 E-20260810-002 为准。

## 阻塞项

- 实时阻塞项只在负责人工作台网页维护；生产放行的可复核 Gate 以 `PROJECT_ACCEPTANCE.md` 的 `G-PROD-001` 为准。
- 显式 virtual 数据通过不解除真实平台资质、真实授权数据、真实渠道收发、长稳和生产安全验收阻塞。

## 本地项目文件职责

- `PROJECT_OVERVIEW.md`：稳定的项目边界和架构决策。
- `PROJECT_FEATURES.md`：功能编号、能力范围和代码事实。
- `PROJECT_VERSIONS.md`：已发布版本与兼容性事实。
- `PROJECT_ACCEPTANCE.md`：已经形成的验收证据与结论。
- `PROJECT_PROGRESS.md`：只说明进度记录规则，不复制网页数据。

当前任务书位于 `docs/tasks/`：M4、M5-R、M6-R 的全部工作包统一由闫睿涵负责。
任务状态、阻塞、工时和排期请在负责人工作台网页查看和维护。

网页统一跟踪前的历史快照保存在
[`archive/PROJECT_PROGRESS_20260807.md`](archive/PROJECT_PROGRESS_20260807.md)，只读保留，
不得作为当前状态依据。

## 进度历史

- 实时进度历史由负责人工作台网页维护；本文件不再追加日期、百分比、工时、分支或 PR 快照。
- 网页统一跟踪前的历史只读快照见 `archive/PROJECT_PROGRESS_20260807.md`；功能变更历史见 `PROJECT_FEATURES.md`，已验证交付历史见 `PROJECT_ACCEPTANCE.md`。
