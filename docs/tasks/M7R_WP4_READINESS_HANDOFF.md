# M7-R WP4 数据准备度、只读工作台与 Demo 交接

> 状态：2026-08-19 开发候选。本文说明 WP4 的接口、状态语义和复验入口，不替代缪海南
> 执行的 M7-R WP5 独立验收、真实平台样本确认、真实经营结论或生产放行。

## 权威入口

- 准备度版本、八类报表时效门槛和四项明确缺口：
  `src/ecommerce_agent/readonly_readiness/policy.py`。
- 只读聚合投影：
  `src/ecommerce_agent/readonly_readiness/service.py` 的 `ReadonlyReadinessService`。
- 脱敏 Demo 装载与固定数据集：
  `src/ecommerce_agent/readonly_readiness/demo.py` 和
  `fixtures/m7r_readonly_demo_v1.json`。
- 管理员 API：`src/ecommerce_agent/readonly_data_api.py`。
- 人工映射与对账列表读侧：
  `src/ecommerce_agent/product_identity/service.py` 的 `list_mappings` 和
  `list_reconciliations`。
- 页面入口：`docs/admin-console.html` 的“数据准备度”。

WP2 的报表 domain、grain、amount unit 和 mapping version 仍以 `REPORT_ADAPTERS` 为唯一
事实源；WP4 只新增管理视图的时效策略，并在导入时交叉校验八类报表集合。页面只渲染 API
返回状态，不复制准备度判断。

## 准备度投影契约

调用 `ReadonlyReadinessService.project(tenant_id, store_id=..., scope=...)`，或请求
`GET /v1/readonly-data/readiness`。所有读取同时受 tenant、store 和 data scope 约束：

- `operational`：只包含 `actual` / `manual`，默认排除 Demo；
- `demo`：只包含 `demo`；
- `all`：调用方显式请求两类范围；
- 没有 manifest 的数据域显示 `source.kind=missing`，不把缺失转成零或伪造导入记录。

每个数据域返回报表和领域名称、粒度、单位、mapping version、来源类型与系统、覆盖起止、
report period、水位、时效、最新 manifest 质量、商品映射状态，以及完整 `import_ids` 追溯。
商品身份摘要保留 reconciliation `run_id`、策略版本、四终态计数和映射事件数；缺口保留
field evidence 与 import 引用。

状态由 `readonly-readiness-v1` 统一解释：

| 层级 | 状态 | 语义 |
|---|---|---|
| 时效 | `missing` / `fresh` / `stale` / `future` | 无水位、在门槛内、超门槛、或水位晚于查询时点 |
| 数据域 | `missing` / `ready` / `attention` | 无 manifest；或时效、质量、映射均通过；其余需关注 |
| 商品身份 | `not_run` / `matched` / `attention` | 未对账；全部逐行匹配；或存在歧义、未映射、拒绝 |
| 整体 | `missing` / `ready` / `attention` | 八域全缺；或八域、四缺口和身份均通过；其余需关注 |

当前最大水位年龄为：商品 72h、库存 24h、订单 48h、物流 24h、经营 48h、营销 48h、
退款 48h、结算 840h。阈值变化必须升级准备度策略版本，不得在页面或下游另建一套判断。
查询响应固定声明 `read_only=true`、`model_used=false` 和
`platform_write_performed=false`。

## 四项明确缺口

准备度 API 与页面从 WP1 field evidence 投影以下字段：

- `purchase_cost`：进货成本；
- `purchase_order`：采购单；
- `transport_cycle`：运输周期；
- `refurbishment_cost`：翻新成本。

没有相应 evidence 时为 `missing/open=true`；存在 `actual`、`manual` 或 `demo` 证据时关闭
对应缺口，并回显证据状态、时间、原因、`evidence_id` 与 `import_id`。Demo 数据集不会为了
“看起来完整”而填充这四项，因此端到端 Demo 的整体状态诚实保持 `attention`。

## 管理员 API

以下端点均要求既有管理员认证，并按认证中的 tenant 限定数据：

| 方法与路径 | 用途 | 写入边界 |
|---|---|---|
| `GET /v1/readonly-data/readiness` | 统一准备度投影 | 只读 |
| `GET /v1/readonly-data/imports` | 导入 manifest | 只读 |
| `GET /v1/readonly-data/row-issues` | 隔离/拒绝行 | 只读 |
| `GET /v1/readonly-data/field-evidence` | 字段证据四态 | 只读 |
| `GET /v1/readonly-data/mappings` | 人工映射事件 | 只读 |
| `GET /v1/readonly-data/reconciliations` | 对账 run 摘要 | 只读 |
| `GET /v1/readonly-data/demo` | Demo 数据集元信息 | 只读 |
| `POST /v1/readonly-data/demo/load` | 显式装载隔离 Demo | 只写本地 Demo 事实并记录管理员审计 |

列表端点 `limit` 为 1～1000；映射默认只返回每个平台 SKU 的最新裁决。读取未知 tenant、
其他店铺或不匹配 scope 均返回空投影，不跨范围回退。

下游应直接消费，例如：
`GET /v1/readonly-data/readiness?store_id=<store>&scope=operational`。需要唯一料号时，还必须
检查 `product_identity.status=matched` 并保留 `run_id`；不得只看“报表已导入”就自行推导
SKU 映射，也不得把 `attention` / `missing` 转成零。

## Demo 装载与重放

唯一当前 fixture 为 `m7r-readonly-demo-v1`。请求体必须显式提供
`fixture_id`、`store_id` 和 `confirm_demo=true`。装载过程复用 WP2 八类报表导入公开服务、
WP3 canonical 商品/人工确认/领域对账公开服务，不直写领域底表；fixture 不含顾客姓名、
电话、地址或顾客自由文本，并继续经过 WP1/WP2 隐私与白名单门禁。

首次运行写入 8 个 Demo manifest、1 个 Demo canonical 商品、1 个映射事件和 1 个对账 run；
同 tenant/store 重放返回 idempotent，不增加重复事实。并发双重放同样只有一份不可变事实。
响应会核对装载前后的 operational import ID 集合并回显
`operational_scope_unchanged=true`。Demo 不调用模型、不访问千牛/浏览器登录态、不连接平台，
也不执行任何平台写动作。每次显式管理员操作会各自追加一条审计事件；审计历史增长不属于
重复经营事实，事件会明确区分首次 applied 与后续 idempotent 重放。

fixture 使用固定、带时区的 2026-08-19 水位，以保证输入摘要和重放结果稳定。随着真实时间
推进，它会按同一时效策略自然显示为 stale；不得把 fixture 时间改成“当前时间”来伪造新鲜度。

## 页面与下钻

后台“数据准备度”页面默认查询 `operational`，进入页面和点击“只读查询”只调用上述 GET。
只有管理员点击“显式装载安全 Demo”才调用 POST，成功后页面显式切换到 `demo` scope。
页面展示八域准备度、四项缺口、商品映射、manifest、隔离行和字段证据；域级数值通过
`import_id` 回到 manifest，映射通过 source import 与 reconciliation run 回溯。

2026-08-19 使用临时数据库和测试管理员完成浏览器自测：

- 初次打开页面前后，manifest / mapping / reconciliation 均为 0，证明无隐式写入；
- 显式 Demo 后为 8 个 manifest、66 条字段证据、1 个商品、1 个映射事件、1 个对账 run、
  3 条对账行；八域均为 passed/fresh，身份为 3/3 matched，四项缺口保持开放；
- 浏览器再次显式重放后上述计数完全不变，operational 仍为空；
- 1280×720 下页面无横向溢出；390×844 下表格只在 panel 内横向滚动，筛选和按钮可操作；
- 装载及重放后 console error/warning 为 0。

## 复现与验证

聚焦测试：

```bash
.venv/bin/python -m pytest -q tests/test_m7r_wp4_readiness.py
```

测试覆盖策略注册表交叉校验、空投影零写入、Demo 幂等与并发、tenant/store/scope 隔离、
fresh/stale/future、manifest partial 质量、字段证据关闭缺口、管理员认证、Demo 写审计、所有
GET 零事实变更、映射历史 active 真值和页面显式动作。开发过程中保留了缺模块、fixture
同时点领域冲突、缺审计事件和已撤销旧确认误标 active 的先红后绿证据。最终聚焦
`9 passed`、WP1～WP4 关联集 `108 passed`、仓库全量
`1034 passed, 24 warnings`（687.63 秒），无 failed/skipped/xfailed；24 条均为既有
Traffic Lab 重复 Operation ID 告警。完整证据记录在 E-20260819-001。

## 兼容与未完成边界

- 沿用 schema v35，无迁移、第三方依赖或灾备 manifest 变化；未编辑 schema 占号表。
- 新增 API、服务属性和后台 view 均为 additive；既有 API 响应、Agent/LangGraph、intent、
  prompt、模型语义权威和平台动作不变。
- WP2 仍使用通用 `generic-cn-v1`；仓库没有经授权真实平台导出，不能声称淘宝/天猫字段全覆盖。
- 当前没有采购成本、采购单、运输周期或翻新成本的真实数据入口；准备度只诚实暴露缺口。
- WP4 只形成开发候选。缪海南仍须从干净状态执行 WP5 独立反例、浏览器和完整回归后，才可
  签署 M7-R；Demo、真实平台接入和生产放行不得互相替代。
