# M4 智能客服后端 WP1–WP2

- 分支：`feature/m4-customer-service`
- 范围：客服会话管理与上下文控制、知识库增强回复生成 Pipeline
- 交付范围：M4 工作包 1（会话管理与上下文控制）与工作包 2（知识库增强回复生成 Pipeline）

## D01 · Token 计数与历史截断

- 基线：全量测试 `302 passed in 130.28s`
- 新增确定性保守估算：中日韩字符按 1 token/字，其余字符按长度除以 4 向上取整
- 历史按完整轮次从最新向前保留；即使最近一轮自身超出预算也完整保留，并通过
  `over_budget` 标识
- 判据：构造 200 条超长历史，在预算内保留连续的最新窗口，最后一条原样保留

## D02 · 配置与知识预算

- 新增 `MODEL_CONTEXT_LIMIT_TOKENS=128000` 与 `CONTEXT_BUDGET_RATIO=0.7`；
  ratio 钳制在 0.1–0.9
- 生成与决策 Prompt 消费上游历史，不再自行截取 6 条
- 知识超过预算时按 score 从低到高移除，存在知识时至少保留最高分一条
- 四项定向测试通过
- 反证：临时把默认 `context_budget_ratio` 从 0.7 改为 0.99，同一历史截断
  用例按预期失败（保留数从 7 变为 9）；还原 0.7 后复验通过

## D03 · 上下文与编排接入

- `ContextBuilder` 对条数上限内的历史再次执行 token 预算，快照写入
  `recent_history_meta` 与不可变 `history_window` evidence
- Graph 四处历史读取均使用预算层；总预算扣除 System Prompt 与用户消息后，
  知识和历史按 6:4 分配
- trace 记录 `context:budget:kept{n}/dropped{n}`
- 端到端预算测试：`5 passed`
- 编排与上下文回归：`17 passed`（`test_react_graph.py`、`test_agent.py`、
  `test_context_builder.py`）

## D04 · 会话 CRUD

- 新增四个 `/v1/chat/sessions` 客户端认证端点；创建重复请求返回同一资源，
  认证作用域冲突返回 409，越权读取统一返回 404
- 会话及消息查询均使用 `tenant_id + subject_hash` 过滤
- 消息分页使用 `created_at|id` 复合游标，非法游标从第一页开始
- DELETE 关闭会话前检查非终态 handoff，存在时返回 409
- 7 项会话 API 判据通过；API 与会话鉴权联合回归 `16 passed`
- 55 条消息分页结果为 20、20、15，无重复、无遗漏

## D05 · 空闲超时与收口

- 新增独立配置 `SESSION_IDLE_TIMEOUT_MINUTES=120`
- 会话 TTL worker 启动后立即检查，此后每 60 秒检查；关闭空闲 active 会话，
  并沿用 retention 的 handoff 终态口径跳过未结人工任务
- 判据：121 分钟普通会话自动关闭，同龄未结 handoff 会话保持 active
- 数据模型和四端点契约见 `SESSION_DATA_MODEL_AND_API.md`
- WP1 定向与 retention 回归：`19 passed`
- 全量回归：`318 passed in 135.84s`

## D06 · 模型网关流式输出

- 新增 `ModelGateway.stream_generate()`，真实上游逐个产出 content delta，
  mock 模式按字符产出
- `_stream_request` 保持整串返回契约，并与 generator 共享请求构造、SSE 解析和
  错误分类
- 红态：3 个流式用例均因缺少 `stream_generate` 失败
- 绿态：`tests/test_llm.py` 共 `14 passed`；覆盖多个 delta、隐藏 reasoning、
  429 provider code 和流中途错误

## D07 · Verify / Persist 复用

- 将输出安全校验抽为模块级 `verify_response`
- 将消息事务、invocation 完成、审计和 SOP handoff 标记抽为模块级
  `persist_response`
- 图内 `verify` / `persist` 节点仅调用抽取函数，节点与边文本前后完全一致
- 三套测试逐套通过：`test_react_graph.py` 4 项、`test_agent.py` 7 项、
  `test_api.py` 3 项

## D08 · 两段式生成

- `AgentService.chat_stream` 复用原图并在 `generate` 前暂停，读取同一不可变
  `context_bundle` 后调用 `stream_generate`
- 流结束调用 `verify_response`，再从原图 `verify` 节点之后续跑 handoff / persist
- clarify、handoff、refuse、retry_later 继续由原图一次性完成
- 红态：3 项均因缺少 `chat_stream` 失败
- 绿态：流式 mock 拼接等于非流式回答；消费一个 delta 后关闭 iterator，
  assistant 消息数为 0；定向编排回归共 `14 passed`

## D09 · SSE 端点与事件协议

- 新增 `POST /v1/chat/stream`，沿用客户端认证和 `Idempotency-Key` 请求头，
  响应类型为 `text/event-stream`
- API 将服务层内部事件映射为 `meta`、`delta`、`citations`、`handoff`、
  `done`、`error` 六类单行 JSON 事件
- 只有实际生成过 delta 才发送 citations；直接转人工路径为
  `meta → handoff → done`
- 模型不可用、模型错误和内部错误均输出脱敏错误；`error` 后立即输出 `done`
- 红态：首个端点测试得到预期 `404`；实现后的首次测试发现直接转人工多发
  citations，修正后复验
- 绿态：`tests/test_chat_stream.py` 共 `5 passed in 15.52s`
- SSE、既有 API 与服务层流式回归合跑：`11 passed in 35.70s`

## D10 · 幂等、降级与收口

- 已完成 invocation 重放时，从 `response_json` 读取已持久化回答，以单个 delta
  发出后 done；message ID 不变、assistant 消息仍为 1 条，模型流式调用计数不增加
- 无知识命中的流式文案与非流式最终回答一致，不发送空 citations；随后沿用既有
  handoff 语义收尾
- `MODEL_ENABLED=false` 且关闭 mock 时，用替换后的 HTTP client 方法计数，
  断言外部请求为 0
- 红态：新增三项中，幂等重放因缺少 delta 失败；无知识命中因流中和最终文案
  不一致失败；模型禁用零请求用例直接通过
- 反证：临时把 mock `stream_generate` 从逐字符改为一次性 yield 全文，
  `test_chat_stream_generation_event_sequence` 按预期失败：
  `assert 1 > 1`；还原后 8 项复验通过
- 流式 8 项最终复验：`8 passed in 13.19s`
- 流式、ReAct 图、Agent、模型网关定向回归：`33 passed in 45.57s`
- 全量回归：`332 passed in 575.66s`
- 前端协议见 `SSE_EVENT_PROTOCOL.md`；本周未新增依赖，LangGraph 节点与边未改

## WP2 补齐复核 · 08-01

- 新增 `POST /v1/chat/sessions/{id}/messages`，路径参数作为唯一 session ID，
  请求体只包含 `message` 与 `context`；复用既有 SSE 适配器和幂等请求头
- 商品问题含“它 / 这个 / 这款”等指代且当前轮无候选时，商品顾问只从
  ContextBuilder 已截断的最近用户消息恢复候选；按匹配词数保留最相关并列项，
  不在歧义时任意选择 SKU
- 多轮反证用例先问“云湃保温杯 500ml 怎么样”，再问“它多少钱”；最终回答引用
  不可变上下文中的目录事实 `89.00 CNY`
- 红态：会话消息 POST 返回 `405`；多轮用例错误回答“补货时间”
- 绿态：两个聚焦用例 `2 passed in 4.98s`；WP2 联合回归
  `63 passed in 87.99s`；全量回归 `352 passed in 549.42s`
- 未新增依赖，LangGraph 节点与边声明零变化
