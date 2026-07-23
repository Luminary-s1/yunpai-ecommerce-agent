# 云湃电商 Agent 0.16.0 完整测试报告

## 1. 当前结论

- 版本：`0.16.0`
- 数据库：schema v16
- 测试日期：2026-07-22
- 本地代码级候选：通过
- 生产放行：NO-GO
- 全量自动化：161 passed，0 failed
- 全项目分支覆盖率：89%
- 生产源码分支覆盖率：85%
- 渠道 Agent 运行时分支覆盖率：85%
- 数据库模块分支覆盖率：95%
- 上下文构建分支覆盖率：93%
- 竞品分析模块分支覆盖率：91%
- 离线信任边界、检索与安全评测：20/20

生产 NO-GO 由真实平台授权、客户数据回放、真实模型、长稳容量、异机灾备和设备密钥等外部验收证据缺失造成，不代表本地自动化失败。

## 2. 自动化与覆盖率

执行：

```powershell
py -3.12 -m coverage erase
py -3.12 -m coverage run --branch -m pytest -q
py -3.12 -m coverage report -m
```

结果：

```text
161 passed in 250.75s
all project: 10388 statements, 89% branch coverage
src/ecommerce_agent: 7185 statements, 85% branch coverage
channel_agent.py 85%
database.py 95%
context_builder.py 93%
business/competitive.py 91%
maintenance.py 100%
```

同时通过：

- 渠道 Agent 专项：11 passed；
- 影子/渠道/奇门定向回归：15 passed；
- `compileall`；
- 管理后台内嵌 JavaScript 编译；
- editable wheel 构建和安装；
- 源码版本与安装元数据均为 `0.16.0`。

## 3. 渠道 Agent 专项场景

| 场景 | 验收点 | 结果 |
|---|---|---|
| Agent 调用重试 | 相同 trace、消息、快照与响应，无重复数据 | 通过 |
| 幂等键冲突 | 相同键绑定不同请求时拒绝 | 通过 |
| Qimen 重投 | 事件和任务均只有一条 | 通过 |
| 12 线程并发领取 | 同一任务只有一个 worker 获得租约 | 通过 |
| worker 丢失 | 过期租约恢复并可再次领取 | 通过 |
| Agent 完成后下游失败 | 第二次尝试复用已完成 invocation | 通过 |
| 同会话新消息插入 | 回复仍精确关联原 source event | 通过 |
| outbox 异步拒绝 | 反写发布观测、任务错误并暂停策略 | 通过 |
| collaborative 严重违规 | 转人工、建任务、不发送 | 通过 |
| shadow 需转人工 | 只观测；不改 owner、不建人工/SOP、不发送 | 通过 |
| owner 并发切换 | Agent 调用前安全阻断 | 通过 |
| 首次失败且预算为 1 | 进入 dead letter | 通过 |
| 管理 API | 认证、租户隔离、列表/详情/汇总/执行 | 通过 |
| worker 生命周期 | 启动、ready、health、关闭状态一致 | 通过 |

## 4. 全量回归范围

161 项覆盖客服 RAG、结构化规划、有界 ReAct、输出安全门、上下文快照、客户端/管理员认证、租户隔离、限流、类型化工具、人工任务、知识治理、自进化、SOP 步骤账本、淘宝 OAuth/TOP/Qimen、可靠 outbox、发布回放与熔断、商品/订单/库存/指标、竞品监控、质检/VOC、灾备备份/恢复/换钥、前向迁移、隐私和留存。

schema 迁移测试覆盖新建 v16，以及 v1/v7/v9/v12/v13/v15 向 v16 的前向升级；v15 历史入站不会自动创建待回复任务。

## 5. 离线评测

隔离数据目录、真实模型外呼关闭时执行：

```text
total=20, passed=20, failed=0
precheck_failures=[]
retrieval_failures=[]
safety_failures=[]
```

它覆盖提示注入/越权预检、12 类知识意图检索和 5 类高风险动作识别，不等同于真实模型或客户语料验收。

## 6. 干净服务端到端验收

使用全新数据目录启动 FastAPI/Uvicorn 单实例，模型关闭真实外呼并启用确定性 mock，渠道 Agent 和竞品 worker 启用，outbox worker 在本次纯 shadow 环境中关闭。创建、回放、审批并激活一条 100% shadow 策略后，通过真实 HTTP 奇门入口投递商品咨询和主动转人工各一条。

运行结果：

| 检查 | 结果 |
|---|---|
| OpenAPI version | `0.16.0` |
| `/health` | 200 / `ok` |
| `/ready` | 200 / `ready`，全部 checks 为 true |
| schema / user_version / migration max | 16 / 16 / 16 |
| `PRAGMA integrity_check` | `ok` |
| `PRAGMA foreign_key_check` | 0 errors |
| 入站事件 / Agent 任务 / invocation | 2 / 2 / 2 |
| 消息 / 上下文快照 / 发布观测 | 4 / 4 / 2 |
| completed 任务缺少 invocation/message/snapshot | 0 |
| 未完成 invocation / message 快照孤儿 | 0 / 0 |
| 人工任务 / SOP run / 草稿 / outbox | 0 / 0 / 0 / 0 |
| 会话所有权 | `bot` |

两条任务均为 `completed`、`done`、`shadow`，尝试次数 1，且有最终上下文快照。主动转人工请求没有改变 owner，也没有创建人工任务，证明执行图和渠道落地层两处影子写屏障同时生效。

## 7. 浏览器验收

使用 Codex In-app Browser 实际完成管理员登录、进入“渠道接待”、检查 KPI、运行账本、手工触发空队列、打开回答证据快照：

- 账本显示已完成 2、待处理 0、安全阻断 0、死信 0；
- 两条记录均显示 `shadow · shadow / completed / done / 1/5`；
- 证据弹窗显示 generation 阶段、readiness、父快照、3 类证据和完整 SHA-256；
- 手工执行 Agent 队列返回“已处理 0 条”，未重复执行历史任务；
- 桌面 1280 x 720：`bodyScrollWidth=bodyClientWidth=1265`，console error 0；
- 移动 390 x 844：`bodyScrollWidth=bodyClientWidth=375`，表格在局部容器横向滚动，页面无全局溢出；
- 移动证据弹窗宽 335px、内容 `clientWidth=scrollWidth=333`，证据清单为单列，console error 0。

## 8. 本机轻量性能

最终 0.16 Uvicorn 单实例、SQLite WAL、关闭真实模型外呼，每个接口顺序请求 30 次：

| 接口 | p50 | p95 | max |
|---|---:|---:|---:|
| `/health` | 19.824 ms | 21.670 ms | 22.950 ms |
| `/ready` | 11.796 ms | 12.509 ms | 13.494 ms |
| Agent job summary | 82.678 ms | 91.257 ms | 100.895 ms |
| Agent job detail | 76.887 ms | 83.035 ms | 91.241 ms |
| context snapshot | 81.361 ms | 88.650 ms | 90.228 ms |

这是本机顺序读取冒烟，不是容量上限或生产 SLA；样本未覆盖真实模型、平台网络、大量历史消息、多进程或多机写入。

## 9. 本轮发现并修复的问题

| 问题 | 风险 | 修复与回归 |
|---|---|---|
| 入站后台回调未持久化 | 请求返回后进程退出会漏执行 | 入站事件与 Agent 任务同事务写入，worker 恢复 |
| Agent 重试缺少请求级幂等 | 重复消息、快照和下游动作 | invocation 账本、规范哈希、稳定 ID 和响应缓存 |
| 回复默认取最新入站事件 | 并发新消息时错配回答 | 草稿/发送显式绑定 `source_event_id` |
| 异步投递失败与发布观测脱节 | 自动策略无法因真实发送失败熔断 | delivery observer 反写观测、任务和策略 |
| 模型需转人工可越过 shadow | 影子流量改变 owner 并建人工任务 | execution mode 下沉 Graph，影子禁写 SOP/人工/发送 |
| 历史任务级联删除 invocation | 留存可能破坏调用审计 | 外键改为 `ON DELETE SET NULL` |

## 10. 未通过的生产门禁

| 门禁 | 状态 | 放行条件 |
|---|---|---|
| 淘宝真实客服授权 | 阻塞 | 客服机器人资格、奇门场景、正式凭证、测试店铺 |
| 真实只读事实 | 未执行 | 商品/订单/物流字段映射、时效和对账 |
| 真实模型 | 未执行 | 固定模型版本、客户问题质量、限流、成本和降级签收 |
| 客户回放 | 未执行 | 脱敏真实问题集、严重事实错误为 0、业务签收 |
| 长稳与容量 | 未执行 | 目标一体机 24/72 小时，明确 QPS/P95/资源/积压阈值 |
| 异机灾备 | 未执行 | 独立介质、设备密钥托管、异机恢复、RPO/RTO 签收 |
| 多实例运行 | 不支持 | 迁移支持行级锁的数据库和跨进程队列后再放行 |

## 11. 发布建议

0.16.0 可作为真实店铺 `shadow` 和 `assist` PoC 的代码基线，不应直接开启正式自动回复。必须先取得合法渠道权限，完成脱敏客户回放与 24 小时 shadow 长稳；业务、安全和运维共同签收后，再创建新版本推进 assist/collaborative，automatic 继续保持关闭或极小白名单流量。
