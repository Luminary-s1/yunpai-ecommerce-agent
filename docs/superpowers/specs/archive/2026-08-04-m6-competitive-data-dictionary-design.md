# M6 竞品数据 Schema 与字段字典

> **FROZEN / SUPERSEDED（2026-08-07）**：本文是旧 M6 竞品分析的历史设计稿，已归档、
> 只读保留。新路线见 [路线重置说明](../../../ROADMAP_RESET_20260807.md) 和
> [M6-R Demand Forecast 工作台](../../../tasks/M6R_DEMAND_FORECAST_WORKBENCH.md)。

> 文档版本：`M6-DD-1.2`
> 日期：2026-08-05
> 状态：接口口径已冻结；Schema 26 数据基础已提交 PR #2，完整工作包验收尚未完成
> 适用范围：M6 工作包 3「竞品数据模型定义与结构化管理服务」（数据层唯一归口）
> 下游消费者：工作包 2「竞品数据采集与结构化管理」只做分析侧取数适配，不重复实现本字典定义的数据层能力

## 1. 目标

本字段字典定义 M6 竞品数据的统一逻辑 Schema、现有物理表映射、字段约束、
CSV 列映射、版本语义、筛选语义和验收追踪关系。

它解决以下问题：

- 区分商品评价评分与同款候选匹配分数。
- 区分低频变化的商品身份、版本化同款关系、时间序列动态事实和内容证据。
- 沿用 F-304 的人工裁决门禁和 F-305 的自有商品事实，不重复建模。
- 为 CSV 导入、多维筛选、自定义维度查询和统一管理 API 提供唯一字段口径。
- 明确当前代码已有能力、建议新增字段和实现阶段仍需完成的工作。
- 固化最新范围边界：CSV 导入、字段校验、品类/品牌/价格/评分筛选和自定义维度
  定义全部属于工作包 3；工作包 2 仅复用这些接口完成 approved-only 取数适配。

本文件是设计依据，不代表数据库迁移、API、CSV 导入、后台页面或测试已经完成。

## 2. 约束与决策

### 2.1 强制约束

- D-014：外部事实使用来源时间与载荷哈希判定版本。旧版本拒绝；同版本同载荷
  幂等；同版本不同载荷冲突；新版本应用。
- D-025：竞品事实必须先经过可解释同款评分和人工裁决。只有 approved 匹配绑定的
  价格、评分、销量排名、卖点和脱敏聚合口碑才能进入分析结论与 Agent 建议。
- 不保存评论者身份或原始评论内容。
- 不自动改价、不自动调整投放；分析只输出建议。
- 数据来源只允许授权 API、许可供应商、人工录入、文件导入或显式虚拟样本。
- 不增加第三方依赖。
- 数据库新增列使用 `_ensure_column`，不重建表。
- 租户 ID 只能来自认证管理员上下文，不能接受 CSV 或请求正文自报。

### 2.2 已确定的字段语义

- `rating_value` 是商品评价原始评分，不是同款候选分数。
- `rating_scale` 是商品评价满分值。统一五星评分只在查询时计算，不重复落库：
  `normalized_rating = rating_value / rating_scale * 5`。
- `rating_value`、`rating_scale`、`sales_rank`、`rank_scope` 是外部动态事实，清洗并
  规范化后全部参与 `payload_hash`；`normalized_rating` 是派生值，不重复参与哈希。
  同一来源时间内任一事实变化属于 D-014 的同版本不同载荷冲突，需要人工核对。
- `match_score` 是同款候选可信度，范围 0–100，只服务于裁决和质量解释。
- 公共治理字段是各事实表的公共列，不创建“公共治理字段表”。
- 自有商品由 F-305 `catalog_items` 管理；竞品模块只保存关联和必要的证据快照。
- 现有四张竞品表继续使用，不为五个逻辑区块重建五张表。

## 3. 逻辑对象与物理存储

| 逻辑对象 | 回答的问题 | 物理存储 | 更新方式 |
|---|---|---|---|
| 商品身份 | 这是什么商品 | `competitive_entity_matches.subject_identity_json` 和 `competitor_identity_json` | 来源新版本更新证据快照，重新进入 pending 裁决 |
| 同款关系 | 它和哪个自有商品比较，是否确认同款 | `competitive_entity_matches` | 乐观锁状态迁移，保留当前投影 |
| 裁决历史 | 谁在什么版本做了什么决定 | `competitive_match_decisions` | 只追加，不修改、不删除 |
| 动态事实 | 某个时间点的价格、评分和销量排名是什么 | `competitor_observations` | 按来源时间写入，保留时间序列 |
| 内容证据 | 商品卖点和聚合口碑是什么 | `competitive_signals` | 按来源版本写入，只保存脱敏摘要和聚合计数 |
| 公共治理 | 数据属于谁、来自哪里、哪个版本 | 分布在以上各表 | 由服务端计算和校验 |

管理端对外提供统一竞品视图，但物理存储继续按上述职责拆分。

## 4. 状态标记

字段表中的状态含义：

- `现有`：当前数据库和领域模型已经存在。
- `建议新增`：完成工作包 3 需要新增或扩展。
- `派生`：查询或响应时计算，不落数据库。
- `只读生成`：由服务端生成，CSV 和普通请求不能提交。

## 5. 字段字典

### 5.1 商品身份 `CompetitiveProductIdentity`

商品身份作为同款判断时的证据快照嵌入 `competitive_entity_matches`。自有商品快照由
F-305 当前版本生成；竞品商品快照来自合法数据源。两侧使用相同结构。

| 字段 | 类型 | 必填 | 状态 | 校验与含义 | 可筛选 | CSV 列/别名 |
|---|---|---:|---|---|---:|---|
| `title` | string | 是 | 现有 | 商品名称，2–500 字符 | 否 | `product_title`、`商品名称`、`商品标题` |
| `brand` | string | 否 | 现有 | 品牌，去除首尾空白，最长 128 | 是，精确不区分大小写 | `brand`、`品牌` |
| `model` | string | 否 | 现有 | 型号，最长 128 | 否 | `model`、`型号` |
| `category` | string | 否 | 现有 | 标准品类，最长 200 | 是，精确不区分大小写 | `category`、`品类`、`类目` |
| `gtin` | string | 否 | 现有 | 8/12/13/14 位数字 | 否 | `gtin`、`条码`、`商品条码` |
| `attributes` | object<string,string> | 否 | 现有 | 最多 32 项；身份匹配使用的关键属性；键 1–64、值 1–200 | 等值查询 | `attr.<key>`、`属性.<key>` |
| `custom_dimensions` | array<CustomDimension> | 否 | 建议新增 | 最多 32 项；用于分析和筛选，不改变原有 `attributes` 契约 | 是 | `dim.<key>`、`维度.<key>` |

`attributes` 保持现有字符串契约，避免破坏同款评分。`custom_dimensions` 使用明确类型：

| 字段 | 类型 | 必填 | 规则 |
|---|---|---:|---|
| `key` | string | 是 | 稳定机器键，1–64 字符；同一身份内按 casefold 唯一；禁止控制字符 |
| `label` | string | 是 | 展示名称，1–64 字符 |
| `value_type` | enum | 是 | `text`、`number` 或 `boolean` |
| `value_text` | string | 条件必填 | `value_type=text` 时唯一有效，最长 200 |
| `value_number` | decimal string | 条件必填 | `value_type=number` 时唯一有效，使用十进制定点文本 |
| `value_boolean` | boolean | 条件必填 | `value_type=boolean` 时唯一有效 |
| `unit` | string | 否 | 仅数值维度允许，最长 32；例如 `GB`、`ml`、`W` |

每个自定义维度只能设置一个值字段。例如：

```json
{
  "key": "memory_gb",
  "label": "内存",
  "value_type": "number",
  "value_number": "32",
  "unit": "GB"
}
```

### 5.2 同款关系 `competitive_entity_matches`

| 字段 | 类型 | 必填 | 状态 | 校验与含义 | 可筛选 | 写入方 |
|---|---|---:|---|---|---:|---|
| `id` | string | 是 | 只读生成 | `compmatch-<uuid>` | 是 | 服务端 |
| `tenant_id` | string | 是 | 现有 | 来自认证上下文 | 强制隔离 | 服务端 |
| `connector_id` | string | 是 | 现有 | 数据连接器 ID，1–128 | 是 | 导入上下文/API |
| `store_id` | string | 是 | 现有 | 自有商品所属店铺，1–128 | 是 | 导入上下文/API |
| `subject_sku` | string | 是 | 现有 | F-305 自有 SKU；同租户同店铺必须存在且非 deleted | 是 | CSV/API |
| `competitor_name` | string | 是 | 现有 | 竞品店铺、供应商或主体展示名，1–200 | 否 | CSV/API |
| `competitor_sku` | string | 是 | 现有 | 来源侧竞品 SKU，1–128 | 是 | CSV/API |
| `subject_identity` | CompetitiveProductIdentity | 是 | 现有 | 从 F-305 读取并保存不可变证据快照 | 否 | 服务端 |
| `competitor_identity` | CompetitiveProductIdentity | 是 | 现有+扩展 | 竞品身份及自定义维度 | 品牌、品类、维度 | CSV/API |
| `comparison_keys` | array<string> | 否 | 现有 | 最多 20 个，单项 1–64，casefold 去重 | 否 | CSV/API |
| `match_score` | integer | 是 | 现有字段 `score` 的逻辑名 | 0–100；确定性计算；不得作为商品评分 | 可独立筛选 | 服务端 |
| `matched_fields` | array | 是 | 只读生成 | 对评分有正贡献的字段证据 | 否 | 服务端 |
| `conflicts` | array | 是 | 只读生成 | 容量、套装、型号等硬冲突 | 否 | 服务端 |
| `missing_fields` | array | 是 | 只读生成 | 无法比较的字段 | 否 | 服务端 |
| `recommended_status` | enum | 是 | 只读生成 | `approved`、`pending`、`rejected` 建议 | 是 | 服务端 |
| `status` | enum | 是 | 现有 | 实际状态 `pending`、`approved`、`rejected`；导入只能产生 pending | 是 | 裁决接口 |
| `record_version` | integer | 是 | 现有 | 从 1 开始；每次裁决条件更新递增 | 否 | 服务端 |
| `reviewed_by` | string | 否 | 现有 | 可信管理员 ID，不接受正文自报 | 否 | 服务端 |
| `reviewed_at` | datetime | 否 | 现有 | UTC 带时区时间 | 否 | 服务端 |
| `review_note` | string | 否 | 现有 | 8–500 字符，禁止原始评论和个人信息 | 否 | 裁决接口 |

数据库仍使用列名 `score`，统一 API 和数据字典使用语义名 `match_score`。实施阶段可在
响应中兼容保留 `score`，不得直接删除或重命名既有字段。

### 5.3 裁决历史 `competitive_match_decisions`

| 字段 | 类型 | 必填 | 状态 | 校验与含义 |
|---|---|---:|---|---|
| `id` | string | 是 | 只读生成 | 决策 ID |
| `tenant_id` | string | 是 | 现有 | 强制租户隔离 |
| `match_id` | string | 是 | 现有 | 关联同租户 match |
| `from_status` | enum | 是 | 现有 | 变更前状态 |
| `to_status` | enum | 是 | 现有 | `approved` 或 `rejected` |
| `match_record_version` | integer | 是 | 现有 | 必须等于更新后的 match 版本 |
| `actor` | string | 是 | 现有 | 认证管理员 ID |
| `note` | string | 是 | 现有 | 裁决依据，8–500 字符 |
| `created_at` | datetime | 是 | 现有 | 服务端 UTC 时间 |

裁决历史只追加。所谓删除同款关系使用 `rejected` 状态，不进行物理删除。

### 5.4 动态事实 `competitor_observations`

动态事实按时间保留历史。管理筛选默认使用每个同款关系最新的有效观察；历史查询
返回完整时间序列。价格范围必须与币种同时使用。

| 字段 | 类型 | 必填 | 状态 | 校验与含义 | 可筛选 | CSV 列/别名 |
|---|---|---:|---|---|---:|---|
| `id` | string | 是 | 只读生成 | `competitor-<uuid>` | 否 | 无 |
| `tenant_id` | string | 是 | 现有 | 来自认证上下文 | 强制隔离 | 无 |
| `connector_id` | string | 是 | 现有 | 1–128 | 是 | 导入上下文 |
| `store_id` | string | 是 | 现有 | 1–128 | 是 | 导入上下文或 `store_id`、`店铺ID` |
| `subject_sku` | string | 是 | 现有 | F-305 自有 SKU | 是 | `subject_sku`、`自有SKU`、`本店SKU` |
| `competitor_name` | string | 是 | 现有 | 1–200 | 否 | `competitor_name`、`竞品名称`、`竞品店铺` |
| `competitor_sku` | string | 是 | 现有 | 1–128 | 是 | `competitor_sku`、`竞品SKU` |
| `subject_price` | decimal string | 是 | 现有 | 大于 0；自有商品采样价格 | 是 | `subject_price`、`自有价格`、`本店价格` |
| `competitor_price` | decimal string | 是 | 现有 | 大于 0；竞品采样价格 | 是 | `competitor_price`、`竞品价格`、`售价` |
| `currency` | ISO-4217 string | 是 | 现有 | 三位大写，默认 `CNY` | 是 | `currency`、`币种` |
| `rating_value` | decimal string | 否 | 建议新增 | 大于等于 0；必须与 `rating_scale` 成对 | 通过派生值 | `rating_value`、`商品评分`、`评分` |
| `rating_scale` | decimal string | 否 | 建议新增 | 大于 0，且 `rating_value <= rating_scale` | 否 | `rating_scale`、`评分满分`、`满分` |
| `normalized_rating` | decimal string | 否 | 派生 | `rating_value / rating_scale * 5`，保留两位 | 是，0–5 | 不入库 |
| `sales_rank` | integer | 否 | 建议新增 | 大于等于 1；必须与 `rank_scope` 成对 | 是 | `sales_rank`、`销量排名`、`排名` |
| `rank_scope` | string | 否 | 建议新增 | 1–200；平台/品类/榜单范围 | 是 | `rank_scope`、`排名范围`、`榜单` |
| `entity_match_id` | string | 否 | 现有 | 同租户且店铺、双方 SKU 必须一致 | 是 | `entity_match_id`、`匹配ID` |
| `source_id` | string | 条件必填 | 现有 | CSV/连接器必填，最长 256；来源稳定记录 ID | 是 | `source_id`、`来源记录ID`、`数据ID` |
| `observed_at` | aware datetime | 是 | 现有 | 来源事实时间，规范为 UTC | 是 | `observed_at`、`采集时间`、`数据时间` |
| `payload_hash` | SHA-256 | 是 | 现有 | 规范化整行载荷计算 | 否 | 服务端生成 |
| `created_at` | datetime | 是 | 现有 | 系统接收时间 | 否 | 服务端生成 |

`rating_value` 与 `rating_scale` 同为空或同时有值；`sales_rank` 与 `rank_scope` 同理。
评分筛选使用 `normalized_rating`，不得使用 `match_score`。

### 5.5 内容证据 `competitive_signals`

| 字段 | 类型 | 必填 | 状态 | 校验与含义 | CSV 列/别名 |
|---|---|---:|---|---|---|
| `id` | string | 是 | 只读生成 | `compsignal-<uuid>` | 无 |
| `tenant_id` | string | 是 | 现有 | 强制租户隔离 | 无 |
| `match_id` | string | 是 | 现有 | 关联同租户 match；可在 pending 状态保存，但不能进入分析 | `entity_match_id`、`匹配ID` |
| `connector_id` | string | 是 | 现有 | 1–128 | 导入上下文 |
| `entity_role` | enum | 是 | 现有 | `subject` 或 `competitor` | `entity_role`、`主体类型` |
| `signal_type` | enum | 是 | 现有 | `product_claim` 或 `review_summary` | 由列类型确定 |
| `aspect` | string | 是 | 现有 | 证据方面，1–128 | `aspect`、`方面` |
| `summary` | string | 是 | 现有 | 2–2000；入库前脱敏 | `selling_points`、`核心卖点`、`review_summary`、`评价摘要` |
| `sample_size` | integer | 条件必填 | 现有 | review_summary 必填，5–10,000,000 | `review_sample_size`、`评价样本数` |
| `positive_count` | integer | 否 | 现有 | 大于等于 0；与负面数之和不得超过样本数 | `positive_count`、`正面数` |
| `negative_count` | integer | 否 | 现有 | 大于等于 0 | `negative_count`、`负面数` |
| `source_id` | string | 是 | 现有 | 来源稳定记录 ID | 由行 `source_id` 派生组件 ID |
| `observed_at` | aware datetime | 是 | 现有 | 来源事实时间 | `observed_at`、`采集时间` |
| `payload_hash` | SHA-256 | 是 | 现有 | 服务端计算 | 无 |
| `created_at` | datetime | 是 | 现有 | 系统接收时间 | 无 |

CSV 中卖点可使用 `|` 分隔为多条 product_claim；评价摘要只能是脱敏聚合文本。
导入器为每个组件生成稳定来源 ID，例如 `<source_id>:claim:1` 和
`<source_id>:review`。CSV 不接受评论者、账号、昵称或原始评论列。

### 5.6 公共治理字段

公共治理字段不单独建表。不同对象按职责使用以下字段：

| 字段 | 类型 | 规则 |
|---|---|---|
| `tenant_id` | string | 只从认证上下文取得；所有读写 SQL 必须包含租户条件 |
| `connector_id` | string | 标识数据接入连接器；文件导入使用受控固定 ID 或导入请求指定的已注册 ID |
| `source_type` | enum | `authorized_api`、`licensed_provider`、`manual`、`file_import`、`virtual` |
| `source_ref` | string | 4–500；文件名、许可供应商记录或受控 URI；不得包含密钥 |
| `source_id` | string | 同一来源业务记录的稳定 ID；CSV/连接器写入必填 |
| `is_estimate` | boolean | 虚拟样本必须为 true；生产来源按证据确定 |
| `observed_at` | aware datetime | 来源事实时间，统一为 UTC；用于 D-014 版本比较 |
| `payload_hash` | SHA-256 | 对清洗、规范化后的业务载荷计算，不包含系统生成 ID 和 `created_at` |
| `record_version` | integer | 需要乐观锁的状态投影从 1 开始递增 |
| `created_at` | datetime | 服务端接收时间，不参与来源版本比较 |
| `updated_at` | datetime | 仅可变投影使用；不可变事实不覆盖原始创建时间 |

### 5.7 统一竞品管理视图

统一视图是 API 响应，不新增物理表。最小结构为：

```json
{
  "match": {
    "id": "compmatch-...",
    "status": "pending",
    "match_score": 82,
    "subject_sku": "YP-SKU-001",
    "competitor_sku": "COMP-001"
  },
  "identity": {
    "title": "示例竞品",
    "brand": "示例品牌",
    "category": "智能客服一体机",
    "custom_dimensions": []
  },
  "latest_observation": {
    "competitor_price": "4999.00",
    "currency": "CNY",
    "rating_value": "4.8",
    "rating_scale": "5",
    "normalized_rating": "4.80",
    "sales_rank": 3,
    "rank_scope": "平台/智能客服一体机/日榜",
    "observed_at": "2026-08-04T00:00:00+00:00"
  },
  "signals": [],
  "actionable": false
}
```

管理视图可以返回 pending/rejected/approved；分析与 Agent 视图中的价格、评分、
销量排名、卖点和脱敏聚合口碑必须强制 `match.status=approved`，不能由客户端关闭
该门禁。

## 6. CSV 导入契约

### 6.1 导入上下文

以下值来自认证和导入请求，不由每行 CSV 任意覆盖：

| 字段 | 来源 | 规则 |
|---|---|---|
| `tenant_id` | 管理员认证 | CSV 禁止提供 |
| `connector_id` | 导入请求 | 必须是允许文件导入的连接器 ID |
| `store_id` | 导入请求或受控列 | 必须属于当前租户 |
| `source_type` | 导入请求 | 默认 `file_import`；虚拟验收必须显式 `virtual` |
| `source_ref` | 导入请求 | 文件名或受控 URI，不保存本机绝对敏感路径 |
| `is_estimate` | 导入请求 | `source_type=virtual` 时强制 true |

### 6.2 规范行

创建完整待审批候选和动态事实时，规范行至少需要：

- `source_id`
- `subject_sku`
- `competitor_name`
- `competitor_sku`
- `product_title`
- `subject_price`
- `competitor_price`
- `currency`
- `observed_at`

可选字段包括品牌、型号、品类、GTIN、属性、自定义维度、商品评分、销量排名、
核心卖点和脱敏评价摘要。若提供 `entity_match_id`，服务端必须校验租户、店铺和双方
SKU 一致；若未提供，则根据身份字段创建或复用 pending 候选，绝不自动批准。

### 6.3 数值清洗

- 价格允许 `¥4,999.00`、`4,999.00` 和 `4999`，清洗后必须是大于 0 的 Decimal。
- 商品评分与满分允许整数或小数，必须成对且 `0 <= value <= scale`。
- 销量排名允许 `3`、`第3名`，清洗后必须是大于等于 1 的整数。
- 空字符串统一为 null，不把缺失值转换为 0。
- Decimal 使用字符串规范化并参与载荷哈希，避免二进制浮点差异。

### 6.4 行级原子性与错误响应

每行先完成全字段校验，再执行写入。一行内的 match、observation 和 signals 必须
全部成功或全部回滚；某行失败不得影响其他行。

```json
{
  "total_rows": 3,
  "accepted_rows": 2,
  "rejected_rows": 1,
  "applied": 1,
  "idempotent": 1,
  "conflicts": 0,
  "records": [],
  "errors": [
    {
      "row": 3,
      "field": "competitor_price",
      "code": "decimal_invalid",
      "message": "竞品价格必须是大于 0 的数字"
    }
  ]
}
```

错误必须包含 `row`、`field`、稳定 `code` 和可读 `message`。错误响应不回显整行，
不记录评论文本和个人信息。

## 7. 来源版本与幂等

规范化后，以 `tenant_id + connector_id + source_id` 标识来源业务记录，以
`observed_at` 作为来源版本时间，以 `payload_hash` 判断同版本内容。

| 输入情况 | 结果 |
|---|---|
| 时间早于现有版本 | `stale_source_version`，该行拒绝 |
| 时间相同且哈希相同 | `idempotent`，不产生重复记录 |
| 时间相同但哈希不同 | `source_version_conflict`，该行拒绝并要求人工核对 |
| 时间晚于现有版本 | `applied`，保留可追溯新版本 |

载荷哈希必须在列名映射、空白清洗、Decimal 规范化和时间 UTC 化之后计算。
同一 CSV 重放必须得到相同的来源 ID、规范载荷和哈希。

载荷字段集扩展后，同一来源版本的重复投递必须接受按扩展前字段集计算的历史哈希，
避免仅因新增字段的默认空值改变哈希口径。兼容候选只能移除本次新增且输入仍为空或
默认值的字段；新增字段出现有效值时仍属于载荷变化，必须返回
`source_version_conflict`，不能以旧字段集哈希判为幂等。

动态事实哈希包含清洗后的 `rating_value`、`rating_scale`、`sales_rank` 和
`rank_scope`。它们与价格、币种及其他来源业务字段共同描述同一次观测；同一
`observed_at` 下任一字段变化必须产生不同哈希并返回 `source_version_conflict`。
派生的 `normalized_rating`、系统生成 ID、`created_at` 和租户认证上下文不进入哈希。

当前竞品部分写入路径只做了同键同哈希幂等或冲突，尚未全部复用
`source_versioning.decide_write` 的新旧版本判断。实施阶段必须统一，不另写第二套规则。

## 8. 查询与筛选语义

### 8.1 标准筛选

| 查询参数 | 数据来源 | 规则 |
|---|---|---|
| `category` | `competitor_identity.category` | 去空白后精确匹配，不区分大小写 |
| `brand` | `competitor_identity.brand` | 去空白后精确匹配，不区分大小写 |
| `price_min` / `price_max` | 最新 `competitor_price` | 任一价格范围出现时必须同时指定 `currency` |
| `currency` | 最新 `currency` | 三位大写；本阶段不自动换汇 |
| `rating_min` / `rating_max` | 派生 `normalized_rating` | 范围 0–5；不得读取 `match_score` |
| `sales_rank_max` | 最新 `sales_rank` | 必须同时指定 `rank_scope` |
| `status` | match status | 管理查询可选；分析固定为 approved |
| `custom_dimensions` | `competitor_identity.custom_dimensions` | 多个条件使用 AND；数值按 Decimal、布尔按 boolean、文本按规范字符串比较 |

`price_min <= price_max`、`rating_min <= rating_max`。没有评分或排名的数据不匹配对应
范围条件，但在未提供该条件时仍可返回。

### 8.2 最新动态事实

管理筛选对每个 `tenant_id + store_id + subject_sku + competitor_sku + entity_match_id`
选择 `observed_at` 最新的有效观察；相同时间以 `created_at` 作为稳定次序。历史接口
继续返回完整时间序列。

### 8.3 自定义维度

第一阶段将自定义维度存入 `competitor_identity_json`，使用 SQLite JSON 查询，不新增
维度表。该选择符合本地 SQLite 和当前数据规模，不引入第三方依赖。若未来真实数据
证明 JSON 查询无法满足容量或延迟 Gate，再以独立迁移和证据决定是否规范化成表。

## 9. 管理生命周期与 CRUD 语义

“完整 CRUD”按项目的不可变事实和审计约束解释：

- Create：创建 pending 同款候选，追加动态事实和内容证据。
- Read：读取单项、列表、历史和统一竞品视图。
- Update：外部事实通过更晚来源版本更新；人工裁决通过带
  `expected_record_version` 的状态迁移更新。
- Delete：不物理删除证据。错误同款关系转为 rejected；错误外部事实通过纠正版本
  取代，旧记录继续保留审计历史。

后台手动表单和逐条 API 在列名映射之后，必须复用 CSV 的规范行模型、字段校验、
来源版本判断和错误码。表单不得维护第二套更宽松的必填项、数值清洗或租户规则；
区别仅是 CSV 返回行号，而逐条 API 返回请求字段路径。

该语义避免物理删除绕过 D-014、D-025 和不可变历史要求。

## 10. 物理迁移影响

当前 `main` 的 `SCHEMA_VERSION = 25`；`CONTRIBUTING.md` 第 9 节已将 Schema 26
分配给本工作包。迁移必须将版本升级到 26，并使用唯一方法名 `_apply_v26`。

建议对 `competitor_observations` 增加以下可空列：

| 列 | SQLite 类型 | 约束 |
|---|---|---|
| `rating_value` | TEXT | null 或 Decimal >= 0 |
| `rating_scale` | TEXT | null 或 Decimal > 0；与 value 成对 |
| `sales_rank` | INTEGER | null 或 >= 1 |
| `rank_scope` | TEXT | null 或 1–200；与 rank 成对 |

`custom_dimensions` 位于现有身份 JSON 中，不需要新列。建议增加固定品类/品牌和最新
观察的查询索引，但不为任意自定义键创建无限索引。

迁移必须使用 `_ensure_column`，不得重建表；四列保持可空以兼容历史数据，并检查
所有本地与远端分支中不存在重复的 `_apply_v26` 方法名。

## 11. 任务内容对齐

| 工作包 3 具体工作 | 字段字典覆盖 | 设计结论 | 实现状态 |
|---|---|---|---|
| 任务正文：手动表单录入、完整 CRUD 与持久化 | 第 5、9 节 | 表单/API/CSV 复用规范模型；删除采用拒绝或纠正版本，不破坏审计历史 | 逐表 API 已有，统一表单和聚合 CRUD 待实现 |
| 1. CSV 批量导入、映射、清洗、字段错误、跳过异常行 | 第 6、7 节 | 已定义规范列、别名、清洗、行原子性和错误结构 | 未实现 |
| 2. 品类、品牌、价格、评分组合筛选 | 第 8 节 | 已定义字段来源、最新值和范围语义 | 未实现 |
| 3. 自定义维度参与查询 | 第 5.1、8.3 节 | 使用带类型和单位的 JSON 维度，支持文本/数值/布尔查询 | 未实现 |
| 4. 关联 F-305 自有商品 | 第 5.2 节 | `tenant_id + store_id + subject_sku` 服务层校验并保存快照 | 关联字段现有，存在性校验待补 |
| 5. 校验规则与错误契约 | 第 5–7 节 | 已定义字段约束、成对字段和稳定错误结构 | 未实现 |
| 6. 后台导入、筛选和裁决 | 第 5.7、8、9 节 | 统一视图和管理生命周期已定义 | 未实现 |
| 7. 核对 F-301 注册表状态 | 第 12 节 | 完整能力上线前不得仅因字段设计完成而更新为完成 | 待实现后核对 |

## 12. 具体需求与验收标准对齐

| 要求/验收 | Schema 支撑 | 完成判定所需证据 | 当前结论 |
|---|---|---|---|
| 标准字段和自定义维度扩展 | 第 5 节覆盖全部标准字段及 typed dimensions | 模型校验、持久化与查询测试 | 设计满足，代码待实现 |
| 手动表单录入、完整 CRUD 与持久化 | 第 5、9 节统一表单/API/CSV 契约并定义不可变删除语义 | 表单、API、数据库读回和历史保留测试 | 设计满足，现有逐表 API 待整合 |
| 常见 CSV 列映射、数值清洗、异常行跳过 | 第 6 节 | 正常/异常混合 CSV 定向测试和反证 | 设计满足，代码待实现 |
| 品类/品牌/价格/评分组合筛选 | 第 8 节 | 单项、组合、空值、边界和租户隔离测试 | 设计满足，代码待实现 |
| 关联 F-305 自有商品 | 第 5.2 节 | 不存在、跨租户、deleted SKU 拒绝测试 | 设计满足，校验待实现 |
| 明确字段级错误 | 第 6.4 节 | `row/field/code/message` 断言 | 设计满足，代码待实现 |
| 跨租户不可互读 | 所有对象强制 `tenant_id` | 双租户 API、服务层和 CSV 测试 | 现有基线需扩展覆盖新接口 |
| 乐观锁裁决冲突 | 第 5.2、5.3 节 | 并发裁决只有一个成功，其余 409 | 现有能力和测试已具备 |
| API 或 CSV 录入并持久化 | 第 5–7 节 | API 端到端测试和数据库复核 | API 逐条录入已有，CSV 待实现 |
| 重复导入幂等、旧版本拒绝、同版本冲突 | 第 7 节 | 四种 D-014 分支测试和反证 | 部分现有，CSV 和统一版本判断待实现 |
| 自定义维度可扩展并参与查询 | 第 5.1、8.3 节 | 文本/数值/布尔三类筛选测试 | 待实现 |
| 竞品可关联自有基准商品 | 第 5.2 节 | 同租户有效 SKU 通过，错误范围拒绝 | 字段现有，严格校验待实现 |
| 未批准数据不进入分析 | 第 2.1、5.7 节 | 移除门禁后测试必须失败，再还原复验 | 现有门禁和测试基线已具备，新增统一视图需复验 |

字段字典在设计层面覆盖工作包 3 的任务内容、具体需求和七项验收标准；它不把
“设计覆盖”冒充“功能验收通过”。真正验收仍需要迁移、领域服务、API、后台、测试、
反证、全量回归和独立验收人员的证据。

## 13. 当前代码差距清单

1. `competitor_observations` 尚无商品评分和销量排名字段。
2. `CompetitiveProductIdentity.attributes` 只能保存字符串，尚无 typed custom dimensions。
3. 列表接口只支持部分店铺/SKU/status 条件，尚无品类、品牌、价格、评分组合筛选。
4. 竞品模块尚无 CSV 导入入口和逐行字段错误契约。
5. 部分竞品写入路径尚未完整复用 `decide_write` 的旧/新版本判断。
6. `subject_sku` 已作为关联字段，但 match 创建时尚未严格验证 F-305 同租户同店铺商品。
7. 统一竞品管理视图尚未实现。
8. 后台尚无 CSV 上传和完整多维筛选控件。

## 14. 实施顺序

1. 按最新工作包边界冻结本字段字典，并将查询接口契约同步给工作包 2 承接人。
2. Schema 26 已在 `CONTRIBUTING.md` 登记；迁移固定使用 `_apply_v26`。
3. 先以测试锁定字段模型、D-014、F-305 关联和租户隔离。
4. 实施数据库迁移和领域模型。
5. 实施统一查询、组合筛选和管理视图。
6. 实施 CSV 解析、行级事务、错误契约和审计。
7. 实施后台上传、筛选和裁决操作。
8. 执行定向测试、CSV 校验反证、D-025 门禁反证、全量测试和独立验收。
