# 客服会话数据模型与 API 契约

## Session

`sessions` 保存会话生命周期和认证作用域。

| 字段 | 含义 |
|---|---|
| `id` | 服务端内部会话 ID，不通过客户端 API 暴露 |
| `tenant_id` | 租户作用域 |
| `external_session_id` | 客户端使用的会话 ID，对应 API 的 `{id}` |
| `subject_hash` | 认证主体的不可逆哈希，用于同租户内隔离 |
| `client_id` | 创建会话的 API 客户端 |
| `status` | `active` 或 `closed` |
| `created_at` | UTC ISO 8601 创建时间 |
| `last_seen_at` | UTC ISO 8601 最后活跃时间 |
| `source_type` | `api`、`channel`、`simulation` 或 `evaluation` |
| `source_reference` | 可选的来源引用 |

`external_session_id` 在租户内唯一。创建、读取、分页和关闭操作均使用
`tenant_id + subject_hash` 校验；越权读取返回 404。空闲超过
`SESSION_IDLE_TIMEOUT_MINUTES`（默认 120）的 active 会话由 worker 关闭，
存在非终态 handoff 时跳过。

## Message

`messages` 按 `session_id + created_at + id` 形成稳定时序。

| 字段 | 含义 |
|---|---|
| `id` | 消息 ID |
| `trace_id` | 本轮 Agent trace ID |
| `session_id` | 内部 Session 外键 |
| `role` | `user` 或 `assistant` |
| `content` | 已按现有隐私规则处理的消息正文 |
| `intent` | 助手消息的意图 |
| `risk_level` | 助手消息的风险级别 |
| `route_reason` | 编排路由原因 |
| `sources_json` | 引用来源 JSON 数组 |
| `model_fallback` | 是否走模型降级 |
| `tenant_id` / `client_id` | 持久化作用域 |
| `redacted` | 是否发生脱敏 |
| `context_snapshot_id` | 回答引用的不可变上下文快照 |
| `created_at` | UTC ISO 8601 创建时间 |

## API

五个端点都要求 `X-Client-Id`、`X-Client-Key`、`X-Subject-Id`。

### `POST /v1/chat/sessions`

请求：

```json
{"session_id": "buyer-chat-001"}
```

首次创建返回 201；同一认证作用域重复创建返回 200 和同一资源；ID 已绑定到其他
主体、客户端或来源时返回 409。响应包含 `id`、`status`、`created_at`、
`last_seen_at`、`message_count`。

### `GET /v1/chat/sessions/{id}`

返回会话状态、创建时间、最后活跃时间和消息数。会话不存在或不属于当前主体时
返回 404。

### `GET /v1/chat/sessions/{id}/messages`

查询参数：

- `limit`：默认 20，范围 1–100。
- `cursor`：上一页返回的 `created_at|id` 复合游标。

响应包含按 `created_at, id` 升序排列的 `items`、`next_cursor` 和 `limit`。
游标无法解码时按未提供游标处理，从第一页开始。

### `POST /v1/chat/sessions/{id}/messages`

请求体包含 `message` 与可选 `context`，路径中的 `{id}` 是本轮唯一 session ID。
返回 `text/event-stream`，事件契约见 `SSE_EVENT_PROTOCOL.md`。可选请求头
`Idempotency-Key` 用于断连重试；命中已完成请求时返回相同 message ID，且不新增
历史消息。

### `DELETE /v1/chat/sessions/{id}`

把会话状态置为 `closed` 并返回会话摘要。不存在或越权返回 404；存在
`proposed`、`accepted`、`working`、`input_required` 或 `review` 状态的 handoff
时返回 409，不关闭会话。
