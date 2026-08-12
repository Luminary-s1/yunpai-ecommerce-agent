# M6-R WP5 Forecast Eval 与完整对抗评审

日期：2026-08-13
开发分支（历史，合入后已删除）：`codex/m6r-wp5-forecast-eval`
起点：`67222d7cf3493fb4565ef14140dac13b10d57bd2`
整链代码合入：`4065b12dd5178ce7239d27b27d71614c8bee77cc` 以 `--ff-only` 前进到
`03d3b85ed104005fd9a537a6685e43f67865ad59`

## 开工与父链证据

- `git fetch origin` 成功。
- 开工时本地 `codex/m6r-wp4-api-agent-admin`、远端同名分支与 `HEAD` 均为
  `67222d7cf3493fb4565ef14140dac13b10d57bd2`，工作树干净。
- `67222d7` 的父提交是 WP4 代码验收 tip `0c283de`；WP1–WP4 均在祖先链。
- 从该精确提交创建本分支，没有从 `main` 丢失未合入的 WP4。
- 开工前 `project-to-act --check` 返回 managed schema v1。

## Evidence-first 红态

### R-001 冷启动 champion 越出固定候选集

先新增反例：policy 候选只有 `last_value` 与 `ewma`，冷启动 champion 必须属于该集合，
且固定 baseline 应为 `last_value`。在修改生产代码前运行：

```text
$ NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
  ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 \
  HTTPS_PROXY=http://127.0.0.1:9 \
  .venv/bin/python -m pytest -q \
  tests/test_forecasting_engine.py::test_cold_start_champion_is_selected_from_the_fixed_candidate_set
F                                                                        [100%]
E       AssertionError: assert 'rolling_mean' in ('last_value', 'ewma')
1 failed in 0.05s
```

失败证明旧实现把 `rolling_mean` 写死为 cold-start champion，绕过了生产 policy 的固定候选集。

### R-002 WP5 runner 尚不存在

在新增 runner 前先加入三条 Eval 契约测试，分别锁定完整数值/结构化门禁、ground truth
污染拒绝，以及独立 oracle 能拒绝错误期望：

```text
$ NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
  ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 \
  HTTPS_PROXY=http://127.0.0.1:9 \
  .venv/bin/python -m pytest -q tests/test_forecasting_eval.py
E   ModuleNotFoundError: No module named 'scripts.run_forecast_eval'
1 error in 0.06s
```

该失败发生在测试收集阶段，证明测试先于 WP5 runner 实现存在。

### R-003 直接 CLI 入口失败

测试导入通过后，第一次按真实脚本入口执行 runner 得到：

```text
$ .venv/bin/python scripts/run_forecast_eval.py \
  evals/forecasting/forecast_eval_v1.json /tmp/.../forecast-eval.sqlite3
ModuleNotFoundError: No module named 'scripts'
exit 1
```

原因是 namespace package 导入只在 `python -m`/pytest 路径成立。修复只区分脚本与包入口，
两者复用同一个 runtime；新增 subprocess 回归后，原始直接命令退出 `0` 并输出
`"passed": true`。

## 开发者验证

### 实现边界

- 十类 synthetic observation 由 `series_spec` 生成，并通过真实 `ForecastRunService`；
  库存场景继续通过真实 `InventoryPlanningService`，不是 Eval 内重写预测或库存公式。
- `_run_scenario` 只接收 `scenario_input`。全部生产调用完成后，runner 才读取 `oracle`
  并评分；报告审计 observation 字段、实际 service/engine/reader 字段、policy 字段和输入
  digest。
- 数值 Gate 的唯一来源是 fixture `numeric_gates`：rolling origin 至少 1 个，P80/P95
  上界覆盖率至少 0.65/0.80，无方向 bias 绝对值至多 0.05，方向 bias 幅度至少 0.05。
- cold-start 固定 baseline 从生产 policy 候选内选择；正常 champion 排名与 2% baseline
  fallback 规则未复制到 Eval。
- 未新增依赖、迁移、API、LangGraph/intent/prompt、关键词路由、采购/付款或库存写入。

### Eval 报告摘要

真实 CLI runner 退出 `0`，总 Gate 为 `passed=true`：

- 十类场景均通过；champion rolling origins 为 1–4 个，全部训练截止早于测试起点。
- 上涨序列 Bias `-0.061068702`，下降序列 `+0.08988764`，平稳/周季节/缺货/
  缺失/冷启动为 `0`；全零序列 WAPE/Bias 明确不可比。
- P80 实测覆盖率最低 `0.8214285714`，P95 最低 `0.9285714286`，且各场景
  `P95 >= P80`；可比序列同时经过各自 WAPE 数值上限。
- 平稳库存场景的 13 项独立数值/结构化检查全部通过：多仓聚合 `on_hand=18`、
  `reserved=4`、`available=14`、`inbound=5`、`future_supply=19`，P80 lead/review
  demand 为 `20/40`，reorder/target 为 `23/43`，MOQ/倍数后建议量 `24`，
  `demand_copy_count=1` 且 `action_mode=advisory_only`。
- boundary 审计记录 32 次真实生产调用，oracle overlap 与 unexpected production field
  均为空；把 `expected_type_code` 注入 observation 后报告稳定变为 failed。

### 真实 mutation（均已还原）

| Mutation | 临时破坏 | 红态 | 还原后 |
|---|---|---|---|
| M-001 未来泄漏 | backtest 从 `values[:origin]` 改为完整 `values` | WP2 + WP5 `2 failed` | 同两项 `2 passed` |
| M-002 baseline fallback | 未达 2% 仍强选 challenger | Engine + WP5 `2 failed` | 同两项 `2 passed` |
| M-003 oracle 污染 | 生产调用前混入 `expected_type_code` | WP5 `1 failed`；boundary overlap 精确命中该字段 | `1 passed` |
| M-004 库存公式 | `available` 忽略 reserved、直接取 on-hand | WP3 + WP5 `2 failed` | 同两项 `2 passed` |

### 开发者命令结果

全部 Python 命令均使用任务指定的断网代理环境。

```text
Forecast Engine + Eval：14 passed in 0.26s
Engine + Eval + Inventory：35 passed in 1.85s
WP1–WP5 forecasting 聚焦矩阵：58 passed in 10.85s
全量 pytest：727 passed, 1 xfailed in 249.33s
直接 CLI Eval：exit 0，passed=true
python -m compileall -q src scripts/run_forecast_eval.py scripts/forecast_eval_runtime.py：exit 0
git diff --check：exit 0
project-to-act --validate：valid=true，issues=[]
```

开发者结论（历史）：WP5 本机代码级候选通过；当时尚不能据此声明 M6-R 可进入合入评审。

## Grok 独立完整评审（同一长生命周期会话）

完整逐轮提示、Grok 原文、独立命令、probe、mutation、修复与最终裁决见
[`GROK_INDEPENDENT_REVIEW.md`](GROK_INDEPENDENT_REVIEW.md)。整个评审只使用会话
`019ff6bf-f868-7520-bcbd-302682b4adad`，首次审阅、修复复验和整链合入裁决均在同一 PTY 中继续。

- 首轮独立全量 `727 passed, 1 xfailed`，五项 mutation 均先失败并精确还原；发现计划脏
  JSON 两个 GET 500、forecast policy 同戳依赖扫描顺序、零宽区间覆盖假阳性三项 P2。
- 修复提交 `d3b8e57` 先补三项失败测试，再统一计划证据类型化 409、显式 `rowid DESC`
  和零宽 sharpness 反证；扩展回归 `54 passed`。
- 二轮 Grok 将四个生产文件退回旧实现，独立得到 `3 failed`；还原后以错误结构 JSON、
  双索引扫描、零宽双向 probe 和新 sharpness mutation 复验，最终全量
  `730 passed, 1 xfailed`（296.80 秒）。
- 第二轮裁决：**PASS**。M6-R 是代码级本机候选，可与尚未合入 main 的 WP4 整链进入合入评审。
- 第三轮整链审阅由持久记录确认 `grok-4.6-build` / `xhigh`，独立全量
  `730 passed, 1 xfailed`（255.95 秒），并新增“已有 sqlite 拒绝覆盖”和同戳 rowid mutation；
  最终对精确 candidate/main SHA 给出 `APPROVE MERGE`。

## 最终开发者收口验证

Grok 最终裁决后，Codex 在已提交治理 tip `4d01185` 上单独运行任务指定的断网代理全量命令，
得到 `730 passed, 1 xfailed in 355.14s`。direct Eval runner 同时得到 10/10、32 次生产调用、
`ground_truth_boundary=passed`、oracle overlap 为空；compileall、`git diff --check` 与
`project-to-act --validate` 均退出 0。该结果记为 E-20260813-003，不替代或混入 Grok 的
E-20260813-002。

## 整链合入结果

- Codex 在批准后再次 fetch 并核对精确 SHA、父链和干净状态，以 `--ff-only` 将 WP4–WP5
  整链合入 `main`，随后用普通 fast-forward 推送 `origin/main=03d3b85`。
- 合入后的 Codex 独立验证为 `730 passed, 1 xfailed in 300.74s`；Eval 10/10、32 次生产调用、
  oracle overlap 为空；compileall、whitespace 与 project-to-act validate 均通过。
- 删除前逐条证明分支 tip 是 `origin/main` 祖先；本地/远端 WP3、WP4、WP5 明确命名工作包
  分支已清理，仅本地的 WP3 `-2` 也用安全 `-d` 删除。混合用途分支未删除。
- 代码合入不豁免服务器 schema v30、真实数据、灾备实操、24/72h 长稳或生产放行。证据见
  E-20260813-004。
