# 云湃经营 Agent 路线重置（2026-08-07）

> 文档性质：路线决策、产品边界和任务书入口。
> 实施进度、工时、状态与日期仅在负责人工作台网页记录。

## 1. 决策摘要

从 2026-08-07 起，项目按以下口径推进：

| 里程碑 | 定位 | 处理方式 |
|---|---|---|
| M4 智能客服 | 现有客服基线 | 保持现有能力与验收基线，后续仅处理缺陷和生产 Gate |
| 旧 M5 运营辅助与文案生成 | 历史路线 | 保留代码、API、schema v25 和历史资料，不再补旧 checklist |
| 旧 M6 竞品分析 | 历史路线 | 保留代码、API、schema v14/v17/v26 和历史资料，不再补旧 checklist |
| M5-R 商品流量实验与推流机制分析 | 当前任务书 | 业务代码统一使用 `traffic_lab`，建设可追溯的受控实验与黑盒统计分析 |
| M6-R 需求预测与智能补货 | 当前任务书 | 业务代码统一使用 `forecasting`，从订单事实生成需求时序和补货建议 |

冻结不等于删除或弃用。旧 M5/M6 的数据库迁移、模块注册、API、测试和后台页面必须继续兼容；
除非另有缺陷修复任务，不做重命名、迁移或清理。

新的产品结构为：

```text
商品 / 订单 / 库存事实
          │
   ┌──────┴──────┐
   │             │
Traffic Lab   Forecasting
标题/主图实验   SKU 需求预测
   │             │
流量机制假设    库存与补货计划
   └──────┬──────┘
          │
      Agent + Admin
```

## 2. 为什么重置

仓库已有商品、订单、库存、营销指标、Connector、SQLite 版本与审计底座，但两条新主线的
关键事实仍缺失：

- M5-R 缺少“标题/主图版本 × 生效时间窗 × SKU 流量结果”的可追溯关系。现有营销指标是
  campaign-day 粒度，不能表示某个 listing revision 的表现。
- M6-R 已能从订单行取得 `sku_id`、`quantity`，也有订单时间和库存事实，但尚未形成
  SKU 日需求时序；现有 `average_daily_sales` 是静态输入，不是预测。
- Traffic Lab 的瓶颈是实验数据，不是 LLM；Forecasting 的第一步是需求口径和时间序列
  回测，不是复杂模型。

因此两条新主线继续沿用项目既有原则：模型负责语义分析和解释，代码负责事实、计算、
权限、门禁和成功判定。

## 3. 不可破坏的产品边界

1. 旧 M5/M6 冻结但不删除，历史 migration 永久保留。
2. M5-R 的核心是实验系统，不是自动文案发布或“破解平台算法”。
3. 推流机制结论必须表述为可复核的黑盒统计假设，不宣称平台内部权重或确定因果。
4. 每个流量数据点必须绑定准确的标题、主图和 listing revision。
5. M6-R 从现有订单行生成需求事实，不导入另一套“销量真相”覆盖订单事实。
6. 缺货期销量属于受截断需求，不能不加标记地当作真实需求训练。
7. 预测模型必须做 rolling-origin backtest；不优于简单 baseline 时自动回退。
8. 第一版只提供实验、标题/主图方向和补货建议，不自动发布商品、不自动下采购单。

## 4. 两条主线的职责

### 4.1 M5-R：`traffic_lab`

目标是在授权自有店铺数据上，通过标题、主图等单变量受控实验，识别与曝光分配、CTR、
CVR、GMV 及后续流量变化稳定相关的因素，形成可复验的推流机制假设。

系统必须拆开四类问题：

| 层 | 问题 | 主要指标 |
|---|---|---|
| 流量分配 | 平台是否多给或少给曝光 | impressions/hour、搜索曝光、推荐曝光 |
| 素材吸引 | 标题或图片是否带来点击 | CTR |
| 商业质量 | 点击以后是否产生有效行为 | 加购率、CVR、订单、GMV |
| 反馈 | 当前表现是否影响后续时窗流量 | 后续推荐曝光增量与 lag |

详细设计与工作包见 [M5R_TRAFFIC_LAB_WORKBENCH.md](tasks/M5R_TRAFFIC_LAB_WORKBENCH.md)。

### 4.2 M6-R：`forecasting`

目标是从订单明细生成 `store + SKU + business_date` 需求事实，预测未来 7/14/30 天需求，
再结合当前库存、在途、补货周期、复核周期和服务水平给出确定性补货建议。

V1 只预测店铺级 SKU 总需求。现有订单行没有可靠的履约仓库字段，不能伪造仓库级历史
需求；多仓库存先汇总判断风险，仓间分配暂按规则处理。

详细设计与工作包见
[M6R_DEMAND_FORECAST_WORKBENCH.md](tasks/M6R_DEMAND_FORECAST_WORKBENCH.md)。

## 5. Schema 版本预留

当前 `main` 的 `Database.SCHEMA_VERSION` 为 v27。路线重置前，`CONTRIBUTING.md` 曾把
v28 预留给旧 M5 的 `ops_operation_records.sku_id`；截至 2026-08-07，对本地及已知远端
分支的 `_apply_vNN` 检查没有发现 `_apply_v28` 实现。旧 M5 冻结后，该旧用途取消。

| Schema | 新用途 | 状态 |
|---:|---|---|
| v28 | Traffic Lab：asset、listing revision、metric bucket、experiment、window、analysis run | 已预留，未实现 |
| v29 | Forecasting：demand fact、forecast policy/run/backtest/point/anomaly | 已预留，未实现 |
| v30 | Inventory planning：planning policy、inventory plan | 已预留，未实现 |
| v31+ | 后续真实需求 | 未分配，按开发时的协调表认领 |

本表是 `CONTRIBUTING.md`「Schema 版本号占用登记」的快照，**以 CONTRIBUTING 为单一
来源**（D-035）；两表不一致时以 CONTRIBUTING 为准并回改本表。

任何在外部未同步分支上继续使用旧 v28 用途的改动都不得直接合入；应先停止旧 M5 工作并
与模块负责人核对。迁移实现仍需遵守 `CONTRIBUTING.md` 的占号、命名、向前兼容和全量回归规则。

## 6. 实施顺序

两条主线并行，但不是“做完 M5-R 才做 M6-R”：

```text
Roadmap Reset
      │
      ├─ M5-R revision / metric 数据采集 ─ switchback ─ lag 分析
      │
      └─ M6-R demand facts ─ baseline/backtest ─ 补货计划
                                                │
                                      Agent + Admin + Eval
```

建议批次：

1. **设计冻结**：评审两份 workbench 的事实口径、表关系、API 和验收门禁。
2. **并行数据层**：M5-R WP1/WP2 与 M6-R WP1 同时开工，优先积累可用历史数据。
3. **确定性引擎**：M5-R 特征/实验分析与 M6-R baseline/backtest/champion。
4. **决策层**：M5-R 假设与下一轮实验建议；M6-R 区间预测与库存计划。
5. **对外入口**：只读 Agent tools、后台控制台和各自的 Eval。
6. **Shadow Mode**：真实授权数据只读运行，完成数据质量和生产边界验收后再讨论动作能力。

## 7. 设计冻结 Gate

进入业务代码开发前必须同时满足：

- M5-R 的 revision 身份、窗口归属、metric bucket 时区和重复写入语义已冻结。
- M5-R 的实验方法、最小样本、washout 和“无证据时拒绝结论”规则已冻结。
- M6-R 的 `demand-v1` 订单状态口径、时区和回补策略已冻结。
- M6-R 的 stockout/censored demand 标记来源已确认；无法可靠判断时显式未知。
- v28/v29/v30 的表名、外键、索引、租户隔离和迁移拆分已评审。
- 两条主线的 synthetic/virtual Eval ground truth 与分析模块物理隔离。
- 第一批 GitHub Issues 已按工作包拆分，每个 Issue 有范围、依赖、验收和非目标。

设计冻结后，只同步稳定的项目边界、功能与版本事实，不在 `.project-to-act` 或任务书中
复制网页进度。未实现能力不得登记为 available，也不得提前修改项目版本号。

## 8. 本次文档影响面

- 新增本路线重置文档。
- 新增 M5-R、M6-R 工作台账。
- 将旧 M5/M6 workbench 和 handoff 移入 `docs/tasks/archive/`，并标记
  `FROZEN / SUPERSEDED`。
- 将旧 M6 专属设计稿移入 `docs/superpowers/specs/archive/`，将旧 M5 专属交付证据移入
  `docs/works/archive/`；跨版本测试报告继续保留在原位置。
- `docs/tasks/PROGRESS.md` 只保留网页单一进度源规则；旧快照移入 `docs/tasks/archive/`。
- 更新 `CONTRIBUTING.md` 的任务索引与 schema 预留。
- 不改业务代码、数据库、API、依赖、功能状态或版本号。
