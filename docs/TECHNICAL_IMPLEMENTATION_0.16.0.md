# 云湃电商 Agent 0.16.0 技术实现说明

## 1. 本版本解决的问题

0.15.0 已能生成可校验的上下文证据快照，但真实渠道入站仍依赖 Web 请求返回后的后台回调：进程在回调前退出时，消息虽然入库，Agent 执行意图却可能丢失；Agent 完成后重试还可能重复写消息、草稿或回复。

0.16.0 把淘宝入站到客服动作改造成持久 Agent 运行时。目标不是增加一个演示入口，而是让每条已验签的入站事件都具备可恢复、可幂等、可分阶段追踪、可按发布策略隔离、可人工处置的执行账本。

## 2. 端到端执行路径

```text
奇门请求验签、租户/店铺解析、消息脱敏
  -> 同一事务写 channel_event + channel_agent_job
  -> HTTP 立即确认；后台回调只负责加速，不承担可靠性
  -> worker 原子领取任务并取得有期限租约
  -> 二次检查会话 owner 与发布策略/稳定分桶
  -> 以 source event 生成稳定 Agent 幂等键
  -> Agent intake -> RAG -> ContextBuilder -> decision -> SOP/tool gate
  -> 同一事务保存 user/assistant message 与完整 ChatResponse
  -> 写 release observation，得到 control/shadow/draft/handoff/send
  -> 二次检查策略和会话所有权
  -> 生成草稿、人工任务，或把精确 source event 回复写入 outbox
  -> outbox worker 调用平台并保存 confirmed/rejected/uncertain/dead_letter
  -> 失败回执反写发布观测、任务错误和自动暂停结果
```

## 3. 入站事务与任务账本

`TaobaoIntegrationService.receive_qimen` 在完成签名、时间窗、路由、店铺凭证和消息类型校验后，在一个 SQLite 事务中写入：

- `channel_events`：脱敏后的不可变入站事实；
- `channel_agent_jobs`：该事件后续应执行 Agent 的持久意图。

`channel_agent_jobs` 对 `(tenant_id, event_id)` 建唯一约束。平台重复投递同一消息时返回原事件 ID，不重复建任务。schema v16 只为升级后的新入站创建任务，不回放历史事件，避免版本升级后误回复旧消息。

任务状态为 `queued/running/retry/completed/blocked/dead_letter`，阶段为 `queued/agent/agent_completed/materialize/done`。账本同时记录发布版本、分桶、动作、Agent 调用、回复消息、上下文快照、发布观测、草稿、outbox、尝试次数、租约、错误和乐观锁版本。

## 4. 任务领取、恢复和错误预算

`ChannelAgentRuntime` 使用 `BEGIN IMMEDIATE`、条件更新和 `record_version` 原子领取任务。同一个任务只能被一个 worker 从 `queued/retry` 改为 `running`：

- 领取时增加 `attempt_count` 并写 `lease_owner/lease_until`；
- 进程退出后，过期 `running` 租约恢复为 `retry`；
- 普通异常按指数退避设置 `next_attempt_at`；
- 达到 `CHANNEL_AGENT_MAX_ATTEMPTS` 后进入 `dead_letter`；
- 安全门拒绝进入 `blocked`，不会盲目重试。

HTTP `BackgroundTasks` 会尝试立即处理新任务，以降低首响延迟；独立 worker 才是可靠执行来源。即使请求已返回后进程退出，重启扫描仍能领取持久任务。

## 5. Agent 调用幂等

`AgentService.chat` 新增 `idempotency_key` 和 `execution_mode`。渠道使用 `channel-event:{event_id}`，并在 `agent_invocations` 保存：

- 租户、客户端、内部会话与幂等键；
- 包含消息、可信 context 和执行模式的规范请求哈希；
- 稳定 `trace_id`、user message ID、assistant message ID；
- `running/completed`、尝试次数、错误和完整响应 JSON。

相同键和相同请求重复调用时直接返回已完成响应；相同键绑定不同请求时拒绝执行。Graph 持久化使用稳定消息 ID，并在同一业务库事务内写消息和完成 invocation，因此任务在 Agent 完成后、下游动作前崩溃，重试不会再产生第二组消息或快照。

这里提供的是单机 SQLite 边界内的可观察幂等，不宣称跨平台分布式“绝对 exactly-once”。外部发送仍以持久 outbox、平台回执和人工核对解决不确定态。

## 6. 可信上下文与执行图

渠道 Agent 以认证配置中的 bootstrap client 建立 `Principal`，买家只使用稳定哈希，不把昵称或正文提升为权限。传给 Agent 的 context 仅包含代码生成的 `platform/shop_id`；订单等受限字段仍要求已授权上游客户端能力。

Graph 延续 0.15 的证据包：可信会话、当前业务主体、知识版本、SOP 版本、工具目录、近期脱敏历史和已验证工具结果。每次 decision/generation 都持久化不可变快照和 SHA-256 校验和，任务账本保存最终 `context_snapshot_id`，后台可以沿证据 ID 复核回答。

`execution_mode=shadow` 会进入同一推理与检索路径，但执行图禁止创建 SOP run、调用写工具、创建人工任务和修改 SOP 状态。需要转人工或执行动作只作为观测结果返回，不能越过影子策略产生线上副作用。

## 7. 发布策略和四种模式

每条任务在 Agent 调用前通过 `ReleaseService.assignment` 选择处于 `active` 的不可变策略版本，并按会话稳定哈希分桶。`RELEASE_GATE_REQUIRED=true` 时，无策略或未命中流量的任务以 `control` 结束，不调用 Agent。

| 模式 | Agent 是否运行 | 下游动作 |
|---|---|---|
| `shadow` | 是 | 只记录回答、证据和发布观测；零草稿、零人工任务、零发送 |
| `assist` | 是 | 合格回答生成精确关联入站事件的人工草稿；不自动发送 |
| `collaborative` | 是 | 合格回答生成草稿；严重违规或需人工时转人工并建任务 |
| `automatic` | 是 | 仅白名单、风险/证据/降级门均通过时写入可靠 outbox，否则转人工 |

发布观测保存意图、风险、是否需人工、证据数、模型降级、违规项、严重性和最终动作。运行错误预算或异步投递失败超过阈值时自动暂停策略。

## 8. 精确事件的草稿、人工和发送

草稿和发送请求新增 `source_event_id`。渠道运行时不再用“会话中最新一条入站”推断回复对象，而是始终绑定触发任务的事件；即使同会话在 Agent 运行期间又收到新消息，也不会把旧答案关联到新问题。

动作落地前再次读取状态：

- 会话已由人工接管或暂停时阻止自动执行；
- 策略在推理期间被暂停时阻止发送；
- `handoff` 原子切换 owner，并用稳定 message ID 幂等创建人工任务；
- `draft` 切换为人工所有权并生成带证据、SOP、风险和 diff 基线的草稿；
- `send` 只把加密载荷和幂等键写入 outbox，不在 Agent worker 内直接依赖一次网络调用。

## 9. 异步投递闭环

`TaobaoIntegrationService` 向渠道运行时注册 delivery observer。outbox 派发、启动恢复扫描和人工核对产生终态时，observer 根据 `(tenant_id, source_event_id)` 找回任务和发布观测。

`rejected/uncertain/dead_letter` 会：

1. 把对应 release observation 更新为 `blocked` 并写严重违规；
2. 触发发布策略运行错误预算和自动暂停；
3. 把 `delivery_*` 写回渠道任务，供后台告警和人工核对；
4. 保留 outbox 原始幂等记录，禁止对不确定投递直接重发。

observer 异常只写审计，不回滚已经确定的平台投递状态。

## 10. 管理 API 与后台

新增租户隔离管理员接口：

```http
GET  /v1/integrations/taobao/agent-jobs/summary
GET  /v1/integrations/taobao/agent-jobs
GET  /v1/integrations/taobao/agent-jobs/{job_id}
POST /v1/integrations/taobao/agent-jobs/run?limit=20
```

“渠道接待”页面新增 Agent 队列 KPI 和运行账本，显示事件、发布模式、状态/阶段、尝试次数、回答摘要、上下文证据、草稿/outbox 和错误。管理员可以打开关联快照核对证据，也可在 worker 关闭的维护场景手工触发到期任务。

`/health` 返回 worker 状态和队列摘要；开启渠道自动回复时，`/ready` 要求渠道 Agent worker 存活。概览页同时显示待处理和死信任务数。

## 11. 运行配置

```text
CHANNEL_AGENT_WORKER_ENABLED=true
CHANNEL_AGENT_POLL_SECONDS=1
CHANNEL_AGENT_LEASE_SECONDS=300
CHANNEL_AGENT_BATCH_SIZE=10
CHANNEL_AGENT_MAX_ATTEMPTS=5
CHANNEL_AGENT_RETRY_BASE_SECONDS=2
CHANNEL_AGENT_RETRY_MAX_SECONDS=300
```

生产中还必须保持 `RELEASE_GATE_REQUIRED=true`、`OUTBOX_WORKER_ENABLED=true`、`OUTBOX_SYNC_DISPATCH=false`。租约应长于一次 Agent 最坏执行时间；退避和最大尝试数要与告警值班能力一起设置。

## 12. 当前边界

0.16.0 已完成本地单实例下的持久渠道 Agent 结构、故障恢复和观测闭环，可以作为真实店铺 shadow/assist PoC 的代码候选，但仍为生产 NO-GO：

- 尚未取得真实淘宝客服机器人资格、奇门场景、正式凭证和测试店铺；
- 真实商品、订单、物流字段映射及时效对账尚未验收；
- 真实模型版本、限流、质量、成本和降级尚未用客户语料签收；
- 尚未完成脱敏客户回放、24/72 小时长稳、目标硬件容量和安全测试；
- SQLite 适合单机一体机，不支持多副本并发 worker；多节点阶段需迁移具备行级锁和队列语义的数据库；
- 异机介质、设备密钥托管和真实 RPO/RTO 演练仍待现场完成。

生产推进顺序仍应为：合法授权 -> 真实只读事实 -> 客户回放 -> shadow -> assist -> collaborative -> 小流量 automatic。
