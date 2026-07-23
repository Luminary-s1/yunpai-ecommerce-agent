# 云湃电商 Agent 0.15.0 完整测试报告

## 1. 结论

- 版本：`0.15.0`
- 数据库：schema v15
- 测试日期：2026-07-22
- 本地代码级候选：通过
- 生产放行：NO-GO
- 全量自动化：149 passed，0 failed
- 源码分支覆盖率：85%
- `context_builder.py` 分支覆盖率：93%
- 数据库模块分支覆盖率：94%
- 留存模块分支覆盖率：100%
- 离线信任边界、检索与安全评测：20/20

生产 NO-GO 原因不是当前自动化失败，而是缺少真实平台授权、真实/脱敏客户数据回放、长稳容量、异机灾备与设备密钥等外部验收证据。

## 2. 测试环境

| 项目 | 值 |
|---|---|
| 操作系统 | Windows |
| Python | 3.12 |
| 存储 | SQLite WAL，业务库与 LangGraph checkpoint 分离 |
| 服务 | FastAPI/Uvicorn，`http://127.0.0.1:8097` |
| 模型模式 | 关闭真实外呼，启用确定性 mock |
| 浏览器 | Codex In-app Browser |
| 桌面/移动视口 | 默认桌面、390 x 844 |

## 3. 最终自动化结果

执行：

```powershell
py -3.12 -m coverage run --branch -m pytest -q
py -3.12 -m coverage report -m --include='src/ecommerce_agent/*'
```

结果：

```text
149 passed in 224.30s
TOTAL 6796 statements, 85% branch coverage
context_builder.py 93%
database.py 94%
maintenance.py 100%
admin.py 90%
admin_api.py 96%
service.py 90%
```

同时通过：

- `py -3.12 -m compileall -q src tests`
- 管理后台内嵌 JavaScript `node --check`
- editable wheel 构建和安装
- 包源码版本与安装元数据均为 `0.15.0`

## 4. 新增上下文专项测试

| 场景 | 预期 | 结果 |
|---|---|---|
| 普通知识回答 | 产生 decision + generation，两者父子关联 | 通过 |
| 消息关联 | assistant message 指向最终快照 | 通过 |
| 店铺身份冲突 | 模型和工具调用前转人工 | 通过 |
| 授权订单上下文 | authority 为 `authorized_platform_context` | 通过 |
| 未授权订单上下文 | 不进入模型 bundle | 通过 |
| 16 路并发重复构建 | 只产生一条快照 | 通过 |
| 同 trace/stage 内容变化 | 拒绝重放覆盖 | 通过 |
| 数据库内容篡改 | 读取时校验和失败 | 通过 |
| 跨租户快照读取 | 返回不存在/404 | 通过 |
| 管理端证据接口认证 | 管理员可读本租户，匿名 401 | 通过 |
| 工具成功 ReAct | decision #0 -> decision #1 -> generation #1 | 通过 |
| 工具后置验证 | generation evidence 标记 `verified_tool` | 通过 |

## 5. 回归范围

全量 149 项覆盖：

- 客服 RAG、结构化决策、有界 ReAct、输出安全门；
- 客户端/管理员认证、租户会话绑定、限流；
- 类型化工具、超时、重试、写入未知态和后置验证；
- 人工任务状态机与乐观锁；
- 分层知识、进化评测、审批、激活、退役、回滚；
- SOP DSL、步骤账本、审批、重试、未知态裁决、补偿和恢复；
- 淘宝 OAuth、TOP/奇门签名、消息幂等和能力门；
- 持久 outbox、租约、重试、死信、核对和人工接管；
- 发布策略、隔离回放、双人审批、灰度和自动暂停；
- 商品、订单、物流、售后、库存和受控指标；
- 竞品策略、原子重评、告警确认/解决/复发和 worker；
- 灾备备份、认证加密、验证、恢复、回滚和换钥；
- schema v1/v7/v9/v12/v13 到 v15 前向迁移；
- 隐私脱敏、指标和留存。

## 6. 留存与隐私测试

新增快照后重新审计留存路径，覆盖：

1. 普通到期会话：消息、两阶段快照、指标和 checkpoint 被清理；
2. 未完成人工任务：消息和快照保留，会话不关闭；
3. 带反馈消息：消息正文改为 `[PURGED_BY_RETENTION]`，快照引用清空，到期快照删除；
4. 快照内手机号等敏感文本：持久化前脱敏；
5. 删除后：消息不存在悬空快照引用。

## 7. 离线评测

隔离临时数据目录执行 `run_offline_evaluation`：

```text
total=20, passed=20, failed=0
precheck_failures=[]
retrieval_failures=[]
safety_failures=[]
```

该评测覆盖提示注入/越权预检、12 类知识意图检索和 5 类高风险业务动作识别，不等同于真实模型或客户回放评测。

## 8. 数据库与运行态检查

最终 0.15 服务重启后：

| 检查 | 结果 |
|---|---|
| OpenAPI version | `0.15.0` |
| `/ready` | `ready` |
| `schema_current` | true |
| `PRAGMA user_version` | 15 |
| schema migration max version | 15 |
| `PRAGMA integrity_check` | ok |
| `PRAGMA foreign_key_check` | 0 errors |
| assistant message 快照关联 | 全部存在 |
| 孤儿 message -> snapshot 引用 | 0 |

重启后再次读取浏览器验收产生的 generation 快照，证据数为 6，接口返回正常。

## 9. 浏览器验收

实际完成：

1. 管理员登录；
2. 进入智能客服；
3. 使用可信客户端发送“尺码怎么选”；
4. 收到 product 知识回答；
5. 打开会话；
6. 点击“证据 6 · ready”；
7. 查看 session、business context、3 条知识和 tool catalog；
8. 核对 generation 阶段、父快照和完整校验和。

桌面结果：弹窗完整显示，双列证据可扫描，console error 0。

首次 390px 检查发现弹窗内部 `scrollWidth=440px`、容器宽 `335px`，原因是 Grid 子项的 min-content 和长等宽字符串。修复 `min-width:0`、`minmax(0,1fr)` 与长字符串换行后复测：

```text
viewport innerWidth=390
bodyScrollWidth=375
dialogWidth=335
dialogScrollWidth=318
evidenceColumns=282px
console errors=0
```

结论：页面和弹窗均无横向溢出，移动端证据清单稳定为单列。

## 10. 本机轻量性能

最终 0.15 Uvicorn 单实例、SQLite WAL、关闭真实模型外呼，每个接口顺序请求 20 次：

| 接口 | p50 | p95 | max |
|---|---:|---:|---:|
| `/health` | 16.905 ms | 18.845 ms | 19.680 ms |
| `/ready` | 8.890 ms | 10.506 ms | 10.683 ms |
| `/v1/admin/context-snapshots/{id}` | 80.983 ms | 87.499 ms | 87.687 ms |
| `/v1/chat` | 149.888 ms | 167.611 ms | 248.912 ms |

这只是本机轻量冒烟，不是容量上限或生产 SLA。样本未覆盖真实模型、真实平台网络、大量历史消息、多进程、多机和高并发写入。

## 11. 测试中发现并修复的问题

| 问题 | 风险 | 修复 |
|---|---|---|
| mock 规划器只识别旧扁平 `authorized` | 授权订单误要求补充信息 | 在结构化 package 外提供明确兼容视图，并保留 package 原文 |
| 灾备 manifest 测试写死 schema 14 | v15 边界测试误失败 | 更新为当前 `Database.SCHEMA_VERSION` 对应值 |
| 移动端证据弹窗内部横向溢出 | 390px 下证据难以阅读 | Grid 最小宽度归零、单列 `minmax`、长字符串换行 |
| 快照未纳入消息留存 | 形成隐私旁路和无限增长 | 新增 dry-run 计数、引用解除、按人工任务保护后删除 |
| 漂移数据库缺 messages 时 v15 先 ALTER | 错误契约不一致 | 缺表时跳过 ALTER，由统一 schema validator 报告 |

## 12. 未通过的生产门禁

| 门禁 | 状态 | 放行条件 |
|---|---|---|
| 淘宝真实客服授权 | 阻塞 | 客服机器人资格、奇门场景、正式凭证、测试店铺 |
| 真实业务只读事实 | 未执行 | 商品/订单/物流授权接口、字段映射、时效和对账验收 |
| 真实低风险写动作 | 未执行 | 官方权限、幂等、稳定读回、补偿、审批和停止开关 |
| 客户数据回放 | 未执行 | 脱敏真实问题集、严重事实错误为 0、业务签收 |
| 长稳与容量 | 未执行 | 目标一体机 24/72 小时，明确 QPS、P95、队列和资源阈值 |
| 异机灾备 | 未执行 | 真实介质、设备密钥托管、异机恢复、业务 RPO/RTO 签收 |
| 真实模型 | 未执行 | 指定模型版本、超时/限流/降级、中文客服质量和成本验收 |

## 13. 发布建议

0.15.0 可以作为下一阶段真实渠道 shadow 和脱敏客户回放的代码基线，不应直接开启自动回复或真实写动作。推荐顺序：

1. 取得首个平台和测试店铺权限；
2. 接入真实只读商品/订单/物流工具，并让工具输出进入现有 evidence；
3. 建立脱敏客户回放集和严重错误 Gate；
4. 在仅提示/shadow 模式完成 24 小时长稳；
5. 通过业务签收后再逐级启用 assist/collaborative，automatic 保持关闭。
