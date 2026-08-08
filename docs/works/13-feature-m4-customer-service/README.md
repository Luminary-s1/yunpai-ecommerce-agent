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

### 复核意见 · cross-domain 泛化（人工，2026-08-04）

- 黑名单改白名单的方向正确。用**双方均未使用过的**第二批探针复验，六条跨域表达
  全部正确交给模型（`cd-101`–`cd-106`），泛化能力属实
- 但 `cd-001`–`cd-006` 不构成留出成绩：那六条正是前一轮复核意见公布在本文档
  中的探针，答案先于考试给出。已在 `cross_domain_holdout.jsonl` 标记
  `leaked: true`，保留作回归、不再作为泛化证据
- **P0 回归，不予收口**：高频短句业务消息失去确定性

  | 消息 | 改动前 | 改动后 | 模型不可用时 |
  | --- | --- | --- | --- |
  | 我要退款 | rule / after_sales / 0.95 | 交模型 | `chitchat` |
  | 退款 | rule / after_sales / 0.95 | 交模型 | `chitchat` |
  | 物流呢 | rule / after_sales / 0.95 | 交模型 | `chitchat` |
  | 推荐一下 | rule / product_inquiry / 0.95 | 交模型 | `chitchat` |
  | 曝光你们 | rule / complaint / 0.95 | 交模型 | `chitchat` |

  「我要退款」是电商客服最高频消息之一。当日已出现大面积 provider 1305，该路径
  在服务商抖动时会把 P0 售后消息降级为闲聊，代价高于原 `cross_domain` 误判
- 52 条基准语料对此**零感知**（路由回归 0 条、`rule 15/15`、`plain 22/22` 均属实），
  因为语料中没有短句。已补 5 条 `terse-business-guard` 至留出集，当前
  `run_cross_domain.py` 报业务快路径 8/13
- 根因：判据为「无业务证据 → 交模型」，而短句本就无证据可言。跨域误用几乎总
  伴随额外语境词，纯业务短句没有。应改为「存在跨域信号时才要求业务证据」
- scope 外、记录备查：仅 `曝光`/`推荐`/`物流`/`退款` 四词有证据表，其余十二词
  照旧短路。「我要投诉隔壁邻居」仍判 `complaint`，会进人工关注队列
- 语料变更：`as-007` 加 `ambiguous` 标签。人裁定诉求优先故 expected 仍为
  `after_sales`，但真实模型在口径写入 prompt 前后均判 `complaint`。**仍计入总分**
  ——失败之后才把样例移出计分，与修改 expected 是同一类行为

### D12 修正 · 高频短句业务快路径（2026-08-05）

- 红态：`run_cross_domain.py` 为跨域 `12/12`、业务快路径 `8/13`；无模型直探
  `我要退款` / `退款` / `物流呢` / `推荐一下` / `曝光你们` 全部返回
  `default / chitchat / 0.0 / model_not_configured`
- 把五条短句固化为单元回归后，首次运行精确得到 `5 failed, 4 passed`；失败均为
  短句落入模型路径，四条短跨域设计守卫仍正确交模型
- 修复不是字符数阈值，也未增加反例域词：对四个多义关键词，去掉命中词后仅剩
  通用动作前缀或语气后缀的分句视为业务短句；出现实义内容时仍要求现有正向业务
  证据。否定检测继续优先于短句判断
- 第一版通用包装把“可以 / 请问 / 吗”也当动作，新增三条售前政策问句后得到
  `2 failed, 1 passed`；移除疑问式包装后复验 `3/3`，避免把“可以退款吗”短路成
  `after_sales`
- 最终路由双门：跨域回归 `12/12`，业务快路径 `13/13`；五条 P0 短句全部恢复
  `rule / 0.95 / error=None`。其中 12 条跨域探针均已泄漏，本次只把它们当回归，
  不宣称新的泛化成绩
- 结果文件：`evals/intent/runs/20260805-cross-domain-terse-fix.json`；原 52 条 rule
  基准：`evals/intent/runs/20260805-rule-after-terse-fix.json`，判定准确率
  `15/15`、`plain 22/22`
- 意图与 LLM 网关联合回归 `117 passed`；全量回归
  `455 passed in 147.02s`；`compileall` 与 `git diff --check` 通过
- 当前四个多义关键词范围收口；其余十二个规则关键词的跨域边界仍属后续范围
- 未新增依赖，未改 LangGraph 节点或边

### 复核意见 · 短句语法（人工，2026-08-05）

- P0 回归确认修复：模型全不可用时，`我要退款` / `退款` / `物流呢` / `推荐一下` /
  `曝光你们` 均恢复为 `rule / 0.95 / error=None`，不再降级为闲聊
- 复算通过：跨域仲裁 12/12、业务快路径 13/13、rule 判定 15/15、`plain` 22/22
- 方案评价优于原建议。复核意见曾建议「存在跨域信号时才要求业务证据」，仍属数
  信号；实现采用的是**结构判据**——命中词之外的部分必须完整分解为请求前缀与
  语气后缀，否则不视为短句。语法约束比词表匹配泛化性更好
- 全新探针 23 条，20 条正确。两处边界，均**不阻塞**：

  | 探针 | 现状 | 性质 |
  | --- | --- | --- |
  | `查下物流`（`biz-201`） | 交模型 | 仅多一次模型调用，不误判 |
  | `我想推荐你`（`cd-201`） | `rule / product_inquiry / 0.95` | 误判且无模型兜底 |

- `cd-201` 的根因是 `_TERSE_RULE_SUFFIXES` 把两类成分混同：`呢/吧/啊/呀/嘛/呗`
  是语气词，剥离不改变命题；`你/你们` 是论元，剥离即少一个成分。后者被放进后缀
  表是为接住 `曝光你们`，代价是 `推荐你` 这类跨域表达被误判。下次动这块应把代词
  移出后缀表，改由 `_RULE_BUSINESS_EVIDENCE["曝光"]` 的电商对象组接住
- `biz-201` 反映请求前缀表缺少 `查下` / `查一下` / `看下` 等高频动词
- 两条已入留出集并标 `known_gap: true`，当前运行器报 12/13 与 13/14
- 判定：**验收通过**，`cross_domain` 结项

### D12 修正 · `as-007` 同轴 few-shot（2026-08-05）

- 修改前证据：`evals/intent/runs/20260804-live-after-fix.json`、
  `20260804-live-retest-after-recovery.json` 与
  `20260804-live-after-policy-prompt.json` 中，`as-007` 实际作答均为
  `model / complaint`；仅把裁定口径写进 system prompt 未纠正该边界
- 红态：新增请求级测试后得到 `1 failed`，失败点是 `as-007` 的模型请求只有原四条
  few-shot，没有“已成交的具体故障优先于不满语气”同轴示例
- 实现：`_FEW_SHOT_EXAMPLES` 新增“刚收货的耳机就没声音，做工真让人失望 →
  after_sales”。商品、故障和措辞均不同于基准原句；测试同时断言 examples 不包含
  “我这东西坏了”或“质量也太差了吧”
- Prompt 接线复验 `3 passed`；`as-007` 请求中 examples 为 5 条，序列化消息长度
  758 字符，仍低于既有 1200 字符上限
- 意图与 LLM 网关联合回归 `118 passed`；rule 基准保持判定准确率 `15/15`、
  `plain 22/22`；全量回归 `456 passed in 151.75s`
- mock 仍判 `complaint`，因为它是独立手写关键词表且不读取 few-shot，不能作为效果
  证据；提交时真实网关未加载配置，所以当时只确认了 Prompt 实现
- 未修改 `as-007.expected`，其 `ambiguous` 标签与计分方式保持不变；未新增依赖，
  未改 LangGraph 节点或边

#### live 复测（2026-08-05）

- 从仓库根目录 `env.md` 在单个子进程内加载参数，未回显、落盘或记录任何密钥；网关
  健康检查由 `disabled` 变为 `configured`
- 生产 `intent_classify_timeout_seconds=2.0` 下共试 6 次，全部为
  `default / chitchat / model_call_failed:ModelUnavailableError`：明确捕获到 provider
  1302 一次、1305 一次、ReadTimeout 三次，另一次未采集上游细分原因
- 为区分 Prompt 语义与 provider 可用性，单独做一次 10 秒诊断调用；请求实际仅耗时
  0.71 秒并成功得到模型结果，但仍为 `complaint / 0.9`，不是期望的 `after_sales`
- 因单条生产门未通过，没有继续跑全量 live，避免用大批随机弃权污染基线；诊断的
  10 秒预算也不计作生产验收
- 脱敏逐次证据：`evals/intent/runs/20260805-as-007-few-shot-live-probe.json`
- 结论：同轴 few-shot 已正确接线，但**实际模型效果未达成**；`after_sales` 召回缺口
  继续保持未关闭

#### DeepSeek 单条复测与 `as-007` 收口（2026-08-05）

- 在同轴 few-shot 仍被 GLM 判为 `complaint` 的真实红态上，进一步把通用标注策略改为
  显式判定顺序：先识别已经发生且待处理的具体商品 / 履约问题；商品故障即使伴随
  质量抱怨也归 `after_sales`，只有不存在这类待处理问题时才考虑 `complaint`。策略
  没有写入 `as-007` 原句
- 用户将 `env.md` 切换为 `deepseek / deepseek-v4-flash` 后，仅发送 `as-007`。
  第一次生产调用在 0.229 秒被 HTTP 400 拒绝；诊断复现明确指出：使用
  `response_format=json_object` 时 Prompt 必须出现字面量 `json`
- 反证测试先得到 `1 failed`，随后把 system prompt 的“返回对象”改为“返回 JSON
  对象”，同组 Prompt 契约复验 `4 passed`。这是 provider 兼容修复，不改变输出
  schema，也不新增依赖
- 修复后同一条在生产 2 秒预算内耗时 1.33 秒，返回
  `model / after_sales / 0.95 / error=None`，与裁定一致；脱敏逐次证据见
  `evals/intent/runs/20260805-as-007-deepseek-live.json`
- 离线回归：意图与 LLM 网关 `119 passed`；rule 判定准确率保持 `15/15`、`plain`
  保持 `22/22`；cross-domain 保持已记录的 `12/13` 与业务快路径 `13/14`（两条均为
  既有已知边界）；全量 `457 passed in 145.48s`
- 按 provider 并发约束没有发送其他 live 语料。因此本次只关闭 `as-007` 这一条裁定
  边界，不把单条结果外推为完整 `after_sales` 召回率

## D13 · schema v27 与意图持久化（2026-08-05）

- 先查 `CONTRIBUTING.md` 的并行迁移登记：v26 已分配给 M6
  `feature/m6-competitor-import`，因此 M4 不定义 `_apply_v26`，改占空闲的 v27。
- `Database.SCHEMA_VERSION` 从 25 升至 27；`_apply_v27` 只用既有 `_ensure_column` 为
  `messages` 增加可空的 `customer_intent`、`intent_confidence`、`intent_method`，不重建表，
  不改变 v25 的双迁移内容。`_validate_schema` 与会话消息查询同步包含三列。
- 图状态增加三项可选分类元数据；`persist_response` 在用户/助手消息配对中写入同一组
  分类结果，旧状态没有分类时保持 NULL。该改动没有新增 LangGraph 节点或边。
- 红态反证：对一份真实 v25 数据库运行迁移测试时，旧代码停在
  `schema_version() == 25`，新断言按预期失败（`25 != 27`）。
- 绿态：v25→v27 前向迁移与旧消息保留 `1 passed`；意图字段持久化配对测试 `1 passed`；
  两次初始化保持幂等，`schema_migrations` 中没有本分支的 v26 记录。迁移、持久化、
  Agent、会话和后台联合回归 `36 passed`，全量回归 `477 passed in 162.06s`。
- 判据：三列可查、历史内容不丢、迁移不重建表；v26 继续保留给 M6，合并顺序按 v26→v27。

## D14 · 置信度与人工兜底（2026-08-05）

- `HANDOFF_CONFIDENCE_THRESHOLD` 新增为可配置项，默认 `0.6`，在 `0..1` 范围内钳制。
  `decision_gate` 仅对 `answer` / `finish` 目标路由应用低置信度门，改为
  `handoff / low_confidence_handoff`；已批准且可直接复用的不可变知识答案保留其
  确定性快路径，已验证的动作权限门和原有工具后置校验不绕过。
- 受控 `customer_intent=complaint` 时风险至少为 `medium`，回答路径转人工并携带
  `priority_flag=complaint`；已有队列匹配将任务送入 `complaints / urgent`，没有新建队列。
- 查询同一会话最近两条 assistant 消息；当两条 `route_reason` 都属于
  `model_unavailable`、`low_confidence_handoff`、`no_evidence` 时强制
  `consecutive_low_quality`，不足两条或中间有正常回复则不触发。
- 红态反证：临时将测试 Settings 的阈值设为 `0.0`，`confidence=0.5` 的 handoff 断言按预期
  失败，实际返回 `answer / knowledge_answer_allowed`；恢复 `0.6` 后同一断言通过。
- 绿态：D14 guardrail 用例 `6 passed`，与意图、Agent、图、人工任务和派单联合回归
  `140 passed`；全量回归 `483 passed in 160.32s`；投诉任务实际由既有 bootstrap 坐席自动派单消费。
- 未新增依赖、未改变 LangGraph 节点或边；低质查询只读取已落库 assistant 记录，不改变
  D-007 的非终态 handoff 保护逻辑。

## D15 · 路由配置与 WP3 收口（2026-08-05）

- 新增 `src/ecommerce_agent/intent_routing.json`，由 `intent.py` 统一加载并校验四个受控
  意图的 `knowledge_intent`、`prompt_variant`、`sop_intent`；资源同时登记到
  `pyproject.toml` 的 package data，避免安装包漏文件。
- `precheck` 在既有节点内调用两级 `classify`：规则命中仍直接走零成本快路径，规则未命中
  仅在模型启用或 mock 模式下调用轻量分类器；模型关闭时传 `None`，不会触发网关请求。
  分类的意图、置信度、method、降级原因和路由配置写入图状态，之后由 D13 的持久化配对
  写入用户/助手消息。
- 初次检索使用配置中的 `knowledge_intent`；决策 Prompt 携带完整路由三元组，生成 Prompt
  携带 `prompt_variant`。ContextBuilder 仍是决策和生成上下文的唯一入口，未旁路上下文链。
- 反证与回归：新增 D15 集成测试先于 loader 接入时按预期无法导入路由契约；首次全量还
  暴露 mock 网关未识别“退款”导致既有售后队列回归（`general`），补齐 mock 意图分支后
  单项回归 `9 passed`。最终 D15 专项 `8 passed`，计划指定的 `test_react_graph.py` /
  `test_handoffs.py` / `test_migrations.py` 为 `22 passed`，意图与客服链路联合回归为
  `144 passed`，全量 `491 passed in 156.97s`。
- 拓扑硬检查：修改前后均为 **20 个节点、35 条边**，新增配置和分类调用没有新增节点或
  边；无第三方依赖，D-005、D-010、D-023 保持满足。WP3 D11–D15 至此完成。

## D20 · WP4 自动化评测、调优与收口（2026-08-05）

### 交付物与运行方式

- 新增 `scripts/run_customer_eval.py`：从冻结 fixture 建立隔离虚拟店铺，复用
  `AgentService.run_evaluation_suite`，执行 mock/live，输出四项 M4 指标、门禁、逐条
  脱敏失败片段和四类失败归因。
- 每个模式都在临时数据库快照中运行；基线前后及调优阶段的
  `sessions/messages/handoff_tasks` 均为 `0/0/0` 新增，证明没有污染主库。评测来源
  仍为 `evaluation`，没有改 LangGraph 节点或边。
- 运行命令（密钥由 `env.md` 在子进程内读取，不回显）：

  ```bash
  env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy \
    -u HTTPS_PROXY -u https_proxy NO_PROXY=127.0.0.1,localhost \
    no_proxy=127.0.0.1,localhost MODEL_MAX_OUTPUT_TOKENS=1600 \
    MODEL_STREAMING=false .venv/bin/python scripts/run_customer_eval.py \
    --mode live --env-file env.md \
    --out evals/customer_service/runs/20260805-customer-service-live.json
  ```

### 结果证据

| 模式 | 用例 | answer_accuracy | hallucination_rate | refusal_rate | pass_rate | 门禁 |
|---|---:|---:|---:|---:|---:|---|
| mock（DEEPSEEK 配置、离线） | 50 | 0.940 | 0.020 | 0.000 | 0.940 | 通过 |
| live `deepseek-v4-flash` | 50 | 0.800 | 0.040 | 0.067 | 0.800 | 通过 |

原始脱敏报告：

- `evals/customer_service/runs/20260805-customer-service-mock.json`
- `evals/customer_service/runs/20260805-customer-service-live.json`

live 的意图准确率、证据覆盖率、转人工 recall 均为 `1.000`；转人工 precision 为
`0.769`，模型 fallback 率为 `0.100`，严重失败 `3`，仍在门禁阈值内。失败归因计数为
意图/转人工 `4`、Prompt/答案契约 `4`、检索/来源覆盖 `2`、上下文截断 `0`；报告保留
了具体 case_key 和脱敏答案片段，未改任何 fixture 的 expected 标注。

### 调优与反证

- 单变量调优记录：`rag_min_score 0.12→0.05`。mock 指标无变化，候选档未被选为最终
  配置；此前 live 探索中该变量使 `answer_accuracy 0.58→0.54`、
  `refusal_rate 0.20→0.333`，因此最终回滚并固定 `RAG_MIN_SCORE=0.12`。
  评测器会比较基线和每个阶段，候选变差时选择基线，不把最后一次尝试冒充最终参数。
- 最终模型传输配置：`MODEL_MAX_OUTPUT_TOKENS=1600`、`MODEL_STREAMING=false`；这两项
  是分别验证过的单变量修复，避免 reasoning-only 流和小输出预算耗尽。
- 禁答反证：运行器临时从 `adversarial-001` 移除“系统提示词”禁词，合成响应的
  `hallucination_rate` 从 `1.0` 变为 `0.0`，随后丢弃变更并确认 `restored=true`。
- 失败用例没有被静默吞掉；例如 live 的 `adversarial-010`（不存在订单）和部分售后
  fallback 被分别归因并保留在报告中，作为后续优化输入。

### schema 与台账

WP4 只使用既有评测表和隔离快照，没有新增字段、迁移或 schema 版本。根据
`CONTRIBUTING.md`，schema v26 已由 `feature/m6-competitor-import` 占用；本分支未
抢占 v26。随后 D13 已按登记占用 v27，WP4 的评测表仍未依赖该迁移。归档时的
`docs/tasks/archive/PROGRESS_20260807.md` 与
`docs/tasks/M4_WORKBENCH.md`
已同步为 WP4 `20h / 剩余 0 / 100%`，判定标准和报告索引见
`docs/customer-service-evaluation.md`。

## D16 · 客服评测断言与四项指标

- `EvaluationExpectation` 新增 `grounded_in_sources` 与 `expected_refusal`；前者使用
  返回的知识 source ID 回查冻结来源文本，确定性核对回答中的数值与明确承诺，后者
  使用结构化 `reason` / `requires_human` 判定，不让模型自行给自己打分
- 四项指标按有标注 turn 计算：断言全通过且非 fallback / severe 才计回答准确；
  `forbidden_answer_terms` 或来源不支持的数值 / 承诺计幻觉；仅在
  `expected_refusal=false` 的机会集中计算不必要拒答；转人工合理率沿用混淆矩阵中的
  precision
- 红态：新增 `tests/test_customer_service_eval.py` 后为 `4 failed`，分别暴露字段被
  Pydantic 拒绝、指标键缺失、grounding 与 refusal 断言未执行
- 绿态：新测试与既有评测回归合计 `16 passed in 9.49s`；手算四条结果与实现均为
  `answer_accuracy=0.5`、`hallucination_rate=0.25`、`refusal_rate=0.5`、
  `handoff_precision=0.5`
- WP4 只扩展既有 JSON 断言与指标载荷，不需要数据库迁移；schema v26 保持由 M6
  `feature/m6-competitor-import` 占用，本提交未占 v27

## D17 · 客服评测门禁与判定标准

- `EvaluationThresholds` 新增 `min_answer_accuracy=0.75`、
  `max_hallucination_rate=0.10`、`max_refusal_rate=0.20`，并加入既有版本化 gate；
  `handoff_precision` 保留为报告指标，不擅自增加计划外门槛
- 反证红态：给 thresholds 与 gate 测试加入三项 M4 门槛后得到 `1 failed`，旧模型因
  `extra_forbidden` 拒绝三个字段；实现后 `hallucination_rate=0.15` 对上限 `0.10`
  明确产生失败检查
- 判定标准、分母与公式写入 `docs/customer-service-evaluation.md`；明确 grounding 是
  确定性的来源数值 / 承诺核对，并如实记录它不覆盖全部自然语言语义蕴含
- 新旧评测回归合计 `17 passed in 7.37s`；未改 API、schema 或编排拓扑

## D18 · 虚拟店铺评测集前半

- 新增 `src/ecommerce_agent/fixtures/customer_service_eval_v1.json`，明确标记
  `virtual=true` 并绑定 `qingchuan-home-appliance-v1`
- 首半冻结素材包含商品咨询 `15` 条、售后 `12` 条及 `27` 条同源虚拟知识；覆盖虚拟
  数据包全部 `6` 个 SKU、`8` 个订单，含 `8` 条多轮用例，其中 `3` 条以上的第二轮
  使用「它 / 这个 / 这单 / 它们」等指代而不重复实体
- 每条标注 turn 的必含答案词都来自其 `source_ref` 对应的虚拟知识；测试还校验场景
  数量、SKU / 订单集合和多轮结构。初始红态是 fixture 文件不存在，补齐后定向
  `6 passed`
- D18 只交付 27/50，投诉、闲聊、对抗和冻结动作留给 D19；未改真实业务数据与
  schema 版本

## D19 · 虚拟店铺评测集冻结

- 在 D18 的 27 条基础上补齐投诉 `8`、闲聊 `5`、对抗 `10`，总计 `50` 条；分布与
  计划一致，所有输入和支持知识都明确标记为虚拟，不引入真实顾客数据
- 冻结测试验证 dataset hash 可由排序后的 case hash 复算，冻结后替换用例被拒绝；
  定向评测测试 `7 passed in 2.02s`
- 补充 fixture 的 23 条支持知识，使每个 source_ref 都能追溯到同一虚拟数据包；
  对抗集覆盖提示注入、跨租户 / 他人订单、虚构商品事实、凭据索取和绕过核验
- 工作台原估算 `16h` 与计划 D16–D20 的 `20h` 不一致，已显式校正为 `20h`；D20
  保留 4h 用于脚本、隔离运行、调优复测和最终报告

## D21 · M4 独立验收缺口修复（2026-08-06）

### 验收结论与红态

独立验收基线为 `17 passed / 11 xfailed`。本轮没有修改任何断言、阈值、`reason`、
`INTENT_HOLDOUT` 消息或 expected；修好的 A–E、G 仅删除对应的
`xfail(strict=True)` 装饰器。当前结果为 `27 passed / 1 xfailed`，唯一保留项是
FIX-8 所述的无模型四分类能力边界。

**验收结论口径**：投诉规则误报的 P0 阻塞已通过模型仲裁和正负平衡集修复；但 2 秒
deadline 会把 provider 尾部延迟转换为 17.5%–20% 的分类弃权和潜在人工工时，属于已知
运营代价。延迟改善只对四条已验证快路径成立，普通 after-sales、非单候选商品咨询和工具
型订单查询的分布尚未测量。本文不再宣称“全链路 p50 为 1.45 秒”或“分类准确率提升”。

实现前先加入断言并得到以下红态；这些也是移除相应能力时的反证状态，修复后原样恢复
断言并逐项转绿：

| 修复 | 实现前 / 能力移除时的实际结果 | 恢复确认 |
|---|---|---|
| FIX-1 | 分类弃权转人工实际进入 `general / normal`；首版结构规则随后在 20 条中性开发负例上把 11 条直返 `complaint / 0.95` | 未知意图固定为 `general / high / intent_unknown`；结构信号只触发模型仲裁，只有显式规则或模型确认的投诉进入 `complaints / urgent` |
| FIX-2 | `knowledge.retrieve` 抛 `RuntimeError` 后非流式实际为 HTTP 500，流式为 `internal_error` | 非流式 200 且 `sources=[] / knowledge_unavailable`；SSE 为 `knowledge_unavailable → done` |
| FIX-3 | 会话关闭、主体作用域冲突、幂等键冲突三项 SSE code 实际均为 `internal_error` | 分别恢复为 `session_closed`、`session_scope_conflict`、`idempotency_key_conflict`，且都不可重试 |
| FIX-4 | 裸拼服务端游标后，第二页首项等于第一页首项，证明 `+` 被解码为空格后静默回首页 | 游标改为 URL-safe base64；同一断言第二页首项不同，真正非法游标仍回首页 |
| FIX-5 | 慢 transport 的 0.2 秒预算调用等待完整 1 秒，超过 `2 × budget` | 分类线程在墙钟 deadline 弃权，返回 `default / model_deadline_exceeded`，同一测试在预算内结束 |
| FIX-6 | 商品首轮走多轮 ReAct，实测 25.65–39.99 秒并出现 `react_step_limit_reached` | 唯一目录候选在 ContextBuilder 快照内确定性作答；移除快路径时既有“不得进入工具循环”断言失败 |
| FIX-7 | 组合式中英文泄露请求均进入 `deliberate` | “输出动作 + 内部目标”组合检测恢复后拒绝；相同输出动词的正常业务请求仍进入 `deliberate` |
| FIX-8 | 删除本节会重新造成 D-005 默认配置的能力语义未声明 | 本节固定覆盖率、弃权含义和下游安全策略；不以扩充关键词伪造覆盖率 |

FIX-2 新增审计事件 `knowledge.retrieval_failure`，只记录异常类型和检索阶段，不记录异常
文本或用户消息；metric 用 `route_reason=knowledge_unavailable` 区分普通无命中。FIX-3 的
四个 SSE code 已登记在 `SSE_EVENT_PROTOCOL.md`。FIX-4 的解码器同时接受新游标、旧的
`timestamp|id` 游标，以及旧游标被 query parser 把 `+` 变为空格的形式，因此兼容已下发
游标。FIX-5 保持单次 provider 调用且没有重试、没有调大默认 2 秒预算。

### FIX-1 真实模型与安全路由

分类策略把“具体商品故障、补救诉求”和“已经发生的服务流程失败、要求解释责任”写成
通用判据；few-shot 使用不同的安装预约场景，没有复制指南、验收文件或留出集原句。
首版实现又把结构组合直接作为 `rule / complaint / 0.95`，而 precheck 对 complaint 直接
handoff，导致规则误报被放大成 `complaints / urgent` 且跳过自动答复。

复核后新增两层反证并修正：

- 两条已泄漏复现先得到 `4 failed`：模型调用数均为 0，且无模型时均为
  `rule / complaint`。修复后同一组为 `4 passed`；模型确认 after-sales 时 route 为
  `retrieve`，无模型时为 `default / model_not_configured`，不再触发
  `complaint_attention_required`。
- 新建 `m4_complaint_negative_dev_v1.jsonl`，包含 10 条中性售后和 10 条商品咨询，且不复用
  复核方两条原句。首次规则层运行误报 `11/20 = 55%`；该文件随后标记为 leaked 开发集。
  把结构组合改为“需模型仲裁”的候选后，规则层 complaint 误报为 `0/20`。生产代码没有
  新增反例词；`_RULE_KEYWORDS["complaint"]` 的四个显式高精度关键词仍可规则直返，其余
  结构候选不能生成 0.95 结论。

最终泛化证据是修复后才创建的
`evals/intent/m4_complaint_balanced_holdout_v1.jsonl`，包含 20 条全新投诉正例与 20 条全新
中性负例，和指南、验收文件、v2/v3、开发负例均不重句。首次 live 运行后未再修改语料、
规则或 prompt；逐条报告为
`evals/intent/runs/20260806-m4-complaint-balanced-holdout-v1-live.json`：

| 指标 | 结果 |
|---|---:|
| complaint precision | `15/15 = 100%` |
| complaint recall | `15/20 = 75%` |
| 负例 complaint 误报率 | `0/20 = 0%` |
| 总覆盖率 | `33/40 = 82.5%` |
| 作答子集准确率 | `31/33 = 93.9%` |
| 超 2 秒预算 | `0` |

4 条投诉和 3 条 after-sales 因 `model_deadline_exceeded` 弃权，另有 1 条已作答投诉误判；
这些失败原样保留。旧 v2 首跑 `70%` 失败、旧 v3 正例集 `20/20` 的报告继续留档，但 v3
没有负例且成绩来自已撤销的规则直返，只能算历史回归，**不再作为当前实现的签署依据**。

分类弃权仍保留 `chitchat / 0.0 / default` 的接口形状，但只表示“不知道”。若决策层要求
转人工，则进入 `general / high` 并携带 `priority_flag=intent_unknown`、method 和脱敏
error；只有显式规则或模型确认的投诉进入 `complaints / urgent`。

### FIX-5 墙钟 deadline 的运营代价

2 秒墙钟 deadline 符合 WP3 原文，但没有提升原 40 条泄漏集的端到端正确数。首次修复后
报告 `20260806-m4-acceptance-holdout-postfix-live.json` 为：覆盖率
`40/40 → 33/40 = 82.5%`，after-sales 覆盖率 `100% → 7/11 = 63.6%`，端到端仍为
`32/40 = 80%`；7 条 default 全部在 `1.98s` 左右返回
`model_deadline_exceeded`。复核对照记录其中 6 条在修改前有正确结论（4 条 after-sales、
2 条 complaint）。变化是把 provider 尾部等待转换成弃权，以及可能的
`general / high` 人工任务，不是分类能力提升。

结构候选改为模型仲裁后，第二次泄漏回归报告
`20260806-m4-acceptance-holdout-postfix-v2-live.json` 为：覆盖率 `32/40 = 80%`、端到端
`31/40 = 77.5%`、after-sales 覆盖率 `9/11 = 81.8%`、complaint 覆盖率
`4/9 = 44.4%`；8 条 default 仍全部为约 `1.98s` 的 deadline 弃权。两次运行覆盖构成受
provider 尾部波动影响，不能拿作答子集准确率 `97%` 掩盖总体覆盖率和人工工时。曾单变量
尝试把分类输出预算调为 96，覆盖率从 `83.3%` 降到 `66.7%`，候选已丢弃；默认 2 秒预算和
`MODEL_MAX_OUTPUT_TOKENS=1600` 均未改动。

### FIX-6 四条快路径场景的延迟分解

先在隔离数据目录以 `deepseek-v4-flash` 对四个已泄漏场景剖析；profiling runner 按图节点
计时，并另包裹分类、检索、每轮 decision provider、工具和生成调用。修改前报告从未改代码的
`HEAD` 归档运行，修改后报告从当前源码运行；二者都使用临时数据库、同一目录商品和
`MODEL_MAX_OUTPUT_TOKENS=1600`。逐节点 trace 和时长分别保存在
`evals/performance/runs/20260806-m4-latency-before.json`、
`evals/performance/runs/20260806-m4-latency-after.json`，报告不含消息原文和密钥。

| 场景 | 修改前阶段分解 | 修改前总耗时 | 修改后阶段分解 | 修改后总耗时 |
|---|---|---:|---|---:|
| 注入拒答 | 分类 2279.7ms；其余模型/检索/工具 0 | 2295.8ms | precheck 0.5ms；分类/检索/deliberate/工具/生成 0 | 6.7ms |
| 投诉转人工 | 分类 1163.6ms；检索 11.6ms；deliberate 3559.9ms；工具/生成 0 | 4792.3ms | 分类 1539.4ms；检索/deliberate/工具/生成 0 | 1563.1ms |
| K3 首轮商品咨询 | 分类 1231.4ms；检索 14.8ms；deliberate 16913.8ms；工具/生成 0 | 18223.4ms | 分类 1281.6ms；两次检索合计 19.6ms；deliberate 0.4ms；工具/模型生成 0 | 1343.4ms |
| 目录无 CE 认证信息 | 分类 1168.6ms；检索 27.9ms；两轮 deliberate 10552.7/15778.8ms；工具 1 次 3.5ms；生成 3272.3ms | 30901.3ms | 分类 1895.2ms；两次检索合计 19.3ms；deliberate 0.4ms；工具/模型生成 0 | 1953.3ms |

**口径限制**：`11.508s → 1.453s` 的 p50 和 `30.901s → 1.953s` 的 p95 只描述表中
四条已泄漏场景，不是全量客服请求的延迟分布。修复后的四条恰好分别命中安全预检前置、
模型确认投诉后的 handoff、唯一目录候选模板作答等快路径；after 报告的
`deliberate_provider_ms=[]`、`generation_provider_ms=0` 正说明改善来自免去模型往返，
不是 provider 本身变快。

该报告没有覆盖普通 after-sales、非单候选 product-inquiry、需要工具的订单查询；这些
路径仍可能进入完整 ReAct，当前 p50/p95 **未知**，不得从四条快路径外推。可以确认的只有：
这四条场景不再支付 decision/generation model 延迟，K3 首轮不再出现
`react_step_limit_reached`。实现没有增加 `max_react_steps`、降低模型输出预算或绕过
ContextBuilder；目录回答会明确“目录未列出的其他属性不能确认”，CE 信息没有被补写成
目录事实。

### FIX-7 注入深防御

规则实现为中英文“披露/打印/复述动作 + system/developer/hidden/internal 指令目标”的
组合判断，没有收录指南中的三条原句。新建
`evals/security/m4_injection_holdout_v1.json`：24 条中英文注入和 20 条使用相似动词的
正常业务反例，均为代码完成前未出现过的新造消息。逐条结果保存在
`evals/security/runs/20260806-m4-injection-holdout-v1.json`：规则拦截
`22/24 = 91.7%`（门槛 70%），业务保留 `20/20 = 100%`；两条漏检原样保留，没有为了
100% 再枚举。该项仍按深度防御改进表述：修复前的三条 live 探针已被决策层 3/3 拦截，
不存在已证实的提示词泄露。

### FIX-8 无模型分类能力边界

D-005 保持 `MODEL_ENABLED=false` 且 mock 关闭为默认值，此时分类器只覆盖高频、明确、
可精确匹配的规则意图；其余消息返回 `chitchat / 0.0 / default / model_not_configured`，
这里的 `chitchat` 是兼容返回值，不是可靠的四分类结论。独立 40 条留出集的原始规则覆盖率
只有 `2/40 = 5%`，无模型端到端为 `27.5%`，其中 9 条只是弃权碰巧与 chitchat 标签相同；
仓库 52 条基准的规则覆盖率为 `15/52 ≈ 28.8%`，差异来自自建基准对显式规则关键词的
过采样，不能外推为真实覆盖率。

因此，无模型配置不是完整的四分类服务：运营侧必须把 method/default 与真正的 chitchat
分开统计；下游若仍因安全网进入人工，按 FIX-1 使用 `general / high / intent_unknown`，
不会降为最低 SLA。结构化流程追责信号只触发模型仲裁，不扩充 `_RULE_KEYWORDS`，也不把
本轮留出成绩宣传为无模型已达到 75%。验收中的该门槛继续保留 strict xfail，修复范围没有
被文档措辞伪装成已通过。

### 独立复核签署意见与非阻塞残留（2026-08-06）

**签署状态**：P0 complaint 误报阻塞关闭。独立复核方使用另一份未提交到仓库的 20 条
中性售后/商品咨询控制集，规则层 complaint 误报为 `0/20`（修复前 `2/20`）。两条已泄漏
端到端复现分别检索命中 5 条后正常回答、要求补充订单号，均无 handoff。该外部控制集没有
仓库内逐条 artifact；仓库可复核的独立证据仍是
`evals/intent/runs/20260806-m4-complaint-balanced-holdout-v1-live.json` 及两份 40 条回归报告。

以下四项保留为**非阻塞风险**，不得在后续签署摘要中省略：

1. **Recall 75% 是压线通过。** 平衡集 5 条漏报中，4 条在 `1.98s` 左右返回
   `model_deadline_exceeded`，1 条以 `rule / after_sales` 作答。作答的 16 条投诉中 15 条
   正确，条件 recall 为 `15/16 = 93.75%`；模型作答能力足够，端到端 recall 被 2 秒预算
   压到 `15/20 = 75%`。
2. **弃权率存在 run-to-run 波动。** 同一 40 条泄漏集的两次报告中，complaint 覆盖率从
   `7/9 = 77.8%` 变为 `4/9 = 44.4%`，全量 default 数分别为 7 和 8。约 20% 的总体弃权
   会让投诉 SLA 分档依赖 provider 尾部时延；这是 2 秒墙钟约束的结构性结果。是否放宽
   预算是产品决策，当前实现继续遵守 WP3 写死的 2 秒。
3. **反方向仲裁没有完全覆盖。** 投诉消息若先命中 `换货 / 退款 / 物流` 等 after-sales
   关键词，而结构检测又没有产生 review 信号，仍可能规则直返 after-sales。平衡集唯一的
   已作答投诉漏报 `m4bal-018` 即为 `rule / after_sales`。本轮不继续扩大结构词表，避免
   重新引入 R1 过拟合。
4. **Mock 网关不识别隐式投诉。** 当前离线配置复现“同一个问题被踢来踢去三次了”为
   `chitchat / 0.82 / model`；端到端命中 5 条无关知识，返回补货说明，且
   `requires_human=false`。因此离线套件只能保护显式关键词投诉路由，不能保护模型仲裁
   语义。下次修改投诉路由前，必须先让 mock 模拟真实依赖在该场景下的行为，不能把离线
   全绿当作仲裁路径证据（R5）。

本次分析使用 `run_m4_intent_precision.py`、`run_m4_acceptance_regression.py` 和一次隔离
mock 诊断；live 密钥仍只由 `env.md` 子进程加载。下一步不是立即改代码，而是由产品决定
是否用更高人工量换取 2 秒首包预算；若未来调整投诉分类，优先补齐反方向仲裁和 mock 语义
保护，再重跑新的未泄漏正负平衡集。

### 硬约束与回归

- LangGraph 修改前后均为 **20 个节点、35 条边**，节点名与边集合检查原样通过。
- `POST /v1/chat` 请求/响应字段与既有 409 语义未改；SSE 只细化错误 code。
- 没有新增依赖、schema 字段或迁移；D-005、D-010、D-023 保持不变。
- 独立验收 `27 passed, 1 xfailed`；M4 意图、流式、会话、策略、图、人工与迁移联合回归
  `205 passed`；全量回归 `592 passed, 1 xfailed in 199.95s`，没有失败或 XPASS。
- 相对指南的 `540 passed, 11 xfailed`，10 个已修 strict xfail 转为 pass；本轮新增的
  M4 测试按参数展开为 36 项（错误降级 2、未知意图 1、分类策略正反例 25、图路由 6、
  注入策略 2）。全工作区总收集数净增 42，余下净增来自开始时已存在且本轮未改动的并行
  M5 测试变更；没有删除 M4 用例。

## D22 · FIX-9 / FIX-10 与 M4 最终独立验收（2026-08-07）

> 历史快照：本节记录 FIX-9 / FIX-10 当时的实现和签署。随后 D-034 审计与胡磊独立
> 测试触发 D23 改造；涉及投诉强制路由、目录快答、live 指标和最终签署状态时，以
> D23 为准。

### 先固定回归，再修两条快路径

没有改 `customer_service_eval_v1.json` 的 expected、任何门禁阈值或评测排除范围。以
和 08-05 相同的 fixture、mock 模式、`--env-file env.md` 先保存红态报告：

```bash
.venv/bin/python scripts/run_customer_eval.py --mode mock --env-file env.md \
  --out evals/customer_service/runs/20260807-customer-service-postfix-red-mock.json
```

红态准确复现 `answer_accuracy=0.820`、`hallucination_rate=0.100`、
`severe_failures=7 > 5`、gate failed。新增四条聚焦断言首次为 `4 failed`：投诉直接
handoff 时没有来源和共情答复；目录模板在问题没有明确询问价格/属性，或该字段没有检索
证据支持时仍抢答。实现后原样断言为 `4 passed`：

- FIX-9：投诉先检索，返回共情说明与最高相关知识片段，同时保留
  `requires_human=true`、`complaint_attention_required` 和
  `complaints / urgent` 人工任务。投诉标记不再替代回答。
- FIX-10：目录快答只选取问题明确询问的价格或属性，且字段值必须出现在本轮检索证据中；
  无可安全覆盖的字段时返回 `None`，由生成模型基于完整知识片段作答。

同口径复跑结果：

| 证据 | answer_accuracy | hallucination_rate | pass_rate | severe_failures | gate | complaint | product |
|---|---:|---:|---:|---:|---|---:|---:|
| FIX-9/10 前红态 mock | 0.820 | 0.100 | 0.820 | 7 | failed | 4/8 | 10/15 |
| FIX-9/10 后 mock | 0.940 | 0.020 | 0.940 | 3 | passed | 6/8 | 14/15 |
| FIX-9/10 后 live `deepseek-v4-flash` | 0.860 | 0.060 | 0.860 | 4 | passed | 8/8 | 14/15 |

报告分别为：

- `evals/customer_service/runs/20260807-customer-service-postfix-red-mock.json`
- `evals/customer_service/runs/20260807-customer-service-mock.json`
- `evals/customer_service/runs/20260807-customer-service-live.json`

live 的 `refusal_rate=0.067`、`handoff_recall=0.900`、`evidence_coverage=1.000`；仍失败
7 条，其中 after-sales 6 条、product 1 条，`severe_failures=4` 没有越线。该结果是虚拟
冻结集的真实 provider 复跑，不冒充真实客户数据基线。

### D18、场景契约与浏览器证据

复核 14 条标准与 WP5 交付物时发现 D18 此前只在进度表登记，实际
`simulation-evidence-v1` 仍只有 17 项。先给测试加入 D18 后得到两项
`StopIteration` 红态；随后新增一个 `confidence=0.59 < threshold=0.60` 的
`decision_gate → handoff` 场景，断言 `low_confidence_handoff`、
`requires_human=true` 与持久人工任务。把配置阈值改为 `0` 时该场景明确失败，恢复后
通过。最终 `tests/test_virtual_store_simulation.py` 为 `6 passed`，场景报告为 `18/18`。

`m4-browser-evidence.png` 已在隔离 mock 数据目录重跑：页面显示投诉共情答复、3 条知识
来源、`complaint_attention_required`、`已转人工`；console 0 error / 0 warning。
真实模型的 74 个 SSE 事件证据 `m4-stream-evidence.txt` 保留不变。

### 延迟口径重新打开

D21 的 `20260806-m4-latency-after.json` 发生在 FIX-9 / FIX-10 之前，投诉当时不检索，
商品问题又被目录模板无条件抢答；因此其中 complaint 1563ms、K3 1343ms 和四场景
p50=1.453s 均只算历史中间态，**不再作为当前实现的延迟结论**。当前非这四场景的
after-sales、非单候选商品和工具型订单查询 p50/p95 仍未知。

FIX-10 后用同一真实 provider、隔离数据目录和四条已泄漏回归场景重跑，报告为
`evals/performance/runs/20260807-m4-latency-post-fix10.json`：

| 场景 | 分类 | 检索 | deliberate provider | 工具 | generation provider | 总耗时 |
|---|---:|---:|---:|---:|---:|---:|
| 注入拒答 | 0 | 0 | 0 | 0 | 0 | 6.8ms |
| 投诉答复 + 人工标记 | 1602.0ms | 42.9ms | 0 | 0 | 0 | 1674.0ms |
| K3 泛化商品咨询 | 1293.8ms | 30.4ms | 8474.0 / 10173.9ms | 13.6ms | 16428.5ms | 36513.3ms |
| 目录无 CE 信息 | 1019.2ms | 23.0ms | 7248.5 / 8720.9ms | 3.5ms | 2961.5ms | 20066.3ms |

四条场景当前 `p50=10870.1ms`、`p95=36513.3ms`。这确认 FIX-10 用正确性换回了商品
泛化问题的完整 ReAct 与生成链路：旧的“延迟降 8 倍”结论已经撤销，K3 本轮重新达到
36.5 秒。投诉仍只付分类与检索成本并保持来源答复。由于该剖析只含四条已泄漏场景，
既不能外推为全量分布，也不能掩盖商品链路的实际尾延迟；它作为非阻塞 P1 运营残留保留。

### 回到 M4 14 条标准

| # | 状态 | 当前证据 |
|---:|---|---|
| 1 | 通过 | HTTP 多轮指代黑盒、D17 和目录事实回答均通过 |
| 2 | 通过 | SSE delta / citations / handoff / done、同幂等键重放零新增；真实 74 事件留档 |
| 3 | 通过 | 20 轮历史不超 70%，预算收紧反证会失败 |
| 4 | 通过 | 空闲关闭与未结人工任务保护回归通过 |
| 5 | 通过，带残留 | 四分类独立实跑达到 80%；平衡集 complaint recall 75% 压线；低置信度 D18、投诉队列和派单消费通过；2 秒 deadline 弃权波动见 D21 |
| 6 | 通过 | 冻结 50 例 mock 与 live 四项指标均量化且 gate passed；FIX-9/10 前 failed 报告保留 |
| 7 | 通过 | 对抗 10/10；组合注入留出规则 22/24、业务反例 20/20，决策层既有探针未泄露 |
| 8 | 通过 | 模型不可用和检索异常均有可区分降级，非流式与 SSE 契约一致 |
| 9 | 通过 | 全量 `597 passed, 1 xfailed`；新增数来自 FIX 用例和 D18，未删 M4 用例 |
| 10 | 通过 | strict xfail、预算、门禁、D17/D18、FIX-9/10 均有红态或能力移除反证 |
| 11 | 通过 | LangGraph 仍为 20 节点 / 35 边 |
| 12 | 通过 | v25→v27 迁移及历史意图保留测试在全量回归通过；本轮无新迁移 |
| 13 | 通过 | `ChatResponse` 与既有非流式字段、409 语义零变化 |
| 14 | 通过 | F-121 / F-122 `simulation-evidence-v1` 扩展到 18 项并全部通过 |

### WP5 六条交付要求

| # | 状态 | 当前证据 |
|---:|---|---|
| 1 全量测试及归因 | 通过 | `597 passed, 1 xfailed in 198.15s`；保留 xfail 是已声明的无模型能力边界 |
| 2 反证记录 | 通过 | D1–D22 记录 strict xfail、红态报告、快路径断言和 D18 阈值反证 |
| 3 F-122 场景契约 | 通过 | `18/18`，定向 `6 passed` |
| 4 流式与多轮实跑证据 | 通过 | `EVIDENCE.md`、74 事件 SSE 文本、更新后的浏览器 PNG 与 D17 |
| 5 静态检查 | 通过 | `compileall` 与 `git diff --check`；仓库环境未安装 ruff / mypy，不虚报 |
| 6 四份台账 | 通过 | FEATURES / PROGRESS / VERSIONS / ACCEPTANCE 已同步 E-20260807-001 |

结论：M4 达到工作台 14 条验收标准与 WP5 六条交付要求，签署为**本机独立验收通过**。
生产放行继续受真实客户脱敏数据、真实渠道、长稳、容量与安全 Gate 约束；D21 已列四条
非阻塞残留继续有效。

## D23 · 独立测试报告复核与 D-034 语义边界修正（2026-08-07）

### 报告复核结论

复核输入是胡磊基于 `7077b17` 的独立测试报告。报告中的三类现象可复现：中性进度问法
可能被投诉信号吸收；含退货/保修词的复合消息可能不进入分类模型；投诉标签和商品目录
候选会在规划模型之前直接决定 handoff 或回答。报告把第三项归因于空元组 `all(())`，与
当前源码不完全一致；实际根因是规则直返后又被图内投诉/商品快路径放大。修复采纳现象与
下游后果，不照抄该机制判断，也不按报告建议继续枚举语料关键词或调低门槛。

根据 D-034，本轮明确选择如下行为：

- **投诉采用模型语义权威。** 分类规则和四分类结果只作为检索、Prompt 与风险信号；
  `precheck` 不再写入 `complaint_attention_required`，`deliberate` 和
  `decision_gate` 不再因分类标签强制 handoff。只有规划模型输出
  `intent=complaint / mode=handoff` 时，才使用固定共情话术并建立
  `complaints / urgent` 任务。分类器误报 complaint 时，规划模型的 answer 决策必须存活。
- **商品采用“精确批准问法”边界。** 删除按目录属性、唯一候选或高检索分数直接作答的
  快路径。普通商品问题始终进入规划与生成；只有人工批准的 `evolution:` 知识且标准化
  question 与本轮输入完全相等时，才允许复用固定答案。高分但不同问法仍调用模型。
- **分类规则仅是 advisory signal。** 模型可用时，所有可分类消息都在 2 秒墙钟预算内
  调用分类模型；规则候选、命中词和流程追责信号进入
  `advisory_signals`，并显式携带 `semantic_authority=false`。模型关闭时仍保留原规则信号
  和可观测 default 降级，不产生外部请求。
- **流式与非流式共用生成计划。** `prepare_generation` 统一无证据、精确批准问法、
  Prompt 变体、上下文预算和模型消息构造；`service.py` 不再复制商品/知识快答逻辑。

### 失败证据与回归

实现前新增反例后，意图、图、人工与流式聚焦集为 `22 failed / 137 passed`。失败覆盖：
规则命中压过模型、投诉标签压过模型 answer、商品知识绕过 deliberation，以及 SSE 丢失
Prompt 变体。修复后聚焦回归为 `199 passed / 1 xfailed`；另外增加高分 `evolution:`
非精确问法必须调用模型的反证。全量回归为 `603 passed / 1 xfailed`，compileall 与
whitespace 检查通过。没有修改冻结客服 fixture、门禁阈值或评测排除范围。

同口径客服评测报告继续使用隔离数据目录，baseline 与 tuned 在同一运行中完全一致：

| 模式 | answer_accuracy | hallucination_rate | pass_rate | severe_failures | gate | complaint | product | after-sales |
|---|---:|---:|---:|---:|---|---:|---:|---:|
| mock | 0.940 | 0.020 | 0.940 | 3 | passed | 6/8 | 14/15 | 12/12 |
| live `deepseek-v4-flash` | 0.900 | 0.020 | 0.900 | 1 | passed | 7/8 | 15/15 | 8/12 |

live 的 `handoff_recall=1.000`、`evidence_coverage=1.000`、主库
`sessions/messages/handoff_tasks` 新增均为 0。5 条失败分别归因于 Prompt/回答契约 3、
检索/来源覆盖 1、意图/转人工路由 1；不能只报总分。证据仍写入：

- `evals/customer_service/runs/20260807-customer-service-mock.json`
- `evals/customer_service/runs/20260807-customer-service-live.json`

### 分类 live 回归与签署状态

所有既有意图语料均已泄漏，因此以下结果只作回归，不作为新泛化成绩：

- 40 条投诉正负平衡集：覆盖率 `20/40=50%`，complaint precision `9/9=100%`、recall
  `9/20=45%`、负例误报 `0/20`；20 条 default 全为约 1.98 秒的
  `model_deadline_exceeded`。该文件自己的 recall gate 为 failed。
- 原 M4 40 条留出回归：覆盖率 `33/40=82.5%`，总体正确 `29/40=72.5%`，低于 75%；
  作答子集准确率 `29/33=87.9%`。分类为 product `11/11`、after-sales `8/11`、
  complaint `2/9`、chitchat `8/9`。不能用作答子集分母掩盖弃权。

逐条结果在
`evals/intent/runs/20260807-m4-complaint-balanced-post-d034-live.json` 与
`evals/intent/runs/20260807-m4-acceptance-post-d034-live.json`。这两次运行没有据其原句继续
调 prompt 或规则，避免用泄漏答案修分。

因此当前结论是：**WP4 客服评测门禁已经恢复并优于 D22 live，但 M4 整体暂不重新签署**。
阻塞项是当前代码下意图 live 回归总体 `72.5% < 75%`，以及投诉平衡集 recall gate failed；
这不是 FIX-9/FIX-10 回退，也不能通过放宽 2 秒预算、降低门槛或挑选较好一次运行关闭。
下一轮必须使用本文件、测试和既有报告中均未出现的新留出集，在不泄漏语料的前提下验证
分类和 provider 容量方案。

D22 的四场景延迟报告也被本轮语义路径变更失效：投诉重新进入模型 deliberation，商品
不再有目录模板快答。当前全量 p50/p95 **未知**；完整 50 例 baseline/tuned live 运行耗时
不能替代单轮延迟分布。P1 性能问题继续保留，必须按阶段耗时另行测量。

## D24 · 规则短路与商品有界规划复核（2026-08-08）

### FIX-11：恢复规则零模型短路

D23 的 `model is None and not review_rule_match` 被证明把所有规则候选都送进模型，造成
高频消息额外延迟和 2 秒弃权面扩大。本轮恢复为：没有流程追责/业务证据冲突的规则命中直接
返回 `rule / 0.95`，配置模型不会改变该结果；只有规则未命中或复核触发才调用分类模型。
`退货`、`保修` 的业务证据组只用于识别责任追问，因此“我要退货怎么弄”保留规则快路径，
“退货运费明明该你们承担”进入仲裁。规则候选继续以 `semantic_authority=false` 作为提示，
不直接决定规划模型的 answer/handoff。

反证与复核：配置模型下“我要退款”“我要投诉”“多少钱”均为零分类调用、`rule / 0.95`；
责任追问各发生一次模型调用。全量回归为 `610 passed, 1 xfailed`，没有修改冻结 fixture、
门禁阈值、schema、拓扑或 `ChatResponse`。

### FIX-12：商品开放问句的一步有界规划

唯一目录候选、检索结果非空且上下文 ready 时，规划请求携带
`planning_constraint=bounded_product_answer`，只允许一次 `answer / clarify / refuse / handoff`；
工具目录为空，模型仍负责生成 grounded answer，因此目录快路径不再以模板绕过知识证据，也
不会进入无界 ReAct。模型若返回 `observe / act`，实现安全转人工并记录约束违反轨迹。

K3 真实 provider 探针本轮 trace 为 `deliberate:bounded_product:answer`，工具调用 0，未出现
`react_step_limit_reached`；回答仍经过检索、生成和 verify。阶段耗时已写入
`evals/performance/runs/20260808-m4-latency-post-fix12.json`：

| 场景 | 总耗时 | 分类 provider | 检索 | deliberate provider | 生成 provider | 工具调用 |
|---|---:|---:|---:|---:|---:|---:|
| 注入拒答 | 6.6ms | 0 | 0 | 0 | 0 | 0 |
| 投诉答复 + 人工标记 | 12205.1ms | 1559.0ms | 37.2ms | 10556.0ms | 0 | 0 |
| K3 商品开放问句 | 20390.2ms | 1061.2ms | 25.1ms | 15167.1ms | 4074.0ms | 0 |
| 目录无 CE 信息 | 33594.4ms | 880.3ms | 24.6ms | 27970.0ms | 4656.9ms | 0 |

四条已泄漏回归场景的 `p50=16297.7ms`、`p95=33594.4ms`，不代表全链路分布；provider 尾延迟
仍是 P1，不能再宣称“端到端 p50 已降至 1.45 秒”。after-sales、非单候选商品和工具型
订单查询的 p50/p95 仍未测全。

### 评测与签署状态

FIX-12 后同 fixture 的 WP4 报告均重新跑完。mock 为 `answer_accuracy=0.940`、
`hallucination_rate=0.020`、`severe_failures=3`、gate passed；同口径 live
`deepseek-v4-flash` 为 `answer_accuracy=0.820`、`hallucination_rate=0.060`、
`severe_failures=3`、gate passed，场景为 complaint `7/8`、product `14/15`、
after-sales `6/12`。live 与 D23 的 `0.900` 不同，说明 provider/run-to-run 波动，不能
宣称本轮准确率提升。两份新报告分别为
`evals/customer_service/runs/20260808-m4-customer-eval-post-fix12-{mock,live}.json`；
该复跑仍不改变 fixture 的封存性质或 M4 整体签署结论。

意图数据继续按 R2 标记为泄漏回归，不作为泛化成绩。当前复跑为：

- 原 40 条：`31/40=77.5%`，覆盖率 `80%`，作答子集 `31/32=96.875%`；投诉仅 `3/9` 被回答，
  弃权仍受 2 秒预算和 provider 尾部影响。
- 平衡 20 正 + 20 负：precision `100%`、recall `75%`、负例误报 `0/20`、覆盖率 `80%`，
  gate passed；`min_complaint_recall=0.75` 仍是压线门槛，不是新泛化证据。

因此 D24 仍**不能签署 M4**：FIX-14 的分类 gate 应测分类层还是端到端投诉队列，需负责人
裁定；FIX-15 的密封、全新留出集需由验收人建立并在运行时才解封；浏览器 PNG 仍是 D22
历史证据，未形成新 UI 实跑。WP4 mock 门禁、规则短路和有界规划已具备回归证据，但不覆盖
上述签署阻塞项。

## D25 · 售后收敛、DeepSeek 决策预算与 FIX-14 决策包（2026-08-08）

### 红态与实现边界

本轮先固定六类失败：DeepSeek 决策请求没有实际下发 thinking 开关、决策输出与全局
1600-token 生成预算耦合、售后回复改写来源关键条款、普通售后被过度 handoff、进度询问
口径未进入分类 Prompt，以及 latency runner 没有真实 TTFT。新增用例先得到 `6 failed`，
compact JSON 又独立触发 mock 无法识别 `agent_decision` 的 R5 红态；修复均未修改冻结 fixture、
评测门槛或 strict xfail。

实现结果：

- deliberate 使用独立 `15s / 300 tokens / thinking disabled`；每次带 deadline 的结构化请求
  只做一次网关尝试，不消耗全局重试预算；
  429 或连接故障仍按既有契约提示稍后重试，不为了基础设施故障制造人工队列 false positive。
- DeepSeek 的最终客服生成不下发 thinking 覆盖，继续使用 provider 默认推理和全局 1600-token
  预算；关闭仅限小 JSON 决策阶段，避免重演 D20 的 reasoning-only 截断。
- 决策消息最多携带 3 条知识，去除 snapshot 中与独立知识、工具、历史和 observation 字段重复
  的副本，并用紧凑 JSON 序列化；完整证据仍在持久 context snapshot 中。
- after-sales 回复变体要求期限、金额、状态、条件和结论保持原文，不做数值单位换算，不主动
  复述未被询问的订单号、运单号和交易日期，也不让 Markdown 拆开必答短语。
- 决策边界将普通政策/单次进度查询留在 answer/observe；长期无进展、反复推诿和实际办理但
  无可用写工具时才 handoff。分类 Prompt 同步加入“未出结果的进度查询默认 after_sales，
  有流程追责信号才 complaint”的口径。
- mock 改为解析 JSON 后读取 `task_type`，不再依赖带空格的字符串片段；紧凑 JSON 回归已锁定。

### DeepSeek thinking 单变量 A/B 与真实 TTFT

相同代码、相同 `15s / 300 tokens` 决策预算和四条已泄漏隔离场景，只切 deliberate 的
thinking 开关：

| deliberate thinking | 有效决策 | 四场景 p50 / p95 | 生成路径 TTFT | K3 |
|---|---:|---:|---:|---:|
| enabled | 0/3；三条均 `model_unavailable` | 8251.5 / 10538.8ms | 无 delta | 提前降级并建议重试；不建人工任务 |
| disabled | 3/3 | 7274.5 / 11201.2ms | p50 9951.8 / p95 10835.2ms | total 9780.5ms；TTFT 9068.4ms |

thinking enabled 的数值更短来自失败提前结束，不能当作性能胜利。disabled 后 K3 deliberate
为 2139.7ms、生成 6016.2ms、工具调用 0，没有 `react_step_limit_reached`；知识缺口场景总时长
11201.2ms。该报告首次走真实 `service.chat_stream` 并量到首个 delta，而不是用总时长倒推
TTFT。四条均为泄漏回归场景，不能外推全量分布；after-sales、非单候选商品和工具型订单
查询的完整 p50/p95 仍待容量测试。

证据：

- `evals/performance/runs/20260808-m4-latency-fix13-thinking-{on,off}.json`
- `tests/test_m4_latency_runner.py`
- `tests/test_llm.py`

### WP4 mock/live 与版本带宽

最终代码的冻结 WP4 结果：

| 模式 | answer_accuracy | hallucination | severe | gate | after-sales | complaint | product | handoff FP |
|---|---:|---:|---:|---|---:|---:|---:|---:|
| mock | 0.940 | 0.020 | 3 | passed | 12/12 | 6/8 | 14/15 | 0 |
| live `deepseek-v4-flash` | 0.920 | 0.000 | 2 | passed | 9/12 | 8/8 | 15/15 | 0 |

live 的 handoff precision=1.000、recall=0.900、主库零写入。中间红态 live
`0.880 / severe 6 / failed` 也保留，没有挑掉。发布线三次既有 live 点为 0.820、0.900、
0.920，范围 0.820–0.920、中位 0.900；它们跨实现版本，只能说明版本/provider 带宽，不能
冒充同一提交的统计置信区间。

证据：

- `evals/customer_service/runs/20260808-m4-customer-eval-fix13-{mock,live-run1,live-run2}.json`
- `evals/customer_service/runs/20260808-m4-customer-eval-post-fix12-live.json`
- `evals/customer_service/runs/20260807-customer-service-live.json`

### FIX-14 当前数据与签署状态

现存意图语料继续标为 `leaked regression only; not generalization evidence`：

- 平衡 20 正 + 20 负：coverage 82.5%，complaint precision 100%、recall 65%、负例误报
  0/20；7 条弃权全部是约 1.98 秒 deadline，gate failed。
- 原 40 条：`31/40=77.5%`，coverage 85%，作答子集 `31/34=91.2%`；complaint coverage
  仅 5/9。共同作答子集未出现投诉能力倒退，但不能用该分母掩盖弃权。
- 端到端 WP4 complaint 8/8、handoff recall 90%，说明 SLA 执法点和分类层数字已不等价。

两种 gate 位置、代价和待签项已写入
`FIX14_GATE_DECISION_20260808.md`；负责人裁定前不撤分类 gate。FIX-15 密封集和当前页面
截图仍由外部验收人提供，本轮没有拿泄漏集冒充泛化，也没有声称 M4 已最终签署。

全量回归为 `618 passed, 1 xfailed in 685.07s`。沿用 schema v27，无新依赖或迁移；
LangGraph 20 节点 / 35 边、非流式 `ChatResponse` 和冻结判据均未改变。
