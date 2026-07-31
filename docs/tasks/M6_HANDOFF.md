# M6 竞品分析模块 — 交接说明

面向承接本模块开发的两位同学。读完这一份加根目录的 `CONTRIBUTING.md` 就能开工。

---

## 1. 分工

| 承接人 | 工作包 | 方向 |
|---|---|---|
| 缪海南 | 竞品数据采集与结构化管理 | 数据层 |
| 缪海南 | 竞品数据模型定义与结构化管理服务 | 数据层 |
| 胡磊 | 竞品对比分析引擎开发 | 分析层 |
| 胡磊 | 竞品多维对比分析与报告生成引擎 | 分析层 |
| 闫睿涵（模块负责人） | 竞品数据准确性与报告质量测试 | 验收测试 |

按模块任务书要求，**开发与测试必须由不同成员承担**，因此两位不需要自测交付，
验收测试由模块负责人执行。但每个 PR 仍需自带定向测试（见第 6 节）。

### 建议的推进顺序

胡磊的分析引擎要消费缪海南定义的数据结构。建议：

- **第 1 周**：缪海南先做「竞品数据采集与结构化管理」，把 Schema 和字段定下来；
  胡磊同期读 `src/ecommerce_agent/business/competitive.py` 熟悉既有实现
- **数据结构定稿时在群里同步一次**，胡磊确认接口后再动分析层
- **第 2 周起**：两条线并行

---

## 2. 这个模块不是从零开始

竞品模块已有约两千行实现（功能台账 F-304），下面这些**已经有了，不要重复造**：

- 可解释的同款候选评分（GTIN / 品牌 / 型号 / 标题 / 关键属性）
- 版本化人工裁决（approved / rejected）与不可变历史
- 价格、卖点、脱敏聚合口碑信号
- 版本化监控阈值、持久告警、幂等重评、监控 worker
- CRUD API：`/v1/competitive/matches`、`/signals`、`/observations`、
  `/monitors`、`/alerts`、`/overview`、`/analysis`
- 来源版本 + 载荷哈希的去重与冲突拒绝

**动手前先读 `docs/tasks/PROGRESS.md` 的 M6 一节**，勾上的是已实现，
没勾的才是你们要写的。

### 实际要补的部分

**缪海南（数据层）**

- **CSV 批量导入通道**——目前竞品模块内完全没有 CSV 导入，只有逐条 API 录入。
  需要列名映射、数值清洗、字段级错误提示、异常行标记并跳过（不能整批失败）
- **多维组合筛选**——品类、品牌、价格区间、评分范围
- **自定义分析维度扩展**——不同品类需要的对比维度不同，Schema 要能扩展且
  扩展出的维度能参与查询
- **竞品与自有商品的对比基准关联**完善

**胡磊（分析层）**

- **用户评价情感倾向占比**——目前只有 sentiment 计数字段，没有正面/负面/中性
  倾向的分析输出。可用模型推断与关键词规则的混合方案
- **结构化对比报告**——先对比表格（价格、评分、卖点一目了然），
  再文字解读（差异分析 + 建议）
- **市场定位建议**——基于以上对比由模型生成
- **自定义分析维度选择**——用户能勾选只看哪几个维度
- **报告导出**——文本 / Markdown 格式

---

## 3. 两条硬约束，踩了要返工

这两条是项目决策记录里的架构约束，不是风格偏好。

### D-025 竞品人工裁决门禁

竞品数据必须先经过可解释的同款候选评分与人工裁决，**批准之后**其价格、卖点与
脱敏聚合口碑才允许进入分析结论与 Agent 建议。

**为什么**：上游 SKU 对应关系会把不同容量、套装、新旧型号误判成同款。
「500ml 装 vs 1L 装」「单品 vs 三件套」「2023 款 vs 2024 款」如果被当成同款，
价格对比直接得出错误结论，然后错误的经营建议就发出去了，而且不可追溯。

**对分析层的意思**：`/analysis` 的输入只能取已批准 match 绑定的数据。
这个门禁不能绕，也不能"先出结果再补批准"。

### D-014 来源版本与载荷哈希契约

外部事实的写入统一用「来源时间 + 载荷哈希」判定版本：

- 旧版本 → 拒绝
- 同版本同载荷 → 幂等（重复导入不产生重复记录）
- 同版本不同载荷 → 冲突，需人工核对

**对数据层的意思**：CSV 导入要沿用这套语义，**不要另写一套去重逻辑**。
既有实现在 `business/competitive.py` 里，照着用。

### 其他边界

- **不保存评论者身份或原始评论内容**，只保存脱敏聚合口碑
- **不自动改价、不自动调整投放**，分析只输出建议
- 竞品数据来源必须是授权 API、许可供应商、人工录入或显式虚拟样本
- 不新增第三方依赖，需要就先提出来讨论
- 数据库加列用既有的 `_ensure_column` helper，不重建表

---

## 4. 代码提到哪里

```
redmaplewww/yunpai-ecommerce-agent     ← 项目主仓库，不要提到这里
        ↑ 由模块负责人定期同步
a1024053774/yunpai-ecommerce-agent     ← PR 提到这里 ★
        ↑
你自己的 fork                            ← 在这里开发
```

fork `a1024053774/yunpai-ecommerce-agent`（**不是**主仓库），然后：

```bash
git clone https://github.com/<你的用户名>/yunpai-ecommerce-agent.git
cd yunpai-ecommerce-agent
git remote add upstream https://github.com/a1024053774/yunpai-ecommerce-agent.git
git fetch upstream
git checkout -b feature/m6-competitor-import upstream/main
```

分支命名 `feature/m6-xxx`，PR 的 base 选 `a1024053774` 的 `main`。
模块负责人每天合一次。分支落后了 `git rebase upstream/main`。

完整的 PR 描述模板见 `CONTRIBUTING.md` 第 3 节。

### schema 版本号要提前占号

当前 `SCHEMA_VERSION = 25`。如果你的改动需要加表或加列，**先在群里说一声占号**。

这条不是理论风险：此前有两条分支各自把版本推到 25 并各自定义了同名的
`_apply_v25` 方法，git 文本合并完全干净没有冲突提示，但后定义的方法静默覆盖了
前一个，导致一组迁移从未执行，合并后 22 个测试失败。详见 `CONTRIBUTING.md`
第 9 节。

---

## 5. 环境与测试命令

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

跑测试**必须屏蔽代理**，否则模型网关相关测试会挂：

```bash
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 .venv/bin/python -m pytest -q
```

全量约 9 分钟，当前基线 **350 passed**。开发时跑定向测试即可，提 PR 前跑一次全量。

密钥放本机的 `env.md`（已被 `.gitignore` 忽略），不要写进代码、文档或命令历史。

---

## 6. 测试要求：反证

本仓库对验收证据的要求比一般项目严。

**每项新能力都要做反证**：把这个能力临时破坏掉，确认对应测试如期失败，
然后还原并复验。反证过程写进提交信息。

真实例子：

```
Counterexample: temporarily changed the default context_budget_ratio from 0.7
to 0.99. The history truncation assertion failed as expected because the budget
rose from 700 to 900 and kept messages rose from 7 to 9. Restored 0.7 and
verified all four context budget tests pass.
```

这条规矩是为了证明测试真的在测东西，而不是恰好都通过。**没有反证记录的验收
结论不被接受。**

针对 M6，至少这两处要做反证：

- 移除人工裁决门禁后，「未批准数据不进入分析」的断言必须失败
- 移除 CSV 校验规则后，异常行相关的断言必须失败

另外：用例数增长要能归因（新增了哪几个测试文件、各多少条）；数值类结论要人工
核对，不能只看接口返回码；测试数据用显式虚拟标记，不混进真实业务数据集。

---

## 7. 相关文件索引

| 路径 | 内容 |
|---|---|
| `CONTRIBUTING.md` | 开发规范、PR 流程、项目决策、常见的坑 |
| `docs/tasks/M6_WORKBENCH.md` | 五个工作包的完整需求与验收标准 |
| `docs/tasks/PROGRESS.md` | 已实现 / 待实现的复选框清单，先看这个 |
| `.project-to-act/PROJECT_FEATURES.md` | 功能台账，F-304 是竞品模块既有部分 |
| `.project-to-act/PROJECT_OVERVIEW.md` | 完整的项目决策记录（D-001 起） |
| `src/ecommerce_agent/business/competitive.py` | 竞品模块主实现 |
| `src/ecommerce_agent/operations_api.py` | 竞品相关 API 路由 |
| `docs/works/12-feature-m5-operations-assistant/README.md` | 交付文档的格式参考 |

---

## 8. 遇到这些情况先问再写

- 需要新增第三方依赖
- 改动会影响既有 API 的响应契约
- 需要占用新的 schema 版本号
- 全量测试出现与你的改动无关的既有失败
- 拿不准某个改动是否违反 D-014 或 D-025

改错方向返工的成本远高于问一句。
