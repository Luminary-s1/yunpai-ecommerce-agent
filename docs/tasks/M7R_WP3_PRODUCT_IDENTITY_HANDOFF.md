# M7-R WP3 商品身份与对账交接

> 状态：2026-08-18 开发候选。本文说明 WP3 的代码边界和下游接入方式，不替代 M7-R
> WP5 独立验收、真实平台样本确认或生产放行。

## 权威入口

- 枚举、输入模型、对账上限与当前策略版本：
  `src/ecommerce_agent/product_identity/models.py`。
- canonical 商品、映射裁决、撤销、历史查询和对账实现：
  `src/ecommerce_agent/product_identity/service.py` 的 `ProductIdentityService`。
- schema v35 表、复合范围外键和不可变触发器：
  `src/ecommerce_agent/database.py` 的 `Database._apply_v35`。
- WP2 领域来源 ID 与 WP1 manifest 的唯一解析入口：
  `src/ecommerce_agent/readonly_data/ingestion.py` 的 `source_manifest_key`。

字段、状态、证据键和策略版本不在本文复制为第二份可变注册表；调用方应从公开 Python
类型读取。当前代码只接受 `product-identity-v1`，读到未知策略版本会显式拒绝，不会按当前
规则静默解释历史数据。

## 身份与范围语义

canonical 商品以 `(tenant_id, store_id, internal_part_number)` 唯一；稳定
`canonical_product_id` 同时包含 tenant 和 store 范围。同一内部料号在不同租户或店铺会
得到不同身份，任何确认、撤销、查询和对账都必须显式带 store scope。

平台映射键为 `(tenant_id, store_id, connector_id, sku_id)`。每个事件保存平台 SKU、可选
item ID、可选商家编码、canonical 商品、`mapping_version`、调用方看到的
`expected_version`、人工原因、脱敏 actor 引用和可选 import manifest 引用。映射事件只追加：

- `confirmed` 建立或显式改判当前映射；
- `revoked` 撤销当前确认，但不删除原事件；
- `decision_key` 保证同一完整裁决重放幂等；同 key 不同载荷拒绝；
- `expected_version` 是乐观锁，旧版本裁决不能覆盖并发新证据。

`source_kind=demo` 的 canonical 商品不进入默认 operational 候选或映射快照。

## 候选、人工裁决与对账

`reconcile` 接受逐行商品身份观测；`reconcile_domain` 从现有公开
`CatalogService`、`InventoryService` 和 `OrderService` 读投影，不直接读取或改写这些领域
底表。每个输入行恰好进入一个终态：

- `matched`：存在仍有效的人工确认，并输出稳定 `canonical_product_id` 和
  `internal_part_number`；
- `ambiguous`：多个候选、商家编码与标题证据冲突，或已确认映射与新 item/商家编码冲突；
- `unmapped`：零候选、唯一候选仍待人工确认，或映射已撤销；
- `rejected`：行结构非法或跨店。

标题只做 NFKC 归一化后的完全匹配候选证据，不建立正式绑定。即使商家编码或标题只有一个
候选，结果仍为 `unmapped/manual_confirmation_required`，必须调用 `confirm_mapping`。
每行同时固化 `evidence_keys`、候选 ID、原因和原始行摘要；非法行只保存摘要和安全原因，
不回显原始载荷。

对账 run 由输入摘要、数据范围、策略版本和当前商品/映射快照摘要稳定决定。同输入与同快照
重放返回原 run；映射确认、改判或撤销后产生新 run，旧 run 和旧明细仍可读取。读侧会核对
总行数、连续行号和四类状态计数，缺行或计数漂移会显式失败。

## operational / demo 来源隔离

WP2 领域表早于来源类型列，但其 `source_id` 由 WP2 单一入口编码报表类型和内容摘要前缀。
`reconcile_domain` 通过 `source_manifest_key` 回到 WP1 manifest 的 `source_kind`：

- operational 只消费 manifest 为 `actual/manual` 的 WP2 事实；
- demo 只消费 manifest 为 `demo` 的 WP2 事实；
- all 显式消费两者；
- 形似 WP2 readonly ID 但无法关联 manifest 的事实在 scoped 视图中 fail closed；
- 历史上非 readonly 的手工领域 source ID 为兼容既有公开服务，仅进入 operational/all，
  不会被当成 demo。

若所选范围没有任何商品、库存或订单行，`reconcile_domain` 返回
`product_reconciliation_source_empty`，不制造空报告或把缺失冒充零。

## 公开调用

- 商品：`register_product`、`get_product`、`list_products`；
- 映射：`confirm_mapping`、`revoke_mapping`、`get_latest_mapping`、`mapping_history`；
- 对账：`reconcile`、`reconcile_domain`、`get_reconciliation`。

M9-R/M10-R 必须消费 `matched` 行固化的 `canonical_product_id` 与
`internal_part_number`，并保留 `run_id`、`policy_version` 和 `mapping_snapshot_digest` 作为
证据引用。不得按标题、当前 catalog 顺序或自建 SKU 字典重新推导料号；`ambiguous`、
`unmapped` 和 `rejected` 都不能进入依赖唯一料号的经营、预测、补货或订购单正式链路。

## schema 与兼容

v35 additive 新增四张表：canonical 商品、映射事件、对账 run 和逐行明细。四表均禁止
UPDATE/DELETE；复合外键约束 tenant/store/product/import/event/run 范围。v34 到 v35 只新增
对象，不重建既有表，重复初始化不会重复应用迁移。

灾备 manifest 精确匹配 schema。升级前应使用旧程序完成停机备份；升级到 v35 后、恢复业务
写入前立即生成并验证新的全量备份。旧程序与旧归档保留到隔离恢复演练通过，不能使用 v35
程序把旧 manifest 冒充兼容。

PR #11 的 v31 尚未进入当前 main。后续合并必须同时保留 v31、v32、v33、v34 和 v35 的
独立迁移块及方法，不能整块选择任一分支覆盖 `database.py`。

## 当前限制与复验

- 本工作包不含 HTTP API、后台页面或准备度聚合；这些属于 WP4。
- 仓库和会话附件仍没有经授权的淘宝/天猫真实脱敏导出，因此当前只证明 WP2
  `generic-cn-v1` 与 canonical 映射契约，不宣称真实平台字段已全覆盖。
- 旧的非 readonly 手工领域事实没有 WP1 manifest 来源四态，只按上述兼容规则处理；正式
  M7-R Demo 应经 WP2 导入，不应依赖该兼容路径。
- 不包含自动平台写回、商品改动、采购、付款、库存调整或生产放行。

开发复验入口：

```bash
.venv/bin/python -m pytest tests/test_product_identity.py -q
.venv/bin/python -m pytest \
  tests/test_product_identity.py \
  tests/test_readonly_data_ingestion.py \
  tests/test_readonly_data_contract.py \
  tests/test_migrations.py \
  tests/test_disaster_recovery.py -q
```

红灯证据覆盖缺实现、证据键/审计列缺失和 Demo 领域事实混入 operational；聚焦、关联与
最终全量结果以项目账本中的最新 WP3 证据为准。
