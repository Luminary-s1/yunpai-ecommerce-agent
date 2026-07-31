# M4 智能客服后端 WP1

- 分支：`feature/m4-customer-service`
- 范围：客服会话管理与上下文控制
- 执行计划：`docs/tasks_intro/M4_DAILY_PLAN.md` D01–D05

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
