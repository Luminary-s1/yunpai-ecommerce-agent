# 任务进度记录规则

> 负责人工作台网页是任务状态与执行进度的唯一来源。本文件不再保存任何本地进度快照。

## 网页记录的内容

以下信息只在负责人工作台网页维护，不写入 Git 文档：

- 任务状态、完成比例和阻塞原因；
- 预计工时、剩余工时、每日投入与起止日期；
- 优先级、派发记录、日报、周报和人员执行动态；
- 分支、提交、PR 或部署的实时状态。

这样可以避免多人在各自分支修改同一组数字、日期或勾选框而产生无意义冲突。

## 本地任务书保留的内容

Git 中的活跃任务书只保存稳定信息：模块目标、范围与非目标、业务规则、数据模型、
接口约束、工作包、依赖、验收标准、交付物和负责人。

| 模块 | 任务书 | 统一负责人 |
|---|---|---|
| M4 智能客服 | [M4_WORKBENCH.md](M4_WORKBENCH.md) | 闫睿涵 |
| M5-R Traffic Lab | [M5R_TRAFFIC_LAB_WORKBENCH.md](M5R_TRAFFIC_LAB_WORKBENCH.md) | 闫睿涵 |
| M6-R Demand Forecast | [M6R_DEMAND_FORECAST_WORKBENCH.md](M6R_DEMAND_FORECAST_WORKBENCH.md) | 闫睿涵 |
| M7-R 只读经营数据与 Demo 事实底座 | [M7R_READONLY_DATA_WORKBENCH.md](M7R_READONLY_DATA_WORKBENCH.md) | 闫睿涵（收口） |
| M8-R 销售与售后客服闭环 | [M8R_CUSTOMER_SERVICE_LOOP_WORKBENCH.md](M8R_CUSTOMER_SERVICE_LOOP_WORKBENCH.md) | 闫睿涵（收口） |
| M9-R 商品流量与生命周期经营 | [M9R_PRODUCT_TRAFFIC_LIFECYCLE_WORKBENCH.md](M9R_PRODUCT_TRAFFIC_LIFECYCLE_WORKBENCH.md) | 闫睿涵（收口） |
| M10-R 预测补货、订购单与经营决策 | [M10R_OPERATING_DECISION_WORKBENCH.md](M10R_OPERATING_DECISION_WORKBENCH.md) | 闫睿涵（收口） |

M7-R～M10-R 的工作包开发人与独立验收人以各 WORKBENCH 分工表为准。任务书中的验收
条目是规范，不是完成勾选项。实际执行状态以网页为准，不回写到本文档。

## 历史资料

过时的 M4/M5/M6 交接、旧工作台、平台工时明细等资料位于
[archive/](archive/README.md)。`docs/tasks_intro/` 中的个人计划和待办也只保留在本地
归档目录，不再创建新的 DAILY PLAN。

历史归档只用于解释旧决策，不作为当前分工、排期或完成状态依据。
