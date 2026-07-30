# M4 智能客服后端 — 剩余增量任务书

> 面向执行者的自包含任务说明。执行前请完整阅读第 0 节。
> 本文描述的是在既有实现之上的**增量**，不是从零开发。

## 0. 全局约束

### 仓库与分支

- 仓库：`yunpai-ecommerce-agent`，Python 3.11+，FastAPI + LangGraph + SQLite
- 分支：从 fork `main` 新开 `feature/m4-customer-service`，不要叠在 `feature/m5-operations-assistant` 上
- 基线：全量 `pytest -q` 应为 `313 passed`。动手前先跑一次确认基线；跑不过先停下报告，不要在坏基线上开工

测试需要屏蔽代理，本仓库固定用法：

```bash
NO_PROXY=127.0.0.1,localhost \
no_proxy=127.0.0.1,localhost \
ALL_PROXY=http://127.0.0.1:9 \
HTTP_PROXY=http://127.0.0.1:9 \
HTTPS_PROXY=http://127.0.0.1:9 \
.venv/bin/python -m pytest -q
```

### 不可破坏的既有边界

这些是项目决策记录（见 `.project-to-act/PROJECT_OVERVIEW.md`），不是可商量的实现细节。

| 编号 | 约束 | 具体含义 |
|---|---|---|
| D-005 | 模型默认禁用 | `MODEL_ENABLED=false` 时不得发出任何模型请求 |
| D-007 | 未结人工任务阻断清理 | 任何清理或关闭逻辑必须跳过存在非终态 handoff 的会话 |
| D-008 | 运行时用 GLM 标准 Chat Completions | 不要引入本地大模型、vLLM、HuggingFace tokenizer 依赖 |
| D-010 | 业务意图不写入编排拓扑 | 意图分类只影响检索范围与 Prompt 变体，不得在 LangGraph 中新增按意图分支的节点或边 |
| D-023 | 回答必须引用不可变上下文快照 | 不得绕过 `ContextBuilder` 直接拼 prompt |
| D-033 | 运营数据默认隔离 | 评测与模拟产生的会话必须落 `simulation` / `evaluation` 来源，不得污染 `operational` |

### 工程约定

- **代码风格**：跟随周边代码。注释密度低、只在非显然处写，与该文件既有风格一致，不要加装饰性注释
- **提交规范**：英文 conventional commits，与 `git log` 既有风格一致（`feat(scope): ...`、`test(scope): ...`、`docs(scope): ...`）。不得添加任何 AI 署名、`Co-Authored-By` 或生成工具页脚。只 commit，不 push
- **依赖**：不新增第三方依赖。若某任务确实需要，先停下来说明理由，不要自行加进 `pyproject.toml`

### 任务顺序

T1 → T3 有依赖，必须先完成 T1。T2、T4、T5 相互独立，可任意顺序或并行分配。

| 任务 | 内容 | 预计工时 | 依赖 |
|---|---|---|---|
| T1 | Token 预算与上下文截断 | 16 | 无 |
| T2 | 会话 CRUD 对外接口 | 8 | 无 |
| T3 | SSE 流式回复接口 | 24 | T1 |
| T4 | 意图四分类与置信度路由 | 20 | 无 |
| T5 | 评测指标扩展与 50+ 用例集 | 20 | 无 |

---

## T1. Token 预算与上下文截断（16 小时）

### 现状

上下文窗口控制目前是三处互不相关的硬编码，没有任何 token 概念：

- `settings.session_history_limit`（默认 6）→ `db.recent_messages()` 取最近 6 条
- `context_builder.py:173` `_safe_value(history[-6:])`
- `prompts.py:66` 与 `prompts.py:113-116` 各自再做一次 `history[-6:]`
- `_safe_value` 对单个字符串截断到 2000 字符

调用点在 `graph.py` 的 123、179、494、555 四处。仓库内无任何 token 计数代码。

### 目标

新增统一的 token 预算层，保证 `System Prompt + RAG 知识片段 + 对话历史 + 用户消息` 的总量不超过模型上下文上限的 70%。

### 改动清单

**新建 `src/ecommerce_agent/tokens.py`**

```python
def count_tokens(text: str) -> int: ...
def count_messages(messages: list[dict[str, str]]) -> int: ...
def truncate_history(
    history: list[dict[str, Any]],
    *,
    budget_tokens: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]: ...
```

- 计数实现不引入新依赖：中日韩字符按 1 token/字，其余按 `len/4` 向上取整，取两者之和。**必须**把估算口径写在模块 docstring 里，并说明这是保守估算而非精确 tokenizer
- `truncate_history` 从最新一条往回保留，直到超预算为止；**永远至少保留最近 1 轮**（1 条 user + 1 条 assistant），即使超预算也保留并在元信息中标记 `over_budget=True`
- 第二个返回值为元信息 `{"kept": n, "dropped": n, "tokens": n, "budget": n, "over_budget": bool}`，供审计与测试断言

**`src/ecommerce_agent/config.py`** 新增配置项：

- `model_context_limit_tokens`：环境变量 `MODEL_CONTEXT_LIMIT_TOKENS`，默认 `128000`
- `context_budget_ratio`：环境变量 `CONTEXT_BUDGET_RATIO`，默认 `0.7`，钳制在 `0.1`–`0.9`
- `session_history_limit` 保留不动，作为条数上限的第二道保险，两个约束取更严的

**`src/ecommerce_agent/context_builder.py`**

- `build()` 新增参数 `history_budget_tokens: int`
- 把 `history[-6:]` 改为调用 `truncate_history`
- 截断元信息进 `bundle["recent_history_meta"]`，并作为一条 evidence（`type="history_window"`）加入证据列表

**`src/ecommerce_agent/prompts.py`**

- `build_messages` 与 `build_decision_messages` 内部的 `history[-6:]` 全部删除，改为直接消费上游已截断好的 history
- 两个函数各新增可选参数 `knowledge_budget_tokens: int | None = None`：知识块拼完超预算时，从 `documents` 末尾（score 最低）开始丢，直到入预算；至少保留 1 条

**`src/ecommerce_agent/graph.py`**

- 四处 `db.recent_messages(...)` 调用统一改为经过预算层
- 预算计算：`总预算 = model_context_limit_tokens * context_budget_ratio`，减去 System Prompt 与用户消息实测 token，余下部分按 `知识 : 历史 = 6 : 4` 分配
- 截断结果写入 `state["trace"]`，格式 `context:budget:kept{n}/dropped{n}`

### 测试要求

新建 `tests/test_context_budget.py`：

1. 中文、英文、混合文本的计数单调性与非零
2. 构造 200 轮超长历史，断言截断后 `tokens <= budget` 且最后一轮内容原样保留
3. 极端情况：单条消息本身就超预算 → 仍保留 1 轮且 `over_budget=True`
4. 知识块超预算时按 score 从低到高丢弃，至少保留 1 条
5. 端到端：一次 `service.chat()` 后，`context_snapshots` 中能查到 `history_window` 证据与截断元信息
6. **反证**：临时把 `context_budget_ratio` 设为 `0.99` 后，第 2 项测试必须失败。反证做完还原，并在提交信息中记录

### 完成判据

上述 6 项通过；全量回归无新增失败；`recent_history` 相关的既有测试若因条数变化而失败，应修改测试断言，不得绕过预算层。

---

## T2. 会话 CRUD 对外接口（8 小时）

### 现状

`sessions` 表（`database.py:272`）与 `Database.resolve_session()`（`database.py:2627`）已具备完整生命周期与租户/主体绑定校验。但对外只有 `POST /v1/chat`（`api.py:490`）隐式建会话；管理员侧 `/v1/admin/conversations` 是只读且消息无分页。

### 目标

新增四个**客户侧**（非管理员）会话端点。

### 改动清单

**新建 `src/ecommerce_agent/chat_sessions_api.py`**，仿照 `src/ecommerce_agent/customer_test_api.py` 的 router 构造方式：

```python
def build_chat_sessions_router(service, require_client) -> APIRouter
```

挂载点在 `api.py:175` 之后，与既有 `include_router` 调用并列。

| 方法 | 路径 | 行为 |
|---|---|---|
| POST | `/v1/chat/sessions` | 幂等创建。body 含 `external_session_id` 与可选 `context`。已存在且作用域匹配返回既有会话（200）；作用域冲突返回 409 |
| GET | `/v1/chat/sessions/{external_session_id}` | 返回状态、创建时间、最后活跃时间、消息数。非本租户或非本主体返回 404，不用 403，避免泄露存在性 |
| GET | `/v1/chat/sessions/{external_session_id}/messages` | 游标分页。query：`cursor`（上一页最后一条的 `created_at\|id` 复合游标）、`limit`（默认 20，上限 100）。按 `created_at ASC, id ASC` 排序 |
| DELETE | `/v1/chat/sessions/{external_session_id}` | 置 `status='closed'`。存在非终态 handoff 时拒绝并返回 409 并说明原因（D-007） |

- 认证一律 `Depends(require_client)`，与 `/v1/chat` 使用同一把锁
- 所有查询必须带 `tenant_id` + `subject_hash` 双重过滤
- 消息内容返回已脱敏的 `content`，不返回 `context_snapshot_id` 以外的内部字段

**`src/ecommerce_agent/database.py`** 新增 `paginated_messages(session_id, cursor, limit)`。游标解码失败按无游标处理，不得抛 500。

### 测试要求

新建 `tests/test_chat_sessions_api.py`：

1. 创建幂等：同 `external_session_id` 调两次返回同一 `session_id`
2. 跨租户、跨主体访问返回 404
3. 分页：写入 55 条消息，按 `limit=20` 翻页取满 55 条且无重复无遗漏
4. 游标非法（乱码、超长）返回首页而非 500
5. 关闭会话后 `POST /v1/chat` 对该会话返回 409
6. 存在 `proposed` 状态 handoff 时 DELETE 返回 409；handoff 转 `completed` 后 DELETE 成功
7. 未认证请求返回 401

### 完成判据

7 项通过；`/openapi.json` 端点数增加 4；既有 `/v1/chat` 行为零变化（跑 `tests/test_api.py`、`tests/test_auth_sessions.py` 确认）。

---

## T3. SSE 流式回复接口（24 小时，风险最高）

> 先读完整段再动手。这是唯一有架构风险的任务，做错方向会推倒重来。

### 现状

`ModelGateway._stream_request`（`llm.py:180`）**已经在用上游 SSE**，但把所有 delta `join` 成整串返回（`llm.py:222`）。对外没有任何流式：`/v1/chat` 是同步 `graph.invoke()` 返回完整 `ChatResponse`。

幂等已经解决：`Idempotency-Key` + `agent_invocations` 表（`service.py:210-221`），重放直接返回同一条回复。**不要另建幂等机制。**

### 架构决策

照这个做，不要自己发挥。**不要**试图让 LangGraph 的同步节点 yield，改用两段式：

1. 复用现有图跑到决策完成，拿到 `route`、`context_bundle`、`retrieved`、`tool_result`
2. 仅当 `route == "generate"` 时，跳出图、用流式网关直接产出 delta
3. 流结束后，把完整回复交回既有的 `verify` 与 `persist` 逻辑（抽成可复用函数，不要复制粘贴）
4. `clarify` / `handoff` / `refuse` / `retry_later` 四条路径**不流式**，直接一次性发完整事件。它们的文案由代码生成，没有流的意义

这样图的拓扑零改动，符合 D-010 与 D-023。

### 改动清单

**`src/ecommerce_agent/llm.py`**

- 新增 `stream_generate(messages) -> Iterator[str]`，与 `_stream_request` 共享请求构造与错误分类，只是 yield delta 而非 join
- 保留 `_stream_request` 原样，决策调用仍需要整串 JSON
- `model_mock_mode` 下 `stream_generate` 按字符切分 mock 结果 yield，保证测试不依赖网络

**`src/ecommerce_agent/service.py`**

- 新增 `chat_stream(principal, session_id, message, context, *, idempotency_key) -> Iterator[dict]`
- 幂等命中已完成的 invocation 时，直接把已存回复作为单个 `delta` 事件发出再发 `done`，不重新调模型
- 把 `graph.py` 的 `verify` 与 `persist` 节点逻辑抽取为可被流式路径复用的函数，图内节点改为调用它们，避免两份实现漂移

**`src/ecommerce_agent/api.py`** 新增 `POST /v1/chat/stream`，返回 `StreamingResponse(media_type="text/event-stream")`，`Depends(require_client)`。

事件协议，每个事件一行 `data: {json}\n\n`：

| event | 载荷 | 时机 |
|---|---|---|
| `meta` | `{session_id, message_id, trace_id}` | 首个事件，流开始即发 |
| `delta` | `{text}` | 逐段回复内容 |
| `citations` | `{sources: [...]}` | 生成结束后、`done` 之前 |
| `handoff` | `{requires_human, handoff_id, handoff_status, reason}` | 需要人工时 |
| `done` | `{message_id, intent, risk_level, model_fallback}` | 正常收尾 |
| `error` | `{code, message, retry_advised}` | 模型不可用、限流、内部错误 |

`error` 后必须紧跟 `done` 关闭流，不要让客户端挂死。

### 测试要求

新建 `tests/test_chat_stream.py`：

1. mock 模型下收到多个 `delta`，拼接结果等于非流式 `/v1/chat` 的 `answer`
2. 同 `Idempotency-Key` 第二次请求：不产生新的模型调用（用 mock 计数断言）、返回同一 `message_id`、`messages` 表行数不变
3. 中途断开（消费一半就关闭迭代器）：数据库不留下半条 assistant 消息
4. 模型抛 `ModelUnavailableError` → 收到 `error` 事件且 `retry_advised=true`，紧跟 `done`
5. RAG 无命中 → 走降级文案，行为与非流式一致
6. 触发转人工的问题 → 收到 `handoff` 事件且 `handoff_id` 非空
7. `MODEL_ENABLED=false` 时端点不发出任何外部请求（D-005）
8. **反证**：临时把 `stream_generate` 改成一次性 yield 全文，第 1 项的"多个 delta"断言必须失败

### 完成判据

8 项通过；`/v1/chat` 非流式行为零变化；`tests/test_react_graph.py`、`tests/test_agent.py`、`tests/test_llm.py` 全绿。抽取 `verify` / `persist` 最容易在这里翻车，重点复验。

---

## T4. 意图四分类与置信度路由（20 小时）

### 现状

`AgentDecision` 已有 `intent`（自由文本）与 `confidence`（`decision.py:24,32`）。**但 `confidence` 从未参与任何路由判定**：`decision_gate`（`graph.py:239`）完全没读它，也没写进 `messages` 表。注意 `database.py:777` 那个 `confidence` 列属于 `channel_reply_drafts`，与消息表无关。

已有可复用的部分：

- 规则门：`precheck_request`、`is_business_action_request`（`policy.py:102,114`）
- Prompt 变体骨架：`sops.resolve_for_session(tenant, session, intent)`（`graph.py:277`）
- 人工接管全套（队列、SLA、坐席、自动派单）。**不要新建队列**

### 目标

补受控四分类与置信度阈值转人工，且不改变 LangGraph 拓扑（D-010）。

### 改动清单

**新建 `src/ecommerce_agent/intent.py`**

```python
CustomerIntent = Literal["product_inquiry", "after_sales", "complaint", "chitchat"]

RULES: tuple[tuple[CustomerIntent, tuple[str, ...]], ...] = (...)

def classify(message: str, *, model: ModelGateway | None) -> IntentResult: ...
```

- `IntentResult` 含 `{intent, confidence, method}`，`method ∈ {"rule", "model", "default"}`
- 两级判定：关键词规则命中即返回 `confidence=0.95, method="rule"`；未命中且 `model` 可用则走 few-shot 轻量分类，**超时 2 秒**；超时或失败返回 `chitchat` + `confidence=0.0` + `method="default"`
- 规则表至少覆盖：退货、退款、换货、保修、物流 → `after_sales`；投诉、差评、举报、曝光 → `complaint`；多少钱、规格、参数、尺寸、材质、对比、推荐 → `product_inquiry`
- 模型分类走独立的短 prompt，**不复用** `DECISION_SYSTEM_PROMPT`

**`src/ecommerce_agent/config.py`** 新增：

- `handoff_confidence_threshold`：环境变量 `HANDOFF_CONFIDENCE_THRESHOLD`，默认 `0.6`，钳制 `0.0`–`1.0`
- `intent_classify_timeout_seconds`：默认 `2.0`

**`src/ecommerce_agent/graph.py`**

- 在既有 `precheck` 节点内部调用 `classify`（**不新增节点**），结果写 `state["customer_intent"]` 与 `state["intent_confidence"]`
- `decision_gate` 内新增判定：`decision.confidence < threshold` 且 `route in {"answer", "finish"}` → 改 `route="handoff"`，`reason="low_confidence_handoff"`
- `customer_intent == "complaint"` → `risk_level` 至少 `medium`，且 handoff payload 带 `priority_flag="complaint"`
- 新增连续低质检测：`db.recent_messages` 中最近 2 条 assistant 消息的 `route_reason` 都属于低质集合（`model_unavailable`、`low_confidence_handoff`、`no_evidence`）→ 强制 handoff，`reason="consecutive_low_quality"`
- 检索范围：把 `customer_intent` 传给 `knowledge.retrieve(intent=...)`，替换 `retrieve` 节点当前写死的 `intent=None`（`graph.py:110`）

**`src/ecommerce_agent/database.py`**

- `SCHEMA_VERSION` 由 **24** 直接设为 **26**，**故意跳过 25**。25 已被尚未合并的
  `feature/m5-operations-assistant` 分支占用（该分支新增 `ops_operation_records`），
  跳号可避免两条分支合并时版本号冲突。迁移用 `_ensure_column` 加列，本身即为
  additive、可从任意历史版本前向迁移，跳号不影响升级路径
- 用既有 `_ensure_column` helper（`database.py:2161`）加列，**不要**重建表：
  - `messages.customer_intent TEXT`
  - `messages.intent_confidence REAL`
  - `messages.intent_method TEXT`
- `_validate_schema` 的 `required` 字典补上这三列
- `persist` 节点写入这三个字段

**新建 `src/ecommerce_agent/intent_routing.json`**：意图到 `{knowledge_intent, prompt_variant, sop_intent}` 的映射表，由 `intent.py` 加载。这是"路由配置文件"这项交付物。

### 测试要求

新建 `tests/test_intent_routing.py`：

1. 四类各 5 条样例，规则层分类正确
2. 规则未命中时走模型；模型超时或异常时降级为 `default` 且不抛
3. `confidence=0.5 < 0.6` → 路由变 handoff，`reason="low_confidence_handoff"`
4. 投诉意图 → `risk_level >= medium` 且 handoff payload 带 `priority_flag`
5. 连续两轮低质 → 第三轮强制 handoff
6. `messages` 表可查到三个新字段
7. schema 从 v24 前向迁移到 v26 成功，旧数据不丢；从更早版本（v19 起）逐级前向迁移
   同样成功（参考 `tests/test_migrations.py` 既有写法）
8. **反证**：临时把 `handoff_confidence_threshold` 设为 `0.0`，第 3 项必须失败

### 完成判据

8 项通过；**LangGraph 的节点与边数量零变化**，这是 D-010 的硬检查，自行 diff 确认；`tests/test_react_graph.py`、`tests/test_handoffs.py`、`tests/test_migrations.py` 全绿。

---

## T5. 评测指标扩展与 50+ 用例集（20 小时）

### 现状

`EvaluationService`（`evaluation.py`，1287 行）已具备：不可变版本、冻结、数据集 SHA-256、隔离 DB 快照跑真实多轮 Agent、跨版本回归、门禁阈值、发布关联。

- 现有指标（`evaluation.py:1029` 附近）：`pass_rate`、`intent_accuracy`、`handoff_recall`、`evidence_coverage`、`severe_failures`、`regression_rate`
- 断言维度 `EvaluationExpectation`（`evaluation.py:34`）已有 `forbidden_answer_terms`，这是幻觉判定的现成抓手
- 对抗模式已有：`policy.py` 的 `PROMPT_INJECTION_PATTERNS`、`FORBIDDEN_OUTPUT_PATTERNS`、`UNAUTHORIZED_DATA_PATTERNS`

### 目标

补四项 M4 口径指标与 50+ 条冻结用例集。在既有服务上扩展，不要另起一套评测。

### 改动清单

**`src/ecommerce_agent/evaluation.py`**

`EvaluationExpectation` 新增两个字段：

- `grounded_in_sources: bool = False` —— 回答中的事实性断言必须能在 `sources` 中找到支撑
- `expected_refusal: bool | None = None` —— 该用例是否应当拒答

`_compute_metrics` 新增四项指标：

| 指标 | 定义 |
|---|---|
| `answer_accuracy` | 断言全通过、非 `model_fallback`、非 severe 的用例占比 |
| `hallucination_rate` | 命中任一 `forbidden_answer_terms`，或 `grounded_in_sources=true` 但回答含 `sources` 无法支撑的具体数值或承诺，占比 |
| `refusal_rate` | `expected_refusal=false` 但实际走了 refuse / no_evidence / handoff 路径的占比，即"应答未答" |
| `handoff_precision` | 实际转人工中 `expected_requires_human=true` 的占比，与既有 `handoff_recall` 互补 |

定义必须写进 docstring 并落到交付文档。

`EvaluationThresholds` 新增 `min_answer_accuracy=0.75`、`max_hallucination_rate=0.10`、`max_refusal_rate=0.20`，并接入 `_build_gate`。

**新建 `src/ecommerce_agent/fixtures/customer_service_eval_v1.json`**，50+ 条用例：

| 类别 | 条数 | 要求 |
|---|---|---|
| 商品咨询 | 15 | 规格、价格、对比、推荐；必须引用虚拟店铺数据包中真实存在的 6 个 SKU |
| 售后问题 | 12 | 退换货、保修、物流；引用数据包中的 8 个订单与售后单 |
| 投诉建议 | 8 | 质量、服务、配送；`expected_requires_human=true` |
| 闲聊其他 | 5 | `expected_intent="chitchat"`，`expected_refusal=false` |
| 对抗 | 10+ | Prompt 注入、越权请求（索取他人订单或其他店铺数据）、诱导幻觉（询问不存在的商品参数）、敏感话题；全部 `expected_refusal=true` 或由 `forbidden_answer_terms` 覆盖系统提示词片段 |

其中至少 8 条为**多轮**用例，包含 3 条专门验证指代消解（"它多少钱"、"这个能退吗"）。

**新建 `scripts/run_customer_eval.py`**：一键导入 fixture、冻结版本、执行 run、输出报告到 `docs/CUSTOMER_SERVICE_EVAL_0.30.0.md`。

### 测试要求

新建 `tests/test_customer_service_eval.py`：

1. fixture 通过 Pydantic 校验，条数 ≥ 50，五类分布符合上表
2. 四项新指标的计算正确性：构造已知结果集，手算期望值并断言
3. 门禁：`hallucination_rate=0.15` 时 gate 必须 fail
4. 评测运行落在隔离快照，主库 `sessions` 表零新增（D-033）
5. 冻结后修改用例被拒绝，数据集哈希稳定
6. **反证**：临时移除某条对抗用例的 `forbidden_answer_terms`，第 3 项的幻觉率必须变化

### 完成判据

6 项通过；`scripts/run_customer_eval.py` 在 mock 模型下可跑完并产出报告；报告中四项指标有实际数值。真实模型下的数值波动属预期，需在报告中如实标注两种模式的结果，不得只报好看的那个。

---

## 交付与提交

### 每个任务完成后

1. 跑该任务的定向测试与全量回归
2. 执行反证：临时破坏该能力 → 对应测试必须失败 → 还原 → 复验
3. 单独 commit，信息格式示例：

```
feat(context): budget conversation history by token count

- tokens.py provides deterministic estimation and history truncation
- context_builder / prompts / graph share one budget layer
- 6 targeted tests plus counter-proof (ratio=0.99 breaks the assertion)
```

### 全部完成后

1. 全量测试，记录最终 `N passed in Xs` 与退出码
2. `git diff --check` 无输出；`python -m compileall -q src` 通过
3. 新建 `docs/works/13-feature-m4-customer-service/README.md`，格式参照 `docs/works/12-feature-m5-operations-assistant/README.md`：交付范围表、关键实现、安全与执行边界、测试证据含反证记录、浏览器实跑
4. 同步 `.project-to-act` 四份台账：
   - `PROJECT_FEATURES.md` 新增 F-124 SSE 流式客服接口、F-125 会话 Token 预算与生命周期、F-126 意图分类与置信度路由
   - `PROJECT_PROGRESS.md` 追加进度历史
   - `PROJECT_VERSIONS.md` 版本推进与兼容性说明
   - `PROJECT_ACCEPTANCE.md` 补证据 ID

### 必须停下来问的情况

- 需要新增第三方依赖
- 发现某项改动会改变 `/v1/chat` 的既有响应契约
- 全量测试出现与本任务无关的既有失败。`live` 模型下客服类虚拟场景本就不稳定，这类要单独标注而不是"修好"
- T3 的两段式架构在实现中被证明走不通
