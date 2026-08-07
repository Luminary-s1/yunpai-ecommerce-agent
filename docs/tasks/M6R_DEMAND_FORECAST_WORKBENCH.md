# M6-R 需求预测与智能补货 — 任务书

> 代码域：`forecasting`。
> 文档性质：任务书；实施进度、工时与日期仅在负责人工作台网页记录。
> 全部工作包统一负责人：**闫睿涵**。
> 路线依据：[ROADMAP_RESET_20260807.md](../ROADMAP_RESET_20260807.md)。

## 1. 目标

从现有订单与订单行生成 `store + SKU + business_date` 日需求事实，预测未来 7/14/30 天
需求分布，再结合可售库存、在途、补货周期、复核周期和服务水平生成确定性库存计划。

```text
commerce_orders + commerce_order_lines
                 │
           Demand Fact Builder
                 │
         SKU 日需求时序（版本化）
                 │
       Forecast + Rolling Backtest
                 │
      P50 / P80 / P95 需求预测
                 │
          Inventory Planning
                 │
     缺货日期 / 风险 / 建议补货量
```

目标不是“必须使用 AI 模型”，而是预测决策不能比可靠的简单 baseline 更差。

对应原始诉求的映射：「历史销售数据」= 订单行重建的日需求事实（§3、§4.1）；
「当前仓库数量」= 库存快照聚合的 `available = on_hand - reserved`（V1 多仓汇总，§7）；
「预测未来卖货数量」= 未来 7/14/30 天逐日 P50/P80/P95 需求（§4.5、§7）。
**WP1 + WP2 构成对应原始诉求的最小可交付**；WP3 库存计划是其自然延伸，可在预测
链路验收后再进入。

## 2. 范围与边界

### 范围内

- 从现有订单事实增量/全量重建 SKU 日需求。
- 版本化需求口径、时区、水位与数据哈希。
- 缺货/受截断需求标记和训练 Gate。
- 纯 Python baseline、间歇性需求模型、rolling-origin backtest 和 champion 选择。
- 7/14/30 天逐日 P50/P80/P95 预测。
- 结合库存与持久补货策略生成计划、风险和解释。
- 只读 Agent tools、后台、synthetic Eval 和 shadow mode。

### 范围外

- V1 仓库级需求预测。现有订单行没有可靠履约仓库事实。
- 用外部 Excel 另造销量真相并覆盖订单事实。
- 在没有 SKU 级退款数量事实时宣称得到精确净销量。
- 第一版引入 LSTM/Transformer、强制新增科学计算依赖或自动采购。
- 随机切分时序 train/test，或把未来数据泄漏到特征与模型选择。

## 3. 需求口径 `demand-v1`

第一项实现任务不是模型，而是冻结“什么叫需求”。V1 主目标：

```text
fulfillable_demand_units
= 已支付且未取消订单行的 quantity
```

`DemandPolicy` 至少版本化：

```text
policy_version = demand-v1
timezone = Asia/Shanghai
included_payment_statuses = [...]
excluded_order_statuses = [...]
late_arrival_policy
rebuild_lookback_days
```

规则：

- 同时保留 gross/eligible 等口径，主预测目标显式命名，不使用含糊的 `sales`。
- after-sale 只有退款金额而没有可靠 SKU 件数时，不能推导精确退货件数。
- 取消、状态更正和迟到订单按固定回补窗口重算，产生新的 fact version/watermark。
- 所有 business date 先按店铺策略时区归日，不直接使用服务器本地日期。

## 4. 数据模型

§4.1–§4.6 属需求预测迁移（占号表当前为 v29），§4.7–§4.8 属库存计划迁移（当前为
v30）。Schema 版本号以 `CONTRIBUTING.md`「Schema 版本号占用登记」表为单一来源
（D-035），本文各小节不再重复标注版本号。

### 4.1 `demand_daily_facts`

```text
id
tenant_id
store_id
sku_id
business_date
gross_units
eligible_units
order_count
sales_amount
available_stock
stockout_flag
stockout_evidence
price
promotion_flag
source_watermark
fact_version
demand_policy_version
payload_hash
created_at
```

约束：

- 唯一事实键至少包含 tenant/store/SKU/date/policy/fact version。
- 同一 source watermark 重放幂等；订单更正通过新 fact version 可追溯更新。
- `stockout_flag` 支持 `true / false / unknown`，不能把未知静默当作未缺货。
- 无订单日需补零还是标缺失由 policy 固定；数据源中断不能被当作真实零需求。

### 4.2 `forecast_policies`

```text
policy_id
tenant_id
store_id
sku_id optional
horizons_json
minimum_history_days
candidate_models_json
backtest_windows
interval_levels_json
demand_policy_version
policy_version
active_from
created_at
```

店铺默认策略可被 SKU 策略覆盖；解析优先级由代码固定并返回最终 policy evidence。

### 4.3 `forecast_runs`

```text
run_id
tenant_id
store_id
sku_id
training_start
training_end
data_hash
demand_policy_version
forecast_policy_version
candidate_models_json
champion_model
champion_reason
model_version
wape
bias
smape
rmse
forecast_horizon
status
created_at
```

### 4.4 `forecast_backtests`

每个候选模型、每个 rolling origin 窗口都保存训练截止日、预测区间、actual、forecast、
误差和失败原因。模型选择必须能追溯到这些结果。

### 4.5 `forecast_points`

```text
point_id
tenant_id
run_id
forecast_date
p50
p80
p95
created_at
```

分位数必须满足 `P50 <= P80 <= P95`；非法输出使 run 失败，不能排序后伪装修复。

### 4.6 `forecast_anomalies`

记录数据缺口、异常峰值、持续偏差、模型失败、区间非法、冷启动和受截断需求过多等问题，
并包含 evidence 与处置状态。

### 4.7 `inventory_planning_policies`

```text
policy_id
tenant_id
store_id
sku_id
warehouse_id optional
supplier_lead_days
review_period_days
service_level
minimum_order_qty
order_multiple
minimum_safety_stock
maximum_stock_days
policy_version
active_from
created_at
```

V1 即使保存 `warehouse_id` 可选覆盖，也只把仓库库存作为 supply location；需求预测仍是
store + SKU 粒度。不得把同一店铺的总预测复制到每个仓库。

### 4.8 `inventory_plans`

保存 forecast run、policy version、库存快照、在途、reorder point、target stock、建议数量、
预计缺货日期、风险等级、舍入过程和 created_at。历史计划不可因库存更新而原地变化。

## 5. 缺货与受截断需求

当真实需求是 100、库存只有 10 时，观测销量 10 不是需求 10。训练必须根据证据：

- 排除明确缺货日；或
- 降权明确受截断日；或
- 使用经评审的缺失需求修正。

V1 先采用最保守的排除/降权策略，不声称恢复了精确潜在需求。若库存快照不足以判断缺货，
设置 `stockout_flag=unknown` 并降低预测质量；不得自动推断为 false。

## 6. Forecast Engine

### 6.1 候选模型

V1 以纯 Python 实现为默认，不修改核心依赖：

| 类型 | 模型 | 适用场景 |
|---|---|---|
| Baseline | Last Value | 最小回归检查 |
| Baseline | 7-day Seasonal Naive | 周周期明显 |
| Baseline | Rolling Mean | 稳定需求 |
| Model | Weighted Moving Average / EWMA | 近期趋势 |
| Model | Croston | 间歇性需求 |
| Model | TSB | 稀疏且需求发生概率变化 |

ETS/SARIMA、GBM 或深度预测只能作为以后单独评审的 optional dependency，不能污染核心安装。

### 6.2 需求类型与冷启动

| 历史情况 | V1 策略 |
|---|---|
| 极少 | `cold_start`，明确低置信度或拒绝长期预测 |
| 少量 | rolling mean / store baseline |
| 周周期可识别 | seasonal naive |
| 足够长 | 多模型 rolling backtest 选 champion |
| 间歇性 | Croston / TSB 与 baseline 对比 |

相似商品迁移需要 catalog 类目、价格带、品牌和属性的独立设计，不进入第一版关键路径。

### 6.3 Rolling-origin backtest

只允许按时间推进：

```text
Train ──────┬─ Test
Train ───────────┬─ Test
Train ─────────────────┬─ Test
```

禁止随机划分。每个 origin 的训练数据截止时间、预测 horizon 和实际值都需保存；特征、
异常处理和模型选择只能看到当时可用的数据。

### 6.4 指标与 champion

主指标：

```text
WAPE = sum(abs(actual - forecast)) / sum(actual)
Bias = sum(forecast - actual) / sum(actual)
```

辅以 sMAPE、RMSE；形成分位数后增加 Pinball Loss。零需求窗口的 WAPE 不可除零，按固定
policy 返回不可比并使用合适的辅助指标。

champion 规则：

- 所有新模型都与可用 baseline 比较。
- 新模型未达到预设改进阈值时，baseline 保持 champion。
- 全部候选失败时返回明确失败或保守 baseline，不能因为“AI 模型必须使用”而强选。
- 保存 champion reason、候选排名和每个窗口表现。

## 7. 预测输出与库存决策

预测至少返回未来逐日 `P50/P80/P95` 以及 horizon 合计，不只返回单个神奇数字。

确定性库存公式：

```text
available = on_hand - reserved
future_supply = available + inbound
reorder_point = target_quantile(lead_time demand)
target_stock = target_quantile(lead_time + review_period demand)
recommended_order_qty = max(0, target_stock - available - inbound)
```

之后依次应用：minimum safety stock、minimum order quantity、order multiple、maximum stock
days。每一步舍入和裁剪必须进入解释，不允许 AI 修改计算结果。

风险输出至少包括：预计进入 P50/P80/P95 缺货区间的日期、过量库存风险、当前库存快照时间、
forecast/backtest 指标和数据质量等级。

## 8. API 草案

| API | 作用 |
|---|---|
| `POST /v1/forecasting/demand/rebuild` | 重建或回补日需求事实 |
| `GET /v1/forecasting/demand` | 查询历史时序与质量标记 |
| `POST /v1/forecasting/runs` | 运行预测与模型选择 |
| `GET /v1/forecasting/runs/{id}` | 查看模型、回测和预测结果 |
| `GET /v1/forecasting/skus/{sku}/forecast` | 获取 SKU 最新有效预测 |
| `GET /v1/forecasting/skus/{sku}/backtest` | 查看候选模型历史表现 |
| `PUT /v1/forecasting/policies/{sku}` | 配置预测/补货策略 |
| `GET /v1/forecasting/skus/{sku}/inventory-plan` | 获取确定性补货建议 |
| `GET /v1/forecasting/risks` | 获取缺货/积压风险列表 |

第一版 Agent 只注册：

```text
get_demand_forecast
get_inventory_plan
```

两者均只读，不创建采购单、不调整库存、不覆盖供应链事实。

## 9. 工作包

### WP1 Demand Fact 数据层

负责人：**闫睿涵**。

交付：`demand-v1`、schema v29 的 daily facts、全量/增量 builder、水位、回补与质量标记。

验收：

- 同一订单重放不重复计数，取消/状态更正可追溯回补。
- 业务日期严格按策略时区归日，跨日边界有测试。
- 无订单、数据缺失和真实零需求可区分。
- 缺货明确、未缺货和未知三种状态可区分。
- 任何 forecast 输入都能追溯到订单事实、水位和 policy version。
- 从 `main` 当前最高已合并版本前向迁移与租户隔离通过。若 M5-R（v28）尚未合入，
  允许 v27 → v29 跳号迁移，不得为凑号实现空的 `_apply_v28`（跳号先例见 v24 → v26）。

依赖：现有 orders/catalog/inventory 公开领域服务，不直接绕过服务写表。

### WP2 Forecast Engine

负责人：**闫睿涵**。

交付：baseline、EWMA/WMA、Croston/TSB、需求类型识别、rolling backtest、champion 和区间预测。

验收：

- 时序切分没有未来泄漏，至少一个反证能证明泄漏门禁有效。
- 每个新模型都与 baseline 同窗比较，劣于 baseline 时自动回退。
- 平稳、趋势、周季节、间歇、大量零值和冷启动序列有确定结果。
- P50/P80/P95 单调且结果可复现。
- 失败模型不会阻断仍可用的候选，最终选择理由可解释。

依赖：WP1。

### WP3 Inventory Planning

负责人：**闫睿涵**。

交付：schema v30 planning policy/plan、库存聚合、lead time 分位需求、MOQ/倍数/上限计算。

验收：

- 同一 forecast、policy 和库存快照产生相同补货结果。
- `on_hand - reserved + inbound`、lead/review period 和分位数均有数值断言。
- MOQ、order multiple、minimum safety stock 和 maximum stock days 的应用顺序固定。
- 多仓不重复计算店铺总需求，V1 仓间分配边界在结果中明确。
- 不存在自动采购或付款动作。

依赖：WP1、WP2 和现有 inventory service。

### WP4 API / Agent / Admin

负责人：**闫睿涵**。

交付：forecasting API、两个只读 tools、预测/库存风险后台和完整解释链。

验收：

- 后台展示历史需求、预测区间、库存线、预计缺货日期、建议量和 backtest。
- Agent 回答引用 forecast run、库存快照、policy 和数据质量证据。
- Agent 工具经动态目录由模型选择，客服链路不为 forecasting 新增关键词路由（D-034）。
- 模型不可用时预测和补货仍完全可用；AI 只做解释。
- 冷启动、受截断过多或输入过期时显式降级，不伪报高置信度。
- 租户隔离、审计和 API 错误契约完整。

依赖：WP1–WP3。

### WP5 Forecast Eval

负责人：**闫睿涵**。

交付：`evals/forecasting/` synthetic fixtures、独立 ground truth、门禁报告和反证记录。

覆盖：

```text
平稳销量 / 上涨趋势 / 下降趋势 / 7 日季节性 / 间歇需求
大量零值 / 促销峰值 / 缺货截断 / 数据缺失 / 冷启动
```

验收：

- 所有序列使用 rolling-origin backtest。
- champion 从候选中按固定规则选出，结果不优于 baseline 时自动 fallback。
- 方向性 bias、WAPE 可比性和区间覆盖均有门禁。
- Eval ground truth 不进入生产 forecast 输入。
- 临时破坏关键算法后对应测试如期失败，恢复后复验通过。

依赖：WP1、WP2；库存决策场景需 WP3。

## 10. 实现纪律（执行者必读）

本任务书晚于 2026-08-07 的决策权边界与可演进性规范定稿，实现必须遵守
`CONTRIBUTING.md` 第 10、11 节（D-034、D-035），审计背景见
`docs/AUDIT_ROUTING_EVOLVABILITY_20260807.md`。落到本模块：

**决策权边界（D-034）**

- 需求重建、模型训练、backtest、champion 选择、库存公式全部是确定性计算，由代码
  固化——这正是 D-034 允许写死的一侧，本模块没有任何环节需要关键词/正则做语义判断。
- AI 的唯一角色是读取结构化结果做解释（趋势成因、风险陈述、建议理由）；不得让 AI
  改预测数字、champion 选择或补货量（§7 已定，此处升格为规范约束）。
- 冷启动、数据质量降级等状态判定按数值规则，不按文本关键词。

**测试与 mock 纪律**

- backtest/公式的数值断言属于「自身增量」，放开写；但不新增全局计数全等断言
  （场景总数、模块总数、拓扑快照），需要动既有全等断言时按第 11 节改下界/成员断言。
- mock 模型不得内置任何预测逻辑；「模型不得被调用」断言仅限 D-005 契约——本模块
  预测链路本来就不依赖 LLM，天然满足。
- Eval ground truth 与生产输入物理隔离（WP5 已定）；Eval 判定使用数值断言。

**Schema 与注册纪律（D-035）**

- 迁移按 `CONTRIBUTING.md` 第 9 节占号规则执行；v29 与 v30 拆成两个独立迁移，写
  `_apply_vNN` 前先全分支搜同名。
- 迁移加表时同步在 `_validate_schema` 的 required 清单加条目，并**确认没有制造重复
  字典键**（该函数曾因重复键静默吞掉 v25 校验）。
- 灾备 manifest 精确比对 schema 版本：v29/v30 合入后历史备份不可恢复，迁移 PR 必须
  写明备份策略（升级后立即全量新备份，见 CONTRIBUTING 第 11 节）。
- `forecasting` 模块登记为 `available` 前须按 D-030 补虚拟店铺场景；未实现不登记。

## 11. Definition of Done

M6-R 只有在以下条件全部满足后才能从设计/开发状态进入本机候选：

- `demand-v1` 和 business date 口径冻结且可重放。
- 缺货/未知/数据缺失不会被静默当成真实低需求或零需求。
- 每个 SKU 的 champion 经过无泄漏 rolling backtest，并保留 baseline fallback。
- 输出逐日 P50/P80/P95、质量等级、backtest 指标和选择理由。
- 库存计划为确定性可测计算，仓库级边界和所有舍入过程可解释。
- 两个 Agent tools 只读，后台与 Eval 有完整证据。
- 全量回归通过，旧 M5/M6 API、迁移与模块行为无回归。
