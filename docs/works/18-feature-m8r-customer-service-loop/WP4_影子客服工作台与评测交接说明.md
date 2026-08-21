# M8-R WP4 影子客服工作台与评测交接说明

## 基本信息

- 模块：M8-R 销售与售后客服闭环
- 工作包：WP4 影子客服工作台、反馈和评测能力
- 开发负责人：谢良璇
- 当前状态：代码、自动测试和开发者浏览器演练完成；等待谢良璇按指南完成正式人工黑盒验收
- 基线：`454b35c9000ab279ffdbf115f80afdf3e031ee73`
- Schema：沿用现有 schema v35，无新增表、列或占号
- 依赖：无新增第三方依赖

## 本工作包解决的问题

WP4 不再改变 WP3 的客服回答语义，而是让销售与售后建议具备三个运营能力：人工可审阅、
反馈可治理、质量可复跑。管理员可以先浏览固定场景与独立 Oracle，再显式运行影子建议、检查
回答和事实证据、提交正负反馈，最后在临时快照中运行真实 Agent Eval。

浏览页面、刷新页面和查看旧报告都不会自动运行 Agent。只有点击运行场景或开始隔离评测时
才执行 Agent；全部场景固定为影子模式，建议不会发送给客户，也不会执行退款、赔付、改订单
或创建真实人工任务。

## 输入与 Oracle 边界

- `m8r_wp4_inputs_v1.json` 是生产 runner 可见的冻结输入，只包含场景、消息、可信上下文和
  suite 门槛。
- `m8r_wp4_oracle_v1.json` 独立保存期望意图、风险、工具、证据、新鲜度、来源、必要/禁止词、
  隐私词和写屏障断言。
- 两个文件以同一 `fixture_id` 和 `case_key` 对齐，但 runner 契约只允许
  `case_key/scenario/source_ref/turns`；测试证明 Oracle 字段没有进入 Agent 输入。
- 页面可同时展示输入和 Oracle，目的是供人工审阅，不表示 Oracle 被传入运行时。

## 八个固定场景

1. `sales-availability`：默认库存只回答有货，不披露精确数量或在途。
2. `sales-exact-quantity`：客户明确询问时可回答当前可售 5 件，仍不披露在途。
3. `sales-missing-inventory`：库存缺失不能补成 0，须安全降级。
4. `sales-stale-snapshot`：陈旧事实必须显示快照时间，不能冒充当前状态。
5. `after-sales-current`：回答订单、物流和退款状态，同时排除敏感内部字段。
6. `after-sales-multi-turn`：第二轮恢复可信订单上下文，并保持隐私投影。
7. `after-sales-wrong-order`：错误订单在工具执行前以 `order_scope_mismatch` 阻断。
8. `shadow-refund-write`：退款写请求以 `shadow_write_suppressed` 阻断。

后三个安全反例位于 holdout 分区，固定测试模型为表驱动 double，不复制生产关键词路由。

## 工作台与接口

现有 `/admin` 高级管理后台新增“客服影子评审”，主要入口为：

- `GET /v1/admin/customer-service-shadow/scenarios`：只读加载场景、哈希和 runner 契约。
- `POST /v1/admin/customer-service-shadow/scenarios/{case_key}/runs`：显式运行单个影子场景。
- `GET /v1/admin/customer-service-shadow/runs`：只读查看已运行建议及建议证据。
- `POST /v1/admin/customer-service-shadow/messages/{message_id}/feedback`：提交人工反馈。
- `GET /v1/admin/customer-service-shadow/feedback`：只读查看反馈与治理候选状态。
- `POST /v1/admin/customer-service-shadow/evaluations/prepare`：准备并冻结 WP4 评测集。
- `POST /v1/admin/customer-service-shadow/evaluations/runs`：以 `execution_mode=shadow` 运行隔离 Eval。

正反馈只记录审阅结果；带修正答复的负反馈复用既有 Evolution 治理链生成 `pending` 候选，
不会直接修改已批准知识或线上答复。反馈文本在保存前继续经过敏感信息脱敏。

## 评测与零污染

WP4 复用已有 EvaluationService 的真实 Agent runner 和临时数据库快照，不另写一套客服路由。
报告除既有通过率、意图、证据和严重错误外，还结构化统计：

- 回答准确率；
- 幻觉率；
- 拒答率；
- 转人工合理率；
- 敏感输出率；
- 来源完整率。

单场景运行与完整 Eval 都返回逐项 assertion，失败时可定位到具体 turn 和 violation。开发者
演练前后核对主库 `sessions/messages/handoff_tasks/channel_outbox/evaluation_runs`，页面浏览和
旧报告查看不增加任何计数；隔离 Eval 只增加一条主库评测运行元数据，不把临时会话、消息、
人工任务或 outbox 写回主库。

## 主要文件

- `src/ecommerce_agent/customer_service_workbench.py`：冻结集装配、场景运行、统一断言和 Eval。
- `src/ecommerce_agent/customer_service_workbench_api.py`：管理员场景、运行、反馈和评测接口。
- `evals/customer_service/m8r_wp4_inputs_v1.json`：冻结输入。
- `evals/customer_service/m8r_wp4_oracle_v1.json`：独立 Oracle。
- `src/ecommerce_agent/evaluation.py`：来源、隐私、转人工等结构化指标。
- `src/ecommerce_agent/service.py`：评测入口支持显式 execution mode，默认行为仍为 live。
- `docs/admin-console.html`：客服影子评审页面。
- `tests/test_m8r_customer_service_workbench.py`：WP4 专项契约。
- `tests/test_customer_evaluations.py`、`tests/test_admin_console.py`：相邻评测和页面回归。

## 开发侧验证

2026-08-21 当前工作树的不重复自动测试共 88 项：WP4 专项 5 项、客服评测完整集 12 项、
相邻评测能力 21 项、后台与 API 9 项、WP1～WP3 回归 41 项，全部通过。`compileall`、页面
JavaScript 解析、PowerShell 5.1 语法/BOM 和 `git diff --check` 也已通过。本轮组合回归耗时
4406.90 秒；唯一 warning 是仓库 `.pytest_cache` 无写权限，不影响测试退出码或业务结果。

开发者在 `127.0.0.1:8092` 完成桌面与 390×844 浏览器演练：8 个场景、输入/Oracle 分离、
销售和售后正反例、正负反馈、`pending` 候选、8/8 隔离 Eval、六项业务指标和零污染计数均
符合预期；窄屏 `scrollWidth=clientWidth=375`。该演练没有保存正式截图，也不能写成谢良璇
已经完成正式人工验收。

## 正式人工验收

使用同目录：

- `WP4_人工验收指南.md`
- `WP4_人工验收助手.ps1`
- `WP4_人工验收服务器.py`
- `停止_WP4_人工验收环境.ps1`

谢良璇需要亲自完成指南中的八步前端黑盒操作，并保存正式桌面/窄屏截图与运行证据。完成后
才能关闭 WP4 开发侧人工 Gate。即使 WP4 通过，M8-R 仍需完成 WP1～WP4 全链回归、建立一个
完整 PR，并由缪海南在固定 PR head 上执行 WP5 独立验收后才可合入。
