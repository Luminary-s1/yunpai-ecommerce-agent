# 云湃电商 Agent 0.13.0 完整测试报告

日期：2026-07-21

## 1. 结论

0.13.0 通过本地生产候选验收，生产放行结论仍为 **NO-GO**。

本轮新增并验证 schema v13 SOP 逐步执行账本、类型化 DSL v2、步骤审批、只读重试、写入不确定态、补偿、重启恢复、结构化结果脱敏、Agent 工具门、管理 API 和后台处置对话框。竞品分析、智能客服、后台管理、可靠发送、灾备和发布门禁完成全量回归。

## 2. 自动化测试

最终命令：

```powershell
coverage erase
coverage run --branch -m pytest -q -p no:cacheprovider
coverage report --include="src/ecommerce_agent/*" -m
```

结果：

- 136 passed，0 failed，耗时 183.25 秒。
- 源码分支覆盖率 85%，统计 6,290 条语句和 1,508 个分支。
- `sops.py` 85%，`database.py` 93%，`tools.py` 87%。
- `business/competitive.py` 86%，`business/orders.py` 90%，`business/service.py` 85%。
- `outbox.py` 85%，`releases.py` 86%，`disaster_recovery.py` 85%。

SOP 专项覆盖：

- 类型化步骤、稳定 ID、歧义/危险元数据拒绝。
- 高风险动作未声明审批时评测失败。
- 缺失可信上下文、步骤顺序错误和新鲜锁版本返回。
- 多步读取、评估审批、动作审批、后置验证和成功完成。
- 读取型失败自动重排、预算耗尽和人工批准后重试。
- 写动作不确定态禁止重试，人工读回确认成功/失败。
- 补偿成功、失败、不确定态和人工读回确认。
- 进程中断时读取重排、动作冻结、补偿冻结和预算耗尽。
- 并发步骤抢占只有一个执行者成功。
- 租户隔离、404、409 乐观锁冲突和管理 API。
- 工具结果中的手机号、密码、令牌、密钥和深层超限数据脱敏。

## 3. 静态与安装检查

```powershell
py -3.12 -m compileall -q src tests
node --check <admin-console-script>
py -3.12 -m pip install -e . --no-deps
py -3.12 -c "import ecommerce_agent; print(ecommerce_agent.__version__)"
```

结果：Python 编译和前端脚本语法均通过；editable 安装成功，包版本为 0.13.0；非历史文档和源码中无残留的 0.12.0/schema v12 标记。

发现并处理的环境问题：当前主机 `PATH` 中 Python 3.10 的 `yunpai-agent.exe` 排在 Python 3.12 之前，直接运行会因项目要求 Python 3.11+ 而失败。使用 `py -3.12 -m ecommerce_agent.cli` 后通过，README 和运维文档已加入解释器与脚本来源核对步骤。这是部署环境问题，不是 3.12 运行时失败。

## 4. 隔离 Agent 评测

在全新临时 `DATA_DIR`、`MODEL_ENABLED=false` 条件下执行：

```powershell
py -3.12 -m ecommerce_agent.cli eval
```

结果：20/20 通过，预检失败 0、检索失败 0、安全失败 0，未发出模型网络请求。

## 5. 运行服务检查

使用 Python 3.12 和独立临时数据目录启动 `http://127.0.0.1:8094`。

- `/health`：200，版本 0.13.0，schema 13，知识 156 条，注册工具 5 个。
- `/ready`：200，状态 ready。
- `/openapi.json`：版本 0.13.0。
- SQLite `PRAGMA quick_check`：ok。
- SQLite `PRAGMA foreign_key_check`：0 个错误。
- `sop_step_runs`：29 个持久字段。
- 运行目录锁实际拒绝第二个 `AgentService` 同时打开相同数据目录。

## 6. 浏览器验收

在真实运行服务中完成：

- 管理员登录成功，页面显示 0.13.0、schema v13 和安全框架模式。
- 知识与 SOP 页面显示 DSL v2 编辑器、内置 SOP 和 SOP 运行表。
- 构造一条本地只读 SOP 运行后，页面正确显示 `step_02 / evaluate / waiting_approval / 锁 2`。
- 使用页面内对话框填写审批依据并提交成功，运行推进到 `step_03 / propose / pending / 锁 3`。
- 390x844 视口下 body 无横向溢出；宽表只在自己的容器内滚动。
- 移动端处置对话框尺寸为 335x320，完整位于视口内，输入和按钮无重叠。
- 恢复桌面视口后页面宽度正常，控制台 warning/error 为 0。

## 7. 本机性能冒烟

隔离服务预热后，每个接口连续执行 100 次请求：

| 接口 | p50 | p95 | 最大值 |
| --- | ---: | ---: | ---: |
| `/health` | 18.840 ms | 21.362 ms | 26.534 ms |
| `/ready` | 9.825 ms | 16.118 ms | 18.705 ms |
| `/v1/admin/sop-runs?limit=100` | 64.922 ms | 80.951 ms | 88.620 ms |

这些数据只代表当前 Windows 开发机和单进程 SQLite 的本机冒烟，不构成生产 SLA。

## 8. 缺陷与修复

本轮测试发现并修复：

- SOP 等待输入/审批后返回旧步骤版本，导致管理端可能立即产生 409。修复为更新后重新读取步骤。
- 模型工具参数曾可冒充可信上下文。修复为 SOP 上下文门只接受认证层注入的可信上下文。
- 工具结果仅做整段文本正则脱敏，无法覆盖 `client_secret`、`*_token` 等字段。修复为结构化键脱敏、文本掩码、深度和数量上限。
- 原生 `window.prompt` 使 SOP 处置无法稳定自动验收。修复为页面内受控表单对话框。
- 默认 SOP 引用了不存在的 `get_order_detail`。修复为已注册的 `get_order_facts`。
- 旧逻辑会在每轮消息结束时直接完成 SOP。修复为仅在全部步骤成功后完成。

## 9. 未完成的生产 Gate

以下项目没有被本报告宣称通过：

- 真实淘宝机器人资格、AppKey/AppSecret、奇门参数、测试店铺和真实消息收发。
- 真实 ERP/OMS/WMS 读回和写动作后置验证。
- 脱敏客户会话回放、真实模型探测和业务验收签字。
- 自动退款、改价、赔付、采购等业务动作的正式工具、审批矩阵和补偿协议。
- 24 小时长稳、网络/进程/磁盘故障注入、异机恢复、设备密钥托管和业务 RPO/RTO。
- 语义型 VOC、真实坐席排队和多店铺容量测试。

因此 0.13.0 可以作为本地联调和受控试点基线，但不能直接开启真实平台自动写操作。
