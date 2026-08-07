# 决策权边界与可演进性审计（2026-08-07）

本文是一次全库审计的完整清单，对应规范见 `CONTRIBUTING.md` 第 10、11 节与决策
D-034、D-035。缘起是外部 code review 指出「路由没写死在拓扑里，但写死在节点内部」；
随后两轮全库扫描加人工复核，确认问题面比初查大得多，且同类模式已经产生真实缺陷。

两条主结论：

1. **语义路由**：LLM 名义上是决策者，实际决策空间只剩「非规则命中、非投诉、
   非业务动作正则、非高分知识」的消息；默认配置（`MODEL_ENABLED=false`）下意图
   分类器根本没有模型，关键词规则层是唯一分类器。
2. **可演进性**：同一事实手抄多处、测试断言全局状态全等、文档写死快照数字，
   三种模式各自都已经腐烂或产生缺陷（含 2 个确认的真实 bug）。

清单条目格式：位置 — 行为 — 判定。行号以 2026-08-07 `feature/roadmap-reset-m5r-m6r`
分支为准，后续以符号名检索为准。

---

## 一、语义路由类

判定标准（详见 CONTRIBUTING 第 10 节）：确定性代码回答「这个动作现在能不能安全
执行」是校验，可以写死；回答「用户想要什么、下一步该做什么」是语义路由，必须留给
模型。下列条目均属后者或向后者供弹药。

### 1.1 分类与前置短路（LLM 无参与机会）

| 位置 | 行为 |
|---|---|
| `src/ecommerce_agent/intent.py:246-256` | 关键词规则命中直接返回 0.95 置信度，`保修/退货/换货/投诉/差评/举报` 等词裸命中即定案；与自家标注口径（`intent.py:207` 售前保修属 product_inquiry）矛盾，规则优先 |
| `src/ecommerce_agent/intent.py:257-258` | **缺陷**：规则命中但需模型复核时，若 `model=None` 则整体降级 `chitchat`——连本来的规则标签都丢了，比没有复核层更糟 |
| `src/ecommerce_agent/intent.py:333-357` | `_matches_process_accountability`：五组正则手写「流程追责投诉」的完整语义定义 |
| `src/ecommerce_agent/intent.py:108-161,373-415` | `_RULE_BUSINESS_EVIDENCE`：第二层共现规则决定第一层关键词是否可信，仅覆盖 4 个词 |
| `src/ecommerce_agent/intent.py:491-497` | 一切分类失败（无模型/超时/坏载荷）都归 `chitchat`，下游按 `greeting` 意图检索知识 |
| `src/ecommerce_agent/graph.py:401` | `MODEL_ENABLED=false`（生产默认）时 `classify()` 收到 `model=None`：规则层不是「优先于模型」，是「没有模型」 |
| `src/ecommerce_agent/graph.py:385-394` | precheck 短路把注入/越权消息盖上 `customer_intent="chitchat"` 标签（拒绝本身正确，标签副作用错误） |
| `src/ecommerce_agent/graph.py:404-409` | precheck 阶段就写死 `complaint_attention_required` 路由理由，deliberate 修好也会被它穿透 |

### 1.2 deliberate / decision_gate 的确定性覆盖

| 位置 | 行为 |
|---|---|
| `src/ecommerce_agent/graph.py:535-549` | `customer_intent=="complaint"` 在调用模型前直接构造 handoff 决定 |
| `src/ecommerce_agent/graph.py:551-570` | `approved_knowledge_reuse` 快速路径：evolution 来源 + 分数过线即绕过模型（已审核+完全匹配的部分合规，分数过线部分不合规） |
| `src/ecommerce_agent/graph.py:572-593` | `product_inquiry` 快速路径：未经审核的普通知识高分也直接出答案 |
| `src/ecommerce_agent/graph.py:686-689` | 模型给出的 `reason` 被 canned 字符串无条件覆盖，审计里无法区分模型决定与代码决定 |
| `src/ecommerce_agent/graph.py:696-701` | `is_business_action_request` 正则把模型的 answer/clarify/observe 强改 handoff；同函数 SOP 分支自己会产出 clarify（`graph.py:746`），双标实锤 |
| `src/ecommerce_agent/graph.py:702-708` | 模型自报 confidence < 阈值即改写 answer/finish 为 handoff |
| `src/ecommerce_agent/graph.py:710-715` | complaint 标签二次强拦（与 :535 冗余） |
| `src/ecommerce_agent/graph.py:730` + `intent_routing.json` | `sop_intent` 配置字段只进 prompt 从未用于 SOP 查找（`sops.resolve_for_session` 用的是模型自由意图串）——死配置 |
| `src/ecommerce_agent/policy.py:31-38` | `HIGH_RISK_ACTION_PATTERNS` 六条正则是「业务动作」的唯一定义，漏检空间（如「我不想要这个订单了」）本身是承载安全的 |

### 1.3 生成与客户话术（绕过生成模型）

| 位置 | 行为 |
|---|---|
| `src/ecommerce_agent/graph.py:47-108` | `catalog_fact_answer`：用 `多少钱/价格/售价/价位` 字面量和属性键名子串判断用户在问什么，直接产出答案 |
| `src/ecommerce_agent/graph.py:1019-1047` | `generate()` 三条旁路：catalog 直答、无证据模板话术、审核知识原文直出 |
| `src/ecommerce_agent/graph.py:1120-1123` | clarify：`asks_for_internal_identifier` 正则命中即丢弃模型草稿整段换模板 |
| `src/ecommerce_agent/graph.py:1130-1163` | handoff 八条客户话术由 `route_reason` 字符串比对选择；投诉话术用字符串拼接把检索证据切 280 字塞进模板；`customer_requested_human` 分支只有 mock 会产生（死分支） |
| `src/ecommerce_agent/service.py:454-510` | **缺陷**：`_generation_deltas` 手抄 `graph.generate` 三条旁路且已漂移——无证据分支话术与非流式不同、`build_messages` 漏传 `prompt_variant`。同一消息走 `/chat` 与 `/chat/stream` 行为不同 |

### 1.4 信号放大链路（标签直通运营后果）

| 位置 | 行为 |
|---|---|
| `src/ecommerce_agent/rag.py:289` | 意图匹配文档 +0.12 加分（入围线 0.08），错标签能把不该入围的文档推过线 |
| `src/ecommerce_agent/graph.py:1198-1199` | complaint 标签直接设 `priority_flag="complaint"` |
| `src/ecommerce_agent/graph.py:1200-1214` | 分类器弃权（`intent_method=="default"`）即产生 `priority="high"` 人工任务 |
| `src/ecommerce_agent/handoff.py:45-59,1348-1367` | complaint 标签直通 5 分钟首响 SLA 的投诉队列——「曝光」二字出现在消息里就能钉死人工 SLA |
| `src/ecommerce_agent/handoff.py:392-396` | `risk_level`（部分来自 :696 的正则）传导进任务优先级 |
| `src/ecommerce_agent/product_advisor.py:11-12,107-144` | 比较意图（7 个字面量正则）与指代消解（7 个代词 + 词重叠计数）决定上下文里有什么，假阳性会让 `catalog_fact_answer` 全程无模型地答错商品 |
| `src/ecommerce_agent/prompts.py:121-133,188-192` | 把关键词标签当事实注入 prompt（`当前客服回复变体：…`），无「可以不采纳」的指示 |

### 1.5 评测与知识门禁层（关键词冒充裁判）

| 位置 | 行为 |
|---|---|
| `src/ecommerce_agent/evaluation.py:250-283,1270-1321` | 「算不算拒绝」由 14 个子串决定；幻觉检测要求答案数字/承诺词逐字出现在原文——正确转述被判幻觉，套用原文词汇的错误结论能过；结论门禁发布审批 |
| `src/ecommerce_agent/releases.py:830-835` | `forbidden_answer_terms` 子串命中即 severe violation，可自动阻断发布 |
| `src/ecommerce_agent/evolution.py:137-142` | 候选知识若命中业务动作正则，其答案必须含 `人工/核对/确认` 等子串才可入库 |
| `src/ecommerce_agent/evals.py:24-30,74-77` | `SAFETY_CASES` 用 5 条硬编码消息测 `is_business_action_request` 自己——正则自证，计入 `baseline_regression` 门禁 |

### 1.6 Mock 与锁定现状的测试

| 位置 | 行为 |
|---|---|
| `src/ecommerce_agent/llm.py:422-468` | mock 意图分类器：第二套关键词表，部分镜像 `intent.py:47-51` |
| `src/ecommerce_agent/llm.py:469-607` | mock 规划器：大段关键词 if/elif，且调用 `is_business_action_request`——与生产 gate 逻辑耦合 |
| `src/ecommerce_agent/llm.py:609-636` | mock 生成器也按 `多少钱/价格` 关键词拼答案 |
| `tests/conftest.py:22-23` | 全部 64 个测试文件默认 `model_mock_mode=True`：大量「模型驱动路由」测试实际验证的是关键词对关键词 |
| `tests/test_intent_routing.py:25-27,429,438` | 断言规则命中路径「不得调用模型」——锁定旁路 |
| `tests/test_intent_routing_integration.py:386-393` | 断言商品快速路径「不得进入工具循环」——锁定旁路 |
| `tests/test_m4_acceptance.py:680-686,717-740` | 对照组（诚实的证据）：无模型端到端准确率 27.5% 的 xfail 记录、规则层覆盖率 ≤0.30 / 精确率 ≥0.75 的记录性断言 |

### 1.7 已核对干净的部分（该硬的硬对了）

- `policy.py:117-130` 注入/越权拒绝
- `context_builder.py:126-151,194-198,320-329` 身份冲突、越权字段、未验证工具结果的 readiness 判定
- `handoff_dispatch.py` / `handoff_staffing.py` 派单与排班（纯 SQL 排序，无正文检查）
- `handoff.py:1304-1319` 订单号提取（词边界 + 恰一匹配，防御性正确）
- `sops.py`（intent 是存储列，SQL 等值匹配）、`quality.py`（只看元数据）、`text_utils.py`（脱敏）
- `graph.py:716-722` 连续低质量熔断（会话级护栏，按历史标签不按正文）

---

## 二、可演进性类

### 2.1 确认的真实缺陷（立即修复级）

1. **`_validate_schema` 重复键静默吞掉 v25 校验**：`src/ecommerce_agent/database.py:2247`
   与 `:2430` 两次定义 `"release_policies"`，Python dict 后者覆盖前者，
   `night_window_start_utc` 等五列从未被校验。正是 CONTRIBUTING 第 9 节警告的
   「同名字典键静默覆盖」，且发生在 v25 事故同一张表上。
2. **流式/非流式生成分支漂移**：`src/ecommerce_agent/service.py:454-510`
   （见 1.3，同时属于两类问题）。
3. **意图复核路径无模型时丢标签**：`src/ecommerce_agent/intent.py:257-258`（见 1.1）。

### 2.2 全局状态全等断言（别人的正常新增即挂）

| 位置 | 断言内容 |
|---|---|
| `tests/test_virtual_store_simulation.py:35-41,46,49-52,81-82,126-139,151,171-181,200,204` | 场景总数==18、available 模块==10、fixture 各表精确条数、回放计数全等——加任何一条数据/一个模块/一个场景即挂 |
| `src/ecommerce_agent/simulation.py:283-305,237-245` | 代码侧门禁：available 模块无场景即整体 failed（门禁本身是决策 D-030，但 `scenario_ids` 映射是手抄清单） |
| `tests/test_intent_routing_integration.py:14-70,488-495` | LangGraph 拓扑 20 节点/35 边全等快照；测试名说「没有 D15 节点」，实际断言「拓扑一字节不许变」 |
| `tests/test_intent_routing_integration.py:90-97` | 意图路由 key 集合严格全等，加第 4 个路由字段即挂 |
| `tests/test_customer_service_eval.py:372-376,400-401` | 评测场景计数全等 + 两个 fixture 双向全等耦合（虚拟店加一个 SKU 必须同时加一条评测用例） |
| `tests/test_m4_acceptance.py:559-560`、`tests/test_pressure_report_page.py:25-26`、`tests/test_ops_assistant.py:224,418,445,642`、`tests/test_marketing_finance_api.py:128`、`tests/test_handoff_dispatch.py:208,237`、`tests/test_disaster_recovery.py:344`、`tests/test_taobao.py:344-348`、`tests/test_chat_stream.py:165-169` | 各类精确总数/精确序列断言 |
| `tests/test_migrations.py:149` | `assert db.schema_version() == 27`——违反 CONTRIBUTING 第 9 节自家规范，v28 合入即挂 |
| `tests/test_admin_console.py:146-254` | 109 处 `in page.text`，含 CSS/JS 源码字面量断言——把实现细节当契约 |

### 2.3 同一事实手抄多处（改一处漏一处不报错）

| 事实 | 定义点 | 失败方式 |
|---|---|---|
| 迁移列 ↔ `_validate_schema` required 清单 | `database.py` 各 `_apply_vNN` vs `:2217-2603` | 历史已漏 3 次：`knowledge` 漏 4 列（:2237）、`api_clients`/`audit_log`/`evolution_*`/`feedback` 5 张表整体不在清单 |
| 优先级枚举 `low/normal/high/urgent` | SQLite CHECK ×3（`database.py:1554,1617,1865`）、Pydantic Literal ×4（`schemas.py`）、`handoff.py:41`、SQL `ORDER BY CASE` ×3（`handoff.py:527`、`handoff_dispatch.py:215,500`） | 前 8 处漏改会报错；后 3 处漏改新档位**静默排最后** |
| 意图清单 | `intent.py:13-18`（Literal）、`:163-165`（frozenset）、`intent_routing.json`、测试再抄一遍 | 加载器严格集合相等且 import 期执行：漏改 JSON = 整包 import 失败 |
| 模块工具登记 | `business/registry.py` `agent_tools` vs `business/service.py:238-330` 实际注册 | 零交叉校验，写错名字无任何报错 |
| `knowledge_count >= 100` 门槛 | `service.py:800`、`:872`、`tests/test_api.py:20`（写的是 150）、`docs/operations.md:8,17`、`README.md:29`（156 条） | 三个不同的数并存 |
| 订单上下文字段 | `policy.py:81-94` vs `context_builder.py:87-93` | 加字段只改一处：能进 context 但不受限，不报错 |
| `Settings` 必填字段 | `config.py`（90 个）vs `tests/conftest.py:10-47`（手抄 36 个） | 加无默认字段 64 个测试文件同挂；加有默认字段则 `from_env` 永无测试覆盖 |
| 环境变量清单 | `config.py` `from_env` vs `.env.example` | 已落后 24 个变量，且 `.env.example` 有 2 个 config 里不存在的死变量 |
| 版本号 | `pyproject.toml:7` vs `src/ecommerce_agent/__init__.py:3` | 手工同步 |
| 阈值 | `evals/intent/run_m4_intent_precision.py:162` vs `:169` | 判定用一份、报告写另一份——改漏即静默证据失真 |
| 虚拟店铺 D 编号 | fixture JSON、`simulation.py:105-232` 逐条硬编码、`:283-294` 映射、`docs/admin-console.html:836` | 四处缺一处失败方式各不相同 |
| CONTEXT_VERSION | `context_builder.py:86` 写入 8 处 | **全仓无任何读回校验/拒绝路径**——只写不读的版本号是假保险 |
| 灾备 schema 匹配 | `disaster_recovery.py:250-252,617` 用 `!=` 精确比对 | 每次迁移作废全部历史备份（v14→v15 已踩过一次，见 `docs/TEST_REPORT_0.15.0.md:183`） |

### 2.4 「断言别人的东西不存在」

- `tests/test_intent_routing.py:381`：`assert not hasattr(intent_module, "_RULE_REVIEW_CONTEXTS")`
- `tests/test_intent_routing.py:382-386`：`_RULE_BUSINESS_EVIDENCE` key 集合全等——补一个业务证据词即挂
- `tests/test_admin_console.py:235-236`：断言某段 JS 源码不存在
- （历史案例）`assert 26 not in migrations`，CONTRIBUTING 第 9 节已记录

### 2.5 现行文档腐烂清单（非 archive）

| 文档 | 写死内容 | 现实 |
|---|---|---|
| `docs/architecture.md:100,102,134,142` | schema v21 | v27 |
| `docs/architecture.md:136` | 13 场景 / 7 个 available 模块 / marketing、finance 按 planned | 18 / 10 / 两者已 available |
| `docs/operations.md:17` | `/health 必须显示 schema v21` | 照做会判所有实例不健康 |
| `docs/handoff-dispatch-runbook.md:21` | schema 22 | 同上 |
| `README.md:50` | 13 需求 / 7 模块 | 18 / 10 |
| `docs/architecture-inspector.html:262,286` | schema v18 | v27 |
| `docs/admin-console.html:580,1906` | 「13 个验收场景」面板标题与成功 toast | 18 |
| `docs/works/01…11/README.md` + `docs/works/index.md:69,75` | 「302 passed」写死在 13 处 | 当前 468 个测试 |
| `.project-to-act/PROJECT_VERSIONS.md:15,45` | 「20 节点/35 边不变」「15→16 项」 | 与测试常量互为副本；现为 18 项 |
| `docs/ROADMAP_RESET_20260807.md:94,98-102` | 当前 v27 + v28/29/30 预留表 | 与 CONTRIBUTING 占号表是两份需人工对齐的同一张表 |
| `docs/tasks/M5R_*.md`、`M6R_*.md` | 各自写死 v28/v29 及前置版本共 11 处 | 插号即全部重编 |

### 2.6 脚本与配置

- `scripts/start-glm-coding-test.sh:8`：`runtime-0.22.1` 数据目录，当前版本 0.30.0——迁移会静默作用在旧目录
- `scripts/start-glm-coding-test.sh:15` / `.ps1:11` / `config.py` 默认值：三处模型名不一致（`glm-4.7` vs `glm-4.7-flash`）
- `scripts/start-glm-coding-test.sh:42` 端口 8104 vs README 全文 8080
- 无 CI（`.github/workflows/` 不存在）：以上所有断言只在有人手工跑全量时暴露，而 CONTRIBUTING 明确说开发中只跑定向测试

---

## 三、修复优先级建议

**P0（真实缺陷，独立可修）**
1. `_validate_schema` 重复键（2.1-1）
2. `service.py` 流式分支漂移——修法应是消灭手抄：流式与 graph 共用同一份分支函数（2.1-2）
3. `intent.py:257` 无模型时保留规则标签而非降级 chitchat（2.1-3）

**P1（低风险机械改造）**
4. `database.py` initialize() if 链改循环；迁移测试抽 `build_db_at_version` helper；`test_migrations.py:149` 改成员断言
5. 全等断言改下界/成员断言（2.2 清单）；拓扑快照改为「节点名不含业务意图词」的不变量断言
6. 文档腐烂清单批量修正（2.5），改法是删数字或指向权威来源，不是更新数字
7. CI 落地 + ruff F811（防同名覆盖）

**P2（行为变更，需产品决策先行）**
8. 路由边界重划（第一部分全部条目）：先定投诉政策（强制转人工还是提优先级）、
   快速路径资格（是否收缩到已审核+完全匹配）、业务动作正则的角色（改 signal），
   再配真实模型意图基准跑基线，然后动代码。锁定现状的测试（1.6）同步改语义。
9. 灾备 manifest 兼容策略（2.3 末行）：要么迁移时强制全量新备份，要么 manifest
   记录 schema 范围并实现升级路径。
