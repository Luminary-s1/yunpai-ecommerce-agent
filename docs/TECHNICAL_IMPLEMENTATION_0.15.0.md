# 云湃电商 Agent 0.15.0 技术实现说明

## 1. 本版本解决的问题

0.14.0 以前，客服 Graph 已具备知识检索、SOP、类型化工具、后置验证和人工转接，但订单上下文、知识结果、SOP 与工具结果分别存在于运行态变量中。一次回复结束后，只能看到知识来源，不能完整回答以下问题：

- 当时使用了哪个租户、店铺、SKU 和订单上下文；
- 上下文是否来自授权上游，是否存在字段冲突；
- 使用了哪些知识版本、SOP 版本和工具能力；
- 工具结果是否经过后置验证；
- 多步 ReAct 的每次规划使用了哪一版证据；
- 人工客服和审计人员如何复核这次回答。

0.15.0 新增 `ContextBuilder` 和 schema v15 不可变上下文快照，把客服 Agent 从“运行时拼提示词”提升为“先形成可信证据包，再规划、执行和生成”。

## 2. 总体执行路径

```text
认证主体与会话绑定
  -> 输入脱敏与 context 白名单
  -> 安全预检
  -> 分层知识检索
  -> decision 快照
      -> 身份/授权冲突：直接转人工，不调用模型或工具
      -> 证据可用：结构化规划
  -> SOP 与类型化工具门
  -> 工具执行和后置验证
  -> 下一轮 decision 快照（有界 ReAct）
  -> generation 快照
  -> 回复生成与输出事实校验
  -> 消息、审计、人工任务关联最终快照
```

核心入口为 `src/ecommerce_agent/context_builder.py` 的 `ContextBuilder`，Graph 接入位于 `src/ecommerce_agent/graph.py` 的 `build_decision_context` 与 `build_generation_context`。

## 3. ContextBuilder 设计

### 3.1 固定装配顺序

每个快照按固定结构生成 `bundle`：

1. `trusted_session_state`：租户隔离、平台、店铺、业务上下文授权标志；
2. `current_subject`：当前商品、SKU、订单、物流和店铺政策；
3. `sop_evidence`：当前可用或已固定的 SOP 版本；
4. `knowledge_evidence`：已审批知识的 ID、版本、层级、来源和命中分；
5. `available_tools`：代码注册的只读/写入能力和参数 schema；
6. `output_constraints`：语言、渠道、冲突处理和业务结果验证要求；
7. `recent_history`：已脱敏的有限会话历史；
8. `latest_tool_result`：工具状态、输出、错误和后置条件。

模型不能新增权限、SOP、工具或证据；它只能在这份代码生成的上下文包内进行规划。

### 3.2 证据清单

每条 evidence 都包含：

- 稳定 `evidence_id`；
- `type` 与 `source_id`；
- `source_version`；
- `authority`，例如 `authenticated_session`、`approved_knowledge`、`active_sop`、`registered_capabilities`、`verified_tool`；
- `freshness` 和可选观测时间；
- 已脱敏摘要；
- 独立 SHA-256 校验和。

整个快照再对阶段、序号、父快照、请求哈希、bundle、evidence、冲突、缺失项和 readiness 计算总校验和。

### 3.3 冲突与降级

代码在模型调用前检查：

- `store_id` 与 `shop_id` 是否指向不同店铺；
- `sku_id` 与 `sku` 是否存在身份冲突；
- 未授权上下文是否携带订单、物流等受限字段；
- generation 阶段的工具结果是否经过后置验证。

高危或严重冲突把 readiness 置为 `handoff_required`，Graph 直接进入人工转接。未授权订单字段不会进入模型上下文包。

### 3.4 不可变与并发幂等

`context_snapshots` 使用 `(tenant_id, trace_id, stage, sequence)` 唯一约束。同一 Graph 节点因重试再次写入时：

- 内容相同：返回原快照；
- 内容不同：抛出 `context snapshot replay mismatch`，不覆盖历史证据；
- 并发重复：由进程写锁和数据库唯一约束共同收敛为一条记录。

读取快照时重新计算总校验和；数据库内容被修改后会立即报 `context snapshot checksum mismatch`。

## 4. schema v15

新增 `context_snapshots`：

| 字段组 | 用途 |
|---|---|
| `tenant_id/session_id/trace_id` | 租户、会话与请求归属 |
| `stage/sequence` | `decision` 或 `generation` 及 ReAct 序号 |
| `parent_snapshot_id` | 形成可回放父子链 |
| `context_version/request_hash` | 上下文契约与请求身份 |
| `bundle_json/evidence_json` | 提示上下文和证据清单 |
| `conflicts_json/missing_json/readiness` | 冲突、缺失与降级结论 |
| `checksum/created_at` | 完整性与审计时间 |

`messages` 增加 `context_snapshot_id`，最终客服回复引用 generation 快照；clarify、refuse 或 handoff 等没有生成阶段的路径引用最后一个 decision 快照。

迁移仍是只向前迁移，schema 初始化后会验证物理表与关键字段，迁移标记不能掩盖缺表或缺列。

## 5. Graph 集成

### 5.1 首次规划

初次知识检索后创建 `decision #0`。只有 readiness 为 `ready` 才进入 LLM 结构化规划；冲突路径在模型前停止。

### 5.2 工具循环

工具注册表继续负责类型、读写模式、必需可信字段、幂等字段、策略和后置验证。每次工具观察后创建 `decision #N`，父节点指向上一快照。读取失败可以继续受限规划；未经验证的结果不能进入最终回答。

### 5.3 最终生成

知识回答在二次意图过滤后创建 generation 快照；工具任务在 `postcondition_met=true` 后创建 generation 快照。生成提示使用最终 bundle，输出安全门也使用同一 bundle 复核数字和业务完成承诺。

### 5.4 持久关联

最终快照 ID 同时进入：

- `ChatResponse.context_snapshot_id`；
- `ChatResponse.context_readiness` 与 `evidence_ids`；
- assistant message；
- `chat.completed` 审计事件；
- 人工任务 payload。

这样 API 调用方、客服工作台、质检和人工转接看到的是同一份证据。

## 6. 管理 API 与工作台

新增管理员接口：

```http
GET /v1/admin/context-snapshots/{snapshot_id}
```

接口按管理员租户过滤，不允许跨租户读取。会话详情为每条 assistant 消息返回快照摘要；工作台显示证据数与 readiness，点击后可查看：

- 阶段、序号和父快照；
- 冲突与降级原因；
- 每条证据的类型、权威级别、版本、新鲜度和校验和前缀；
- 完整快照校验和。

后台在桌面使用双列证据清单，390px 下切为单列并允许长 ID/校验和换行，不产生横向溢出。

## 7. 隐私与留存

- 外部 context 在 checkpoint 前执行字段白名单和敏感信息脱敏；
- 未授权订单字段在 `AgentService` 和 `ContextBuilder` 两层阻断；
- 快照只记录已脱敏内容，证据 ID 不使用客户明文；
- 留存任务统计并删除到期快照；
- 未完成的人工任务继续保护会话和快照；
- 因反馈需要保留的消息会清空正文和快照引用，证据快照仍按到期策略删除；
- 删除前解除 message 引用，避免孤儿关联。

## 8. 当前边界

本版本完成的是可运行、可回放、可审计的本地客服 Agent 上下文结构，不代表生产平台已经放行：

- 淘宝真实客服机器人资格、奇门场景、正式凭证和测试店铺仍未取得；
- 真实订单/物流/商品读工具尚未完成客户数据映射和时效验收；
- 真实写动作尚未完成官方权限、读回、补偿和业务审批；
- 脱敏客户回放集、24/72 小时长稳、并发容量、异机恢复和设备密钥托管尚未完成。

因此 0.15.0 的结论是“本地代码级候选通过，生产 NO-GO”。
