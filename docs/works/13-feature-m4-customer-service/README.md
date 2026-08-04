# M4 智能客服后端 WP1–WP3

- 分支：`feature/m4-customer-service`
- 范围：客服会话管理与上下文控制、知识库增强回复生成 Pipeline、客服意图识别与路由
- 交付范围：M4 工作包 1、工作包 2 与工作包 3（D11 起）

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

## D11 · 受控意图枚举与规则分类

- 新增 `CustomerIntent` 四分类与 `IntentResult`，判定方式限定为 rule / model /
  default
- `_RULE_PRIORITY` 显式声明投诉 > 售后 > 商品咨询，`_RULE_KEYWORDS` 只保存关键词；
  倒序重排关键词映射后重叠消息仍按显式优先级判定
- 规则命中置信度由 `_RULE_CONFIDENCE = 0.95` 单点声明；生产意图分类无第二处
  0.95 硬编码
- 空白和纯符号输入直接安全降级；超长输入仍可由规则层确定性分类
- 红态：聚焦测试因缺少 `ecommerce_agent.intent` 在收集阶段失败
- 绿态：`tests/test_intent_routing.py` 为 `28 passed`；四类各 5 条样例全部正确，
  规则表一致性为 `20/20`
- 上述 20 条基本复述规则关键词，不作为泛化准确率 ≥75% 的证据；换措辞的 20 条
  留出样本复核仅为 30%，WP4 将用独立留出集重新量化并据此调优
- 显式声明反证：旧实现缺少 `_RULE_PRIORITY` / `_RULE_CONFIDENCE`，两项测试按预期
  失败；重构后 `2 passed`
- 显式声明收口：完整意图套件 `36 passed`；全量回归 `390 passed in 182.12s`
- 未新增依赖，未改 LangGraph 节点或边

## D12 · 轻量模型分类与降级

- 规则未命中时调用独立的两消息 few-shot Prompt，不复用
  `DECISION_SYSTEM_PROMPT`；模型结果经四分类枚举和置信度范围校验
- 新增 `INTENT_CLASSIFY_TIMEOUT_SECONDS`，默认 2.0 秒；分类预算同时传入模型网关
  的普通 JSON 请求与 SSE 请求
- 超时、模型异常、模型禁用或非法结果均返回 `chitchat`、0.0、default，且不抛异常
- 红态：新增测试得到 `5 failed, 44 passed`，分别证明旧实现未调用模型、缺少超时配置，
  且网关不接受单次超时
- 首次绿测发现实现把 0.02 秒配置抬高到 0.05 秒，测试按预期失败；移除过度限制后
  同组测试为 `49 passed`
- 延迟判据：超时测试精确传入 0.02 秒预算、只调用一次并在 0.5 秒墙钟上限内安全
  降级；禁用模型测试确认外部请求数为 0
- mock 网关端到端反证：规则未命中的商品问法修复前错误降级为
  `chitchat/default`；增加独立任务分支后返回 `product_inquiry/0.82/model`
- 重试反证：分类专用超时下临时保留网关既有重试，连接超时实际调用 3 次、单次调用
  断言失败；改为 deadline 调用不重试后复验通过
- 最终聚焦测试：`51 passed in 0.81s`；相关回归：`72 passed in 16.33s`
- 沙箱内全量曾有 1 项既有工具计时断言以 0.161 秒超过 0.15 秒阈值；同项沙箱外
  复验 `1 passed in 0.10s`，最终沙箱外全量回归 `389 passed in 327.34s`
- 未新增依赖，未改 LangGraph 节点或边

## D12 修正 · 真实模型下的响应形状与降级可观测性

- 缺陷：`classify()` 用 `payload["intent"]` 直接下标取值，而真实
  `glm-4.7-flash` 稳定把结果包成 `{"answer": {...}}`，`KeyError` 被
  `except Exception` 吞掉，整条链路静默降级为 `chitchat/0.0/default`
- 影响范围：52 条基准语料中 32 次规则未命中，仅 1 次返回 `method="model"`；
  模型每次都给出了正确分类，成本每次都已支付，结果全部丢弃
- 该缺陷对既有测试完全不可见：`_mock_generate` 返回的是解析代码期望的完美
  形状，mock 与解析代码出自同一假设，测试只能验证实现符合作者的假设
- 修复一：system prompt 直接印出目标对象 `{"intent": ..., "confidence": ...}`，
  并明确不要嵌套 / 包装 / 额外字段；原措辞只用自然语言描述字段，few-shot
  示例演示的是标注方式而非输出形状
- 修复二：新增 `_coerce_model_payload` / `_unwrap_envelope` 归一化层。拆信封
  限定「单键且内层为 dict」，`{"intent": "chitchat"}` 不会被误拆，深度限 3 层；
  intent 大小写与空白归一；confidence 越界截断而非否决（intent 才是有效载荷）
- 修复三：`_mock_generate` 改为返回带信封的形状，与真实依赖行为一致——mock 的
  职责是模拟依赖的实际行为，不是模拟依赖的理想状态
- 修复四：`IntentResult` 新增 `error` 字段，`method="default"` 时必然非空，
  取值区分 `unclassifiable_input` / `model_not_configured` /
  `model_call_failed:<异常类型>` / `model_payload_rejected:<键名集合>`；
  形状串只记键名不记内容，不将用户消息带入日志
- 排查过程记录：先后误判为环境变量缺失、2.0 秒超时不足、SSE 流式开销，均被
  实测否定（真实 p50 为 0.74 秒，远在预算内）。三轮误判的共同原因是
  `except Exception` 未留任何痕迹——这正是修复四要解决的问题
- 绿态：`tests/test_intent_routing.py` 新增 21 项（形状归一化 10 组、不可用
  载荷 7 组、降级原因区分、`httpx.MockTransport` 端到端复现真实响应体），
  全量回归 `413 passed in 361.59s`
- 基准量化（`evals/intent/`，52 条语料，live 模式打真实模型）：

  | 指标 | 修复前 | 修复后 |
  | --- | --- | --- |
  | 端到端准确率 | 53.8% | 80.8% |
  | 覆盖率（非弃权） | 40.4% | 92.3% |
  | `method="model"` 命中 | 1/52 | 28/52 |
  | model 层准确率 | — | 82.1% |

- 遗留：`negation` / `cross_domain` 两类仍为 0%，成因是规则层命中即短路、模型
  无从介入，属两级链结构性问题，另行处理
- 未新增依赖，未改 LangGraph 节点或边

## D12 修正 · 两级链风险仲裁

- 缺陷反证：五条否定 / 跨域消息在旧实现中全部由规则直接返回，模型调用数均为
  0；无模型时仍伪装成 0.95 高置信规则结果。新增的两组参数化测试在修改前为
  `10 failed`，分别约束“必须交给模型”和“无模型时必须带原因弃权”
- 规则层仍先按 `_RULE_PRIORITY` 选择候选，`_RULE_KEYWORDS` 未承担优先级；只有命中词
  被局部否定，或 `曝光` / `推荐` / `物流` 出现在已知多义上下文时才请求模型，普通
  规则命中继续零模型调用
- 风险路径使用独立的补充指令和一条换措辞示例“不用办理退货了”；普通规则未命中的
  Prompt 不携带该示例，避免给所有模型请求增加 token
- rule 模式修改前后使用同一份 52 条语料，结果如下：

  | 指标 | 修改前 | 修改后 |
  | --- | --- | --- |
  | 端到端准确率 | 51.9%（27/52） | 57.7%（30/52） |
  | 覆盖率（非弃权） | 38.5%（20/52） | 28.8%（15/52） |
  | 判定准确率 | 75.0%（15/20） | 100.0%（15/15） |
  | `negation` | 0%（0/1） | 100%（1/1） |
  | `cross_domain` | 0%（0/4） | 50%（2/4） |
  | `plain` | 100%（22/22） | 100%（22/22） |

- rule 结果文件：`evals/intent/runs/20260804-rule-before-two-stage-fix.json`、
  `evals/intent/runs/20260804-rule-after-two-stage-fix.json`；程序化核对两份文件的
  52 组 `id + expected` 完全一致，未修改语料 expected
- rule-only 覆盖率下降是风险规则改为弃权的预期结果；mock 基准确认完整两级路由为
  `rule=15 / model=33 / default=4`，`plain` 仍为 100%
- 真实模型逐条可用时，四条 `cross_domain` 与一条 `negation` 探针均由
  `method=model` 返回正确类别；但 2026-08-04 16:00–17:00 的全量 live 多次收到
  HTTP 429 / provider code 1305（模型池过载），不能把这些运行当成分类能力成绩
- 两次保留完整逐条错误的运行分别为：30 秒间隔 `39/52`、覆盖率 65.4%、14 条
  `ModelUnavailableError`；45 秒间隔 `34/52`、覆盖率 44.2%、25 条
  `ModelUnavailableError`。两次均为 `negation=100%`、`cross_domain=50%`、
  `plain=100%`，但端到端未达到 42/52，因此未宣称 live 验收通过
- 当前 live 证据文件为
  `evals/intent/runs/20260804-live-after-two-stage-fix.json`，其中逐条保存 `error`，
  且顶层保存 `request_interval`；需在模型池恢复后原命令重跑并达到至少 42/52
- 风险路径测试与普通规则 / 优先级联合复验 `35 passed`；最终全量回归
  `423 passed in 230.38s`
- 未新增依赖，未改 LangGraph 节点或边

### 复核意见（人工，2026-08-04）

- 真实增益应以**规则层精度**表述：判定准确率 75.0%(15/20) → 100.0%(15/15)。
  五条误命中不再冒充 0.95 高置信规则结果，这是结构性修复的直接证据
- 上表中 `negation` 0%→100%、`cross_domain` 0%→50% **不构成能力证据**：rule 模式
  下这五条一律走 `default → chitchat`，而其中三条的 expected 恰为 `chitchat`，
  弃权与答对在计分上无法区分。live 模式两次给出完全相同的 100% / 50%，进一步
  说明这两个数字由语料标签分布决定，与模型是否作答无关
- `_RULE_REVIEW_CONTEXTS` 的三张上下文词表逐条对应基准里的 `cross_domain` 样本，
  属对基准过拟合。留出探针验证（语料外表达，rule 模式）：

  | 消息 | 结果 | 是否交给模型 |
  | --- | --- | --- |
  | 帮我推荐一家医院 | rule / product_inquiry | 否 |
  | 推荐点好玩的地方 | rule / product_inquiry | 否 |
  | 曝光度调高一点 | rule / complaint | 否 |
  | 退款这词你懂吗 | rule / after_sales | 否 |
  | 这张照片曝光过度了 | default（已交给模型） | 是 |
  | 我在物流行业干了十年 | default（已交给模型） | 是 |

  六条留出表达中四条仍被规则短路。仲裁机制本身成立，触发条件的泛化能力不足
- 结论：合入，但 `cross_domain` 不计入已解决。后续应把「命中词是否处于业务语境」
  改为可泛化的判据（如要求与品类/订单词共现），而非枚举已知反例
- 语料口径变更：`as-013`「支持七天无理由吗」按售前咨询裁定，改为
  `pi-014` / `product_inquiry`

### live 恢复复测（2026-08-04）

- 命令：`evals/intent/run.py --mode live --request-interval 30`；结果文件：
  `evals/intent/runs/20260804-live-retest-after-recovery.json`
- 端到端 `41/52`（78.8%）、覆盖率 `36/52`（69.2%）、非弃权判定准确率
  `33/36`（91.7%）；路径分布为 `rule=15 / model=21 / default=16`
- 16 条 default 中，4 条为退化输入，12 条仍为
  `model_call_failed:ModelUnavailableError`。模型池只部分恢复，本次结果仍不加入
  live 基线；41/52 也低于旧基线 42/52
- `negation` 的 1 条与 `cross_domain` 的 4 条均为实际 `method=model`，且 5/5
  正确，不再是弃权伪装；这只能说明基准内五条样本，不能推翻上述留出探针仅 2/6
  被仲裁的过拟合结论
- `plain` 表面为 22/22，但实际覆盖 19/22；非弃权的 19 条为 19/19，另 3 条
  chitchat 由 1305 降级后碰巧计为正确
- `pi-014` 在全量中因 1305 弃权；随后独立单条探针返回
  `product_inquiry / 0.9 / model / error=None`。该探针只验证新口径，不回填全量成绩

### 验收判定（人工，2026-08-04 复测后）

- 原定的「live 端到端 ≥ 42/52」是错误门槛：端到端准确率把 provider 可用性算进
  分类成绩，1305 每多一条该指标就下降，与被测行为无关
- 改用**两次运行共同作答子集**（`method != "default"` 的交集）判定，该口径与
  平台可用性无关：

  | 运行 | 交集 n=36 上的判定准确率 |
  | --- | --- |
  | `runs/20260804-live-after-fix.json` | 27/36 = 75.0% |
  | `runs/20260804-live-retest-after-recovery.json` | 33/36 = 91.7% |

- 6 条翻盘全部为修好、零回退，可逐条归因：

  | 样例 | 修改前 | 修改后 | 期望 |
  | --- | --- | --- | --- |
  | `cc-007` | rule / product_inquiry | model / chitchat | chitchat |
  | `cc-008` | rule / after_sales | model / chitchat | chitchat |
  | `cc-009` | rule / after_sales | model / chitchat | chitchat |
  | `cp-010` | rule / product_inquiry | model / complaint | complaint |
  | `pi-011` | rule / complaint | model / product_inquiry | product_inquiry |
  | `as-008` | model / complaint | model / after_sales | after_sales |

  前五条即风险仲裁打开的短路路径，第六条为模型判定自身改善
- 判定：**两级链风险仲裁验收通过**。剩余 12 条 `ModelUnavailableError` 为随机
  缺测，非失败
- 保留意见：交集法假设「哪些条被 1305 打掉」与样例难度无关，缺测 12/52 时大致
  成立但非严格无偏。模型池完全恢复后应补一次完整 live 并写入「历次 live 基线」
- 不受本次判定影响、仍然挂起：`_RULE_REVIEW_CONTEXTS` 对基准过拟合，留出表达
  仅 2/6 被仲裁，`cross_domain` 不计入已解决

### 标注口径写入分类 Prompt（2026-08-04）

- 动机：「诉求优先于语气」「售前咨询归商品咨询」两条口径由人裁定后只存在于
  `evals/intent/README.md` 与语料标注中，`_MODEL_SYSTEM_PROMPT` 未传达，模型
  无从遵循；`after_sales` 召回长期低于其他三类
- 实现：新增 `_LABELLING_POLICY` 常量并拼入分类 system prompt，传达的是**判据**
  而非具体样例。刻意不把 `as-007` / `pi-014` 写成 few-shot——那是对基准过拟合，
  分数会涨而能力不会。新增测试同时约束「口径必须到达模型」与「口径不得以语料
  原句形式出现」
- Prompt 体积：序列化后 677 字符，仍在既有 1200 上限内
- 效果按共同作答子集判定（`runs/20260804-live-retest-after-recovery.json` 对
  `runs/20260804-live-after-policy-prompt.json`）：

  | 运行 | 交集 n=33 |
  | --- | --- |
  | 口径前 | 30/33 = 90.9% |
  | 口径后 | 31/33 = 93.9% |

- **未达成既定目标**：净增仅 1 条（`as-006`，complaint → after_sales，方向确在
  口径轴上）；而该口径专为之裁定的 `as-007`「我这东西坏了，质量也太差了吧」
  修改前后均判 `complaint`，未被纠正。`pi-014` 两次运行都因 1305 弃权，未受测
- 同次 live 的端到端 78.8% → 86.5%、`after_sales` 召回 61.5% → 83.3% **不可
  归因于本次改动**：缺测由 16 条降至 9 条，涨幅主要来自平台可用性恢复。共同
  作答子集才是本次改动的真实效果
- 保留该改动的理由是口径正确、零回退、机制已验证连通，而非分数提升
- 结论：`after_sales` 召回缺口**不关闭**

### D12 修正 · cross-domain 正向业务证据门（2026-08-04）

- 红态先固定独立路由留出集，不依赖真实模型判定：6 条外部跨域表达中只有 2 条
  进入模型，另外 4 条仍以 `rule / 0.95` 短路；8 条明确业务守卫全部留在规则层
- 新增回归后先跑得到 `4 failed, 10 passed`；四个失败分别是医院推荐、地点推荐、
  曝光度和退款元问题，失败点均为模型调用数 `0 != 1`
- 删除 `_RULE_REVIEW_CONTEXTS` 反例域词表；新增 `_RULE_BUSINESS_EVIDENCE`，只为
  已证实多义的 `曝光` / `推荐` / `物流` / `退款` 声明正向业务锚点。无证据时交给
  模型，有证据时继续规则直返；否定检测与显式优先级未改
- 第一版正向共现仍被无关分句污染，新增四条“业务词在前句、跨域关键词在后句”的
  反证再次得到 `4 failed`；最终把证据限制在命中词所在分句，且 `曝光` 要求投诉
  动作与电商对象两组证据同时成立，四条复验全绿
- 路由留出修改前后：跨域仲裁 `2/6 → 6/6`，业务快路径 `8/8 → 8/8`。两份逐条
  证据为 `evals/intent/runs/20260804-cross-domain-before.json` 与
  `evals/intent/runs/20260804-cross-domain-after.json`
- 防基准泄漏检查：生产证据表不包含医院、景点、照片、摄影、行业、公司等反例域词；
  测试和留出数据可以保存反例，生产判断只能保存业务证据
- 原 52 条 rule 基准仍为判定准确率 `15/15`、`plain 22/22`；和旧证据共同的 51 个
  ID 路由零变化，差异 ID 仅为此前人工裁定的 `as-013 → pi-014`
- 绿态：完整意图测试 `89 passed`；意图与 LLM 网关联合回归 `105 passed`；mock
  基准路径分布仍为 `rule=15 / model=33 / default=4`；全量回归
  `443 passed in 150.82s`
- live 未伪造：健康检查得到 `healthy=False, reason=disabled`，当前进程和仓库都无
  模型配置。因此本次只关闭规则短路的泛化缺陷，六条留出表达的模型语义准确率待
  有效 `MODEL_ENABLED` / `MODEL_API_KEY` 环境补测
- 未新增依赖，未改 LangGraph 节点或边
