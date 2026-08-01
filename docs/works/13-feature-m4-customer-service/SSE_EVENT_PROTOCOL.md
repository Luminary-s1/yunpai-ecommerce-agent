# 客服流式回复 SSE 协议

## 请求

推荐路径：`POST /v1/chat/sessions/{session_id}/messages`

请求体：

```json
{
  "message": "尺码怎么选",
  "context": {}
}
```

兼容路径：`POST /v1/chat/stream`

请求头：

| 名称 | 必填 | 含义 |
|---|---|---|
| `X-Client-Id` | 是 | 客户端 ID |
| `X-Client-Key` | 是 | 客户端密钥 |
| `X-Subject-Id` | 是 | 当前顾客主体 ID |
| `Idempotency-Key` | 否 | 1–200 字符；断连重试时复用同一值 |

兼容路径的请求体：

```json
{
  "session_id": "buyer-chat-001",
  "message": "尺码怎么选",
  "context": {}
}
```

成功建立流后响应类型为 `text/event-stream`。每个事件只包含一行 UTF-8 JSON，
格式固定为：

```text
data: {"event":"meta","session_id":"buyer-chat-001","message_id":"msg-...","trace_id":"trace-..."}

```

协议不使用 SSE 的 `event:` 字段；客户端按 JSON 内的 `event` 分派。

## 事件

| `event` | 其余字段 | 含义 |
|---|---|---|
| `meta` | `session_id`, `message_id`, `trace_id` | 首个事件，声明本轮稳定标识 |
| `delta` | `text` | 一段回复文本，按接收顺序拼接 |
| `citations` | `sources` | 本轮生成引用的知识来源 |
| `handoff` | `requires_human`, `handoff_id`, `handoff_status`, `reason` | 已进入或建议进入人工处理 |
| `done` | `message_id`, `intent`, `risk_level`, `model_fallback` | 本轮逻辑流结束 |
| `error` | `code`, `message`, `retry_advised` | 流内错误，下一事件必为 `done` |

`sources` 中每项包含 `id`、`category`、`source`、`version`、`score`。
`handoff` 沿用非流式响应的字段和状态语义。错误消息已脱敏，不包含上游响应正文。

## 事件顺序

有知识引用的正常生成：

```text
meta → delta+ → citations → done
```

无知识命中的降级生成：

```text
meta → delta → handoff → done
```

直接转人工等非生成路径：

```text
meta → handoff → done
```

命中已完成的幂等请求：

```text
meta → delta → done
```

其中单个 `delta` 是数据库已保存的完整回复，不会再次调用模型，也不会新增消息。

流内失败：

```text
meta? → delta* → error → done
```

客户端收到 `done` 后必须关闭本轮消费；即使前一事件是 `error`，也不得继续等待。
`retry_advised=true` 时可使用同一 `Idempotency-Key` 重试。

## 错误码

| `code` | `retry_advised` | 含义 |
|---|---:|---|
| `model_unavailable` | `true` | 模型服务临时不可用或限流 |
| `model_error` | `false` | 模型生成失败 |
| `internal_error` | `false` | 流式处理内部失败 |

HTTP 鉴权和请求校验在流建立前完成，继续使用标准的 4xx/5xx JSON 响应，不转换为
SSE `error` 事件。
