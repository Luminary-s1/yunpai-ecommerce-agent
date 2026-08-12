# M6-R WP1–WP5 Grok independent adversarial review

- Date: 2026-08-13
- Branch: `codex/m6r-wp5-forecast-eval`
- Required WP4 ancestor: `67222d7cf3493fb4565ef14140dac13b10d57bd2`
- Long-lived Grok conversation: `019ff6bf-f868-7520-bcbd-302682b4adad`
- Launch command: `grok --no-alt-screen --minimal --always-approve --no-subagents --disable-web-search`
- Network policy: web search disabled; pytest commands used the required offline proxy environment.
- Transcript policy: prompts and final Grok replies are preserved verbatim. Grok internal reasoning and transient terminal rendering are not copied.

## Round 1 — complete WP1–WP5 review

### Codex prompt (verbatim)

~~~text
你是 M6-R（Demand Forecast & Inventory Planning）WP1–WP5 的独立对抗验收人。请在当前仓库 /Users/luckye/Documents/Code/yunpai-ecommerce-agent 做完整、证据驱动的代码审阅和独立验证，不要只复述开发者证据，也不要默认现有测试正确。

当前分支必须是 codex/m6r-wp5-forecast-eval，审阅起点链为已验收但未合入 main 的 WP4：67222d7cf3493fb4565ef14140dac13b10d57bd2；当前候选 tip 应为 1d4fa83。先自行核对 git status、tip、父链与 source/main 关系。WP4 尚未合入 main，不得因为对 main 比较而漏掉 WP4。不要 commit、push、merge、切分支或删除分支；若做临时 mutation，必须在报告前精确还原并证明工作树干净。

必须先读并逐条对照：
1. AGENTS.md；
2. CONTRIBUTING.md 第 5、9、10、11 节；
3. docs/tasks/M6R_DEMAND_FORECAST_WORKBENCH.md 全文，尤其 §9 WP1–WP5、§10、§11；
4. docs/AUDIT_ROUTING_EVOLVABILITY_20260807.md 的评测关键词裁判、全等断言、D-034/D-035；
5. .project-to-act 五本台账，但把它们只当待核验证据，不当真相；
6. WP1–WP5 生产代码、迁移、API、Agent tools、后台、simulation、tests、eval fixture/runner，以及 docs/works/14-feature-m6r-forecast-eval/README.md。

独立验收范围必须覆盖并明确逐项 PASS/FAIL/NOT PROVEN：

WP1：
- demand-v1 单一口径、Asia/Shanghai 归日；
- paid/partially_refunded/refunded 与 canceled 排除；
- 水位、固定回补、取消/状态更正追加不可变版本；
- stockout true/false/unknown；
- 缺日、来源缺失、真零不可混淆；
- tenant/store/SKU 隔离与 lineage/hash/policy 可追溯。

WP2：
- 七模型、六类/全部规定序列；
- 所有候选同一 rolling-origin，训练只见 origin 之前，真实未来泄漏反证；
- champion 必须属于固定候选，challenger 不达阈值必须 baseline fallback；
- 候选失败隔离/全失败类型化失败；
- WAPE/Bias 零分母可比性、sMAPE/RMSE；
- P50<=P80<=P95、区间构造与持久化 backtest 证据；
- 脏 JSON/失败证据/策略重放与冲突。

WP3：
- store+SKU demand 不按仓复制，多仓库存聚合；
- available=max(0,on_hand-reserved)、reservation shortfall、inbound day-0 假设；
- lead/review、选定分位、安全库存、MOQ、order multiple、maximum stock cap 固定顺序；
- 仓级 recommended qty withheld；
- 缺货日期、risk、plan quality、陈旧/混时/畸形输入；
- plan/policy 不可变、tenant FK、相同输入幂等；
- advisory_only，绝无采购/付款/库存事实改写。

WP4：
- 九个或实际完整 API 的 tenant/store/error/审计契约；
- policy exact/store default/warehouse inheritance、同时间戳与原子性；
- 动态工具目录与 get_demand_forecast/get_inventory_plan 两个只读工具；
- 工具只能读取固化证据，读边界应覆盖实际完整 forecasting 表；
- D20 public-service virtual 场景、forecasting available 交叉校验；
- 后台只有显式触发，degraded/失败不静默，前端不二算库存；
- 不新增 forecasting 关键词路由、LangGraph 拓扑或语义 fast path。

WP5：
- evals/forecasting synthetic fixture 与 oracle 是否真正独立；
- 平稳、上涨、下降、7日季节、间歇、大量零、促销峰、缺货截断、数据缺失、冷启动；
- 每个序列确有 rolling-origin；
- production ForecastRunService/InventoryPlanningService 实收字段与 oracle overlap 的可审计证据；
- signed Bias 双向（正、负、拒绝不存在效果）、WAPE 可比/不可比、P80/P95 coverage 的明确数值 Gate；
- champion/fallback Gate 不能复制生产算法自证；
- 禁止关键词/子串裁判、常量等于自己、全局计数/拓扑全等；
- runner 直接 CLI 可复跑且失败时非零退出；
- ground-truth 污染必须能让 Gate 失败。

跨 WP 必须主动攻击开发者可能没覆盖的面：
- 恶意/损坏 JSON、null/NaN/Infinity、空数据、负值、极大值、边界天数；
- 相同时间戳、多租户/多店铺、并发、幂等、旧版本读侧；
- 时间语义、数据脏度、最新记录决胜、partial failure；
- schema v29→v30、_validate_schema、不可变 trigger/FK、灾备 manifest 兼容策略；
- 旧 M5/M6 全量回归；
- 依赖、迁移、API 兼容、路由、自动动作与误用面；
- 自行寻找至少一个开发者没有列出的攻击面。

独立执行要求：
- 所有 pytest 必须使用：
  NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 .venv/bin/python -m pytest -q ...
- 运行你自己选择的聚焦测试、独立探针和至少四项 mutation，不能只引用 docs 中的 mutation。至少包括未来泄漏、baseline fallback、ground-truth 污染、库存关键公式或只读边界；再增加你发现的新攻击面 mutation/probe。
- mutation 必须报告：改了什么、哪个门禁先失败、还原后哪个命令通过。可在 /tmp 保存临时探针；不要留下仓库脏改动。
- 运行 compileall、git diff --check、project-to-act --validate；是否需要再跑全量 pytest 由你独立判断，但最终必须明确你实际跑了什么，不能把开发者全量当成你的全量。
- 不得联网、不得读取或输出密钥/真实敏感数据。
- 发现问题时不要直接替开发者修复；给出最小可复现证据、严重级别、file:line、为何现有 Gate 未捕获、建议的最小修复与必须先补的失败测试。然后停下来等 Codex 修复，我们会在同一会话请求你复验。
- 如果没有发现问题，也必须给出你独立新增的探针/mutation 证据，而不是“测试都过了”。

首轮回复格式：
A. Git/范围核对；
B. 工作台 WP1–WP5 逐条矩阵（PASS/FAIL/NOT PROVEN + 证据）；
C. 独立命令与原始结果摘要；
D. 独立 probe/mutation（含还原证明）；
E. 新发现，按 P0/P1/P2/P3 排序并给 file:line；
F. 当前工作树清洁证明；
G. 首轮明确裁决：PASS 或 FAIL，以及精确修复清单。

最终裁决现在不要提前给“生产通过”。即使代码无缺陷，也必须严格区分：
1. M6-R 代码级本机候选；
2. 是否可进入合入评审；
3. 尚未豁免的 main 合入；
4. 服务器 schema v30；
5. 真实数据；
6. 24/72h 长稳；
7. 生产放行。

请现在开始独立审阅，完成首轮后停在会话中等待下一条追问。


~~~

### Grok response (verbatim)

# M6-R WP1–WP5 独立对抗验收 · 首轮

台账与开发者 README 只当待核验，不以它们为真相。本轮所有命令、探针和 mutation 均为本验收人独立执行。

---

## A. Git / 范围核对

| 项 | 独立结果 |
|---|---|
| 分支 | `codex/m6r-wp5-forecast-eval` |
| HEAD | `1d4fa83e4736261d7b484df6eaab16c82f25038d`（与要求的 `1d4fa83` 一致） |
| 工作树 | 干净（`git status --porcelain` 空；相对 HEAD 无 diff） |
| WP4 起点 | `67222d7cf3493fb4565ef14140dac13b10d57bd2` **是** HEAD 祖先（`merge-base --is-ancestor` exit 0） |
| `origin/main` | `4065b12dd5178ce7239d27b27d71614c8bee77cc` |
| WP4 是否在 main | **否**（`67222d7` 不是 `origin/main` 祖先，exit 1） |
| HEAD 相对 WP4 | 6 个 WP5 提交：`255f7df` → `3f5a051` → `38ad83d` → `3fa0ac8` → `e3bf261` → `1d4fa83` |
| 相对 main 的范围 | 必须包含 **未合入的 WP4 + WP5**。本轮按祖先链审阅，没有用 `git diff origin/main` 代替 WP4。 |
| WP5 vs WP4 文件 | 12 个文件、+999/−5；无 `pyproject.toml`、无 `graph.py` / `intent.py` / `prompts.py` / `llm.py` 变更 |
| Schema | `Database.SCHEMA_VERSION = 30`；`_apply_v29` / `_apply_v30` 各一处；无新迁移 |
| 本轮动作 | 无 commit / push / merge / 切分支 / 删分支 |

父链（与要求一致）：

`4065b12 (main, WP3 已合入) → … → 0c283de (WP4 代码 tip) → 67222d7 (WP4 验收记录) → 255f7df … → 1d4fa83`

---

## B. 工作台 WP1–WP5 逐条矩阵

图例：独立代码审阅 + 本轮命令/探针。台账 E-20260813-001 / E-20260812-008 只作对照，不作证据。

### WP1 Demand Fact

| 验收项 | 裁决 | 独立证据 |
|---|---|---|
| demand-v1 单一口径、Asia/Shanghai 归日 | **PASS** | `DemandPolicy` 冻结 `timezone=Asia/Shanghai`、`paid/partially_refunded/refunded`、排除 `canceled`（`models.py:14-23`）。UTC `15:59`/`16:00` 分属 8/10 与 8/11，测试与代码一致。 |
| paid/partially_refunded/refunded 与 canceled 排除 | **PASS** | `_demand_values` 用 policy 集合过滤（`service.py:238-246`）；取消回补产生 fact_version 2 且 `eligible_units=0`。 |
| 水位、固定回补、取消/状态更正追加不可变版本 | **PASS** | 同 hash 短路；变更追加 version；`trg_demand_daily_facts_immutable_*` 拒绝 UPDATE/DELETE。回补窗口 14 日写在 policy，不在请求里漂移。 |
| stockout true/false/unknown | **PASS** | 无同日快照 / 同仓歧义 → `unknown`；`available<=0` → `true`，否则 `false`。未知不会写成 false。 |
| 缺日、来源缺失、真零不可混淆 | **PASS** | `coverage_complete=True` 无单 → `0` + `zero_demand`；`False` → `None` + `data_coverage_missing`。**残余 P2**：零/缺日开关是 rebuild 请求旗标，未写入 `DemandPolicy`（工作台写“由 policy 固定”）。 |
| tenant/store/SKU 隔离与 lineage/hash/policy 可追溯 | **PASS** | 查询带 `tenant_id`；跨租户读空；payload 含 watermark / lineage / policy / hash。 |

### WP2 Forecast Engine

| 验收项 | 裁决 | 独立证据 |
|---|---|---|
| 七模型、六类/全部规定序列 | **PASS** | `SUPPORTED_FORECAST_MODELS` 七项纯 Python。引擎测试覆盖平稳/升/周季节/间歇/全零/冷启动；WP5 另覆盖下降、促销、缺货、缺失。 |
| 同一 rolling-origin，训练只见 origin 之前；泄漏反证 | **PASS** | `values[:origin]`（`engine.py:176`）。本轮 M1 改为 `values` 后 2 failed；还原后通过。 |
| champion ∈ 固定候选；challenger 不达阈值 baseline fallback | **PASS** | 冷启动从 `_BASELINE_ORDER ∩ candidate_models` 取（`engine.py:205-207`）。改进门槛 2%。M2 去掉门槛后 champion 变成 `croston`，2 failed。 |
| 候选失败隔离 / 全失败类型化 | **PASS** | 单模型 `RuntimeError` 不阻断；全失败 `forecast_engine_failed:forecast_baseline_failed`。 |
| WAPE/Bias 零分母、sMAPE/RMSE | **PASS** | `denominator==0` → WAPE/Bias `None`，RMSE 保留。CLI `many_zeros` 为 `incomparable`。 |
| P50≤P80≤P95、区间构造、持久化 backtest | **PASS** | 残差分位加性上界；DB `CHECK(p50<=p80 AND p80<=p95)`；backtest 逐窗落库。 |
| 脏 JSON / 失败证据 / 策略重放与冲突 | **PASS**（run 路径） | `get_run` 走 `_evidence_json` → `ForecastRunError` → HTTP 409。同版本内容漂移拒绝。 |

### WP3 Inventory Planning

| 验收项 | 裁决 | 独立证据 |
|---|---|---|
| store+SKU 需求不按仓复制；多仓聚合 | **PASS** | `demand_copy_count=1`；on_hand 12+8、reserved 2+1 → available 17。 |
| available=max(0,on_hand−reserved)、shortfall、inbound day-0 | **PASS** | reserved 12 / on_hand 5 → available 0、shortfall 7。M4 忽略 reserved 后 available 变成 5/20，3 failed。inbound>0 记 `inbound_eta_unavailable`。 |
| lead/review、分位、安全库存、MOQ、multiple、cap 固定顺序 | **PASS** | `_calculate` 顺序固定（`planning.py:731-758`）。CLI 库存 oracle：lead/review 20/40，reorder/target 23/43，建议量 24。 |
| 仓级 recommended qty withheld | **PASS** | `warehouse_id is not None` → qty `None` / `withheld`。 |
| 缺货日、risk、plan quality、陈旧/混时/畸形 | **PASS** | 风险按选定分位相对 lead/review 分层；畸形快照类型化错误。 |
| plan/policy 不可变、tenant FK、同输入幂等 | **PASS** | v30 不可变 trigger + 复合 FK；同 `input_hash` 重放。 |
| advisory_only，无采购/付款/库存改写 | **PASS** | `action_mode` CHECK 只能 `advisory_only`；计划创建不写 inventory 表。 |

### WP4 API / Agent / Admin

| 验收项 | 裁决 | 独立证据 |
|---|---|---|
| 九 API 的 tenant/store/error/审计 | **PASS**（主路径） | 九路径存在；跨租户 404；缺 store 422；审计三类事件。**残余 P2**：计划脏 JSON 读侧 500（见 E）。 |
| policy exact / store default / warehouse 继承、同戳、原子性 | **PASS**（主路径） | PUT 同一事务、同一 `created_at`；冲突回滚。库存 resolve 有 `rowid DESC`。**残余 P2**：`forecast_policies` resolve 无 `rowid DESC`（`run_service.py:66-68`）。 |
| 动态目录 + 两个只读工具 | **PASS** | 注册 `get_demand_forecast` / `get_inventory_plan`，`kind=read`，`computed_now=false`，`action_allowed=false`。 |
| 工具只读固化证据；读边界覆盖 forecasting 表 | **PASS** | 九表快照（含 `inventory_balances`）在工具执行前后全等。本轮 M5 证明缺日变 0 会被 run 路径抓住。 |
| D20 virtual + forecasting available | **PASS** | 仿真 48 passed（含 D20）；`virtual=true`；工具只读；复跑 `reused`。`graph.py`/`intent.py` 无 forecasting 关键词。 |
| 后台仅显式触发；失败不静默；前端不二算库存 | **PASS** | GET `/admin` 不增证据；查询/运行是按钮；部分失败 toast；库存线读 `plan.risk_evidence.inventory_projection`，无 `on_hand-reserved` 前端公式。 |
| 不新增关键词路由 / 拓扑 / 语义 fast path | **PASS** | WP4+WP5 相对 main 未改 graph/intent/prompts/llm。 |

### WP5 Forecast Eval

| 验收项 | 裁决 | 独立证据 |
|---|---|---|
| fixture / oracle 物理独立 | **PASS** | `run_scenario` 只收 `scenario_input`；全部生产调用后才读 oracle。字段审计 32 次调用、overlap=[]。本轮 M3 在生产前注入 `expected_type_code` → eval 失败。 |
| 十类序列 | **PASS** | CLI 十场景均 `passed=true`，类型与 oracle 一致。 |
| 每序列确有 rolling-origin | **PASS** | origins：4/4/4/4/4/4/4/3/3/1（冷启动 1 个完整窗）。`training_end < forecast_start`。 |
| 生产 service 实收字段与 oracle overlap 可审计 | **PASS** | `ALLOWED_CALL_FIELDS` + overlap/unexpected Gate。污染 CLI exit 1，overlap=`expected_type_code`。 |
| signed Bias 双向、WAPE 可比/不可比、P80/P95 数值 Gate | **PASS**（有门禁） | 上涨 Bias `−0.061`、下降 `+0.089`、全零不可比。翻转 Bias oracle → `bias_direction=false`。**残余 P2**：覆盖率门禁单侧且样本内（见 E）。 |
| champion/fallback 不复制生产算法自证 | **PASS** | Eval 只比 `champion ∈ candidates` 与 oracle 的 fallback 布尔；不重算 2%。间歇独立期望 `fallback=false`，实收 `tsb`。 |
| 禁止关键词裁判 / 常量自证 / 全局计数全等 | **PASS** | 数值/成员断言；`REQUIRED_CATEGORIES <=`；无场景总数 `== N`。 |
| CLI 可复跑；失败非零 | **PASS** | 干净 fixture exit 0、`passed=true`；污染 exit 1。 |
| ground-truth 污染必须让 Gate 失败 | **PASS** | 库内测试 + 本轮 M3 + CLI 污染。 |

跨 WP 主动面：NaN/Inf/负值/空序列/非连续日被引擎拒绝；脏计划 JSON 500（新发现）；forecast 表除 fact/plan/planning-policy 外无不可变 trigger（已知 WP2 形态）；v29→v30 `_validate_schema` 有 required 清单；灾备仍精确比对 schema（`docs/operations.md` 已写升级后全量新备份）。未做多进程压测并发，标 **NOT PROVEN**（实现上有 `BEGIN IMMEDIATE` + 写锁）。

---

## C. 独立命令与原始结果摘要

全部 pytest 使用规定的断网代理环境。**不是**转述开发者 727。

| 命令 | 结果 |
|---|---|
| 聚焦 WP1–WP5 六文件 | **58 passed in 12.78s** |
| migrations + disaster_recovery + virtual_store + operations_modules + cli | **48 passed in 32.21s** |
| mutation 还原后六文件再跑 | **64 passed in 10.94s**（含重复点名的子集；collect-only 仍为 58） |
| **独立全量** `.venv/bin/python -m pytest -q` | **727 passed, 1 xfailed in 250.25s** |
| `python scripts/run_forecast_eval.py evals/forecasting/forecast_eval_v1.json /tmp/m6r_wp5_cli.sqlite3` | **exit 0**，`passed=true`，10/10，32 次生产调用，overlap=[] |
| 污染 CLI | **exit 1**，`passed=false`，overlap=`['expected_type_code']` |
| `python -m compileall -q src scripts/run_forecast_eval.py scripts/forecast_eval_runtime.py` | exit 0 |
| `git diff --check` | exit 0 |
| `project-to-act --validate` | `valid=true`，`issues=[]` |

独立 CLI 数值（本机重跑，非文档抄写）：

| 场景 | type | champion | WAPE | Bias | origins | P80/P95 cov |
|---|---|---|---|---|---|---|
| stable_sales | stable | last_value | 0 | 0 | 4 | 1.0 / 1.0 |
| rising_trend | rising_trend | last_value | 0.061 | **−0.061** | 4 | 0.857 / 1.0 |
| falling_trend | falling_trend | last_value | 0.090 | **+0.090** | 4 | 1.0 / 1.0 |
| weekly_seasonality | weekly_seasonal | seasonal_naive_7 | 0 | 0 | 4 | 1.0 / 1.0 |
| intermittent_demand | intermittent | **tsb** | 1.754 | +0.145 | 4 | 0.821 / 0.929 |
| many_zeros | intermittent | last_value | None | None | 4 | 1.0 / 1.0 |
| promotion_spikes | variable | rolling_mean | 0.470 | +0.167 | 4 | 0.964 / 0.964 |
| stockout_truncation | stable | last_value | 0 | 0 | 3 | 1.0 / 1.0 |
| missing_observations | stable | last_value | 0 | 0 | 3 | 1.0 / 1.0 |
| cold_start | cold_start | last_value | 0 | 0 | **1** | 1.0 / 1.0 |

库存稳定场景与工作台手算一致：on_hand 18、reserved 4、available 14、inbound 5、future_supply 19、P80 lead/review 20/40、建议量 24、`demand_copy_count=1`、`advisory_only`。

---

## D. 独立 probe / mutation（含还原证明）

探针在 `/tmp/m6r_wp15_*`，**未写入仓库**。

### 独立探针（开发者清单里没有）

| ID | 攻击面 | 结果 |
|---|---|---|
| P-NEW-1 | `forecast_policies` 相同 `active_from/created_at` | 无 `rowid DESC`；同戳胜者未写入契约。v1→v2 碰巧新版本胜出，但不能当规范。 |
| P-NEW-2 | 对 `inventory_plans` **INSERT** 损坏 JSON（UPDATE 被不可变 trigger 拒绝） | `get_plan` 抛 `JSONDecodeError`；`GET .../inventory-plan` 与 `GET /risks` 均为 **HTTP 500**。 |
| P-NEW-3 | 下降序列把 P80/P95 塌成 P50 后再跑 WP5 覆盖率公式 | 覆盖率仍 **1.0 / 1.0**，仍会过 `0.65/0.80` Gate。上涨序列塌缩则为 0.0（门禁并非完全死）。 |
| P-NEW-4 | 空序列 / NaN / Inf / 负值 / 缺日 | 全部 `ValueError`（`forecast_series_*`）。 |
| P-NEW-5 | 把上涨 oracle 的 Bias 期望改成 `positive` | `passed=false`，`bias_direction=false`，观测 Bias `−0.061`、effect `negative`。 |
| P-NEW-6 | CLI + fixture 污染 | exit 1。 |

### Mutation（五项，含一项新攻击面）

每次改生产/runner → 看哪扇门先红 → `git checkout -- <file>` → 再跑原命令。

| ID | 改了什么 | 先失败的门禁 | 还原后 |
|---|---|---|---|
| M1 未来泄漏 | `engine.py` `values[:origin]` → `values` | `test_rolling_backtest_uses_only_each_origins_past` + WP5 总 Gate，**2 failed** | `git checkout -- engine.py`；同两项通过 |
| M2 baseline fallback | `_select_champion` 改为“有 challenger 就选” | `test_baseline_is_retained_...`（champion=`croston`）+ WP5，**2 failed** | `git checkout -- engine.py`；还原 |
| M3 ground-truth 污染 | `run_forecast_eval.py` 在 `run_scenario` **之前**把 `expected_type_code` 打进 input | WP5 总 Gate **1 failed** | `git checkout -- scripts/run_forecast_eval.py` |
| M4 库存公式 | `available` 改为只用 `on_hand` | `test_reserved_above_on_hand_...`（5≠0）、多仓 available 20≠17、WP5，**3 failed** | `git checkout -- planning.py` |
| M5 **新攻击面**：缺日冒充真零 | `_series` 对 missing 仍记 anomaly，但把值写成 `0` | `test_run_marks_gaps_stockouts_...`（要求 `None`）+ WP5，**2 failed** | `git checkout -- run_service.py` |

还原证明：`git diff` 对上述四文件为空；`git status --porcelain` 空。还原后聚焦 58 collect / 再跑通过；全量 **727 passed, 1 xfailed**。

---

## E. 新发现（按严重度）

**无 P0。** 没有发现会在正常 API 路径上改写库存/采购、泄漏未来、或关键词路由的缺陷。

### P2-1 — 损坏的 `inventory_plans` JSON 使读 API 500

- **位置**：`planning.py:430` `json.loads` 无类型化包装；`forecasting_api.py:292` `list_risks` 也未走 `call()`。
- **复现**：合法 `create_plan` 后 **INSERT**（不是 UPDATE）一行 `stockout_dates_json='{'`。`get_plan` → `JSONDecodeError`；`GET /v1/forecasting/skus/{sku}/inventory-plan` 与 `GET /risks` → **500**。
- **为何现有 Gate 未捕获**：WP4 只修了可 UPDATE 的 `forecast_runs` 脏 JSON（409）。`inventory_plans` UPDATE 被 trigger 挡住，测试从未 INSERT 坏 JSON。
- **最小修复**：`get_plan` 把 `json.loads` 收进 `InventoryPlanningError("inventory_plan_evidence_invalid")`；`/risks` 走同一映射，409 而非 500。
- **必须先补的失败测试**：INSERT 坏 JSON 后断言上述两个 GET 为 409 且 `detail=inventory_plan_evidence_invalid`（`raise_server_exceptions=False`）。修复前该测试应红。

### P2-2 — WP5 P80/P95 覆盖率门禁是单侧、样本内自洽

- **位置**：`scripts/forecast_eval_runtime.py:222-245`：`actual <= predicted + (p80-p50)`，宽度来自**同一批** champion 残差构造的点。
- **复现**：下降序列 last_value 系统性高估；把 P80/P95 塌成 P50 后覆盖率仍 1.0，**仍过** `minimum_p80_coverage=0.65`。上涨序列塌缩会失败，所以门禁没完全死。
- **为何现有 Gate 未捕获**：阈值低于构造分位（0.8），且只检查上侧。
- **最小修复**：覆盖率改为 `|actual-predicted| <= width`，或对 held-out / 扰动后的窗口计覆盖；至少加一条“过预报 + 零宽度必须失败”的反例。
- **必须先补的失败测试**：构造 last_value 过预报序列，断言塌缩区间后 `interval_coverage` 检查为 false。

### P2-3 — `forecast_policies` 同戳缺少 `rowid DESC`

- **位置**：`run_service.py:66-68`。对照已修的 `planning.py:120-131`。
- **复现**：两行相同 `active_from/created_at`、不同 `policy_version` 时，胜者未写入 ORDER BY。库存侧有 `test_policy_resolution_breaks_equal_timestamps_by_newest_rowid`；预测侧没有。
- **为何未捕获**：预测测试只用不同时间戳，或 SKU vs store。
- **最小修复**：ORDER BY 末尾加 `rowid DESC`，并移植同戳测试。
- **实际风险**：`utc_now()` 带微秒，公开 PUT 撞同戳概率低。

### P3

- `coverage_complete` 未进入 `DemandPolicy`（工作台“由 policy 固定”）。WP1 已验收形态，标残余。
- `forecast_runs/policies/backtests/points/anomalies` 无不可变 trigger（WP2 已合入 main 的形态）。
- 间歇场景 `maximum_wape: 2.0` 很松。
- `_validate_schema` 仍有重复键 `"release_policies"`（`database.py:2863` 与 `:3046`）——审计旧债，非本分支引入。

未把开发者已列的四项 mutation 当新发现。

---

## F. 当前工作树清洁证明

```text
On branch codex/m6r-wp5-forecast-eval
nothing to commit, working tree clean
HEAD 1d4fa83e4736261d7b484df6eaab16c82f25038d
git status --porcelain   # empty
git diff --stat          # empty
git diff --cached --stat # empty
```

临时探针只在 `/tmp/m6r_wp15_*`、`/tmp/m6r_wp5_cli*`。仓库内无残留改动。

---

## G. 首轮裁决

### 本轮总裁决：**PASS**（M6-R **代码级本机候选**）

未发现 P0/P1：无未来泄漏、无 baseline 强选、无缺日/缺货冒充 0、无采购/付款/库存事实写、无新关键词路由、Eval oracle 污染可被抓住。五项独立 mutation 均按预期失败并已还原。独立全量 **727 passed, 1 xfailed**。

P2-1/P2-2/P2-3 **不阻断本机候选**，但应在合入评审里明示；**本轮不替开发者改代码**。若你们要把 P2-1（500）或 P2-2（覆盖率反例）升为合入前必修，在本会话说一声，我按同一标准复验。

### 精确修复清单（非阻断，建议合入前处理）

1. **P2-1**：`get_plan` 类型化 JSON 错误 + `/risks` 映射 409；补 INSERT 坏 JSON 测试。  
2. **P2-2**：覆盖率改为双侧或 held-out，并补“过预报 + 零宽度失败”。  
3. **P2-3**：`ForecastRunService.resolve_policy` 加 `rowid DESC` + 同戳测试。

### 七层状态（全部未豁免，除非下面写明）

| 层 | 状态 |
|---|---|
| 1. M6-R 代码级本机候选 | **PASS**（本轮独立证据） |
| 2. 是否可进入合入评审 | **可以**（WP4 未入 main，应与 WP5 **整链**评审；披露 P2） |
| 3. 尚未豁免的 main 合入 | **未授权** |
| 4. 服务器 schema v30 | **未验证 / 未豁免** |
| 5. 真实数据 | **未验证 / 未豁免** |
| 6. 24/72h 长稳 | **未验证 / 未豁免** |
| 7. 生产放行 | **未授权** |

停在本会话，等下一条追问或是否要把 P2 当合入前必修。

## Round 2 — fixes, independent re-verification, and final verdict

### Codex prompt (verbatim)

~~~text
继续同一个 M6-R WP1–WP5 独立对抗验收会话，不得另开会话，也不要把开发者陈述当成证据。

你首轮报告了 P2-1（inventory_plans 损坏 JSON 导致两个 GET 500）、P2-2（WP5 覆盖率单侧，下降序列零宽仍过门）、P2-3（forecast_policies 同时间戳缺显式 rowid tie-break）。我已按 evidence-first 先补失败测试，再做最小修复，当前 HEAD 为 d3b8e57822df5d228cb69e0ecda33af7578b7515，提交为：
d3b8e57 fix(forecasting): close adversarial review gaps

开发者红态证据（请独立复验，不要直接采信）：
1. 零宽过预报反例：期望 P80/P95 coverage=0，修复前实际 1.0/1.0，失败。
2. INSERT 损坏 stockout_dates_json 后，latest inventory-plan GET 修复前为 HTTP 500，失败。
3. 同 active_from/created_at 插入 older=zzz、newer=aaa，并增加合法索引改变扫描顺序及 ANALYZE；修复前 resolve 返回 older zzz，失败。
首次合跑结果为 2 failed, 1 passed；补强同戳反例单跑为 1 failed。

实现：
- InventoryPlanningService.get_plan 对 8 个 JSON 证据字段统一做 json.loads + 预期 list/dict 类型校验，失败抛 inventory_plan_evidence_invalid；/v1/forecasting/risks 改为复用统一 call() 映射，因此两个读 API 应为 409。
- ForecastRunService.resolve_policy 在 active_from DESC, created_at DESC 后显式增加 rowid DESC。
- WP5 保留 P80/P95“上分位需求”的单侧覆盖语义，但增加 sharpness 反证：若 backtest 存在非零误差且生产区间宽度 <= 0，该 level 的 coverage 直接为 0。没有采用 abs(actual-predicted)<=width，因为那会把上分位数偷换成双侧置信带；实际扩展回归证明这种改法令 promotion_spikes P95 从 0.964 降为 0.714，并使两个 Eval 测试失败。

开发者绿态证据（仍请独立复验）：
- 三项定点：3 passed。
- forecasting engine/run/inventory/WP4/WP5 五文件：54 passed。
- git diff --check 通过；修复提交 120 additions / 14 deletions，小于 500 行。
- 提交后工作树干净。

请现在在本会话内独立完成修复复验并给最终裁决：
1. 审阅 d3b8e57 相对 1d4fa83 的完整 diff，检查修复是否引入 API 契约、schema、路由、自动动作、租户隔离或证据类型回归。
2. 独立重做三项原始复现，尤其：
   - 损坏 inventory_plans JSON 时 inventory-plan 与 /risks 两个路径都必须是 409，detail 精确为 inventory_plan_evidence_invalid；再试一个“合法 JSON 但错误结构类型”的字段。
   - 用不同有效索引/扫描顺序扰动同戳策略解析，必须仍选择最新 rowid；同时确认 SKU override 仍优先于 store default。
   - 把存在系统性过预报的 P80/P95 压到 P50，必须使 coverage Gate 失败；干净十场景 runner 必须仍 PASS。请主动攻击“非零但极小宽度”“完全无误差时零宽”两边，判断 sharpness 约束是否真正双向可证伪；若你认为样本内覆盖仍构成阻断，请给出可执行反例和严重度，不要只重复首轮描述。
3. 独立运行相关聚焦门禁、Eval runner、compileall、git diff --check、project-to-act --validate，并用规定断网代理环境重新运行全量 pytest：
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 .venv/bin/python -m pytest -q
4. 确认首轮五项 mutation 已还原、当前工作树干净，并自行做至少一个针对本轮修复的新 mutation；mutation 必须先让对应门禁失败，随后还原并复验。
5. 最终回复请完整列出：独立命令/原始结果摘要、新 probe/mutation、剩余发现及严重度、明确 PASS/FAIL。若 PASS，明确区分“代码级本机候选/可进入合入评审”与尚未豁免的 main 合入、服务器 v30、真实数据、长稳、生产放行；若 FAIL，给出最小失败证据和必须修复项。

不要替我改代码或提交；停留在这个同一会话等待后续追问。
~~~

### Grok response (verbatim)

# M6-R WP1–WP5 修复复验 · 最终裁决

修复提交 `d3b8e57` 相对 `1d4fa83` 的代码已独立审阅并复验。本轮没有采信开发者红/绿叙述；红态是把生产文件退回 `1d4fa83` 后自己跑出来的。

**HEAD 说明（非本验收人操作）：** 复验开始时 HEAD 为 `d3b8e57`。红态还原后发现分支上多了一个 `aaadb46 docs(forecasting): preserve Grok review round one`（作者 LuckyE），只新增 `docs/works/14-feature-m6r-forecast-eval/GROK_INDEPENDENT_REVIEW.md`。`src/`、`scripts/`、`tests/` 与 `d3b8e57` 的 diff 为空。当前工作树干净。未切分支、未改代码、未提交。

---

## 1. `d3b8e57` vs `1d4fa83` 审阅

7 文件，+120 / −14。生产改动只有四处：

| 文件 | 行为 |
|---|---|
| `planning.py` | `get_plan` 对 8 个 JSON 字段 `loads` + `list`/`dict` 类型校验，失败 `inventory_plan_evidence_invalid` |
| `forecasting_api.py` | `GET /risks` 改走统一 `call()` |
| `run_service.py` | `resolve_policy` 增加 `rowid DESC` |
| `forecast_eval_runtime.py` | 有非零误差且 width≤0 时该 level coverage=0 |

未改 schema、依赖、LangGraph/intent/prompt、模块登记、采购/付款/库存写路径。成功读路径的响应形状不变；失败路径从 500 收成 409，是错误契约补全，不是成功契约破坏。租户过滤仍在 `tenant_id=?` 查询上。

---

## 2. 红态独立复验（生产退回 `1d4fa83`，测试留在修复后）

`git checkout 1d4fa83 --` 四个生产文件后：

```text
3 failed in 1.34s
```

| 测试 | 修复前实测 |
|---|---|
| `test_interval_coverage_rejects_zero_width_overforecast` | `{'p80': 1.0, 'p95': 1.0}` ≠ `{0.0, 0.0}` |
| `test_forecasting_api_rejects_corrupt_inventory_plan_as_domain_error` | **HTTP 500** ≠ 409 |
| `test_policy_resolution_breaks_equal_timestamps_by_newest_rowid` | resolve 得到 **`forecast-same-time-zzz`**（older） |

随后 `git checkout HEAD --` 四个文件，工作树恢复干净。这与“先补失败测试”一致；最终测试集对旧实现是 **3 failed**（比开发者第一次合跑的 2 failed / 1 passed 更严，因为同戳用例后来加了索引扰动）。

---

## 3. 绿态三项原始复现 + 额外攻击

### P2-1 损坏 JSON / 错误结构

独立探针 INSERT 后 GET（`raise_server_exceptions=False`）：

| 载荷 | latest inventory-plan | `/risks` |
|---|---|---|
| `stockout_dates_json='{'` | **409** `inventory_plan_evidence_invalid` | **409** 同 detail |
| 合法 JSON、错误类型 `stockout_dates_json='[]'`（期望 dict） | **409** 同 detail | **409** 同 detail |
| `assumptions_json='[1]'`（期望 dict） | **409** 同 detail | — |

定点测试：`3 passed in 1.17s`。

### P2-3 同戳 + 扫序

在 store default + older `zzz` + newer `aaa`（同一 `active_from/created_at`）上建 `policy_version ASC` **和** `DESC` 索引并 `ANALYZE`。`EXPLAIN QUERY PLAN` 走了 `probe_scan_policy_version_asc`，但有 `USE TEMP B-TREE FOR ORDER BY`，最终仍选 **`forecast-same-time-aaa` / 28 天**。无 SKU override 的 SKU 仍落到 store default。SKU 优先未被 `rowid DESC` 破坏。

### P2-2 覆盖率 sharpness

| 情形 | 独立 `_interval_coverage` |
|---|---|
| 过预报 + P80/P95=P50（width=0） | **p80=0, p95=0**（Gate 会失败） |
| 欠预报 + 零宽 | **0 / 0** |
| 完全无误差 + 零宽 | **1.0 / 1.0**（应通过） |
| 过预报 + width=`1e-15` | **仍 1.0 / 1.0** |

干净十场景 CLI：`exit 0`，`passed=true`，10/10，32 次生产调用，overlap=[]。`promotion_spikes` P95 仍为 **0.964**（未改成双侧 `abs`）。

**sharpness 是否双向可证伪：**
- 有误差 × 零宽 → 失败：可证伪。
- 无误差 × 零宽 → 通过：可证伪。
- 有误差 × **非零但极小正宽度**：单侧过预报仍 100% 覆盖。这不是“零宽过预报仍过门”的回归，而是 tripwire 只卡 `width<=0`。生产宽度来自残差分位，有非零 \|残差\| 时不会塌成 0。**不构成阻断。** 标 P3 残余，见下。

样本内单侧覆盖仍是上分位需求语义，不是双侧 CI。首轮那个可执行反例（零宽过预报过门）已被新 Gate 抓住；没有新的阻断级反例。

---

## 4. 独立命令与原始结果

| 命令 | 结果 |
|---|---|
| 旧生产 × 三项新测试 | **3 failed**（红态） |
| 三项定点（修复后） | **3 passed in 1.17s** |
| engine + run + inventory + WP4 + WP5 | **54 passed in 11.22s** |
| CLI `scripts/run_forecast_eval.py evals/forecasting/forecast_eval_v1.json /tmp/m6r_rereview_cli.sqlite3` | **exit 0**，`passed=true` |
| `compileall -q src scripts/run_forecast_eval.py scripts/forecast_eval_runtime.py` | exit 0 |
| `git diff --check` | exit 0 |
| `project-to-act --validate` | `valid=true`，`issues=[]` |
| **全量** 规定代理 pytest | **730 passed, 1 xfailed in 296.80s**（首轮 727 + 本轮 3 个新测试） |

首轮五项 mutation 未留在树里：相对 `d3b8e57` 的 `src/scripts/tests` 无脏 diff；当前 `git status --porcelain` 空。

---

## 5. 本轮新 mutation

去掉 sharpness 分支（保留单侧 `actual <= predicted + width`）：

```text
test_interval_coverage_rejects_zero_width_overforecast
assert {'p80': 1.0, 'p95': 1.0} == {'p80': 0.0, 'p95': 0.0}
1 failed
```

`git checkout -- scripts/forecast_eval_runtime.py` 后同测试 + WP5 总 Gate：**2 passed**。工作树再次干净。

---

## 6. 剩余发现

| 级 | 项 | 结论 |
|---|---|---|
| — | P2-1 / P2-2 / P2-3 | **已关闭**（独立红/绿 + 探针） |
| **P3** | 过预报 + 极小正宽度仍 100% 覆盖 | sharpness 只卡 `width<=0`。不阻断本机候选或合入评审。若以后要加强，应对 `width` 相对 \|误差\| 设下限，而不是改成 `abs` 双侧带。 |
| P3 | HEAD 现为文档提交 `aaadb46` | 不改变裁决；生产代码仍是 `d3b8e57`。 |

无 P0/P1。无新的 API/schema/路由/自动动作回归。

---

## 7. 最终裁决：**PASS**

| 层 | 状态 |
|---|---|
| 1. M6-R 代码级本机候选 | **PASS**（WP1–WP5 + 本轮三项对抗修复） |
| 2. 可进入合入评审 | **可以**（与未入 main 的 WP4 **整链**一起） |
| 3. main 合入 | **未授权 / 未豁免** |
| 4. 服务器 schema v30 | **未验证 / 未豁免** |
| 5. 真实数据 | **未验证 / 未豁免** |
| 6. 24/72h 长稳 | **未验证 / 未豁免** |
| 7. 生产放行 | **未授权** |

必须修复项：**无。**

停在本会话，等后续追问。
