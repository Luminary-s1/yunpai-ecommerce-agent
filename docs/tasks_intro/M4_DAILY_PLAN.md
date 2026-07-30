# M4 智能客服后端 — 逐日执行计划

> 配套文档：技术实现细节见 `docs/tasks_intro/M4_TASK_SPEC.md`；
> 工作台填写口径见 `docs/tasks/M4_WORKBENCH.md`；
> 真实完成状态见 `docs/tasks/PROGRESS.md`。

## 使用说明

- 节奏：**每日 4 小时**（三模块并行，M4 占 4h/天），剩余 194 小时 = **49 个工作日**
- 起始：2026-07-30（周四），预计完成 2026-10-06
- 每天一格，含目标、动作、产出、当日完成判据
- 一天没做完就顺延，**不要压缩后面的天**——把延期原因记进工作台的「阻塞或延期原因」

### 关于执行顺序

工作台里工作包 2 的优先级是 1，但**执行顺序把工作包 1 排在前面**，原因是技术依赖：
流式生成同样要先算 Token 预算再拼 Prompt，先做 SSE 会在预算层落地后返工一遍。
优先级 1 表达的是业务重要性，不是执行次序。这一点在向上汇报时要说明。

### 日历风险

D46–D49（2026-10-01 至 10-06）落在国庆假期区间。按实际放假安排，
这四天大概率要顺延到 10-08 之后，整体完成时间相应推迟约一周。
**排期时先把这件事跟负责人对齐**，不要到 10 月才说。

---

## 阶段总览

| 阶段 | 工作包 | 子任务数 | 工时 | 日期区间 |
|---|---|---:|---:|---|
| A | WP1 客服会话管理与上下文控制 | 4 | 32h | D01–D08（07-30 ~ 08-10） |
| B | WP2 知识库增强回复生成 Pipeline | 6 | 56h | D09–D22（08-11 ~ 08-28） |
| C | WP3 客服意图识别与路由逻辑 | 5 | 32h | D23–D30（08-31 ~ 09-09） |
| D | WP4 客服模块效果评测与调优 | 6 | 34h | D31–D39（09-10 ~ 09-22） |
| E | WP5 端到端联调与交付 | 5 | 40h | D40–D49（09-23 ~ 10-06） |
| | **合计** | **26** | **194h** | **49 个工作日** |

---

## 子任务拆解

### 阶段 A｜WP1 客服会话管理与上下文控制（32h）

| 子任务 | 内容 | 工时 | 天 |
|---|---|---:|---|
| 1.1 | Token 计数与截断层 | 12h | D01–D03 |
| 1.2 | 接入编排与上下文快照 | 8h | D04–D05 |
| 1.3 | 会话 CRUD API | 8h | D06–D07 |
| 1.4 | 空闲超时 worker 与数据模型文档 | 4h | D08 |

### 阶段 B｜WP2 知识库增强回复生成 Pipeline（56h）

| 子任务 | 内容 | 工时 | 天 |
|---|---|---:|---|
| 2.1 | 模型网关流式产出 | 8h | D09–D10 |
| 2.2 | verify / persist 抽取为可复用函数 | 8h | D11–D12 |
| 2.3 | 两段式生成架构 | 16h | D13–D16 |
| 2.4 | SSE 端点与事件协议 | 12h | D17–D19 |
| 2.5 | 幂等重放与断连处理 | 8h | D20–D21 |
| 2.6 | 降级路径收口与协议文档 | 4h | D22 |

### 阶段 C｜WP3 客服意图识别与路由逻辑（32h）

| 子任务 | 内容 | 工时 | 天 |
|---|---|---:|---|
| 3.1 | 规则分类表与分类器骨架 | 8h | D23–D24 |
| 3.2 | 模型轻量分类与超时降级 | 8h | D25–D26 |
| 3.3 | schema 迁移与意图持久化 | 4h | D27 |
| 3.4 | 置信度阈值路由与投诉优先 | 8h | D28–D29 |
| 3.5 | 路由配置文件与检索范围接入 | 4h | D30 |

### 阶段 D｜WP4 客服模块效果评测与调优（34h）

| 子任务 | 内容 | 工时 | 天 |
|---|---|---:|---|
| 4.1 | 四项指标计算与门禁接入 | 8h | D31–D32 |
| 4.2 | 判定标准定义文档 | 4h | D33 |
| 4.3 | 50+ 条用例集构建 | 12h | D34–D36 |
| 4.4 | 自动化评测脚本与首轮基线 | 4h | D37 |
| 4.5 | 根因分析与针对性调优 | 4h | D38 |
| 4.6 | 复测验证与最终配置输出 | 2h | D39 |

### 阶段 E｜WP5 端到端联调与交付（40h）

| 子任务 | 内容 | 工时 | 天 |
|---|---|---:|---|
| 5.1 | 真实模型端到端联调 | 8h | D40–D41 |
| 5.2 | 新增虚拟店铺场景 D17 / D18 | 12h | D42–D44 |
| 5.3 | 全量回归与反证记录 | 8h | D45–D46 |
| 5.4 | 模块验证文档与实跑截图 | 8h | D47–D48 |
| 5.5 | 四份项目台账同步 | 4h | D49 |

---

## 阶段 A｜WP1 客服会话管理与上下文控制

### D01 · 07-30（四）· 子任务 1.1

**目标**：拉起分支与基线，写出 token 计数模块骨架。

- 确认在 `feature/m4-customer-service` 分支，与 main 同步
- 跑一次全量测试拿基线，记录 `N passed`；跑不过先停下查因，不在坏基线上开工
- 新建 `src/ecommerce_agent/tokens.py`，实现 `count_tokens` 与 `count_messages`
- 计数口径写进模块 docstring：中日韩字符 1 token/字，其余 `len/4` 向上取整，
  两者相加；明确标注这是保守估算而非精确 tokenizer

**产出**：`tokens.py` 计数函数可用
**判据**：中文、英文、混合文本的计数单调且非零，有临时脚本验证

---

### D02 · 07-31（五）· 子任务 1.1

**目标**：实现历史截断算法。

- 实现 `truncate_history(history, *, budget_tokens)`，从最新往回保留
- 处理极端情况：单条消息本身超预算时仍保留最近 1 轮，元信息标 `over_budget=True`
- 返回元信息 `{kept, dropped, tokens, budget, over_budget}`
- 新建 `tests/test_context_budget.py`，先写第 1–3 项测试

**产出**：截断函数 + 3 项测试
**判据**：200 轮超长历史截断后 `tokens <= budget` 且最后一轮原样保留

---

### D03 · 08-03（一）· 子任务 1.1

**目标**：知识块预算与配置项落地。

- `config.py` 新增 `model_context_limit_tokens`（默认 128000）、
  `context_budget_ratio`（默认 0.7，钳制 0.1–0.9）
- `prompts.py` 的 `build_messages` / `build_decision_messages` 新增
  `knowledge_budget_tokens` 参数：超预算时从 score 最低的文档开始丢，至少留 1 条
- 补测试第 4 项（知识块按 score 丢弃）
- 做反证：`context_budget_ratio` 临时设 0.99，确认截断断言失败，然后还原

**产出**：配置项 + 知识块预算 + 反证记录
**判据**：反证成立并已记录；`tests/test_context_budget.py` 4 项通过

---

### D04 · 08-04（二）· 子任务 1.2

**目标**：把预算层接进上下文构建。

- `context_builder.py` 的 `build()` 新增 `history_budget_tokens` 参数
- 把 `history[-6:]` 改为调用 `truncate_history`
- 截断元信息写入 `bundle["recent_history_meta"]`
- 新增一条 `history_window` 类型的 evidence 进证据列表

**产出**：上下文快照携带截断元信息
**判据**：一次 chat 后能在 `context_snapshots` 查到 `history_window` 证据

---

### D05 · 08-05（三）· 子任务 1.2

**目标**：编排层四处调用点统一走预算。

- `graph.py` 的 123 / 179 / 494 / 555 四处 `db.recent_messages` 改为经预算层
- 预算计算：总预算 = 上限 × ratio，减去 System Prompt 与用户消息实测 token，
  余下按 `知识 : 历史 = 6 : 4` 分配
- 截断结果写入 `state["trace"]`，格式 `context:budget:kept{n}/dropped{n}`
- 补测试第 5 项（端到端），跑 `test_react_graph.py` / `test_agent.py` 确认不回归

**产出**：预算层全链路生效
**判据**：5 项定向测试通过；既有编排测试全绿

---

### D06 · 08-06（四）· 子任务 1.3

**目标**：会话 CRUD 的创建与查询两个端点。

- 新建 `src/ecommerce_agent/chat_sessions_api.py`，仿 `customer_test_api.py` 的
  router 构造方式，认证用 `Depends(require_client)`
- `POST /v1/chat/sessions`：幂等创建，作用域冲突返回 409
- `GET /v1/chat/sessions/{id}`：返回状态、创建时间、最后活跃时间、消息数；
  非本租户或非本主体返回 404 而非 403
- 在 `api.py` 挂载 router

**产出**：两个端点可用
**判据**：幂等创建与跨租户 404 两项测试通过

---

### D07 · 08-07（五）· 子任务 1.3

**目标**：历史分页与关闭会话两个端点。

- `database.py` 新增 `paginated_messages(session_id, cursor, limit)`，
  游标为 `created_at|id` 复合游标，解码失败按无游标处理不抛 500
- `GET /v1/chat/sessions/{id}/messages`：默认 limit 20，上限 100
- `DELETE /v1/chat/sessions/{id}`：置 closed；存在非终态 handoff 时 409（D-007）
- 新建 `tests/test_chat_sessions_api.py`，补齐 7 项测试

**产出**：四个端点齐全
**判据**：55 条消息按 limit=20 翻页无重复无遗漏；带未结 handoff 时 DELETE 返回 409

---

### D08 · 08-10（一）· 子任务 1.4

**目标**：空闲超时与阶段收口。

- `config.py` 新增 `SESSION_IDLE_TIMEOUT_MINUTES`（默认 120），
  与 `MESSAGE_RETENTION_DAYS` 解耦
- 实现会话 TTL worker，套用项目既有 worker 线程模式；跳过有未结人工任务的会话
- 写数据模型文档（Session / Message 字段说明 + 接口契约）
- 阶段 A 全量回归，按子任务分 commit

**产出**：阶段 A 完整交付
**判据**：空闲 121 分钟会话自动关闭且带 handoff 的不被关；全量测试通过

---

## 阶段 B｜WP2 知识库增强回复生成 Pipeline

### D09 · 08-11（二）· 子任务 2.1

**目标**：模型网关流式产出。

- `llm.py` 新增 `stream_generate(messages) -> Iterator[str]`，与 `_stream_request`
  共享请求构造与错误分类，只 yield delta 不 join
- **保留 `_stream_request` 原样**——决策调用仍需要整串 JSON
- `model_mock_mode` 下按字符切分 mock 结果 yield，保证测试不依赖网络

**产出**：流式 generator
**判据**：mock 模式下能拿到多个 delta，拼接结果与非流式一致

---

### D10 · 08-12（三）· 子任务 2.1

**目标**：流式错误分类对齐。

- 429 provider code 与 `ModelUnavailableError` 的分类在流式路径同样生效
- 上游流中途报错时能正确抛出而不是静默截断
- 连接失败重试逻辑复用既有退避
- 补 `tests/test_llm.py` 的流式分支用例

**产出**：流式错误路径可靠
**判据**：`tests/test_llm.py` 全绿，新增流式用例覆盖限流与中途报错

---

### D11 · 08-13（四）· 子任务 2.2

**目标**：抽取 verify 节点逻辑。

- 把 `graph.py` 的 `verify` 节点逻辑抽成模块级可复用函数
- 图内节点改为调用该函数，**行为零变化**
- 跑 `test_react_graph.py` 确认无回归

**产出**：`verify` 可被流式路径复用
**判据**：编排测试全绿，diff 中图拓扑无变化

---

### D12 · 08-14（五）· 子任务 2.2

**目标**：抽取 persist 节点逻辑。

- 同样把 `persist` 抽成可复用函数，含消息落库、invocation 完成、审计三段
- 图内节点改为调用
- 跑 `test_agent.py`、`test_react_graph.py`、`test_api.py` 三套确认无回归

**产出**：`persist` 可被流式路径复用
**判据**：三套测试全绿；这一步最容易翻车，务必逐套跑

---

### D13 · 08-17（一）· 子任务 2.3

**目标**：两段式架构骨架。

- `service.py` 新增 `chat_stream(...) -> Iterator[dict]`
- 第一段：复用现有图跑到决策完成，拿到 `route`、`context_bundle`、`retrieved`、
  `tool_result`
- **不要**让 LangGraph 同步节点 yield，图拓扑必须零改动（D-010、D-023）

**产出**：第一段可跑通并返回决策结果
**判据**：能在不产生回复的前提下拿到完整决策上下文

---

### D14 · 08-18（二）· 子任务 2.3

**目标**：生成段流式产出。

- `route == "generate"` 时跳出图，用 `stream_generate` 直接产出 delta
- 流结束后把完整回复交回 D11/D12 抽出的 `verify` 函数
- 校验不通过时按既有语义转 handoff

**产出**：generate 路径可流式
**判据**：mock 模式下拼接结果等于非流式 `answer`

---

### D15 · 08-19（三）· 子任务 2.3

**目标**：非流式路径接入。

- `clarify` / `handoff` / `refuse` / `retry_later` 四条路径不流式，
  直接一次性发完整事件（文案由代码生成，流没有意义）
- 四条路径的落库时机与非流式保持一致

**产出**：五条路径全覆盖
**判据**：四条非生成路径的输出与非流式接口逐字段一致

---

### D16 · 08-20（四）· 子任务 2.3

**目标**：落库时机与中断处理。

- 明确流中断时的 persist 时机：中途断开不得留下半条 assistant 消息
- 消费一半就关闭迭代器的场景走通
- 新建 `tests/test_chat_stream.py`，补第 1、3 项测试

**产出**：中断安全
**判据**：中途断开后 `messages` 表无半条记录

---

### D17 · 08-21（五）· 子任务 2.4

**目标**：SSE 端点与事件框架。

- `api.py` 新增 `POST /v1/chat/stream`，返回
  `StreamingResponse(media_type="text/event-stream")`，`Depends(require_client)`
- 事件格式统一为每事件一行 `data: {json}\n\n`
- 实现 `meta` 事件（首个事件，含 session_id / message_id / trace_id）

**产出**：端点可连、能收到首事件
**判据**：curl 能建立连接并收到 `meta`

---

### D18 · 08-24（一）· 子任务 2.4

**目标**：delta / citations / done 三个事件。

- `delta`：逐段回复内容
- `citations`：生成结束后、`done` 之前发出，含检索到的 sources
- `done`：含 message_id / intent / risk_level / model_fallback
- 补 `tests/test_chat_stream.py` 第 1 项完整断言

**产出**：正常路径事件齐全
**判据**：一次完整对话的事件序列为 meta → delta* → citations → done

---

### D19 · 08-25（二）· 子任务 2.4

**目标**：handoff / error 两个事件。

- `handoff`：含 requires_human / handoff_id / handoff_status / reason，
  复用既有字段语义不改名
- `error`：含 code / message / retry_advised
- **`error` 后必须紧跟 `done` 关闭流**，不能让客户端挂死
- 补测试第 4、6 项

**产出**：异常路径事件齐全
**判据**：模型抛 `ModelUnavailableError` 时收到 error 且 `retry_advised=true`，
紧跟 done

---

### D20 · 08-26（三）· 子任务 2.5

**目标**：幂等重放。

- 幂等命中已完成 invocation 时，直接把已存回复作为单个 `delta` 发出再发 `done`
- **不重新调模型**——用 mock 调用计数断言
- 复用既有 `Idempotency-Key` 与 `agent_invocations` 表，不另建机制

**产出**：断连重发不重复回复
**判据**：同 key 第二次请求返回同一 message_id，模型调用计数不增，
`messages` 表行数不变

---

### D21 · 08-27（四）· 子任务 2.5

**目标**：断连重试端到端验证。

- 模拟客户端断连后重连带同一 Idempotency-Key
- 验证不产生重复内容、不产生重复人工任务
- 补测试第 2 项完整断言

**产出**：幂等路径验证完成
**判据**：`tests/test_chat_stream.py` 第 2 项通过

---

### D22 · 08-28（五）· 子任务 2.6

**目标**：降级收口与阶段交付。

- 检索无命中：走既有降级文案，行为与非流式一致（测试第 5 项）
- `MODEL_ENABLED=false` 时端点不发任何外部请求（测试第 7 项，D-005）
- 反证：`stream_generate` 临时改成一次性 yield 全文，确认"多个 delta"断言失败，
  还原后复验（测试第 8 项）
- 写 SSE 事件协议说明文档，供前端对接
- 阶段 B 全量回归，按子任务分 commit

**产出**：阶段 B 完整交付 + 协议文档
**判据**：8 项测试全通过；`test_react_graph.py` / `test_agent.py` / `test_llm.py`
全绿

---

## 阶段 C｜WP3 客服意图识别与路由逻辑

### D23 · 08-31（一）· 子任务 3.1

**目标**：分类器骨架与规则表。

- 新建 `src/ecommerce_agent/intent.py`，定义 `CustomerIntent` 四值枚举
  （product_inquiry / after_sales / complaint / chitchat）
- 定义 `IntentResult`：`{intent, confidence, method}`，
  method ∈ rule / model / default
- 编写关键词规则表：退货/退款/换货/保修/物流 → after_sales；
  投诉/差评/举报/曝光 → complaint；多少钱/规格/参数/尺寸/材质/对比/推荐 →
  product_inquiry

**产出**：规则层分类可用
**判据**：四类各 5 条样例，规则命中的返回 `confidence=0.95, method="rule"`

---

### D24 · 09-01（二）· 子任务 3.1

**目标**：规则层测试与边界。

- 新建 `tests/test_intent_routing.py`，补第 1 项（四类各 5 条）
- 处理多规则同时命中的优先级（投诉 > 售后 > 商品咨询）
- 处理空消息、超长消息、纯符号消息

**产出**：规则层稳定
**判据**：第 1 项测试通过，准确率 ≥75%

---

### D25 · 09-02（三）· 子任务 3.2

**目标**：模型轻量分类。

- 规则未命中时调用模型做 few-shot 分类
- 走**独立的短 prompt**，不复用 `DECISION_SYSTEM_PROMPT`
- `config.py` 新增 `intent_classify_timeout_seconds`（默认 2.0）

**产出**：模型分类可用
**判据**：规则未命中的消息能拿到模型给出的意图标签与置信度

---

### D26 · 09-03（四）· 子任务 3.2

**目标**：超时与失败降级。

- 超时或异常时返回 `chitchat` + `confidence=0.0` + `method="default"`，**不抛异常**
- 确认分类耗时不显著增加用户感知延迟
- 补测试第 2 项

**产出**：分类链路容错
**判据**：模型超时时降级不抛且延迟受控

---

### D27 · 09-04（五）· 子任务 3.3

**目标**：schema 迁移与持久化。

- `SCHEMA_VERSION` 由 24 直接设为 **26，故意跳过 25**（25 为未合并的 M5 分支占用）
- 用既有 `_ensure_column` 加列，**不重建表**：
  `messages.customer_intent TEXT` / `intent_confidence REAL` / `intent_method TEXT`
- `_validate_schema` 的 required 字典补上三列
- `persist` 写入这三个字段
- 补测试第 6、7 项

**产出**：意图可追溯
**判据**：schema v24 → v26 前向迁移成功旧数据不丢；消息表可查三个新字段

---

### D28 · 09-07（一）· 子任务 3.4

**目标**：置信度阈值转人工。

- `config.py` 新增 `handoff_confidence_threshold`（默认 0.6，钳制 0–1）
- `decision_gate` 新增判定：`confidence < threshold` 且
  `route in {"answer","finish"}` → 改 handoff，reason `low_confidence_handoff`
- **注意**：现状是 confidence 已产出但从未参与路由，这是本子任务的核心

**产出**：低置信度转人工生效
**判据**：`confidence=0.5` 时路由变 handoff

---

### D29 · 09-08（二）· 子任务 3.4

**目标**：投诉优先与连续低质检测。

- `customer_intent == "complaint"` → risk_level 至少 medium，
  handoff payload 带 `priority_flag="complaint"`，接入既有队列优先级不新建队列
- 连续低质检测：最近 2 条 assistant 消息的 route_reason 都属于
  {model_unavailable, low_confidence_handoff, no_evidence} → 强制 handoff
- 补测试第 3、4、5 项
- 反证：阈值临时设 0，确认第 3 项失败后还原

**产出**：三条兜底路径齐全
**判据**：投诉任务进入队列可被自动派单消费；反证成立

---

### D30 · 09-09（三）· 子任务 3.5

**目标**：路由配置与阶段收口。

- 新建 `src/ecommerce_agent/intent_routing.json`：意图 →
  `{knowledge_intent, prompt_variant, sop_intent}` 映射
- `retrieve` 节点把写死的 `intent=None`（graph.py:110）改为传 `customer_intent`
- **自行 diff 确认编排节点与边数量零变化**（D-010 硬检查）
- 阶段 C 全量回归，跑 `test_react_graph.py` / `test_handoffs.py` /
  `test_migrations.py`

**产出**：阶段 C 完整交付
**判据**：8 项测试全通过；拓扑零变化；三套既有测试全绿

---

## 阶段 D｜WP4 客服模块效果评测与调优

### D31 · 09-10（四）· 子任务 4.1

**目标**：断言字段与两项指标。

- `EvaluationExpectation` 新增 `grounded_in_sources: bool = False` 与
  `expected_refusal: bool | None = None`
- `_compute_metrics` 新增 `answer_accuracy`（断言全通过 且 非 model_fallback
  且 非 severe 的占比）
- 新增 `hallucination_rate`（命中 forbidden_answer_terms，或声明
  grounded_in_sources 但回答含 sources 无法支撑的数值/承诺）

**产出**：两项指标可算
**判据**：构造已知结果集手算期望值并断言通过

---

### D32 · 09-11（五）· 子任务 4.1

**目标**：另两项指标与门禁。

- `refusal_rate`：`expected_refusal=false` 但实际走 refuse / no_evidence /
  handoff 的占比（应答未答）
- `handoff_precision`：实际转人工中 `expected_requires_human=true` 的占比
- `EvaluationThresholds` 新增 `min_answer_accuracy=0.75`、
  `max_hallucination_rate=0.10`、`max_refusal_rate=0.20`，接入 `_build_gate`
- 新建 `tests/test_customer_service_eval.py`，补第 2、3 项

**产出**：四项指标 + 门禁
**判据**：`hallucination_rate=0.15` 时 gate 必须 fail

---

### D33 · 09-14（一）· 子任务 4.2

**目标**：判定标准文档化。

- 写明什么算"准确"：答案基于知识库且事实正确，可追溯到具体 source
- 写明什么算"幻觉"：编造知识库中不存在的信息，含未出现的价格、库存、
  到账时间等具体承诺
- 写明什么算"应答未答"与"转人工合理"
- 四项指标的计算公式写进 docstring 并同步到交付文档

**产出**：判定标准定义文档
**判据**：任意两人按该标准标注同一批回复，结论一致

---

### D34 · 09-15（二）· 子任务 4.3

**目标**：商品与售后用例。

- 新建 `src/ecommerce_agent/fixtures/customer_service_eval_v1.json`
- 商品咨询 15 条：规格、价格、对比、推荐；**必须引用虚拟店铺数据包里真实存在的
  6 个 SKU**
- 售后 12 条：退换货、保修、物流；引用数据包里的 8 个订单与售后单

**产出**：27 条用例
**判据**：每条的期望答案能在知识库中找到依据

---

### D35 · 09-16（三）· 子任务 4.3

**目标**：投诉、闲聊与多轮用例。

- 投诉 8 条：质量、服务、配送；全部 `expected_requires_human=true`
- 闲聊 5 条：`expected_intent="chitchat"`，`expected_refusal=false`
- 至少 8 条改造为**多轮**用例，其中 3 条专测指代消解
  （"它多少钱"、"这个能退吗"）

**产出**：累计 40 条
**判据**：多轮用例的第二轮确实依赖第一轮上下文才能答对

---

### D36 · 09-17（四）· 子任务 4.3

**目标**：对抗用例与冻结。

- 对抗 10+ 条：Prompt 注入、越权请求（索取他人订单或其他店铺数据）、
  诱导幻觉（询问不存在的商品参数）、敏感话题
- 全部 `expected_refusal=true` 或由 `forbidden_answer_terms` 覆盖系统提示词片段
- 走既有冻结版本与数据集哈希机制，补测试第 1、5 项

**产出**：50+ 条用例集冻结
**判据**：条数 ≥50，五类分布符合要求；冻结后修改被拒绝，哈希稳定

---

### D37 · 09-18（五）· 子任务 4.4

**目标**：评测脚本与首轮基线。

- 新建 `scripts/run_customer_eval.py`：一键导入 fixture、冻结版本、执行 run、
  输出报告
- 在隔离数据库快照中跑首轮，**主库 sessions 表零新增**（D-033，测试第 4 项）
- 记录四项指标的首轮数值作为调优基线

**产出**：可复跑的评测脚本 + 首轮报告
**判据**：脚本在 mock 模型下跑完并产出四项指标数值

---

### D38 · 09-21（一）· 子任务 4.5

**目标**：根因分析与调优。

- 逐条分析失败用例，归因到四类之一：Prompt 不够明确 / 检索召回不到 /
  意图路由错误 / 截断丢失关键上下文
- 针对性调优：Prompt 模板、检索 top-k 与 min_score、意图规则与转人工阈值、
  截断窗口比例
- 每次调整只动一个变量，记录调整前后的指标变化

**产出**：调优记录
**判据**：每项调整都有对应的指标变化数据，不是拍脑袋改

---

### D39 · 09-22（二）· 子任务 4.6（2h）

**目标**：复测与阶段收口。

- 用调优后配置重跑评测，确认 `answer_accuracy ≥ 0.75`、
  `hallucination_rate ≤ 0.10`
- 反证：临时移除某条对抗用例的 `forbidden_answer_terms`，确认幻觉率变化后还原
- 输出最终 Prompt 模板与参数配置，**同时标注 mock 与 live 两种模式的结果**
- 阶段 D 全量回归与 commit

**产出**：阶段 D 完整交付
**判据**：两项基线达标；6 项测试全通过

> 本日只排 2 小时，剩余 2 小时作为阶段 A–D 的缓冲。若前面有顺延，在此吸收。

---

## 阶段 E｜WP5 端到端联调与交付

### D40 · 09-23（三）· 子任务 5.1

**目标**：真实模型环境准备与首轮联调。

- 用隔离 `DATA_DIR` 与真实 GLM 配置启动服务（密钥从本机 `env.md` 加载，
  不写进命令历史或文档）
- 走通 `/admin` 顾客直测页，确认基础对话可用
- 记录 `yunpai-agent init` 的 schema 版本与模型标识

**产出**：真实模型环境跑通
**判据**：直测页能收到真实模型回答，审计轨迹完整

---

### D41 · 09-24（四）· 子任务 5.1

**目标**：流式与多轮实跑证据。

- 取得流式逐字输出的实跑截图
- 取得多轮指代（"它多少钱"）的实跑截图
- 取得低置信度触发转人工的实跑截图
- 浏览器 console error / warning 应为 0

**产出**：三组实跑截图
**判据**：截图为真实 PNG 且能看出逐字输出与上下文连续

---

### D42 · 09-25（五）· 子任务 5.2

**目标**：场景 D17 多轮指代。

- 在 `fixtures/virtual_store_v1.json` 新增场景定义，含固定 input / expected /
  assertions
- 在 `simulation.py` 实现 `_verify_multi_turn_reference`
- 断言必须可确定性复现，不依赖模型每次输出一致

**产出**：D17 场景
**判据**：场景独立跑通且断言稳定

---

### D43 · 09-28（一）· 子任务 5.2

**目标**：场景 D18 意图路由与转人工。

- 新增场景定义与 `_verify_intent_routing`
- 覆盖四类意图各一条 + 一条低置信度转人工
- 断言意图标签、路由结果与 handoff 触发

**产出**：D18 场景
**判据**：场景独立跑通且断言稳定

---

### D44 · 09-29（二）· 子任务 5.2

**目标**：场景契约扩展到 18 项。

- 把 `simulation-evidence-v1` 契约从 16 项扩到 18 项
- 更新 `tests/test_virtual_store_simulation.py` 的场景总数与模块覆盖断言
- 做门禁反证：临时移除某条断言，确认 `report["passed"]` 由 True 变 False

**产出**：18 项场景契约
**判据**：18 项全通过；门禁反证成立并还原

---

### D45 · 09-30（三）· 子任务 5.3

**目标**：全量回归。

- 跑完整 `pytest -q`，记录 `N passed in Xs` 与退出码
- 用例数增长要能逐条归因到本模块新增的测试文件
- `git diff --check` 无输出；`python -m compileall -q src` 通过

**产出**：全量回归结果
**判据**：全绿，或失败项能明确归因为既有的 live 模型波动

---

### D46 · 10-01（四）· 子任务 5.3 ⚠ 国庆假期

**目标**：反证记录汇总。

- 汇总阶段 A–D 的全部反证：预算 ratio、流式 yield、置信度阈值、幻觉率
  四处反证的过程与结果
- 每条记录格式：临时改了什么 → 哪个断言失败 → 已还原 → 还原后复验结果

**产出**：反证记录清单
**判据**：四处反证均有可复现的记录

> ⚠ 本日落在国庆假期，实际大概率顺延。后续四天同此。

---

### D47 · 10-02（五）· 子任务 5.4 ⚠ 国庆假期

**目标**：验证文档主体。

- 新建 `docs/works/13-feature-m4-customer-service/README.md`
- 按既有格式写：交付范围表、关键实现、安全与执行边界
- 格式参照 `docs/works/12-feature-m5-operations-assistant/README.md`

**产出**：文档主体
**判据**：交付范围表逐项对应到验收标准

---

### D48 · 10-05（一）· 子任务 5.4 ⚠ 国庆假期

**目标**：测试证据与截图归档。

- 补文档的测试证据章节：反证与回归门禁、最终结果、定向复验
- 归档 D41 的三组实跑截图到文档目录
- **如实记录 live 模型下客服类场景的波动**：既往两次实跑分别为
  15 通过 1 失败与 14 通过 2 失败，本模块改动会放大该波动；
  验收以受控测试断言为准，live 结果单独标注

**产出**：完整验证文档
**判据**：截图为真实 PNG；波动风险已如实写入

---

### D49 · 10-06（二）· 子任务 5.5 ⚠ 国庆假期

**目标**：台账同步与模块交付。

- `PROJECT_FEATURES.md`：登记 F-124 SSE 流式客服接口、F-125 会话 Token 预算与
  生命周期、F-126 意图分类与置信度路由，更新 F-117 状态
- `PROJECT_PROGRESS.md`：追加进度历史
- `PROJECT_VERSIONS.md`：版本推进与兼容性说明（schema v26 additive）
- `PROJECT_ACCEPTANCE.md`：补证据 ID `E-2026MMDD-NNN`
- 工作台五个工作包进度更新，模块交付

**产出**：M4 模块交付完成
**判据**：四份台账已同步且证据 ID 可追溯

---

## 每日收尾动作（每天都做）

1. 当日代码单独 commit，英文 conventional commits，不加 AI 署名
2. 跑当日涉及的定向测试，不必每天跑全量
3. 更新 `docs/tasks/PROGRESS.md` 的复选框
4. 工作台更新剩余工时；有延期就填「阻塞或延期原因」，不要事后补

## 需要停下来找人的情况

- 需要新增第三方依赖
- 某项改动会改变 `/v1/chat` 既有响应契约
- D13–D16 的两段式架构被证明走不通
- 全量测试出现与本模块无关的既有失败
- 国庆假期安排明确后，整体交期需要重新对齐
