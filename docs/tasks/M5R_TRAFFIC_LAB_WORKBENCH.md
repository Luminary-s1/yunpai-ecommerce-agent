# M5-R 商品流量实验与推流机制分析 — 任务书

> 代码域：`traffic_lab`。
> 文档性质：任务书；实施进度、工时与日期仅在负责人工作台网页记录。
> 全部工作包统一负责人：**闫睿涵**。
> 路线依据：[ROADMAP_RESET_20260807.md](../ROADMAP_RESET_20260807.md)。

## 1. 目标与正确表述

在授权的自有店铺数据上，通过标题、主图等变量的受控实验，采集
“listing revision → 曝光 → 点击 → 加购/成交 → 后续流量变化”，用确定性统计分析形成
可复验的黑盒推流机制假设，并由 AI 解释证据、反证和下一轮实验建议。

原始诉求「分析商品名和图片的推流算法逻辑」在本任务书中的可执行形式就是黑盒机制
假设：平台内部权重不可观测，能采集并复核的只有「我们改了什么 → 流量如何变化」的
稳定输入输出关系。AI 的分析体现在三处——读取固化统计结果生成机制假设、解释证据与
反证、设计下一轮实验（改哪个变量、预期观察什么）。**新品上架期是第一优先应用场景**：
新品没有历史流量包袱，候选标题/主图在上架初期做受控测试的信噪比最高；存量商品优化
实验复用同一套 revision/实验/分析链路。

允许的结论示例：

> 过去 8 次实验中有 6 次出现 CTR 上升后 4–12 小时推荐曝光增加；控制价格、广告和
> 小时效应后方向仍为正，当前支持度为 medium，尚不能证明平台直接使用 CTR 排序。

禁止的结论示例：

> 平台算法中图片权重为 27%，标题权重为 19%。

## 2. 范围与边界

### 范围内

- 商品标题、主图、价格快照和控制变量的不可变 listing revision。
- 小时级优先、日级可退化的 SKU/listing 流量指标导入。
- A/A、平台真 A/B、switchback 和后续 Difference-in-Differences 分析。
- 标题/图片确定性特征、可选 AI 语义标签及其版本记录。
- uplift、置信区间、时间滞后、样本量、数据质量和反证。
- 虚拟推流器、Eval、只读 Agent tool 和后台实验控制台。

### 范围外

- 未授权平台抓取、Cookie 复用、页面注入或算法逆向。
- 自动改标题、自动换图、自动调广告或自动发布渠道商品。
- 第一阶段同时修改标题与图片，或以 LLM 输出替代统计事实。
- 在样本不足、窗口污染或关键控制变量缺失时强行给出有效性结论。

## 3. 分析对象

推流问题必须拆成四层，不允许只看 CTR：

| 层 | 研究目标 | 核心指标 |
|---|---|---|
| 流量分配 | 平台为什么多给或少给曝光 | impressions/hour、搜索曝光、推荐曝光 |
| 素材吸引 | 标题或图片是否促成点击 | CTR |
| 商业质量 | 点击以后是否形成有效行为 | 收藏、加购、CVR、订单、GMV |
| 反馈 | 当前表现是否影响下一时窗推流 | 下一时窗曝光增量、推荐曝光增量、lag |

第一阶段实验必须单变量：

```text
标题实验：标题 A + 图片 A  vs  标题 B + 图片 A
图片实验：标题 B + 图片 A  vs  标题 B + 图片 B
```

只有前述机制稳定后才允许 2×2 interaction experiment。

## 4. 数据模型（schema v28）

所有表必须包含租户隔离、必要索引、创建/更新时间和项目统一的来源版本/载荷哈希语义。
以下是领域最小字段，不替代迁移评审。

> Schema 版本号以 `CONTRIBUTING.md`「Schema 版本号占用登记」表为单一来源（D-035）；
> 若占号调整，以该表为准，本文不另行维护版本事实。

### 4.1 `creative_assets`

图片不能只保存 URL；资源内容变化必须可识别。

```text
asset_id
tenant_id
sha256
mime_type
width
height
storage_ref
source_ref
feature_schema_version
created_at
```

同租户同 SHA-256 幂等；`storage_ref` 只能引用项目批准的本地/对象存储位置，不能泄露凭证。

### 4.2 `listing_revisions`

一件商品每次标题、主图、价格或控制变量变化都形成不可变版本。

```text
id
tenant_id
connector_id
store_id
item_id
sku_id
revision_no
title
main_image_asset_id
sale_price
attributes_json
active_from
active_to
source_updated_at
payload_hash
created_at
```

约束：

- 同一 listing 的 revision number 不重复。
- 生效窗口不得无解释重叠；来源乱序沿用 D-014 处理。
- 历史 revision 不原地修改，纠错通过新版本或显式撤销记录完成。

### 4.3 `traffic_metric_buckets`

```text
id
tenant_id
listing_revision_id
metric_start
metric_end
bucket_granularity
traffic_source
impressions
clicks
visitors
favorites
cart_adds
orders
sales_amount
ad_spend
search_impressions
recommend_impressions
data_as_of
source_id
payload_hash
created_at
```

门禁：计数不得为负；点击不得大于曝光；订单与金额的异常关系应标记数据质量问题而不是
静默修正；无法唯一归属到 revision 的 bucket 进入隔离队列，不参与分析。

### 4.4 `traffic_experiments`

```text
experiment_id
tenant_id
store_id
sku_id
experiment_type
primary_metric
status
started_at
ended_at
control_revision_id
treatment_revision_id
minimum_exposure
washout_window
analysis_policy_version
created_at
```

状态机：

```text
draft -> ready -> running -> completed
                  ├-> paused
                  └-> invalid
```

状态转换由代码校验。`completed` 只表示数据窗口结束，不表示实验效果显著。

### 4.5 `traffic_experiment_windows`

记录每个时窗真实使用的 revision，支持 switchback：

```text
window_id
tenant_id
experiment_id
listing_revision_id
window_start
window_end
assignment
washout
source_receipt_id optional
created_at
```

分析必须按实际窗口而不是计划窗口取数；有重叠、缺口或无法确认变更回执时降低质量等级或
将实验标记 invalid。

### 4.6 `traffic_analysis_runs`

分析结果必须版本化：

```text
analysis_run_id
tenant_id
experiment_id
method
data_window_json
sample_size_json
effect_estimate_json
confidence_interval_json
evidence_json
counter_evidence_json
hypotheses_json
model_provider optional
model_name optional
prompt_version optional
analysis_code_version
created_at
```

统计结果先由固化代码产生，AI 只能读取结构化结果生成解释；AI 不得覆盖 effect、区间、
样本量、质量等级或显著性状态。

## 5. 特征引擎

### 5.1 标题特征

V1 先用确定性特征：

```text
title_length
brand_present
category_keyword_present
brand_position
category_keyword_position
numeric_token_count
benefit_keyword_count
scenario_keyword_count
promotion_keyword_count
duplicate_term_ratio
first_10_chars_information_density
```

AI 可补充语义标签和结构解释，但必须保存 extractor/model/prompt/schema 版本。

### 5.2 图片特征

V1 的确定性层优先覆盖：尺寸、宽高比、文件大小、亮度、对比度、清晰度、边缘密度、
文字面积、主体面积和留白比例。多模态模型只作为可选语义层，输出场景/白底、人物、
卖点突出度、拥挤度和视觉风格等标签。

同一资产与同一 extractor version 的结果必须可复现；升级 extractor 不覆盖旧结果。

## 6. 实验与分析规则

### 6.1 实验方法优先级

| 方法 | 场景 | 第一阶段定位 |
|---|---|---|
| A/A | 验证采集和分组不会制造假阳性 | 必做 Gate |
| 平台 A/B | 平台提供真正随机分流 | 有能力时优先 |
| Switchback | 同一 SKU 按时间交替 revision | V1 主路径 |
| Difference-in-Differences | 有稳定相似控制商品 | 后续增强 |

Switchback 分配必须平衡星期、小时和先后顺序，并显式记录 washout 窗口。

### 6.2 最低控制变量

- SKU、类目、售价和库存/缺货状态。
- 小时、星期、节假日和店铺总流量。
- 广告花费、活动、历史 CTR/CVR。
- 标题 revision、图片 asset/revision 和实验 assignment。
- 后期接入的竞品价格只能来自既有 approved-only 事实。

缺货时流量下降不能归因于素材；价格或广告在实验窗内发生未计划变化时必须进入污染标记。

### 6.3 分析能力递进

| Level | 方法 | Gate |
|---:|---|---|
| L0 | 描述统计 | 展示趋势，不下因果结论 |
| L1 | A/A | 假阳性受控后才能开放 uplift 结论 |
| L2 | A/B / switchback uplift | 样本量与窗口完整性达标 |
| L3 | lag analysis | 明确当前窗与未来窗，防止时间泄漏 |
| L4 | 多变量回归 | 控制变量和诊断满足评审要求 |
| L5+ | 非线性/因果/贝叶斯 | 数据量和独立评测达标后另行设计 |

输出关注方向、稳定性、置信区间、跨实验复现和滞后时间，不把单次 p-value 当作业务真相。

## 7. Connector 资源

`VirtualTaobaoConnector` 和后续真实 Connector 增加统一资源：

```text
listing_revision
traffic_metrics
```

真实平台能力允许后再评估：

```text
traffic_source_breakdown
search_query_metrics
listing_change_receipt
```

虚拟连接器必须包含分析模块不可读取的隐藏策略，例如基础曝光、标题/图片特征、最近 CTR/CVR
反馈、缺货惩罚与随机噪声。ground truth 只用于 Eval 判定，不进入业务分析上下文。

## 8. API 草案

| API | 作用 |
|---|---|
| `POST /v1/traffic-lab/assets` | 注册主图资产 |
| `POST /v1/traffic-lab/revisions` | 创建不可变 listing revision |
| `GET /v1/traffic-lab/revisions` | 查询历史版本 |
| `POST /v1/traffic-lab/metrics/import` | CSV/JSON 导入平台流量 |
| `POST /v1/traffic-lab/experiments` | 创建实验 |
| `POST /v1/traffic-lab/experiments/{id}/windows` | 记录实际生效窗口 |
| `GET /v1/traffic-lab/experiments/{id}` | 查看状态和数据质量 |
| `POST /v1/traffic-lab/experiments/{id}/analyze` | 运行固化统计分析 |
| `GET /v1/traffic-lab/experiments/{id}/analysis` | 获取版本化分析 |
| `GET /v1/traffic-lab/items/{sku_id}/insights` | 获取 SKU 跨实验规律 |
| `POST /v1/traffic-lab/hypotheses` | 基于已固化证据生成下一轮假设 |

API 请求/响应需在实现 Issue 中冻结；本表不授权外部写商品动作。

## 9. 工作包

### WP1 Listing / Creative 数据模型

负责人：**闫睿涵**。

交付：schema v28、领域 service、asset/revision/metric/experiment/window/analysis-run 模型与
租户隔离查询。

验收：

- 任一 metric bucket 可追溯到唯一标题、主图、价格快照和生效时窗。
- 旧版本拒绝、同版本同载荷幂等、同版本不同载荷冲突。
- revision 不可变，窗口重叠/缺口可检测。
- 跨租户 ID 猜测不能读写数据。
- v27 → v28 前向迁移和存量数据库回归通过。

依赖：无。阻塞 WP2–WP5 的持久化契约。

### WP2 数据接入与虚拟推流器

负责人：**闫睿涵**。

交付：Connector resources、CSV/JSON importer、变更回执、隐藏虚拟推流策略和可重放 fixture。

验收：

- 小时级和日级输入都能规范化，粒度不会混算。
- 重放相同数据不产生重复 bucket。
- 无法归属 revision 的数据被隔离且不进入分析。
- 分析代码无法访问虚拟 ground truth。
- 已知隐藏策略可生成方向可判定且含噪声的评测数据。

依赖：WP1 的数据契约。

### WP3 标题 / 图片特征引擎

负责人：**闫睿涵**。

交付：确定性标题/图片特征、可选 AI 语义层、版本化 extractor 输出。

验收：

- 相同输入和相同版本的确定性特征完全一致。
- AI 不可用时确定性特征链可独立工作并显式标记降级。
- 模型或 feature schema 升级不会覆盖旧结果。
- 标题与图片特征均绑定具体 revision/asset。

依赖：WP1；AI 语义层非关键路径。

### WP4 实验与黑盒分析引擎

负责人：**闫睿涵**。

交付：A/A、switchback uplift、置信区间、lag、数据质量 Gate 和版本化假设。

验收：

- A/A fixture 不产生稳定虚假效果。
- 无样本、样本不足、窗口污染或关键控制变量缺失时拒绝强结论。
- 分析只使用实际窗口，不使用未来数据或虚拟 ground truth。
- 已知 CTR/CVR/库存/无影响策略的方向识别通过独立 Eval。
- 每次分析保存证据、反证、代码版本和完整输入窗口。

依赖：WP1、WP2；WP3 特征完成后开放特征级解释。

### WP5 Agent / Admin / Eval

负责人：**闫睿涵**。

交付：只读 `get_listing_traffic_insights` tool、实验控制台、虚拟评测套件和交付证据。

验收：

- Agent 只能读取结构化分析证据，不能自行重算或宣称平台权重。
- Agent 工具经动态目录由模型选择，客服链路不为 traffic_lab 新增关键词路由（D-034）。
- 后台展示 control/treatment、窗口、样本量、uplift、区间、lag、污染和反证。
- Eval 覆盖无影响、CTR/CVR 反馈、库存惩罚、标题/图片权重、interaction 和时间噪声。
- Eval 判定使用数值与结构化断言，不引入子串关键词裁判。
- 能识别存在的方向，也能拒绝不存在的效果。
- 第一版不存在自动发布商品或自动投放动作。

依赖：WP1–WP4。

## 10. 实现纪律（执行者必读）

本任务书晚于 2026-08-07 的决策权边界与可演进性规范定稿，实现必须遵守
`CONTRIBUTING.md` 第 10、11 节（D-034、D-035），审计背景见
`docs/AUDIT_ROUTING_EVOLVABILITY_20260807.md`。落到本模块：

**决策权边界（D-034）**

- 统计计算、实验状态机、数据质量门禁、样本量判定由代码固化；机制假设、证据解释、
  下一轮实验建议由模型生成。两者的边界即 `traffic_analysis_runs` 的字段边界：AI 不得
  覆盖 effect、区间、样本量、质量等级。
- 标题特征里的词表（卖点词、促销词等）是**统计特征**，不是路由——不得用这些词表
  在对话链路或分析链路里替模型做语义判断。
- AI 语义标签是 signal：进特征表、进解释文本；不得单独决定实验有效性或门禁结论。
- 词表、特征清单随 `feature_schema_version` 单点版本化，不散落多处手抄。

**测试与 mock 纪律**

- mock 模型不得复刻生产分析逻辑；用固定/表驱动输出。
- 「模型不得被调用」断言仅限 `MODEL_ENABLED=false` 契约（D-005），不得用来锁定
  语义路径绕过模型。
- 新增测试断言自身增量（自己的表、列、场景存在），不新增全局计数全等断言；
  涉及既有全等断言（如虚拟店铺场景计数、拓扑快照）需要变更时，按第 11 节改成
  下界/成员断言，不是把数字 +1。

**Schema 与注册纪律（D-035）**

- 迁移按 `CONTRIBUTING.md` 第 9 节占号规则执行；写 `_apply_vNN` 前先全分支搜同名。
- 迁移加列/加表时同步检查 `_validate_schema` 的 required 清单：新表要加条目，且
  **确认没有制造重复字典键**（该函数曾因重复键静默吞掉 v25 校验）。
- 灾备 manifest 精确比对 schema 版本：v28 合入后历史备份不可恢复，迁移 PR 必须写明
  备份策略（升级后立即全量新备份，见 CONTRIBUTING 第 11 节）。
- `traffic_lab` 模块登记为 `available` 前须按 D-030 补虚拟店铺场景；未实现不登记。

## 11. Definition of Done

M5-R 只有在以下条件全部满足后才能从设计/开发状态进入本机候选：

- 数据链路能把流量点唯一追溯到 listing revision。
- A/A、switchback、lag 和数据质量门禁具有有效反证测试。
- 虚拟推流 ground truth 与分析模块隔离，关键方向恢复评测通过。
- 统计结果与 AI 表达权责分离，模型不可用时仍可返回确定性分析。
- 只读 Agent tool、后台、审计、租户隔离和生产边界完整。
- 全量回归通过，旧 M5/M6 API、迁移与模块行为无回归。
