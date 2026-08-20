# 电商后台数据库与经营数据接入实施指南

> 文档用途：供现场调研、方案评审、连接器开发、联调、独立测试和后续交接使用。
>
> 调研基线：2026-08-20，源代码基线 `454b35c`。运行时契约可能继续演进，字段、枚举、
> 适配器和配置的最终权威来源始终是本文引用的代码，而不是本文中的示例。
>
> 生产声明：本文不代表真实淘宝/天猫、ERP、OMS、WMS 或客户数据库已经接通，也不豁免
> 平台资质、客户授权、安全评审、数据对账、长稳和生产发布门禁。

## 1. 决策摘要

接客户电商后台时，默认方案是：

1. 客户后台、官方平台、ERP/OMS/WMS 或客户只读数据库继续作为外部事实源。
2. 在云湃系统边界增加真实 Connector 或只读报表适配器。
3. 将外部字段规范化为商品、库存、订单、物流、售后、经营、营销和结算等领域模型。
4. 通过现有领域服务写入云湃自己的运行库，并保留来源版本、幂等、隔离和审计证据。

**明天现场接入不应全量替换本系统 SQLite，也不应让 Agent 直接查询客户业务库。**

只有客户明确要求云湃自身运行在 PostgreSQL/MySQL、需要多进程并发或集中式高可用时，
才另立“内部持久层迁移”项目。那不是修改一个连接串能够完成的工作。

## 2. 三类问题必须分开

| 问题 | 正确处理 | 是否需要替换云湃 SQLite |
|---|---|---:|
| 读取客户商品、库存、订单等经营数据 | 实现 Connector、API/ERP 适配器或只读数据库适配器 | 否 |
| 客户只能导出 CSV/XLSX | 使用只读报表导入服务并补平台 mapping version | 否 |
| 客户要求云湃自身改用 PostgreSQL/MySQL | 单独进行持久层、SQL 方言、迁移、checkpoint、备份恢复改造 | 是，且属于独立项目 |

淘宝客服 OAuth、奇门消息入站、TOP 回复和人工接管属于“客服渠道接入”，不等于商品、
订单和库存的数据同步。两条链路可以共用租户和店铺身份，但必须独立联调和验收。

## 3. 当前仓库能力真相

### 3.1 已有能力

- 统一 Connector 协议已经定义 `capabilities`、`test_connection`、`pull`、
  `verify_webhook`、`execute` 和 `verify`。权威定义见
  [`connectors/base.py`](../../../src/ecommerce_agent/connectors/base.py)。
- 管理端已经暴露 Connector 目录、连接测试、拉取同步、动作执行和 Webhook 入口。权威路由见
  [`operations_api.py`](../../../src/ecommerce_agent/operations_api.py)。
- Connector 拉取结果可以规范化写入商品、订单、库存、竞品和 Traffic Lab 等现有领域服务，
  并记录同步运行、游标、收取数量、应用数量和审计事件。权威编排见
  [`business/service.py`](../../../src/ecommerce_agent/business/service.py)。
- 只读报表服务已经具备 CSV/XLSX 解析、字段白名单、逐行隔离、来源版本、批次部分成功和
  领域服务写入能力。权威实现见
  [`readonly_data/adapters.py`](../../../src/ecommerce_agent/readonly_data/adapters.py) 和
  [`readonly_data/ingestion.py`](../../../src/ecommerce_agent/readonly_data/ingestion.py)。
- 运行库路径由 `DATA_DIR` 决定，业务库为 `agent.sqlite3`，LangGraph checkpoint 库为
  `checkpoints.sqlite3`。权威配置见
  [`config.py`](../../../src/ecommerce_agent/config.py)。

### 3.2 尚未完成的能力

- 当前 `OperationsService` 只注册了 `VirtualTaobaoConnector`；它明确
  `virtual=true`，不访问真实平台或客户数据库。
- 当前没有可填写 DSN 后直接使用的通用 MySQL、PostgreSQL、SQL Server 或 Oracle Connector。
- 当前没有经过真实淘宝/天猫脱敏导出样本确认的平台专属字段 mapping。
- 当前只读数据 HTTP API 提供准备度、导入记录、问题行、字段证据、身份映射和 Demo 接口，
  但没有正式的文件上传/导入 `POST` 入口。现有路由见
  [`readonly_data_api.py`](../../../src/ecommerce_agent/readonly_data_api.py)。
- 当前 Connector 同步由管理 API 或代码显式触发，没有面向真实客户数据源的周期调度器。
- 当前内部 `Database` 直接依赖 `sqlite3`、WAL、PRAGMA、`BEGIN IMMEDIATE` 和 SQLite
  备份语义，不是可切换数据库驱动。权威实现见
  [`database.py`](../../../src/ecommerce_agent/database.py)。

## 4. 推荐目标架构

```text
客户官方 API / ERP API / 只读数据库 / 受控 CSV/XLSX
                         │
                         ▼
              客户专属 Connector / Mapping
                         │
                 来源校验与规范化
                         │
       ┌─────────────────┼─────────────────┐
       ▼                 ▼                 ▼
     商品/库存          订单/物流/售后      经营/营销/财务
       └─────────────────┼─────────────────┘
                         ▼
                 云湃公开领域服务
                         ▼
          DATA_DIR/agent.sqlite3（当前 V1）
                         ▼
                API / 管理后台 / Agent
```

关键边界：

- Agent 只能读取云湃已经完成租户隔离、规范化和审计的领域事实。
- 领域模块不直接保存客户数据库连接，也不直接执行第三方 SQL/HTTP。
- Connector 负责外部连接、分页、游标、签名和外部状态；领域服务负责内部业务约束。
- 客户数据库表名和列名不得扩散到 Agent Prompt、工具 schema 或业务核心表。
- 写回外部平台必须走独立 `execute → verify` 路径，并满足权限、幂等、后置验证和人工补偿。

## 5. 接入方式选择

优先级建议如下：

1. **官方开放平台/API**：身份、权限、限流、增量和回写边界最明确，优先采用。
2. **客户已有 ERP/OMS/WMS API**：由客户系统统一屏蔽平台差异，通常比直连业务库稳定。
3. **客户只读数据库或只读视图**：适合客户可控内网环境；必须使用最小权限和固定查询。
4. **CSV/XLSX 报表**：作为只读兜底或首期快速验证，不冒充实时数据接入。
5. **RPA、Cookie、客户端注入、数据库超级账号**：不属于本项目允许的产品路线。

现场确认数据源后，只选择一条生产主路径；不要长期维护 API、数据库直连和文件导入三套
彼此竞争的生产真相。文件导入可以保留为灾备或人工补数路径，但要明确优先级和审计来源。

## 6. 现场必须获取的信息

### 6.1 系统与网络

- 平台或系统名称：淘宝/天猫、京东、抖店、拼多多，或具体 ERP/OMS/WMS。
- 接入方式：官方 API、服务商 API、数据库、SFTP 文件或人工导出。
- 数据库类型、版本、字符集、时区和部署位置。
- 是否需要 VPN、堡垒机、SSH Tunnel、专线或 IP 白名单。
- 测试、预生产和生产环境是否隔离。
- 客户侧技术负责人、业务口径负责人和安全负责人。

### 6.2 数据契约

- 店铺、租户、渠道的唯一标识和对应关系。
- 商品 ID、平台 SKU、商家编码、物料号之间的映射关系。
- 订单、子订单、支付单、物流单和售后单的主外键关系。
- 仓库、库存地点和可售/现有/占用/在途库存口径。
- 金额单位、币种、含税/不含税、退款和平台费用口径。
- 状态枚举及状态迁移说明。
- 所有业务时间字段的时区、精度和更新时间语义。
- 全量数据规模、日增量、峰值和历史回溯范围。
- 删除、关闭、撤销和冲正如何表达。

### 6.3 增量与可靠性

- 是否有 `updated_at`、自增流水号、CDC、消息队列或官方游标。
- 增量字段是否会回写旧记录，是否可能同一时间戳多条记录。
- 是否支持按稳定主键进行 keyset pagination。
- 是否能取得一次同步的高水位，防止同步过程中数据不断变化。
- 接口限流、超时、重试和 token 生命周期。
- 客户可接受的同步延迟、停机窗口和对账频率。

### 6.4 样本与隐私

- 每个数据域至少一份脱敏样本或受控测试库。
- 完整字段字典、枚举字典和口径说明。
- 明确哪些字段包含姓名、手机号、地址、身份证、原始评论或其他个人信息。
- 默认不接入姓名、手机号、地址、原始评论和评论者身份。
- 真实密钥、密码和 token 通过现场密钥系统或进程环境交付，不进入 Git、文档或聊天记录。

## 7. 直接读取客户数据库的实施规范

### 7.1 客户侧账号和视图

客户应提供专用只读账号，推荐只授权给专门的集成视图。账号要求：

- 仅允许 `SELECT`；禁止 `INSERT`、`UPDATE`、`DELETE`、DDL、存储过程执行和管理员权限。
- 限定数据库、schema、视图、来源 IP 和连接时段。
- 使用 TLS、VPN、专线或受控隧道；禁止把数据库直接暴露到公网。
- 设置连接超时、查询超时和最大并发，避免影响客户生产库。
- 连接测试只执行 `SELECT 1`、权限检查和视图元数据检查，不读取大批业务数据。

推荐由客户创建以下逻辑视图；名称只是建议，不是云湃硬编码要求：

| 建议视图 | 内容 |
|---|---|
| `v_yunpai_catalog` | 店铺商品、SKU、状态、售价和商家编码 |
| `v_yunpai_inventory` | 仓库 SKU 库存快照 |
| `v_yunpai_orders` | 订单头、状态、金额和业务时间 |
| `v_yunpai_order_lines` | 子订单/订单行和 SKU |
| `v_yunpai_fulfillment` | 物流状态和脱敏运单信息 |
| `v_yunpai_after_sales` | 售后、退款和状态 |

视图应屏蔽顾客姓名、手机号和地址。若某个业务流程必须关联顾客，只提供客户批准的稳定
匿名引用；不要把原始顾客身份写入云湃业务事实。

### 7.2 Connector 实现位置

真实数据库 Connector 应实现
[`Connector` 协议](../../../src/ecommerce_agent/connectors/base.py)，并在服务组装边界注册。
领域模块不得自行打开客户数据库连接。

只读数据库 Connector 的能力声明建议为：

- `virtual=false`。
- `modes=["read", "polling"]`；确有数据库原生 CDC 时再声明相应能力。
- 首期 `resources` 只登记已经完成规范化和验收的资源。
- `actions=[]`，直到客户明确授权写回且独立完成写操作门禁。
- `test_connection` 必须无副作用。

当前通用同步编排已直接支持 `catalog`、`inventory` 和包含订单行的 `orders`。独立物流、
售后报表不能仅靠新增 resource 名称自动工作：可以在 Connector 侧合并成完整订单快照，
或者为物流/售后增加明确的领域 normalizer；不得绕过 `OrderService` 直接插表。

### 7.3 PullRecord 契约

每条外部记录必须产生：

| 字段 | 要求 |
|---|---|
| `source_id` | 外部来源内稳定、可重复计算的业务身份；不能使用随机 UUID |
| `source_version` | 可比较且带时区的来源更新时间；用于旧版本拒绝和幂等判断 |
| `occurred_at` | 事实发生时间或来源事件时间 |
| `payload` | 已转换为云湃领域模型的业务字段，不包含租户外数据和未授权 PII |

商品 PullBatch 示例：

```json
{
  "connector_id": "customer-erp-readonly-v1",
  "resource": "catalog",
  "records": [
    {
      "source_id": "store-001:sku-10001",
      "source_version": "2026-08-20T09:30:00+08:00",
      "occurred_at": "2026-08-20T09:30:00+08:00",
      "payload": {
        "store_id": "store-001",
        "item_id": "item-9001",
        "sku_id": "sku-10001",
        "title": "示例商品",
        "status": "active",
        "sale_price": "299.00",
        "currency": "CNY",
        "attributes": {
          "merchant_code": "M-10001"
        }
      }
    }
  ],
  "next_cursor": "<opaque-cursor>",
  "has_more": false,
  "data_as_of": "2026-08-20T09:30:05+08:00"
}
```

`tenant_id` 不由客户 payload 提供，而由云湃已经认证的管理员/任务上下文确定，防止外部数据
自行选择租户。

### 7.4 领域映射基线

下表用于现场确认，不替代代码中的 Pydantic 模型：

| 领域 | 稳定身份 | 核心事实 | 权威模型 |
|---|---|---|---|
| 商品 | store + item + SKU | 标题、状态、售价、币种、受控属性 | [`CatalogItemUpsert`](../../../src/ecommerce_agent/business/catalog.py) |
| 库存 | store + warehouse + SKU | 现有、占用、在途、日均销量 | [`InventoryBalanceUpsert`](../../../src/ecommerce_agent/business/inventory.py) |
| 订单 | store + order + line | 订单/支付状态、金额、下单时间、订单行 | [`OrderUpsert`](../../../src/ecommerce_agent/business/orders.py) |
| 物流 | store + order | 承运商、脱敏运单号、状态、最新事件 | [`LogisticsSnapshotInput`](../../../src/ecommerce_agent/business/orders.py) |
| 售后 | store + order + case | 类型、状态、申请/批准金额和时间 | [`AfterSaleCaseInput`](../../../src/ecommerce_agent/business/orders.py) |

状态枚举、长度限制、金额约束和时间校验直接读取上述模型。实现者不得在 Connector 中另建一套
长期漂移的枚举；客户枚举只在 mapping 层转换到权威领域枚举。

### 7.5 增量查询与游标

优先使用 `(updated_at, stable_primary_key)` 组合游标，按两个字段升序做 keyset pagination，
不要使用大表 `OFFSET` 分页。概念 SQL 如下，实际占位符按所选数据库驱动调整：

```sql
SELECT
    store_id,
    sku_id,
    updated_at,
    ...
FROM integration_catalog_view
WHERE (
    updated_at > :last_updated_at
    OR (updated_at = :last_updated_at AND sku_id > :last_primary_key)
)
AND updated_at <= :high_watermark
ORDER BY updated_at ASC, sku_id ASC
LIMIT :batch_size;
```

游标要求：

- 对调用方保持不透明，内部包含游标版本、最后更新时间、最后主键和本轮高水位。
- 只有整个 PullBatch 已成功完成规范化和同步记录更新后才返回/保存新游标。
- 网络或映射失败不能静默推进游标。
- 同一时间戳下必须使用稳定主键打破排序并列。
- 不把“本批没有出现”解释为删除；删除必须有明确 tombstone、删除状态或全量快照对账。
- 首次全量和后续增量必须使用同一业务身份与来源版本规则。

### 7.6 来源版本和幂等

项目 D-014 要求：

- 旧 `source_version`：拒绝，不能覆盖新事实。
- 同版本、同 payload：幂等，不创建重复业务版本。
- 同版本、不同 payload：冲突，进入错误/隔离处理，不猜测谁正确。
- 新版本：正常应用，并保留来源和审计证据。

当前 Connector 同步逐条调用领域服务。若一批中后续记录失败，前面已提交的记录可能保留，
同步运行会标记失败；因此真实 Connector 的重放必须稳定且幂等，不能依赖“整批数据库事务回滚”。

### 7.7 查询实现要求

- 所有参数使用数据库驱动的参数化查询，禁止拼接店铺 ID、游标或时间条件。
- 查询语句和允许访问的视图应固定在 Connector 内部，不接受聊天输入或 HTTP payload 传入 SQL。
- 一对多订单数据按批次取订单头和订单行并在内存中按稳定 ID 聚合，避免逐订单 N+1 查询。
- 单批大小先使用保守值，依据现场延迟、客户库负载和云湃写入速度实测后调整。
- 超时、断连和权限失败返回明确错误；不得返回空批伪装成功。
- 数据库驱动依赖必须在确认客户数据库类型后单独评审；本仓库规则禁止未经确认新增依赖。

### 7.8 配置与密钥

当前 `Settings` 尚未定义客户数据库连接参数。实现真实 Connector 时，应在
[`config.py`](../../../src/ecommerce_agent/config.py) 集中增加配置并由服务组装层注入，
不要在多个模块散落 `os.getenv`。

至少需要以下配置概念：

- Connector ID 和启用开关。
- 数据库类型、主机、端口、数据库/schema。
- 只读用户名和密码。
- TLS 模式和 CA 证书路径。
- 连接超时、查询超时、批次大小。
- 店铺/租户映射。

这些是待实现配置项，不是当前已经可用的启动参数。真实值只放进部署环境或密钥系统；仓库中
最多提交无密钥的 `.example` 模板。

## 8. 官方 API、ERP API 和 Webhook 接入

使用官方 API 或 ERP API 时，仍然实现同一个 Connector 契约：

- `test_connection`：验证 endpoint、凭证有效性、授权店铺和最小权限。
- `pull`：处理分页、游标、限流、token 刷新和来源时间。
- `verify_webhook`：先验签、校验事件 ID 和店铺归属，再返回 `VerifiedEvent`。
- `execute`：只用于已授权动作，必须携带稳定幂等键。
- `verify`：执行后通过官方查询或回执确认结果；网络超时不能直接宣称失败并盲目重试。

Webhook 只负责提供事件触发或增量线索，不能因为收到事件就跳过正式数据读取、权限校验和
来源版本比较。涉及真实回写时，应先只读联调通过，再另开写能力验收。

## 9. CSV/XLSX 报表接入

### 9.1 当前支持范围

调研基线下，`REPORT_ADAPTERS` 的 `generic-cn-v1` 登记以下报表；运行时仍以注册表为准：

| report_type | 业务域 | 主要粒度 |
|---|---|---|
| `catalog_snapshot` | 商品 | 店铺 SKU 快照 |
| `inventory_snapshot` | 库存 | 仓库 SKU 快照 |
| `order_snapshot` | 订单 | 订单行快照 |
| `fulfillment_snapshot` | 物流 | 订单履约快照 |
| `operations_daily` | 店铺经营 | 店铺渠道日 |
| `marketing_daily` | 推广 | 计划日 |
| `refund_snapshot` | 售后退款 | 订单售后单 |
| `settlement_statement` | 结算 | 店铺结算周期 |

除经营日报当前只接受 CSV 外，其他已登记适配器按注册表声明接受 CSV/XLSX。字段、中文别名、
枚举、粒度和单位的唯一权威来源是
[`REPORT_ADAPTERS`](../../../src/ecommerce_agent/readonly_data/adapters.py)，不要从本文复制一份
新的运行时字段表。

### 9.2 真实样本处理流程

1. 客户提供脱敏样本和字段字典。
2. 核对表头、工作表、粒度、金额单位、时区、枚举和敏感列。
3. 若 `generic-cn-v1` 不完全匹配，新增平台专属 mapping version；不要修改通用版本去猜平台。
4. 先对少量样本运行导入，检查 accepted/quarantined/rejected 是否覆盖全部行。
5. 对账通过后再导入完整周期。
6. 相同导出重放应返回幂等结果；同版本不同内容或旧版本必须在领域写入前拒绝。

现有 `ReadonlyReportIngestionService` 可以由内部 Python 服务调用，但正式交付还需要二选一：

- 增加管理员鉴权的文件导入 API/CLI；或
- 由受控后台任务把文件放入允许的 `readonly-imports` 存储后调用导入服务。

不得把任意本机路径、宏、公式、外链或文件中的文字指令当成可执行内容。更完整的现有边界见
[M7-R WP2 报表适配器交接](../../tasks/M7R_WP2_REPORT_ADAPTER_HANDOFF.md)。

## 10. 现有 Connector 管理 API

以下接口使用管理端 `X-Admin-Id` 和 `X-Admin-Key`。示例中的值均为占位符：

### 10.1 查看已注册 Connector

```bash
curl -sS "http://127.0.0.1:8080/v1/connectors/catalog" \
  -H "X-Admin-Id: <ADMIN_ID>" \
  -H "X-Admin-Key: <ADMIN_API_KEY>"
```

### 10.2 无副作用连接测试

```bash
curl -sS -X POST \
  "http://127.0.0.1:8080/v1/connectors/<CONNECTOR_ID>/test" \
  -H "X-Admin-Id: <ADMIN_ID>" \
  -H "X-Admin-Key: <ADMIN_API_KEY>"
```

### 10.3 拉取一批数据

```bash
curl -sS -X POST \
  "http://127.0.0.1:8080/v1/connectors/<CONNECTOR_ID>/sync" \
  -H "Content-Type: application/json" \
  -H "X-Admin-Id: <ADMIN_ID>" \
  -H "X-Admin-Key: <ADMIN_API_KEY>" \
  -d '{
    "resource": "catalog",
    "cursor": null,
    "limit": 100
  }'
```

调用方读取响应中的 `has_more` 和 `next_cursor`，按同一 resource 继续拉取。当前没有真实
Connector 时，目录中只会看到虚拟实现；不能用虚拟结果作为客户数据库接入通过证据。

### 10.4 Webhook

`POST /v1/connectors/{connector_id}/webhook` 不依赖管理端身份头，而依赖 Connector 对平台请求
本身完成签名、事件 ID、店铺范围和重放校验。真实 Connector 未实现前不得开放公网路由。

## 11. 云湃内部 SQLite 与多人测试

### 11.1 当前数据文件

```text
DATA_DIR/
├── agent.sqlite3          # 业务、审计、同步、只读导入等事实
└── checkpoints.sqlite3    # LangGraph checkpoint
```

SQLite 使用 WAL 和进程运行目录锁。服务运行时不要手工复制主库文件，也不要删除 `-wal` 或
`-shm` 文件。备份和恢复使用项目已有运维流程。

### 11.2 别人是否能看到你接入的数据

本机 SQLite 不会自动共享给其他电脑。给多人测试有两种正确方式：

1. **推荐：共享测试服务。** 在一台受控测试主机运行一个云湃后端实例，完成一次客户数据同步，
   所有测试人员通过该实例的 API/前端访问。SQLite 文件只由服务进程访问，不放在 NFS 共享盘。
2. **独立环境。** 每位测试人员在自己的 `DATA_DIR` 中重新执行同一脱敏导入/同步。此方式适合
   离线验收，但每份库互相独立。

真实客户数据不应打包进 Git 或普通交接压缩包。需要离线演示时，使用脱敏、显式标记的 fixture
或经过授权的测试种子；“晴川”虚拟数据与真实客户数据必须保持来源和展示范围隔离。

## 12. 为什么不能现场直接换成 PostgreSQL/MySQL

当前项目不只是业务表使用 SQLite，还依赖：

- `sqlite3.connect` 和 SQLite row factory。
- `PRAGMA foreign_keys/WAL/busy_timeout`。
- `BEGIN IMMEDIATE`、SQLite `ON CONFLICT` 和现有事务/锁语义。
- 版本化 SQLite migration 和 schema 校验。
- SQLite online backup、双库一致性检查和恢复流程。
- 独立的 SQLite LangGraph checkpoint 库。

因此内部数据库迁移至少包含数据库抽象、SQL 方言、事务隔离、锁与并发、迁移工具、checkpoint、
备份恢复、部署、性能和全量回归，不能通过修改 `DATA_DIR` 或 DSN 完成。

项目稳定决策是：单机 V1 继续使用 SQLite；进入多进程经营 AI 阶段后再按全域方案迁移
PostgreSQL。权威决策见
[`PROJECT_OVERVIEW.md`](../../../.project-to-act/PROJECT_OVERVIEW.md)。

## 13. 安全与隐私要求

- 所有查询和写入按 `tenant_id + connector_id + store_id` 隔离。
- 外部 payload 不得决定 `tenant_id`。
- 默认禁止导入顾客姓名、手机号、地址、身份证、原始评论和评论者身份。
- 物流只保存脱敏运单号；日志、错误和隔离记录不得回显原始敏感行。
- 密码、token、AppSecret、证书私钥不得进入 Git、Markdown、测试 fixture 或截图。
- Connector 账号遵循最小权限；只读阶段不配置写权限。
- 客户文件和数据库内容均视为不可信数据，不能执行其中公式、宏、链接或文字指令。
- 真实写回必须具备授权、类型化参数、幂等键、后置读回、审计和人工补偿。
- 数据保留、删除、备份和恢复应在客户签署的范围内执行。

## 14. 对账、可观测性和失败处理

每次全量或增量同步至少记录：

- Connector、租户、店铺、resource 和同步运行 ID。
- 游标前后值、数据截止时间和运行状态。
- 接收、应用、幂等、隔离和拒绝数量。
- 来源系统、mapping version 和脱敏样本/文件引用。
- 失败类型和可安全公开的错误摘要。

对账至少覆盖：

- 商品/SKU 数量和状态分布。
- 各仓库 SKU 库存总量及关键 SKU 抽查。
- 订单、订单行、退款单数量。
- 订单金额、退款金额和结算金额；误差容忍度必须由业务方明确签署，不能自行假定。
- 最早/最晚业务时间及同步水位。

失败处理规则：

- 连接失败：同步运行失败，游标不推进。
- 单条映射失败：按具体链路记录隔离/拒绝，不把错误行改成零值。
- 旧版本：拒绝并保留证据。
- 同版本冲突：停止猜测，交给字段映射或来源负责人裁决。
- 运行中断：使用旧游标重放，依靠来源版本契约保持幂等。
- 数据口径错误：先停 Connector/调度，保留现场证据，再修 mapping；不要删除整个运行库掩盖问题。

## 15. 独立验收门禁

以下门禁必须由未参与 Connector 开发的人重新执行；不能只采用开发者已经记录的 `passed`：

| Gate | 验收内容 | 通过条件 |
|---|---|---|
| G1 连接 | 使用只读账号执行 `test_connection` | 无写权限，店铺和视图范围正确 |
| G2 字段 | 脱敏样本逐字段映射 | 主键、枚举、金额、时区和隐私列均有明确结论 |
| G3 首次全量 | 小范围后完整同步 | 数量、状态和金额按已签口径对账 |
| G4 增量 | 新增、更新、同时间戳多记录 | 无丢失，游标稳定，顺序可复现 |
| G5 幂等 | 原批次重复执行 | 不产生重复事实或错误版本增长 |
| G6 乱序/冲突 | 旧版本、同版本异内容 | 分别拒绝和报冲突，不覆盖新事实 |
| G7 删除 | tombstone、关闭或删除状态 | 不依赖“本批缺失”猜删除 |
| G8 故障恢复 | 断网、超时、进程重启 | 游标不误推进，可从安全位置重放 |
| G9 隔离 | 两租户/两店铺交叉探针 | 无跨租户、跨店铺读取或写入 |
| G10 隐私 | 敏感列和值探针 | 原始 PII 不入库、不进日志、不进错误响应 |
| G11 多人测试 | 第二台客户端访问共享服务 | 看到同一受控数据范围且权限正确 |
| G12 写回 | 仅在后续启用写动作时执行 | 幂等、读回、未知态和人工补偿全部通过 |

正式验收记录应包含日期、代码 commit、Connector/mapping version、脱敏样本引用、命令、
退出状态、同步运行 ID、对账结果和未通过项。任何未运行的门禁都应写“未验证”，不能写成通过。

## 16. 现场实施顺序

### 阶段 A：接入确认

1. 确认数据源和合法授权路径。
2. 取得只读测试权限、网络条件、脱敏样本和字段字典。
3. 冻结首期店铺、数据域、历史范围和同步延迟目标。
4. 决定官方 API、ERP API、数据库 Connector 或报表 mapping 的唯一主路径。

### 阶段 B：只读最小闭环

1. 首接商品和 SKU。
2. 接库存。
3. 接订单和订单行。
4. 接物流。
5. 接售后退款。
6. 完成小样本、全量、增量、重放和对账。

经营日报、推广和结算可在五个核心域稳定后接入。写回、自动退款、改价、采购和付款不进入首期。

### 阶段 C：共享测试

1. 在受控测试主机部署一个后端实例。
2. 使用独立 `DATA_DIR` 和测试租户。
3. 完成一次脱敏全量同步。
4. 给测试人员分配独立管理身份，不共享管理员密钥。
5. 由独立测试人按第 15 节重新执行验收。

### 阶段 D：生产前

1. 完成正式网络、密钥轮换、备份恢复和监控。
2. 明确数据保留与删除策略。
3. 完成长稳和故障演练。
4. 只读生产通过后，再单独申请和验收写能力。

## 17. 现场交接记录模板

```text
客户/项目：
环境：测试 / 预生产 / 生产
数据源：官方 API / ERP API / MySQL / PostgreSQL / SQL Server / 文件
平台或系统版本：
Connector ID：
租户 ID：
店铺 ID：
授权范围：只读 / 允许写回（列明动作）
网络方式：VPN / 专线 / SSH Tunnel / IP 白名单
主数据身份：商品 ID / SKU / 商家编码 / 物料号
增量字段：
稳定主键：
删除表达：
来源时区：
金额单位与币种：
日增量与历史范围：
脱敏样本位置：
字段字典位置：
mapping version：
同步运行 ID：
对账结论：
隐私字段处置：
未解决问题：
客户技术负责人：
客户业务口径负责人：
云湃实施负责人：
独立验收人：
```

密钥、密码、token 和真实顾客数据不得填写在此模板中。

## 18. 当前需要开发的明确清单

确定客户接入方式后，至少要从下列清单中选择实际需要的工作，不要一次性全部开发：

1. 客户专属官方 API/ERP/只读数据库 Connector。
2. Connector 配置和密钥注入。
3. 客户字段到权威领域模型的 mapping。
4. 必要时增加物流、售后等 resource normalizer。
5. 正式文件导入 API/CLI，或受控后台导入任务。
6. 增量调度、游标持久化和失败告警。
7. 数据准备度页面与对账报告。
8. 独立验收探针和真实脱敏样本测试。

在客户未提供数据库类型、字段字典、样本和增量机制之前，不应先写一个猜测性的“万能数据库
Connector”。明天现场最重要的交付物是确认真实接入路径、冻结数据契约并取得可复核样本。
