# 开发指南

面向新加入本仓库的开发者。读完这一份就能开始干活，不用先通读全部文档。

---

## 1. 这个项目是什么

云湃电商一体机 Agent：面向本地一体机的轻量电商经营 Agent。

- **技术栈**：Python 3.11+ / FastAPI / LangGraph / SQLite / Pydantic v2
- **架构原则**：**模型负责理解和建议，代码负责权限、幂等、业务规则和成功判定。**
  模型可以选择做什么，但不能自己放行权限、不能自己判定操作成功
- **业务模块**：商品、订单、仓储、竞品、营销、财务、指标、客服、运营辅助；新路线
  增加 `traffic_lab`（商品流量实验）和 `forecasting`（需求预测与智能补货）
- **当前阶段**：本机候选，生产放行仍阻塞于真实平台权限

代码在 `src/ecommerce_agent/`，测试在 `tests/`，文档在 `docs/`。

---

## 2. 环境搭建

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

确认 `python --version` 是 3.11 或更高。

跑测试（本仓库固定屏蔽代理，否则模型网关的测试会挂）：

```bash
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 .venv/bin/python -m pytest -q
```

全量约 9 分钟。开发过程中跑相关的定向测试就行，提 PR 前跑一次全量。

本地起服务需要一组环境变量，见 `README.md` 的快速启动一节。**密钥放在本机的
`env.md` 里（已被 `.gitignore` 忽略），不要写进代码、文档或命令历史。**

---

## 3. 分支与 PR 流程

这是最重要的一节，先看清楚再动手。

### 仓库关系

```
redmaplewww/yunpai-ecommerce-agent     ← upstream，项目主仓库
        ↑ 定期同步（由模块负责人操作）
a1024053774/yunpai-ecommerce-agent     ← origin，模块负责人的 fork
        ↑ PR 提到这里 ★
你自己的 fork                            ← 你在这里开发
```

### 你要做的

**第一步，fork 并克隆**

fork `a1024053774/yunpai-ecommerce-agent`（**不是** upstream），然后：

```bash
git clone https://github.com/<你的用户名>/yunpai-ecommerce-agent.git
cd yunpai-ecommerce-agent
git remote add upstream https://github.com/a1024053774/yunpai-ecommerce-agent.git
```

注意这里的 `upstream` 对你而言是模块负责人的 fork，不是项目主仓库。

**第二步，开分支**

每个工作包一条分支，从最新的 `upstream/main` 开：

```bash
git fetch upstream
git checkout -b feature/m6-competitor-import upstream/main
```

分支命名：`feature/<模块>-<简短描述>`，例如
`feature/m6-competitor-import`、`fix/m6-csv-encoding`。

**第三步，提 PR**

```bash
git push origin feature/m6-competitor-import
```

然后在 GitHub 上开 PR：

- **base（目标）**：`a1024053774/yunpai-ecommerce-agent` 的 `main` ★
- **compare（来源）**：你 fork 的功能分支

**不要**直接提到 `redmaplewww`。模块负责人合并后会统一同步到主仓库。

### PR 要求

一个 PR 对应一个工作包或一个可独立验收的子任务。太大的 PR review 不动，
太碎的 PR review 成本高。经验值是单个 PR 改动在 500 行以内。

PR 描述里写清楚四件事：

```markdown
## 做了什么
（一两句话）

## 关联工作包
M6 工作包 2 · 竞品数据采集与结构化管理

## 验收对照
- [x] CSV 批量导入可用，异常行有明确报错且不影响其余行入库
- [x] 重复导入幂等，版本冲突可识别
- [ ] 自定义维度可扩展并参与查询（本 PR 未覆盖，见 #xx）

## 测试
定向：`pytest tests/test_competitive.py` → 12 passed
全量：`pytest -q` → 320 passed
反证：临时移除批准门禁后 `test_unapproved_match_excluded` 如期失败，已还原复验
```

### 合并节奏

模块负责人每天合一次。你的分支落后了就 rebase：

```bash
git fetch upstream
git rebase upstream/main
```

---

## 4. 提交规范

英文 conventional commits，跟随仓库既有风格：

```
feat(competitive): add CSV bulk import for competitor records
fix(finance): stop truncating expense amounts
test(competitive): cover column mapping and malformed rows
docs(works): record M6 delivery evidence
```

常用 scope：`competitive` `ops-assistant` `chat` `sessions` `context` `tokens`
`graph` `llm` `api` `admin` `simulation` `works`。

**不要在提交信息里添加任何 AI 署名、`Co-Authored-By` 或生成工具页脚。**

---

## 5. 不可破坏的项目决策

这些是写在 `.project-to-act/PROJECT_OVERVIEW.md` 里的架构决策，不是风格偏好。
改动碰到相关区域时必须遵守，拿不准就先问。

| 编号 | 约束 |
|---|---|
| D-005 | `MODEL_ENABLED=false` 时不得发出任何模型请求 |
| D-007 | 清理与关闭逻辑必须跳过存在非终态人工任务的会话 |
| D-008 | 运行时统一用 GLM 标准 Chat Completions，不引入本地大模型或第三方 tokenizer |
| D-010 | 具体业务意图不写入 LangGraph 拓扑，不新增按意图分支的节点或边 |
| D-014 | 外部事实统一用来源时间 + 载荷哈希版本契约：旧版本拒绝、同版本同载荷幂等、同版本不同载荷冲突 |
| D-023 | 回答必须引用不可变上下文快照，不绕过 `ContextBuilder` |
| D-025 | 竞品事实必须经可解释同款候选评分与人工裁决，批准后才能进入分析与 Agent 建议 |
| D-033 | 评测与模拟产生的会话落 `evaluation` / `simulation` 来源，不污染 `operational` |
| D-034 | 确定性代码不做语义路由：关键词、正则、前置分类只作信号，不替模型决定下一步（见第 10 节） |
| D-035 | 共享事实单一权威源，测试只断言自身增量，不断言全局状态全等（见第 11 节） |

**做 M6 的同学重点看 D-014 和 D-025。** 竞品模块的核心不是把数据存进去，
而是保证进入分析结论的每一条竞品事实都可追溯、可解释、经过人工确认。
不同容量、套装、新旧型号被误判成同款，会直接产出错误的经营建议。

其他硬性边界：

- **不新增第三方依赖。** 确实需要就先提出来讨论，不要自行改 `pyproject.toml`
- **不改动既有 API 的响应契约。** 加字段可以，改名或删字段要先确认
- **数据库加列用既有的 `_ensure_column` helper，不重建表**
- **不保存顾客个人信息、评论者身份或原始评论内容**

---

## 6. 测试要求

本仓库对测试的要求比一般项目严，因为它要能拿出可复核的验收证据。

### 反证门禁

**每项新能力都要做反证：临时把这个能力破坏掉，确认对应的测试如期失败，
然后还原并复验。** 反证过程写进提交信息。

举个真实例子：

```
Counterexample: temporarily changed the default context_budget_ratio from 0.7
to 0.99. The history truncation assertion failed as expected because the budget
rose from 700 to 900 and kept messages rose from 7 to 9. Restored 0.7 and
verified all four context budget tests pass.
```

这条规矩的意义是证明测试真的在测东西，而不是恰好都通过。没有反证记录的
验收结论不被接受。

### 其他要求

- 用例数增长要能归因：新增了几个测试文件、各多少条，说得清楚
- 数值类结论要人工核对，不能只看接口返回码
- 测试数据用显式虚拟标记，不能混进真实业务数据集
- 开发和测试由不同成员承接。**你自己写的功能，验收测试由别人执行**

---

## 7. 代码约定

- 跟随周边代码的风格，不要引入新的格式化偏好
- **注释密度低，只在非显然处写。** 不写装饰性注释，不写复述代码本身的注释
- 类型标注跟随既有文件的做法
- 中文注释和英文注释都可以，与所在文件保持一致
- 提交前跑 `python -m compileall -q src` 和 `git diff --check`

---

## 8. 你的任务在哪看

| 文档 | 内容 |
|---|---|
| `docs/ROADMAP_RESET_20260807.md` | 2026-08-07 路线重置、新旧里程碑边界与实施顺序 |
| `docs/tasks/README.md` | 当前任务文档入口与历史归档索引 |
| `docs/tasks/M5R_TRAFFIC_LAB_WORKBENCH.md` | M5-R 流量实验任务书、数据模型与验收 |
| `docs/tasks/M6R_DEMAND_FORECAST_WORKBENCH.md` | M6-R 需求预测任务书、数据模型与验收 |
| `docs/tasks/archive/` | 已冻结的旧 M5/M6 工作台与交接说明，只保留历史上下文 |
| `docs/tasks/PROGRESS.md` | 进度记录规则；实际状态只在负责人工作台网页维护 |
| `.project-to-act/PROJECT_FEATURES.md` | 功能台账，F-xxx 编号与状态 |
| `docs/works/` | 交付文档；已冻结里程碑的证据归入 `docs/works/archive/` |

旧 M5/M6 自 2026-08-07 起 `FROZEN / SUPERSEDED`，不得继续按其未完成 checklist 派发
开发。新工作先读路线重置，再进入对应 M5-R/M6-R 工作台；设计冻结前不得把新模块登记为
available。旧代码、API、数据库迁移和测试仍需保持兼容。

---

## 9. 常见的坑

**测试挂在网络上**：没加代理屏蔽的环境变量。用第 2 节那条完整命令。

**改了 `simulation.py` 之后虚拟店铺测试失败**：场景总数和模块覆盖数是硬断言，
新增场景要同步更新 `tests/test_virtual_store_simulation.py` 里的计数。

**接入真实模型后客服类场景不稳定**：这是已知现象，历史上两次连续实跑分别是
15 通过 1 失败和 14 通过 2 失败，集中在高风险诉求转人工判定。验收以模型受控的
测试套件断言为准，真实模型实跑结果单独标注。不要为了让它通过就移除场景登记
或放宽门禁。

**注册表登记为 `available` 的模块必须有通过场景**：这是门禁，不能靠把登记
删掉来规避。

**schema 版本号冲突（真实踩过的坑）**：多条分支并行时，先确认当前
`SCHEMA_VERSION`，需要占号先说一声，避免两条分支用同一个号。

这条不是理论风险。曾有两条分支各自把 `SCHEMA_VERSION` 推到 25，并各自在
`Database` 类里定义了一个 `_apply_v25` 方法——一个给 `release_policies` 加列，
一个建 `ops_operation_records`。两个方法在文件里不重叠，**git 文本合并完全干净，
没有任何冲突提示**，但 Python 里后定义的方法静默覆盖前一个，导致其中一组迁移
从未执行，合并后 22 个发布相关测试全挂在
`table release_policies has no column named night_window_start_utc`。

所以：**占号要提前说，合并后必须跑全量**。`grep` 到语句在文件里不等于它会被执行，
同名函数、同名类方法、同名字典键都可能被静默覆盖。

### Schema 版本号占用登记

**要加表或加列，先在这张表里占号，再写迁移。** 占号之后其他人就能自己查，
不用逐个问模块负责人。

| 版本 | 占用者 | 模块 / 分支 | 用途 | 状态 |
|---:|---|---|---|---|
| ≤ 25 | — | 已合并进 `main` | 历史迁移，`_apply_v1` ~ `_apply_v25` | 已合并 |
| 26 | 缪海南 | M6 / 已合并进 `main` | `competitor_observations` 新增 `rating_value`、`rating_scale`、`sales_rank`、`rank_scope` | 已合并 |
| 27 | 闫睿涵 | M4 / 已合并进 `main` | `messages` 新增 `customer_intent`、`intent_confidence`、`intent_method`（D13 意图分类） | 已合并 |
| 28 | M5-R | Traffic Lab / `main` | creative asset、listing revision、metric bucket、experiment/window/analysis run | 已合并（WP1–WP5） |
| **29** | M6-R | Forecasting / `main` | demand fact、forecast policy/run/backtest/point/anomaly | WP1–WP2 已合并；WP5 Eval 留待后续 |
| **30** | M6-R | Inventory Planning / `main` | planning policy、inventory plan | 已合并（WP3） |
| **31** | PR #11 | 统一工作区会话持久化 / `codex/workspace-conversation-history` | `workspace_conversations` / `workspace_messages`（`_apply_v31`） | **占用中，未合并** |
| **32** | M5-R | Traffic Lab / `main` | 店铺业务日历 + Traffic metric 三元身份 | 已合并（F-322 / PR #14） |
| **33** | M3 | 知识库 / `main` | knowledge_key active 唯一索引 + `retrieval_logs` | 已合并（PR #10 / `1906365`） |
| **34** | M7-R | Readonly Data / `codex/m7r-wp1-readonly-contract` | import manifest、field evidence、row isolation issue（WP1） | **占用中，未合并** |
| 35+ | *（空闲）* | | | |

旧 M5 工作包 3 对 v28 的预留已随路线冻结取消；M5-R 的 `_apply_v28` 已合入 `main`。
26–30、32、33 已在 `main`。**31 仍只属于开放中的 PR #11**，`main` 的 `initialize()` 目前是 30→32→33，故意没有 `_apply_v31`。M7-R WP1 已占用 **34**，下一空闲号从 **35** 起。

### 合并 PR #11 前必扫（PR #10 合入后）

扫描词：`PR #11`、`_apply_v31`、`MERGE-GATE PR-11`、`workspace_conversations`、`不得覆盖 v32`、`不得覆盖 v33`。

`main` 已有 `_apply_v32`（F-322）和 `_apply_v33`（M3 knowledge_key 唯一索引 + retrieval_logs）。PR #11 自带另一套 `_apply_v31`（workspace 会话表），且分支里也带了 v32。合 #11 时 `database.py` **必然交叉**。

**禁止**整块接受 ours/theirs，丢掉已在 `main` 的 v32 或 v33。那会重演同名方法静默覆盖事故。

合入时必须同时成立：

1. `SCHEMA_VERSION` 取较大值，结果必须是 **33**（不是 31）。
2. `initialize()` **三块都保留**，按号排序：`if 31`（#11 workspace）+ `if 32`（日历/身份）+ `if 33`（knowledge 唯一索引 + retrieval_logs）。
3. 三个方法都在且语义不混：`_apply_v31` 只建 workspace 表；`_apply_v32` 只日历/身份；`_apply_v33` 只 knowledge 索引 + `retrieval_logs`。
4. 合后自检：
   ```bash
   git grep -n "def _apply_v3[123]" -- src/ecommerce_agent/database.py
   ```
   必须同时打出 31、32、33。缺任一号即合坏。
5. 测试只断言自己的成员/表（`31 in migrations`、workspace 表存在），**不要** `schema_version() == 31`。
6. 已知合成失败不在合 #11 时用 ours 抹掉 #10：workspace 测试里的旧 stream URL、Forecasting 控制台应打 `/admin/advanced`。这两条是页面断言，修好测试即可，不要回退 schema。

**并行占号的分支合并时，`database.py` 必然在三处
冲突**（2026-08-06 实测：26 对 27 就是这三处，已按下面的解法合入）：

1. `SCHEMA_VERSION` 那一行 —— 取两者较大值
2. `initialize()` 里的 `if NN not in applied` 块 —— **两个块都保留**，按版本号排序
3. 两个 `_apply_vNN` 方法 —— **都保留**，方法名不同不会互相覆盖

整块取 ours 或 theirs 就会丢掉一组迁移，那正是下面第 3 条要防的事故。迁移按
`schema_migrations` 的成员判断执行，与先后合并顺序无关。

**迁移测试不要断言全局版本号。** `assert db.schema_version() == 26` 和
`assert 26 not in migrations` 这类写法，把「当前最大版本」或「别人的迁移还没合」
写成了永久不变量，别人一合就挂——2026-08-06 两条测试同时踩了这个。改成断言
自己关心的东西：`assert NN in migrations` 加上自己那几列存在。

占号规则：

1. **认领前先自己查一遍**，别只看 `main`：
   ```bash
   for b in $(git for-each-ref --format='%(refname:short)' refs/heads refs/remotes | grep -v HEAD); do
     git grep -ho "_apply_v[0-9]\+" "$b" -- src/ecommerce_agent/database.py 2>/dev/null | sort -u -V | tail -1
   done | sort -u -V | tail -3
   ```
   未合并的分支也会占号，只看 `main` 就是 v25 那次事故的成因
2. 在上表加一行，连同占号的提交一起提 PR，**在群里说一声**
3. 迁移方法名必须是 `_apply_v<你的号>`，不得与任何分支重名
4. 加列用 `_ensure_column`，**不重建表**；范例见 `database.py` 的 `_apply_v8`
   （给 `competitor_observations` 加 `payload_hash`）
5. 存量行没有新列的值，所以**新列必须可空或带默认值**，不能是无默认的 `NOT NULL`
6. 合并后跑一次全量测试再关掉这一行

### 协调文档的冲突约定

2026-08-05 一次合并撞出 24 个冲突块，**全部在协调文档里，代码零冲突**，其中
23 个是「同一张表被两个人从不同分支改了数字或名字」。所以有下面四条：

**1. 可变状态不进 git。** 工时、剩余、进度百分比、状态、日期、优先级、派发、日报
和周报一律不写进文档，负责人工作台网页是唯一源。`*_WORKBENCH.md` 只保留任务范围、
需求、验收、交付、依赖与负责人。已按此清理，别再加回来。

**2. 文档有主，功能分支不碰别人的。**

| 文档 | 谁能改 |
|---|---|
| `docs/tasks/*WORKBENCH.md` | 闫睿涵 |
| `docs/tasks/PROGRESS.md`、`CONTRIBUTING.md`、一切分工调整 | 闫睿涵，且只在 `main` 上改 |

功能分支只改自己模块的文档和代码。要动别人的文档，在群里说，由对应的人改。

**3. 不在文档里抄分支状态。** 分支和 PR 的状态用 `gh pr list` 看，实时且不会
过期。文档里抄一份必然和现实不一致。

**4. 落后超过 3 个提交就先同步。** 今天那 24 个冲突块，起因是一条分支从
`36a3779` 一路开到 `main` 走完 15 个提交才合。天天同步，冲突永远是小的。

---

## 10. 决策权边界：哪些可以写死，哪些必须留给模型（D-034）

D-001 和 D-010 定了拓扑层面的边界：业务意图不进 LangGraph 节点和边。2026-08-07
的全库审计（`docs/AUDIT_ROUTING_EVOLVABILITY_20260807.md`）发现拓扑守住了，但
**节点内部**大量确定性代码在做同一件被禁止的事：用关键词、正则、前置分类标签替
模型决定路由和话术。本节把 D-010 延伸到节点内部。

**一句话判据：确定性代码回答「这个动作现在能不能安全执行」；模型回答「用户想要
什么、下一步做什么」。前者可以写死，后者不行。**

### 允许写死（安全与可执行性校验）

- 提示注入、越权数据请求的拒绝（`policy.precheck_request`）
- 身份与权限：店铺/SKU 标识冲突、未授权订单字段（`context_builder` readiness）
- 工具 schema、权限、幂等键、后置验证（postcondition）
- 循环上限（`max_react_steps`）、SOP 步骤完整性、shadow 模式写抑制
- 发布门禁、错误预算、连续低质量熔断（按历史元数据，不按正文）

共同特征：触发条件是**可验证的执行事实**（校验和不匹配、字段缺失、步数超限），
不是对用户原文的语义解读。

### 禁止写死（语义路由）——新代码不得新增以下任何一种

1. **用关键词/正则/前置分类标签直接决定 route 或跳过模型。** 包括 deliberate
   前的短路、按分类标签强制 handoff、按分数阈值绕过决策模型。
2. **用正则重新解读用户原文并覆盖模型已做出的决定。** gate 只能因可验证的执行
   失败（postcondition 未过、SOP 缺失、超步数、权限不足、shadow）向更保守方向
   降级，不得基于正文再解读改写模型的 answer/clarify/observe。
3. **绕过生成模型拼接客户话术。** 固定模板话术（转人工提示等）允许，但不得用
   关键词解析用户问句来选内容，不得把检索证据字符串拼接进模板冒充生成。
4. **静默丢弃模型草稿整段换模板。** 草稿校验不过的正确动作是重试、降级或转
   人工，并留下审计记录。
5. **分类标签直通队列、SLA、优先级。** 分类误差不能直接变成 5 分钟人工 SLA。

### 信号化原则

关键词命中、四分类结果、检索分数是 **signal**，合法用途：进 prompt（须说明
「仅供参考」）、进检索加权、进 `risk_level`、进审计日志；模型做出决定**之后**
可以参与优先级计算。非法用途：替模型决定下一步。

### 快速路径资格

绕过模型直接回答，必须**同时**满足：内容已经人工审核（approved / `evolution:`
来源），且与问题完全匹配（normalize 后相等）。「检索分数高」不是资格。

### Mock 与测试纪律

- mock 不得复刻生产路由的关键词逻辑。mock 关键词表与 `intent.py` 关键词表互相
  印证是循环论证，不是验收证据。
- 「模型不得被调用」断言只允许用于：`MODEL_ENABLED=false` 契约（D-005）、注入
  拒绝、身份冲突。语义路径不允许锁「不调模型」。
- 改动路由边界必须跑真实模型意图基准（`evals/intent/`），离线 mock 结果单独
  标注、不充当验收。

### 双通道一致性

`/v1/chat` 与 `/v1/chat/stream` 必须复用同一份决策/生成分支代码。禁止在
`service.py` 手抄 graph 节点逻辑——审计已发现两处漂移（话术不一致、漏传
`prompt_variant`）。

### 存量欠账

审计文档第一部分列了全部存量违例。**它们不是新代码的先例。** 清理属于 P2 行为
变更，动之前先定投诉政策与快速路径范围，并注意有五处测试把现状锁成了回归断言，
要同步改语义。

---

## 11. 可演进性：单一事实源与断言纪律（D-035）

同一次审计的第二部分：schema、枚举、清单、版本号每演进一次都要人工同步多处，
漏改要么静默腐烂要么全线红。已产生 2 个确认缺陷和大面积文档腐烂。规则如下。

### 单一事实源

- 同一事实（枚举、版本号、字段清单、阈值、注册表）只允许**一个权威定义点**，
  其余位置引用它。确实需要第二份（如 SQL CHECK 无法引用 Python 常量）时，必须
  配一条**交叉校验测试**比对两份，或由代码生成。
- 已知反例见审计 2.3：优先级枚举写死 10 处（其中 3 处 SQL `ORDER BY CASE` 漏改
  会静默排错序）、意图清单 4 处、`knowledge_count` 门槛三个不同的数并存。
- **同名覆盖是本仓库反复踩的坑**（v25 双 `_apply_v25`、`_validate_schema` 双
  `"release_policies"` 键）。同名函数、同名方法、同名字典键都会静默覆盖，
  `grep` 到不等于会执行。写迁移和校验清单时先搜一遍同名。

### 断言纪律

第 9 节「迁移测试不要断言全局版本号」推广到一切共享状态：

- **断言自己的增量**：`assert mine in collection`、自己加的列/字段/场景存在。
- **不断言全局计数全等**（`== 18`、`len(...) == 10`）。计数门禁一律用下界或
  成员包含（`>=`、`<=`、`in`）。别人的正常新增不应该挂你的测试。
- **不断言「别人的东西不存在」**（`not hasattr`、`not in`、全等快照）。要守
  不变量就直接断言不变量本身——例如守 D-010 应断言「节点名不含业务意图词」，
  而不是把 20 个节点 35 条边抄成快照全等。
- **实现细节不进断言**：CSS 字面量、JS 源码子串、内部调用序列不是契约。

### 版本化字段要有读侧

写进库的版本标记（如 `context_version`）必须有读回校验、迁移或拒绝路径之一；
三者都没有就删掉这个字段。只写不读的版本号是假保险。

### Schema 演进补充（接第 9 节占号规则）

- 迁移加列时**必须检查 `_validate_schema` 的 required 清单要不要同步**，PR 里
  写明加没加、为什么。历史已漏 3 次。
- 灾备 manifest 用 `!=` 精确比对 schema 版本：**每次迁移都会作废全部历史备份**
  （v14→v15 已踩过）。bump schema 的 PR 必须写明备份策略：升级后立即全量新备份，
  或实现 manifest 兼容。
- `initialize()` 的 if 链与迁移边界测试样板的重构已登记为待办（审计 P1），落地
  前新迁移仍按第 9 节现行写法。

### 文档纪律

- **现行文档不写快照数字。** README、architecture、operations、runbook、后台
  HTML 里不出现「当前 schema vNN」「当前 N 个场景」「N 个模块」——要么删数字，
  要么指向权威来源（`/health`、注册表、本文件占号表）。审计 2.5 列了 11 处已
  腐烂的实例；修复方式是**删数字，不是更新数字**。
- **交付文档里的运行结果是历史证据**，必须带日期与 commit，不作现状陈述。
  「302 passed」写死在 13 处而当前测试有 468 个，就是反面教材。
- 脚本里的版本号、端口、模型名与文档/配置对齐（审计 2.6）。

---

有拿不准的，先问再写。改错方向返工的成本远高于问一句。
