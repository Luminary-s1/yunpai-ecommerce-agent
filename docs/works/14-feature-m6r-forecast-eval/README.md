# M6-R WP5 Forecast Eval 与完整对抗评审

日期：2026-08-12
分支：`codex/m6r-wp5-forecast-eval`
起点：`67222d7cf3493fb4565ef14140dac13b10d57bd2`

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

开发者结论：WP5 本机代码级候选通过，完整 M6-R 独立对抗评审仍待下节 Grok 会话；
尚不能据此声明 M6-R 可进入合入评审。

## Grok 独立完整评审（同一长生命周期会话）

待 WP5 开发者门禁全部通过后开始。本节将逐轮保存 Codex 提问、Grok 原文回答、Grok
独立命令与结果、mutation、修复复验和最终裁决；开发者证据与 Grok 证据保持分开。
