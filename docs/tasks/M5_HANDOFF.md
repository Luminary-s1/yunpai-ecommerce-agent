# M5 运营辅助与文案生成模块 — 交接说明

面向谢良璇，承接**工作包 5「运营分析与报告生成开发」**。读完这一份加根目录的
`CONTRIBUTING.md` 就能开工。

---

## 1. 分工

| 承接人 | 工作包 | 方向 |
|---|---|---|
| 谢良璇 | 运营分析与报告生成开发 | 报告层 |
| 闫睿涵（模块负责人） | 运营数据接口与解析模块开发 | 数据层 |
| 闫睿涵（模块负责人） | 运营数据解析与上传服务开发 | 数据层 |
| 闫睿涵（模块负责人） | 营销文案生成引擎开发 | 文案层 |
| 缪海南 | 运营内容与数据服务验收测试 | 验收测试 |

按模块任务书要求，**开发与测试必须由不同成员承担**。缪海南在 M5 不承接任何开发包
（他的开发工作在 M6），所以由他做全模块的验收测试是合规的。你不用自测交付，
但每个 PR 仍需自带定向测试（见第 6 节）。

**你只需要关心工作包 5。** 其余三个开发包由模块负责人做，你不用等他们，
也不会被他们阻塞——报告层消费的是已经入库的运营数据，不依赖新的解析适配器。

### 推进顺序

六项内容，24 小时，按 2 小时/天算 12 个工作日。**按下面的顺序做，不要跳**：

| 顺序 | 内容 | 工时 |
|---:|---|---:|
| 1 | 环比计算 | 4 |
| 2 | 同比计算 | 4 |
| 3 | 异常检测阈值配置文件 | 4 |
| 4 | 异常预警独立章节 | 4 |
| 5 | 优化建议绑定具体数值 | 4 |
| 6 | 报告导出为文本 | 4 |

两处顺序是有依赖的，不是随便排的：

- **第 4 项要排在第 1、3 项之后**——异常预警章节既要消费环比的结果
  （销量环比下降 > 50% 这类规则），也要读第 3 项抽出来的阈值配置。
  这两个的输出结构没定下来，预警章节写了也要返工
- **第 6 项必须放最后**——导出要把前面新增的章节全都包进去，早做就得重做

第 1、2 项的输出结构定稿后，**在群里同步一次**再往下走。

---

## 2. 这个模块不是从零开始

M5 主体实现已经在 `main` 上（功能台账 F-311，
`src/ecommerce_agent/business/ops_assistant.py`，822 行）。下面这些**已经有了，
不要重复造**：

- CSV / JSON 上传解析、表单录入、按日期与渠道查询三条链路
- 报告生成 API `POST /v1/ops-assistant/reports/analysis`，固定结构
  （数据概览 → 趋势 → 建议）
- 总量、趋势、渠道表现、ROI 的**代码化计算**（`_totals` / `_trends` /
  `_channel_breakdown`）
- 规则化建议码（`sales_declining`、`spend_up_sales_flat`、
  `conversion_declining`、`roi_below_break_even`、`channel_conversion_low`）
- 模型只做文字解读（`_model_narrative`），模型不可用时降级为确定性摘要
- 报告读取不复用列表接口的 500 条上限，大数据集不被静默截断
- 按租户、数据集、日期、渠道幂等写入并版本化

**动手前先读 `docs/tasks/PROGRESS.md` 的 M5 一节**，勾上的是已实现，
没勾的才是你要写的。

### 你要补的六项

工时合计 24 小时，按 2 小时/天算 12 个工作日。

#### 2.1 环比计算（本周 vs 上周）— 4h

**现状**：`_trends`（`ops_assistant.py:588`）算的是"前半段 vs 后半段"——
`_split_halves`（`:576`）把日期区间对半切。这不是环比。

**要做**：按自然周分组（周一为周首），本周对上周，输出各指标变化率。

- 跨月边界按日期分组，不要按月切
- 数据不足两个完整周时，明确标注"不可比"并说明原因，**不能返回 0，也不能抛异常**
- **新增输出键，不要改 `trends` 现有结构**——既有测试依赖 `first_half` /
  `second_half` / `change_pct` 这几个字段

#### 2.2 同比计算（本月 vs 上月）— 4h

同上，按自然月分组。额外注意：两个月的数据天数往往不等，直接比总量会得出错误
结论。输出里要带可比性标记（比如 `comparable: false` + 原因），或者按日均归一
后再比——选哪种都行，但要在提交信息里说明选择理由。

#### 2.3 异常检测阈值抽到配置文件 — 4h

**现状**：阈值散在代码里硬编码——

- `_trends:606` 的 `Decimal("0.05")`（判定 flat 的死区）
- `_findings:714` 的 `roi < 1`
- `_findings:727` 的"渠道转化率 < 整体一半"
- `_findings:739` 的 `len(rows) < 4`

**要做**：抽到 JSON 配置文件，路径可用环境变量覆盖，**内置默认值兜底**。
至少要覆盖任务书点名的两条：转化率下降 > 30%、销量环比下降 > 50%。

- 配置文件读取参考 `cli.py:253` 的 `read_text` + `json.loads`
- 环境变量读取参考 `config.py` 里 `Settings.load` 的 `os.getenv` 写法
- **配置文件缺失或损坏时用默认值继续，并在报告的 `data_quality` 里标注**，
  不能让报告接口崩掉

**反证**：改一个阈值，对应预警的触发行为必须跟着变。

#### 2.4 异常预警独立章节 — 4h

**现状**：预警混在 `findings` 里，靠 `severity` 字段区分。

**要做**：报告结构补一个独立的 `alerts` 键，顺序变成
数据概览 → 趋势分析 → **异常预警** → 优化建议。每条预警要带：规则码、
触发的阈值、实际值、严重度。

- `findings` 保留不动（既有测试依赖），`alerts` 是新增的视图
- 正常数据下 `alerts` 返回空数组，**不能缺键**

#### 2.5 优化建议绑定具体数值 — 4h

**现状**：一半建议已经带数值了（`_findings:719` 的 ROI、`:733` 的渠道转化率），
另一半是空泛的——`:683` 的"建议排查价格、评价与竞品动作"、`:701` 的
"建议人工复核投放计划与素材"，都没说是多少。

**要做**：每条 `recommendation` 必须引用具体数值。目标形态是
「本周转化率 2.3%，较上周下降 35%，建议检查详情页主图是否更换」。

加一条测试断言：所有 `findings` 的 `recommendation` 至少包含一个数字。

**边界**：数值只能取自代码算出来的 `totals` / `trends`，
**不能让模型生成数字，也不能用模型输出回填统计值**。

#### 2.6 报告导出为文本 — 4h

新增导出端点，参考 `ops_assistant_api.py:119` 的 `/reports/analysis` 写法。

- 纯文本即可，本阶段不要求 PDF
- 固定顺序：数据概览 → 趋势分析 → 异常预警 → 优化建议
- **导出内容必须来自 `analysis_report` 的返回结构，不能在导出里二次计算**——
  两处各算一遍，口径迟早会漂

---

## 3. 三条硬约束，踩了要返工

### 代码算数，模型只解读

这是 M5 的核心定位。环比、同比、阈值判定、预警触发**全部在 Python 里算**，
`_model_narrative`（`:789`）只拿算好的结果做文字解读。

**为什么**：模型算出来的数字不可复现、不可审计，而运营报告是要拿去做经营决策的。
一旦模型能改统计值，整份报告的可信度就没了。

**具体的意思**：任何时候都不能把模型的输出写回 `totals` / `trends` / `alerts`。
模型的产出只能落在 `narrative` 字段里。

### 报告不执行动作

`analysis_report` 返回值里的 `action_boundary`（`:384`）那条边界不能删。
报告只输出数据解读和建议，**不执行预算、价格、库存或发布操作**。

### D-033 虚拟与真实数据分离

测试数据必须打显式虚拟标记，不能混进真实运营数据集，两者不混算。

### 其他边界

- **只新增返回键，不改既有结构**——现有测试依赖 `trends` / `findings` /
  `summary` 的字段名，改了会连带弄挂一批不属于你的测试
- 不新增第三方依赖，需要就先提出来讨论
- 本包理论上不需要动数据库表。如果你发现需要加表或加列，**先在群里说一声占号**
  （原因见第 4 节）

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
git checkout -b feature/m5-report-comparison upstream/main
```

M5 主体代码已经在 `upstream/main` 上，从这里开分支就能拿到，不用等谁合并。

分支命名 `feature/m5-xxx`，PR 的 base 选 `a1024053774` 的 `main`。
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

全量约 9 分钟，**从 `upstream/main` 开分支时用例数是 352**
（`pytest --collect-only -q` 可确认）。
开发时跑 `tests/test_ops_assistant.py` 即可，提 PR 前跑一次全量。

数字对不上先看你在哪条分支：未合并的功能分支会带自己的新测试，
数量比 main 多。以你分支的起点数为准，PR 里说明净增了多少条。

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

针对工作包 5，至少这三处要做反证：

- 调整阈值配置文件后，预警触发行为必须相应变化
- 移除环比计算后，环比数值断言必须失败
- 移除"建议必须含具体数值"的绑定后，那条数字断言必须失败

另外：用例数增长要能归因（新增了哪几个测试、各多少条）；环比同比这类数值结论
**要自己拿计算器算一遍核对**，不能只看接口返回码。

---

## 7. 相关文件索引

| 路径 | 内容 |
|---|---|
| `CONTRIBUTING.md` | 开发规范、PR 流程、项目决策、常见的坑 |
| `docs/tasks/M5_WORKBENCH.md` | 五个工作包的完整需求与验收标准，你的是工作包 5 |
| `docs/tasks/PROGRESS.md` | 已实现 / 待实现的复选框清单，先看这个 |
| `src/ecommerce_agent/business/ops_assistant.py` | 本模块主实现，你改的基本都在这里 |
| `src/ecommerce_agent/ops_assistant_api.py` | 路由层，导出端点加在这里 |
| `tests/test_ops_assistant.py` | 既有测试，新测试跟着加 |
| `src/ecommerce_agent/config.py` | 环境变量配置的写法参考 |
| `.project-to-act/PROJECT_FEATURES.md` | 功能台账，F-311 是本模块 |
| `.project-to-act/PROJECT_OVERVIEW.md` | 完整的项目决策记录（D-001 起） |
| `docs/works/12-feature-m5-operations-assistant/README.md` | 交付文档的格式参考 |

---

## 8. 遇到这些情况先问再写

- 需要新增第三方依赖
- 改动会影响 `analysis_report` 既有返回字段的含义或结构
- 需要占用新的 schema 版本号
- 全量测试出现与你的改动无关的既有失败
- 环比 / 同比在数据不足或跨月边界时该怎么表现，你拿不准

改错方向返工的成本远高于问一句。
