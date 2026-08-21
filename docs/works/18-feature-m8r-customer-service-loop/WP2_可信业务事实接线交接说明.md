# M8-R WP2 可信业务事实接线交接说明

## 基本信息

- 模块：M8-R 销售与售后客服闭环
- 工作包：WP2 商品、订单、库存、履约物流快照可信上下文接线
- 开发负责人：谢良璇
- 当前状态：开发侧代码、自动化与谢良璇 8 步人工黑盒验收已完成；不替代缪海南 WP5 独立验收
- 基线：`454b35c9000ab279ffdbf115f80afdf3e031ee73`

## 本工作包解决的问题

WP2 不负责理解客户意图或生成最终客服话术。它只负责在模型使用业务事实前，提供一层可验证、
可追溯、按身份范围隔离的最小只读投影。

- 销售事实：商品、SKU、售价、库存可用量和在途量。
- 售后事实：订单状态、支付状态、订单行、物流状态和退款/售后状态。
- 安全事实：租户、店铺和订单范围，来源类型，数据时间，新鲜度，缺失状态。
- 历史事实：订单更正前的旧版本仍可读取，但明确标记为 `superseded`，不能冒充当前状态。

## 新增工具

### `get_customer_sales_facts`

输入：

```json
{
  "store_id": "store-a",
  "sku_id": "SKU-1"
}
```

调用前必须存在可信 `authorized=true` 和 `shop_id`，输入店铺必须与可信店铺一致。

### `get_customer_after_sales_facts`

输入：

```json
{
  "store_id": "store-a",
  "order_id": "ORDER-1",
  "include_history": true
}
```

调用前必须存在可信 `authorized=true + order_id + shop_id`，输入订单号和店铺号必须同时匹配。
这落实 D-015，不能只凭一个 `authorized` 布尔值读取任意订单。

## 客服字段白名单

权威定义位于 `src/ecommerce_agent/customer_service_facts.py` 的
`CUSTOMER_SERVICE_FIELD_WHITELISTS`。

| 子对象 | 允许字段 |
|---|---|
| product | sku_id、title、status、sale_price、currency |
| inventory | available_quantity、inbound_quantity |
| order | order_id、order_status、payment_status、currency、total_amount、placed_at、lines |
| order_line | sku_id、title、quantity、unit_price |
| logistics | carrier、status、last_event、last_event_at |
| after_sale | case_type、status、requested_amount、approved_amount、reason_code、opened_at、updated_at |

`buyer_ref_hash`、运单号、内部行号、内部售后单号、仓库号、原始 source_id、payload hash 和商品
任意 attributes 不进入客服投影。允许的自由文本在投影边界再次脱敏；完整手机号不会出现，
必要上下文中只允许保留 `138****8000` 这类脱敏形式。

## 状态语义

- `available`：所需事实存在；是否可当作当前事实仍必须看 freshness。
- `partial`：部分事实存在，缺失项列在 `missing`，数值保持 `null`，不补成 0。
- `missing`：在当前租户/店铺/订单范围内没有事实。
- `blocked`：来源不明、商品身份冲突或对象范围不可信，`facts` 为空。

freshness 复用 `evidence-freshness-v1`，阈值复用 M7-R 的
`READINESS_REPORT_POLICIES`。过期或未来时间的快照会设置
`usable_as_current=false` 并保留 `data_as_of`，供 WP3 禁止生成“刚刚”“当前已发货”等实时措辞。

source provenance 复用 `source-provenance-v1`：

- M7-R import manifest 的 `actual/manual` 映射为 `operational`。
- manifest 的 `demo` 或明确虚拟 Connector 映射为 `virtual`。
- 缺少来源、无法匹配 manifest 或未知 Connector 映射为 `unknown`，并安全阻断事实输出。

商品身份复用 M7-R `ProductIdentityService`。只有最新事件为 `confirmed` 时才返回
`canonical_product_id`；参与同一投影的每个 Connector/SKU 来源都必须确认且指向同一商品。
任一来源未确认、已撤销或确认结果冲突时保持 `unmatched`/`conflict`，不会借用其他来源的身份。

## 实现边界

- 只调用 `CatalogService`、`InventoryService`、`OrderService`、`ReadonlyDataService` 和
  `ProductIdentityService` 等公开服务，不直接读取或写入业务表。
- 新工具通过既有 `ToolRegistry` 注册，后续由既有 Agent 工具链和 `ContextBuilder` 形成不可变上下文快照。
- 没有新增数据库表或列，不占 schema 号。
- 没有新增第三方依赖、前端、外部模型调用、平台写动作或客户回复语义路由。
- 未来实时接口只需继续写入现有领域服务并提供可识别 provenance，不改变客服投影契约。

## 开发侧验证

- WP2 新增契约测试：字段白名单、租户/店铺/订单 Gate、缺失不补零、过期快照、
  operational/demo/unknown 来源、全来源商品身份确认、隐私投影、稳定阻断结构和订单历史更正。
- 实际 mutation：临时移除可信订单号比较后，目标用例因未抛出 `order_scope_mismatch` 如期失败；
  临时恢复“任一来源确认即可”的旧商品身份判断后，跨来源反例因错误返回 `confirmed` 如期失败；
  两处恢复正式实现后目标用例均通过。
- 当前代码哈希上的有效复验为：WP2 + 模块登记 `17 passed`，M7 商品/订单/导入直接依赖
  `13 passed`，商品身份 `17 passed`，ContextBuilder + WP1 `19 passed`，合计 `66 passed`。
- compileall 与 `git diff --check` 已通过。
- 早期一次未拆组关联运行达到 5 分钟工具时限后被中止；最终收尾时一次广覆盖依赖复跑达到
  15 分钟仍无完整统计；两次均作废且不计通过。上述 66 项均为拆组取得的退出码 0 结果。
- 仓库完整全量另行运行 15 分钟仍未取得最终统计，已按证据规则作废；当前不声称全量通过。
- `WP2_人工验收助手.ps1 -AutoConfirm` 已在 F 盘完成 8 步开发侧演练，结果为
  `developer_rehearsal_passed`。该模式只证明验收工具可运行，不替代谢良璇人工确认。

## 人工验收

使用同目录：

- `WP2_人工验收指南.md`
- `WP2_人工验收助手.ps1`
- `WP2_人工验收场景.py`

2026-08-20 17:17:01 +08:00，谢良璇在无 `-AutoConfirm` 模式下完成 8 步逐项观察：

- `confirmation_mode=human`
- `automatic_contract_checks=passed`
- 8/8 observations 为 `confirmed=true`
- `human_observations_passed=true`
- `final_status=human_accepted`
- `external_model_called=false`

正反例对应关系：

| 能力 | 正例 | 反例 |
|---|---|---|
| 销售事实 | 商品、价格、库存正常返回 | 缺库存保持 `missing/null`，不补零 |
| 新鲜度 | 当前快照可使用 | 五天旧快照为 `stale`，不可冒充当前 |
| 来源 | 已登记虚拟来源明确为 `virtual` | 未知来源 `blocked` 且 `facts={}` |
| 范围 | 正确 tenant/store/order 返回售后事实 | 错订单号、错店铺号在执行前阻断 |
| 隐私 | 订单、物流、退款必要事实可见 | 内部字段和完整手机号不可见，手机号仅保留脱敏形式 |
| 历史 | 版本 2 为唯一 `current` | 版本 1 保留但为 `superseded` |
| 租户 | tenant-a 可读自身 SKU | tenant-b 对同一 SKU 只得到 `missing` |

证据：

- 结果：`F:\CodexProjects\yunpai-ecommerce-agent-m8r-runtime\wp2-manual-evidence\谢良璇_WP2人工验收结果_20260820-171701.json`
- 过程：`F:\CodexProjects\yunpai-ecommerce-agent-m8r-runtime\wp2-manual-evidence\谢良璇_WP2人工验收过程_20260820-171701.txt`

本结论只关闭 WP2 开发侧人工验收。WP3、WP4 和完整 M8-R PR #25 已形成开发侧候选；
缪海南 WP5、项目负责人审阅/合入、真实渠道/数据、长稳和生产 Gate 仍未完成。
