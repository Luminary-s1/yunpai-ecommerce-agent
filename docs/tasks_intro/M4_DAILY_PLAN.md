# M4 智能客服后端 — 逐日执行计划

> 本文件是 M4 执行的唯一技术依据，自包含，不依赖其他文档。
> 工作台填写口径见 `docs/tasks/M4_WORKBENCH.md`；
> 真实完成状态见 `docs/tasks/PROGRESS.md`。

## 排期

- 节奏：**每周一个工作包**，每周 5 个工作日 × 4 小时 = 20 小时
- 总量：剩余 100 小时 = **5 周 / 25 个工作日**
- 起止：2026-07-30（周四）～ 2026-09-02（周三）

| 周 | 工作包 | 日期 | 工时 |
|---|---|---|---:|
| 第 1 周 | WP1 客服会话管理与上下文控制 | 07-30 ~ 08-05 | 20h |
| 第 2 周 | WP2 知识库增强回复生成 Pipeline | 08-06 ~ 08-12 | 20h |
| 第 3 周 | WP3 客服意图识别与路由逻辑 | 08-13 ~ 08-19 | 20h |
| 第 4 周 | WP4 客服模块效果评测与调优 | 08-20 ~ 08-26 | 20h |
| 第 5 周 | WP5 端到端联调与交付 | 08-27 ~ 09-02 | 20h |

### 关于执行顺序

工作台里 WP2 优先级是 1，但**执行顺序把 WP1 排在第 1 周**，原因是技术依赖：
流式生成同样要先算 Token 预算再拼 Prompt，先做 SSE 会在预算层落地后返工一遍。
优先级 1 表达的是业务重要性，不是执行次序。向上汇报时要说明这一点。

### 风险提示

**第 2 周是全程最紧的一周。** SSE 改造要动模型网关、抽取编排节点、加两段式生成、
建事件协议、做幂等，20 小时没有任何余量。如果第 2 周要延，第一时间报，
不要挤压第 3 周。

---

## 全局约束（每天都适用）

**分支**：`feature/m4-customer-service`，从 main 开出，不要叠在
`feature/m5-operations-assistant` 上。

**测试命令**（本仓库固定屏蔽代理）：

```bash
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 .venv/bin/python -m pytest -q
```

**不可破坏的项目决策**：

| 编号 | 约束 |
|---|---|
| D-005 | `MODEL_ENABLED=false` 时不得发出任何模型请求 |
| D-007 | 清理与关闭逻辑必须跳过存在非终态 handoff 的会话 |
| D-008 | 运行时用 GLM 标准 Chat Completions，不引入本地大模型或第三方 tokenizer |
| D-010 | 业务意图不写入编排拓扑，不得新增按意图分支的节点或边 |
| D-023 | 回答必须引用不可变上下文快照，不得绕过 `ContextBuilder` |
| D-033 | 评测与模拟会话落 `evaluation` / `simulation` 来源，不污染 `operational` |

**工程约定**：不新增第三方依赖；代码风格跟随周边文件；提交用英文
conventional commits，不加 AI 署名；只 commit 不 push。

---

# 第 1 周｜WP1 客服会话管理与上下文控制

**本周目标**：Token 预算层落地并接进全链路，会话 CRUD 四个端点可用，空闲超时生效。

**现状**：会话表、消息表、持久化、作用域校验、空闲关闭骨架都已存在。
上下文控制目前是三处互不相关的硬编码，没有 token 概念：
`settings.session_history_limit`（默认 6）、`context_builder.py:173` 的
`history[-6:]`、`prompts.py:66` 与 `prompts.py:113-116` 各自再切一次。
调用点在 `graph.py` 的 123 / 179 / 494 / 555 四处。

---

## D01 · 07-30（四）

**目标**：拉起基线，写出 token 计数与截断层。

- 确认在 `feature/m4-customer-service` 分支，与 main 同步
- 跑一次全量测试拿基线，记录 `N passed`；跑不过先停下查因
- 新建 `src/ecommerce_agent/tokens.py`：
  - `count_tokens(text) -> int`：中日韩字符按 1 token/字，其余按 `len/4`
    向上取整，两者相加。**口径写进模块 docstring 并注明是保守估算**
  - `count_messages(messages) -> int`
  - `truncate_history(history, *, budget_tokens) -> tuple[list, dict]`：
    从最新往回保留；**永远至少保留最近 1 轮**，超预算时元信息标 `over_budget=True`；
    元信息为 `{kept, dropped, tokens, budget, over_budget}`

**产出**：`tokens.py`
**判据**：200 轮超长历史截断后 `tokens <= budget` 且最后一轮原样保留

---

## D02 · 07-31（五）

**目标**：配置项与知识块预算，做反证。

- `config.py` 新增 `model_context_limit_tokens`（env `MODEL_CONTEXT_LIMIT_TOKENS`，
  默认 128000）、`context_budget_ratio`（env `CONTEXT_BUDGET_RATIO`，默认 0.7，
  钳制 0.1–0.9）。`session_history_limit` 保留作为条数上限，两个约束取更严的
- `prompts.py` 的 `build_messages` / `build_decision_messages` 删掉内部
  `history[-6:]`，改为消费上游已截断的 history；新增可选参数
  `knowledge_budget_tokens`：知识块超预算时从 score 最低的文档开始丢，至少留 1 条
- 新建 `tests/test_context_budget.py`，写前 4 项测试
- **反证**：`context_budget_ratio` 临时设 0.99 → 截断断言必须失败 → 还原 → 复验

**产出**：配置项 + 知识块预算 + 反证记录
**判据**：4 项测试通过，反证成立并已记录进提交信息

---

## D03 · 08-03（一）

**目标**：接入上下文构建与编排四处调用点。

- `context_builder.py` 的 `build()` 新增 `history_budget_tokens` 参数，
  把 `history[-6:]` 改为调用 `truncate_history`；截断元信息写入
  `bundle["recent_history_meta"]`，并新增一条 `history_window` 类型 evidence
- `graph.py` 四处 `db.recent_messages` 统一走预算层。预算算法：
  总预算 = 上限 × ratio，减去 System Prompt 与用户消息实测 token，
  余下按 `知识 : 历史 = 6 : 4` 分配
- `state["trace"]` 追加 `context:budget:kept{n}/dropped{n}`
- 补第 5 项测试（端到端），跑 `test_react_graph.py` / `test_agent.py`

**产出**：预算层全链路生效
**判据**：一次 chat 后能在 `context_snapshots` 查到 `history_window` 证据；
既有编排测试全绿

---

## D04 · 08-04（二）

**目标**：会话 CRUD 四个端点。

- 新建 `src/ecommerce_agent/chat_sessions_api.py`，仿 `customer_test_api.py` 的
  `build_xxx_router(service, require_client)` 模式，在 `api.py` 挂载
- 四个端点，认证一律 `Depends(require_client)`：

| 方法 | 路径 | 行为 |
|---|---|---|
| POST | `/v1/chat/sessions` | 幂等创建，作用域冲突 409 |
| GET | `/v1/chat/sessions/{id}` | 状态、创建时间、最后活跃、消息数；越权 404 不用 403 |
| GET | `/v1/chat/sessions/{id}/messages` | 游标分页，默认 limit 20 上限 100 |
| DELETE | `/v1/chat/sessions/{id}` | 置 closed；有非终态 handoff 时 409（D-007） |

- `database.py` 新增 `paginated_messages(session_id, cursor, limit)`，
  游标为 `created_at|id` 复合游标，解码失败按无游标处理不抛 500
- 所有查询带 `tenant_id` + `subject_hash` 双重过滤
- 新建 `tests/test_chat_sessions_api.py`，补 7 项测试

**产出**：四个端点齐全
**判据**：55 条消息按 limit=20 翻页无重复无遗漏；带未结 handoff 时 DELETE 返回 409

---

## D05 · 08-05（三）

**目标**：空闲超时与本周收口。

- `config.py` 新增 `SESSION_IDLE_TIMEOUT_MINUTES`（默认 120），与
  `MESSAGE_RETENTION_DAYS` 解耦
- 实现会话 TTL worker，套用项目既有 worker 线程模式；沿用 `purge_expired`
  已有的「跳过未结人工任务」约束
- 写数据模型文档：Session / Message 字段说明 + 四个端点的接口契约
- 全量回归；按子任务分 commit（tokens / 接入 / API / TTL 各一个）
- 更新 `docs/tasks/PROGRESS.md` 复选框，工作台剩余工时改 0

**产出**：WP1 完整交付
**判据**：空闲 121 分钟会话自动关闭且带 handoff 的不被关；全量测试通过

---

# 第 2 周｜WP2 知识库增强回复生成 Pipeline

**本周目标**：对外 SSE 流式输出可用，幂等与降级与非流式一致。

**现状**：RAG 检索、Prompt 组装、防幻觉指令、输出安全门、幂等重放
（`Idempotency-Key` + `agent_invocations` 表，见 `service.py:210-221`）、
三条降级路径都已具备。缺的只有对外流式——`ModelGateway._stream_request`
（`llm.py:180`）**已经在用上游 SSE**，但把 delta 全部 join 成整串返回
（`llm.py:222`）。

**架构决策**（照做，不要自己发挥）：**不要**让 LangGraph 同步节点 yield。
用两段式：① 复用现有图跑到决策完成 → ② `route == "generate"` 时跳出图流式产出
→ ③ 流结束交回既有 `verify` / `persist`（抽成可复用函数）→ ④ `clarify` /
`handoff` / `refuse` / `retry_later` 四条路径不流式，一次性发完整事件。
这样图拓扑零改动，符合 D-010 与 D-023。

---

## D06 · 08-06（四）

**目标**：模型网关流式产出。

- `llm.py` 新增 `stream_generate(messages) -> Iterator[str]`，与
  `_stream_request` 共享请求构造与错误分类，只 yield delta 不 join
- **保留 `_stream_request` 原样**——决策调用仍需要整串 JSON
- `model_mock_mode` 下按字符切分 mock 结果 yield，保证测试不依赖网络
- 429 provider code 与 `ModelUnavailableError` 的分类在流式路径同样生效；
  上游流中途报错要能抛出而不是静默截断
- 补 `tests/test_llm.py` 的流式分支用例

**产出**：流式 generator
**判据**：mock 下能拿到多个 delta，拼接结果与非流式一致；`test_llm.py` 全绿

---

## D07 · 08-07（五）

**目标**：抽取 verify / persist 为可复用函数。

- 把 `graph.py` 的 `verify` 节点逻辑抽成模块级函数，图内节点改为调用
- 把 `persist` 节点抽成模块级函数（含消息落库、invocation 完成、审计三段），
  图内节点改为调用
- **行为必须零变化**，图拓扑不动
- 逐套跑 `test_react_graph.py`、`test_agent.py`、`test_api.py`

**产出**：两个函数可被流式路径复用
**判据**：三套测试全绿；diff 中图拓扑无变化

> 这一步最容易翻车，三套测试必须逐套跑，不要只跑一套就过。

---

## D08 · 08-10（一）

**目标**：两段式生成架构。

- `service.py` 新增 `chat_stream(principal, session_id, message, context, *,
  idempotency_key) -> Iterator[dict]`
- 第一段：复用现有图跑到决策完成，拿 `route` / `context_bundle` / `retrieved` /
  `tool_result`
- 第二段：`route == "generate"` 时跳出图，用 `stream_generate` 产出 delta，
  流结束交回 D07 抽出的 `verify`；校验不通过按既有语义转 handoff
- 四条非生成路径一次性发完整事件，落库时机与非流式一致
- 中断安全：消费一半就关闭迭代器时，数据库不得留半条 assistant 消息

**产出**：两段式跑通
**判据**：mock 下拼接结果等于非流式 `answer`；中途断开无半条记录

---

## D09 · 08-11（二）

**目标**：SSE 端点与六个事件。

- `api.py` 新增 `POST /v1/chat/stream`，返回
  `StreamingResponse(media_type="text/event-stream")`，`Depends(require_client)`
- 每事件一行 `data: {json}\n\n`：

| event | 载荷 | 时机 |
|---|---|---|
| `meta` | `{session_id, message_id, trace_id}` | 首个事件 |
| `delta` | `{text}` | 逐段回复 |
| `citations` | `{sources: [...]}` | 生成结束后、`done` 之前 |
| `handoff` | `{requires_human, handoff_id, handoff_status, reason}` | 需要人工时 |
| `done` | `{message_id, intent, risk_level, model_fallback}` | 正常收尾 |
| `error` | `{code, message, retry_advised}` | 不可用、限流、内部错误 |

- `handoff` 复用既有字段语义不改名；**`error` 后必须紧跟 `done` 关闭流**
- 新建 `tests/test_chat_stream.py`，补事件序列断言

**产出**：端点与事件协议可用
**判据**：一次完整对话的事件序列为 meta → delta* → citations → done

---

## D10 · 08-12（三）

**目标**：幂等、降级与本周收口。

- 幂等命中已完成 invocation 时，直接把已存回复作为单个 `delta` 发出再发 `done`，
  **不重新调模型**（用 mock 调用计数断言）；复用既有幂等键，不另建机制
- 检索无命中走既有降级文案，行为与非流式一致
- `MODEL_ENABLED=false` 时端点不发任何外部请求（D-005）
- **反证**：`stream_generate` 临时改成一次性 yield 全文 → "多个 delta" 断言
  必须失败 → 还原 → 复验
- 写 SSE 事件协议说明文档，供前端对接
- 全量回归，按子任务分 commit

**产出**：WP2 完整交付 + 协议文档
**判据**：8 项流式测试全通过；`test_react_graph.py` / `test_agent.py` /
`test_llm.py` 全绿

---

# 第 3 周｜WP3 客服意图识别与路由逻辑

**本周目标**：受控四分类可用，置信度阈值真正参与路由，意图可追溯。

**现状**：`AgentDecision` 已有 `intent`（自由文本）与 `confidence`
（`decision.py:24,32`），**但 `confidence` 从未参与任何路由判定**——
`decision_gate`（`graph.py:239`）完全没读它，也没写进 `messages` 表。
注意 `database.py:777` 那个 `confidence` 列属于 `channel_reply_drafts`，别弄混。
规则门（`policy.py:102,114`）、SOP 按意图解析版本（`graph.py:277`）、
人工队列全套都已具备，**不要新建队列**。

---

## D11 · 08-13（四）

**目标**：分类器骨架与规则层。

- 新建 `src/ecommerce_agent/intent.py`：
  - `CustomerIntent = Literal["product_inquiry", "after_sales", "complaint",
    "chitchat"]`
  - `IntentResult`：`{intent, confidence, method}`，method ∈ rule / model / default
  - `classify(message, *, model) -> IntentResult`
- 关键词规则表，命中即返回 `confidence=0.95, method="rule"`：
  - 退货 / 退款 / 换货 / 保修 / 物流 → `after_sales`
  - 投诉 / 差评 / 举报 / 曝光 → `complaint`
  - 多少钱 / 规格 / 参数 / 尺寸 / 材质 / 对比 / 推荐 → `product_inquiry`
- 多规则同时命中的优先级：投诉 > 售后 > 商品咨询
- 处理空消息、超长消息、纯符号消息
- 新建 `tests/test_intent_routing.py`，四类各 5 条样例

**产出**：规则层分类可用
**判据**：四类样例分类准确率 ≥75%

---

## D12 · 08-14（五）

**目标**：模型轻量分类与降级。

- 规则未命中时调模型做 few-shot 分类，走**独立的短 prompt**，
  **不复用** `DECISION_SYSTEM_PROMPT`
- `config.py` 新增 `intent_classify_timeout_seconds`（默认 2.0）
- 超时或异常返回 `chitchat` + `confidence=0.0` + `method="default"`，**不抛异常**
- 确认分类不显著增加用户感知延迟

**产出**：两级分类链路完整
**判据**：模型超时时降级不抛且延迟受控

---

## D13 · 08-17（一）

**目标**：schema 迁移与意图持久化。

- `SCHEMA_VERSION` 由 **24 直接设为 26，故意跳过 25**——25 已被尚未合并的
  `feature/m5-operations-assistant` 分支占用（新增 `ops_operation_records`），
  跳号避免两条分支合并时版本号冲突
- 用既有 `_ensure_column`（`database.py:2161`）加列，**不重建表**：
  `messages.customer_intent TEXT` / `intent_confidence REAL` / `intent_method TEXT`
- `_validate_schema` 的 required 字典补上这三列
- `persist` 写入这三个字段
- 补迁移测试，参考 `tests/test_migrations.py` 既有写法

**产出**：意图可追溯
**判据**：v24 → v26 前向迁移成功旧数据不丢；消息表可查三个新字段

---

## D14 · 08-18（二）

**目标**：置信度阈值与投诉优先。

- `config.py` 新增 `handoff_confidence_threshold`（env
  `HANDOFF_CONFIDENCE_THRESHOLD`，默认 0.6，钳制 0–1）
- `decision_gate` 新增判定：`decision.confidence < threshold` 且
  `route in {"answer", "finish"}` → 改 `route="handoff"`，
  `reason="low_confidence_handoff"`
- `customer_intent == "complaint"` → `risk_level` 至少 medium，
  handoff payload 带 `priority_flag="complaint"`，接入既有队列优先级
- 连续低质检测：最近 2 条 assistant 消息的 `route_reason` 都属于
  `{model_unavailable, low_confidence_handoff, no_evidence}` → 强制 handoff，
  `reason="consecutive_low_quality"`
- **反证**：阈值临时设 0 → 低置信度断言必须失败 → 还原 → 复验

**产出**：三条兜底路径齐全
**判据**：`confidence=0.5` 时路由变 handoff；投诉任务进队列可被自动派单消费

---

## D15 · 08-19（三）

**目标**：路由配置与本周收口。

- 新建 `src/ecommerce_agent/intent_routing.json`：意图 →
  `{knowledge_intent, prompt_variant, sop_intent}` 映射，由 `intent.py` 加载
- `retrieve` 节点把写死的 `intent=None`（`graph.py:110`）改为传 `customer_intent`
- 在 `precheck` 节点内部调用 `classify`，**不新增节点**；结果写
  `state["customer_intent"]` 与 `state["intent_confidence"]`
- **自行 diff 确认编排节点与边数量零变化**（D-010 硬检查）
- 全量回归，跑 `test_react_graph.py` / `test_handoffs.py` / `test_migrations.py`

**产出**：WP3 完整交付
**判据**：8 项测试全通过；拓扑零变化；三套既有测试全绿

---

# 第 4 周｜WP4 客服模块效果评测与调优

**本周目标**：四项 M4 口径指标可算，50+ 条冻结用例集就位，调优后达标。

**现状**：`EvaluationService`（`evaluation.py`，1287 行）已有不可变版本、
数据集 SHA-256、隔离快照跑真实多轮 Agent、跨版本回归、门禁阈值。
现有指标是 `pass_rate` / `intent_accuracy` / `handoff_recall` /
`evidence_coverage` / `severe_failures` / `regression_rate`，与 M4 口径不同。
`EvaluationExpectation`（`evaluation.py:34`）已有 `forbidden_answer_terms`，
就是幻觉判定的现成抓手。对抗模式在 `policy.py` 已有三组。

---

## D16 · 08-20（四）

**目标**：断言字段与四项指标。

- `EvaluationExpectation` 新增两个字段：
  - `grounded_in_sources: bool = False`——事实性断言必须能在 sources 找到支撑
  - `expected_refusal: bool | None = None`——该用例是否应当拒答
- `_compute_metrics`（`evaluation.py:1029` 附近）新增四项：

| 指标 | 定义 |
|---|---|
| `answer_accuracy` | 断言全通过、非 `model_fallback`、非 severe 的占比 |
| `hallucination_rate` | 命中 `forbidden_answer_terms`，或声明 `grounded_in_sources` 但回答含 sources 无法支撑的数值/承诺 |
| `refusal_rate` | `expected_refusal=false` 但实际走 refuse / no_evidence / handoff 的占比 |
| `handoff_precision` | 实际转人工中 `expected_requires_human=true` 的占比 |

- 定义写进 docstring
- 新建 `tests/test_customer_service_eval.py`，构造已知结果集手算期望值断言

**产出**：四项指标可算
**判据**：手算期望值与计算结果一致

---

## D17 · 08-21（五）

**目标**：门禁阈值与判定标准文档。

- `EvaluationThresholds` 新增 `min_answer_accuracy=0.75`、
  `max_hallucination_rate=0.10`、`max_refusal_rate=0.20`，接入 `_build_gate`
- 写判定标准定义文档：
  - 什么算「准确」：基于知识库且事实正确，可追溯到具体 source
  - 什么算「幻觉」：编造知识库中不存在的信息，含未出现的价格、库存、到账时间
  - 什么算「应答未答」与「转人工合理」
- 四项指标的计算公式同步到交付文档

**产出**：门禁 + 判定标准文档
**判据**：`hallucination_rate=0.15` 时 gate 必须 fail

---

## D18 · 08-24（一）

**目标**：用例集前半（商品 + 售后 + 多轮）。

- 新建 `src/ecommerce_agent/fixtures/customer_service_eval_v1.json`
- 商品咨询 15 条：规格、价格、对比、推荐；**必须引用虚拟店铺数据包里真实存在的
  6 个 SKU**
- 售后 12 条：退换货、保修、物流；引用数据包里的 8 个订单与售后单
- 其中至少 8 条改造为**多轮**用例，含 3 条专测指代消解
  （「它多少钱」「这个能退吗」）

**产出**：27 条用例
**判据**：每条的期望答案能在知识库中找到依据；多轮用例第二轮确实依赖上下文

---

## D19 · 08-25（二）

**目标**：用例集后半与冻结。

- 投诉 8 条：质量、服务、配送；全部 `expected_requires_human=true`
- 闲聊 5 条：`expected_intent="chitchat"`，`expected_refusal=false`
- 对抗 10+ 条：Prompt 注入、越权请求（索取他人订单或其他店铺数据）、
  诱导幻觉（问不存在的商品参数）、敏感话题；全部 `expected_refusal=true`
  或由 `forbidden_answer_terms` 覆盖系统提示词片段
- 走既有冻结版本与数据集哈希机制冻结

**产出**：50+ 条用例集冻结
**判据**：条数 ≥50，五类分布符合要求；冻结后修改被拒绝，哈希稳定

---

## D20 · 08-26（三）

**目标**：评测、调优、复测，本周收口。

- 新建 `scripts/run_customer_eval.py`：一键导入 fixture、冻结版本、执行 run、
  输出报告
- 在隔离数据库快照跑首轮，**主库 `sessions` 表零新增**（D-033），记录基线数值
- 逐条分析失败用例，归因到四类之一：Prompt 不明确 / 检索召回不到 /
  意图路由错误 / 截断丢失关键上下文
- 针对性调优（每次只动一个变量并记录指标变化）：Prompt 模板、检索 top-k 与
  min_score、意图规则与转人工阈值、截断窗口比例
- 复测确认 `answer_accuracy ≥ 0.75`、`hallucination_rate ≤ 0.10`
- **反证**：临时移除某条对抗用例的 `forbidden_answer_terms` → 幻觉率必须变化
  → 还原
- 输出最终 Prompt 模板与参数配置，**同时标注 mock 与 live 两种模式的结果**

**产出**：WP4 完整交付
**判据**：两项基线达标；6 项测试全通过；调优记录有指标变化数据

---

# 第 5 周｜WP5 端到端联调与交付

**本周目标**：真实模型跑通并留证，场景契约扩到 18 项，交付文档与台账齐全。

**现状**：虚拟店铺场景契约当前 16 项（`simulation-evidence-v1`），
本机顾客直测入口在 `/admin` 智能客服页，管理后台已聚合会话回放与评测。
M4 目前在 `docs/works/` 下尚无交付文档。

---

## D21 · 08-27（四）

**目标**：真实模型联调与实跑证据。

- 用隔离 `DATA_DIR` 与真实 GLM 配置启动（密钥从本机 `env.md` 加载，
  **不写进命令历史或文档**）
- 走通 `/admin` 顾客直测页，记录 `yunpai-agent init` 的 schema 版本与模型标识
- 取三组实跑截图：流式逐字输出、多轮指代（「它多少钱」）、
  低置信度触发转人工
- 浏览器 console error / warning 应为 0

**产出**：三组实跑截图
**判据**：截图为真实 PNG，能看出逐字输出与上下文连续

---

## D22 · 08-28（五）

**目标**：场景 D17 多轮指代。

- `fixtures/virtual_store_v1.json` 新增场景定义，含固定 input / expected /
  assertions
- `simulation.py` 实现 `_verify_multi_turn_reference`
- 断言必须可确定性复现，**不依赖模型每次输出一致**

**产出**：D17 场景
**判据**：场景独立跑通且断言稳定

---

## D23 · 08-31（一）

**目标**：场景 D18 意图路由，契约扩到 18 项。

- 新增场景定义与 `_verify_intent_routing`，覆盖四类意图各一条 +
  一条低置信度转人工
- 把 `simulation-evidence-v1` 契约从 16 项扩到 18 项
- 更新 `tests/test_virtual_store_simulation.py` 的场景总数与模块覆盖断言
- **门禁反证**：临时移除某条断言 → `report["passed"]` 由 True 变 False → 还原

**产出**：18 项场景契约
**判据**：18 项全通过；门禁反证成立并还原

---

## D24 · 09-01（二）

**目标**：全量回归与验证文档主体。

- 跑完整 `pytest -q`，记录 `N passed in Xs` 与退出码；用例数增长要能逐条归因
- `git diff --check` 无输出；`python -m compileall -q src` 通过
- 汇总五处反证的过程与结果（预算 ratio、流式 yield、置信度阈值、幻觉率、
  场景门禁），格式：临时改了什么 → 哪个断言失败 → 已还原 → 复验结果
- 新建 `docs/works/13-feature-m4-customer-service/README.md`，
  格式参照 `docs/works/12-feature-m5-operations-assistant/README.md`：
  交付范围表、关键实现、安全与执行边界

**产出**：全量回归结果 + 文档主体
**判据**：全绿或失败项能明确归因；交付范围表逐项对应验收标准

---

## D25 · 09-02（三）

**目标**：截图归档、台账同步、模块交付。

- 归档 D21 三组实跑截图到文档目录，补测试证据章节
- **如实记录 live 模型波动**：既往两次实跑分别为 15 通过 1 失败与
  14 通过 2 失败，失败集中在高风险诉求转人工判定；本模块改动会放大该波动；
  验收以受控测试断言为准，live 结果单独标注
- 同步四份台账：
  - `PROJECT_FEATURES.md` 登记 F-124 SSE 流式客服接口、F-125 会话 Token 预算与
    生命周期、F-126 意图分类与置信度路由，更新 F-117 状态
  - `PROJECT_PROGRESS.md` 追加进度历史
  - `PROJECT_VERSIONS.md` 版本推进与兼容性说明（schema v26 additive）
  - `PROJECT_ACCEPTANCE.md` 补证据 ID `E-2026MMDD-NNN`
- 工作台五个工作包剩余工时改 0、进度改 100%，模块交付

**产出**：M4 模块交付完成
**判据**：四份台账已同步且证据 ID 可追溯

---

## 每日收尾（每天都做）

1. 当日代码单独 commit，英文 conventional commits，不加 AI 署名
2. 跑当日涉及的定向测试，不必每天跑全量
3. 更新 `docs/tasks/PROGRESS.md` 复选框
4. 工作台更新剩余工时；有延期当天就填「阻塞或延期原因」，不要事后补

## 需要停下来找人的情况

- 需要新增第三方依赖
- 某项改动会改变 `/v1/chat` 既有响应契约
- D08 的两段式架构被证明走不通
- 全量测试出现与本模块无关的既有失败（live 模型下客服类场景本就不稳定，
  这类单独标注而不是「修好」）
- 任一周延期超过 2 天
