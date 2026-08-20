# M7-R WP2 报表适配器交接

> 状态：2026-08-18 开发候选。本文是交接说明，不替代运行时注册表、正式 WP5 独立验收、
> 真实平台样本确认或生产放行。

## 权威入口

- 适配器、字段白名单、别名、枚举、粒度、单位和格式：
  `src/ecommerce_agent/readonly_data/adapters.py` 的 `REPORT_ADAPTERS`。
- CSV/XLSX 不可信输入边界：
  `src/ecommerce_agent/readonly_data/file_parser.py`。
- 逐行隔离、批次部分成功、manifest/evidence 和领域服务编排：
  `src/ecommerce_agent/readonly_data/ingestion.py` 的
  `ReadonlyReportIngestionService`。
- 来源版本与不可变导入事实：
  `src/ecommerce_agent/readonly_data/service.py`。

字段清单和别名不在本文复制为第二份可变真相。调用方应从 `REPORT_ADAPTERS` 按
`(report_type, mapping_version)` 读取当前契约；测试同时断言适配器 row model 与 WP1
字段策略是同一份定义。

## 当前适配范围

`generic-cn-v1` 登记商品、库存、订单行、履约物流、店铺渠道日经营、推广计划日、订单售后
和结算单八类报表。CSV 使用严格 UTF-8（允许 BOM）；XLSX 只读取显式选中的工作表，经营
日报因现有公开领域服务只声明 `csv/json/form` 来源而暂只接受 CSV。

当前仓库及会话附件中没有找到经授权的淘宝/天猫真实受控导出样本，因此
`generic-cn-v1` 只冻结规范英文表头和已登记的通用中文别名，不能宣称已覆盖某个平台的
全部原生列。收到脱敏受控样本后，应先核对表头、粒度、金额单位、时间、枚举和敏感列，再
以新的平台 mapping version 登记；不得把猜测字段冒充真实平台契约。

## 规范化与安全语义

- 每个请求显式携带 store、来源类型、来源系统、报表周期、导出时间、数据截止时间、IANA
  时区、mapping version、格式和受控原始文件引用。
- CSV 日期使用 ISO 8601。XLSX 同时接受 ISO 文本和 Excel 1900/1904 日期序列；无时区的
  业务时间按请求的来源时区解释，领域服务写入前转为规范 UTC。
- 金额不按猜测换算。币种字段由 row model 校验，粒度、金额单位和来源时区写入受控
  source receipt，并在导入结果 `trace` 中回显。
- 宏、公式、外链、超限压缩内容、路径穿越和所选工作表超链接在解析边界拒绝；文件内容
  从不作为代码执行。
- WP1 字段名和值级隐私门在 Pydantic 领域模型之前执行。隔离结果只暴露行号、字段键、
  安全原因码和原始行摘要，不回显原始行或顾客 PII。
- 缺字段和非法类型为 `rejected`；重复业务身份、跨店、父订单缺失及领域来源冲突为
  `quarantined`。accepted + quarantined + rejected 必须覆盖全部解析行。
- 同一导出版本、同一完整输入重放返回原 manifest；同版本不同内容和旧版本在任何领域
  写入前拒绝。批次中一个文件失败不会抹除其他文件已经成功的领域事实，批次结果明确标记
  `passed/partial/failed`。

## 领域边界

规范化写入只调用 `CatalogService`、`InventoryService`、`OrderService`、
`OpsAssistantService`、`MarketingService` 和 `FinanceService` 的公开入口，不直接写商品、
订单、库存、经营、推广或财务底表。订单行快照使用公开合并入口，更新订单主体时保留分别
导入的物流和售后子事实；履约和退款仍要求同 tenant/connector/store/order 的父订单存在。

原始文件上传不属于本服务职责。调用方必须先把文件放入 WP1 允许的
`readonly-imports` 受控存储路径，再把内容字节和对应引用交给导入服务。

## 调用与复验

单文件调用 `ReadonlyReportIngestionService.ingest(tenant_id, request, content)`；多文件调用
`ingest_batch(tenant_id, jobs)`。请求和 job 类型从
`ecommerce_agent.readonly_data` 导出。

开发测试入口：

```bash
.venv/bin/python -m pytest tests/test_readonly_data_ingestion.py tests/test_readonly_data_contract.py -q
.venv/bin/python -m pytest \
  tests/test_catalog_orders_metrics.py \
  tests/test_inventory_planning.py \
  tests/test_marketing_finance_api.py \
  tests/test_marketing_finance_pressure.py \
  tests/test_ops_assistant.py \
  tests/test_order_handoff_visibility.py -q
```

聚焦用例按每个已登记适配器覆盖正常、缺字段、非法类型、重复、乱序和跨店，并额外覆盖
敏感列/值、CSV/XLSX、Excel 日期、公式拒绝、批次部分成功、冲突前置拒绝、订单子事实保留
及公开领域投影。最终通过数量和全量回归结果以项目账本中的最新 WP2 证据为准。
