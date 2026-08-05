# 开发指南

面向新加入本仓库的开发者。读完这一份就能开始干活，不用先通读全部文档。

---

## 1. 这个项目是什么

云湃电商一体机 Agent：面向本地一体机的轻量电商经营 Agent。

- **技术栈**：Python 3.11+ / FastAPI / LangGraph / SQLite / Pydantic v2
- **架构原则**：**模型负责理解和建议，代码负责权限、幂等、业务规则和成功判定。**
  模型可以选择做什么，但不能自己放行权限、不能自己判定操作成功
- **业务模块**：商品、订单、仓储、竞品、营销、财务、指标、客服、运营辅助
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
| `docs/tasks/M6_WORKBENCH.md` | M6 模块的工作包拆解、需求、验收标准 |
| `docs/tasks/PROGRESS.md` | 各模块已实现与待实现项的复选框清单 |
| `.project-to-act/PROJECT_FEATURES.md` | 功能台账，F-xxx 编号与状态 |
| `docs/works/` | 历史交付文档，看格式参考 `12-feature-m5-operations-assistant/README.md` |

M6 目前分成 5 个工作包，其中工作包 1 是独立测试（不由开发者自测）。
先读 `docs/tasks/M6_WORKBENCH.md`，再对照 `docs/tasks/PROGRESS.md` 看哪些已经
有了、哪些要新写——竞品模块的实体匹配、人工裁决、监控告警已经存在，
缺的主要是 CSV 批量导入、多维筛选、情感倾向分析和报告导出。

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

### 当前 schema 版本占用登记

| 版本 | 模块 / 分支 | 用途 | 状态 |
|---:|---|---|---|
| ≤ 25 | 已合并历史迁移 | `_apply_v1` ~ `_apply_v25` | 已合并 |
| **26** | M6 / `feature/m6-competitor-import` | `competitor_observations` 的评分与排名字段 | 已分配，未合并 |
| **27** | M4 / `feature/m4-customer-service` | `messages` 意图分类三列 | 已占用，待合并 |

M4 的迁移因此必须实现为 `_apply_v27`；不要在本分支创建 `_apply_v26`，也不要把
v26 当作“当前最大版本”复用。合并 M6 后仍应按迁移记录顺序执行 v26，再执行 v27。

---

有拿不准的，先问再写。改错方向返工的成本远高于问一句。
