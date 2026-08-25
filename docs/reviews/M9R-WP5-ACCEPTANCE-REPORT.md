# M9-R PR #19 WP5 重验候选报告

> 性质：开发侧修复与负责人式预验收证据，不是闫睿涵的独立 WP5 签署。
> 日期：2026-08-25。
> PR：#19，分支 `feature/m9r-read-model`。

## 1. 固定对象

| 字段 | 值 |
|---|---|
| Base | `1ee68cb686fd4f3c86c22c51b6f57c84042d6d45` |
| 负责人最新不通过 Head | `8e7ede34225e080f8343138c7060aa216832009f` |
| 本轮实现提交 | `f23dba31de755ee7df5dbc0cde894d41c06b47ad` |
| 实现提交 tree | `434181ad4e4030b1a9a51e7323e4fa561c351fff` |
| schema | v36 与 v39 属于 M9-R；v37 与 v38 不在本提交 |

负责人已说明此前本地 `main ahead 36` 是临时合并旧 Head 造成，已恢复到
`origin/main`，不要求 PR #19 回滚或额外同步。本轮因此只以 PR #19 的提交链和上述
Base 为准。

本报告与项目账本位于实现提交之后的证据提交中。Git 提交不能在自身文件内容里保存
自身 SHA，故远端最终 Head 以推送后的 `gh pr view 19` 回读为准；最终 Head 必须是
`f23dba3` 的直接后代，且除证据文档外不再改变已测试代码树。

## 2. 结论

负责人确认 `8e7ede3` 已闭环此前 5 项阻断，本轮又修复其唯一剩余项：PR 历史
`753ff15` 可公开写出的“订单头 item 非空、订单行无 item 字段”v36 事实升 v39 后，
同水位正常重放误报冲突。开发侧从负责人验收思路执行了真实历史版本链、迁移、D-014、证据诚实、
D-034、oracle 隔离、真实模型、浏览器、反证和全量回归检查，当前未发现新的 M9-R
P0/P1 阻断。

当前结论仅为：**PR #19 已形成可供闫睿涵从干净远端 Head 重新执行 WP5 的开发侧候选。**
只有闫睿涵完成独立场景、mutation、浏览器和回归后才能签署 M9-R。

## 3. 最新阻断闭环

| # | 负责人阻断 | 修复 | 独立可复核证据 |
|---|---|---|---|
| 1 | 旧 v36 升 v39 后，同水位正常重放误报 D-014 冲突，包括订单头 item 非空、旧订单行无 item 字段的 `753ff15` 公开形态 | 只对缺省的新增 item 字段计算受控旧 payload hash 候选；保留订单头 item，不改历史 hash；不同真实载荷仍冲突 | `tests/test_m9r_v39_legacy_replay.py`；`753ff15` 真实服务跨版本探针 |
| 2 | `refunds=MISSING` 时 `net_sales` 仍以 gross/ACTUAL 冒充净销 | 退款来源不可用或无法归属时，`refunds` 与 `net_sales` 同步 MISSING；payments 保持可用且可追溯 | `tests/test_m9r_query_source_honesty.py` |
| 3 | Eval 从 SKU 编码改成 `required_signals` 编码，仍未发现方向 | 删除 `required_signals` 和固定信号映射；生产形态原始指标进入诊断/建议模型；oracle 预检只读场景名与 input；真实方向由独立禁 mock live gate 证明 | `evals/product_lifecycle/run_m9r_direction_eval.py`、`tests/test_m9r_direction_live_gate.py` |
| 4 | 页面只显示 evidence key，不显示具体引用值 | 递归展开 `evidence_references` 的路径和值，并用真实浏览器 fixture 锁 revision/source/mapping 引用 | `docs/admin-console.html`、`tests/test_m9r_workbench_browser.py` |
| 5 | 报告 Head 漂移、项目环境浏览器不可复现 | Playwright 已在 `pyproject.toml` dev 依赖中；项目 `.venv` 实跑 5 项浏览器测试；本报告固定 Base、实现提交、真实计数和证据边界 | 本报告与第 5 节命令 |

额外发现并修复：模型在两次 execution feedback 后若第三次仍返回缺可信前提的类型或
禁用理由，旧循环会放行第三次结果。现在重试耗尽后返回
`KEEP_OBSERVE/model_output_rejected`，保留模型来源并显式 degraded；正常模型决定不被
该 Gate 改写。

## 4. 负责人式验收矩阵

| 维度 | 检查结果 |
|---|---|
| WP1 粒度与隔离 | store/item/SKU/revision、跨仓、订单行 item 归属和来源追溯通过；店铺流量不拆 SKU |
| D-014 与迁移 | 旧 v36 公开事实升 v39 后正常同水位重放 idempotent；同水位不同载荷仍 conflict |
| 退款口径 | 订单来源与退款来源分开表达；退款未知时不产正式 net sales |
| WP2 证据 Gate | revision、实际窗口、A/A、样本、控制变量、freshness、污染和 provenance 保持确定性边界 |
| D-034 | 模型决定诊断和建议语义；代码只计算数值、校验可信前提、输出安全、状态和写屏障 |
| WP3 状态与执行 | 建议默认 draft；人工审核；幂等、stale、不可变历史和审计保持；批准不触发平台写 |
| WP4 Eval | `/mechanism` 只声明 `fixed_ruleset_mechanics`，不冒充方向发现；live gate 才作为模型方向证据 |
| oracle 隔离 | 生产输入禁止 expected/oracle/建议类型/旧 signals，输入 hash 在模型前生成，expected 在对应模型调用后读取 |
| 页面下钻 | revision 时间窗、实验与来源、诊断、语义来源及具体 evidence reference 值可见 |
| schema 协调 | `_apply_v36`、`_apply_v39` 各一份；初始化只含 36、39；未混入 v37/v38 |
| 生产边界 | 无发布商品、改价、换图、投放、活动或下架动作；Demo 不进入默认 operational |

## 5. 新鲜验证证据

| Gate | 结果 |
|---|---|
| 全仓收集 | `1316 tests collected`，exit 0 |
| 全仓串行回归 | `1316 passed in 1204.75s`，0 failed / 0 skipped / 0 xfailed |
| M9-R 专项 | `252 passed in 74.56s` |
| WP1 | 18 项全部 PASS，脚本 exit 0 |
| WP2 | 12 项全部 PASS，脚本 exit 0 |
| WP3 | 8 项全部 PASS，脚本 exit 0 |
| WP4 | 10 项全部 PASS，包含直接 Playwright 门禁，脚本 exit 0 |
| 浏览器 | `5 passed`；1280x720、390x844、console、显式生成/提交/审计、具体证据值 |
| live 方向 gate A | DeepSeek `deepseek-v4-flash`，非 mock，温度 0，`5/5` |
| live 方向 gate B | 同配置独立复跑，`5/5` |
| 静态门禁 | `compileall`、`git diff --check`、迁移唯一性扫描均 exit 0 |

两轮 live 报告：

- `docs/reviews/M9R-DIRECTION-LIVE-20260825-A.json`，SHA-256
  `096156D7C3F96410801354E6F143DD519CF3E440B5ADE4E5EC20826F1C701FE0`
- `docs/reviews/M9R-DIRECTION-LIVE-20260825-B.json`，SHA-256
  `3530006D459ACC4824779D09F13007F7B87C9F9C7F9580AA81BB008B7BB4B8E8`

报告均记录 `mode=live`、`evaluation_temperature=0.0`、
`production_input_oracle_separated=true` 和 `oracle_read_after_model_call=true`，不含密钥、
token、密码、Cookie 或原始顾客数据。

## 6. 反证与 mutation

| 反证 | 破坏后 | 恢复后 |
|---|---|---|
| 恢复旧限制、拒绝订单头 item 非空的 v36 候选 | `source_version_conflict`，新增回归 `1 failed` | 新增回归及整个文件 `3 passed` |
| `753ff15` 公开服务写 v36 后由当前代码升 v39 | 修复前负责人稳定复现 conflict | 原事件 `idempotent`、version 1、订单行回填 item；金额变化仍 conflict |
| 移除模型重试耗尽安全收口 | 2 项失败：缺 revision 的实验被放行；禁用理由未 degraded | 2 passed |
| 旧 v36 同水位载荷改变 | `source_version_conflict` | 正常原事件 replay 为 idempotent |
| 方向输入注入建议值或 oracle key | oracle preflight 抛错 | 5 个原始业务场景通过 preflight |
| 方向模型固定返回错误类型 | 方向场景 oracle 失败 | 两轮真实模型均 5/5 |

## 7. 可复现命令

PowerShell，使用项目环境：

```powershell
$m9rTests = Get-ChildItem tests -File -Filter 'test_m9r_*.py' |
  Sort-Object Name | ForEach-Object { $_.FullName }
& .venv\Scripts\python.exe -m pytest $m9rTests -q -p no:cacheprovider `
  --basetemp=.tmp_m9r_wp5
& .venv\Scripts\python.exe tests\verify_wp1_acceptance.py
& .venv\Scripts\python.exe tests\verify_wp2_acceptance.py
& .venv\Scripts\python.exe tests\verify_wp3_acceptance.py
& .venv\Scripts\python.exe tests\verify_wp4_acceptance.py
& .venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider `
  --basetemp=.tmp_m9r_full
& .venv\Scripts\python.exe -m compileall -q src evals\product_lifecycle tests
git diff --check
```

真实模型 gate 需要验收人自己的受控 env 文件，不得提交凭据：

```powershell
& .venv\Scripts\python.exe evals\product_lifecycle\run_m9r_direction_eval.py `
  --env-file <受控-env-file> --out <report-a.json>
& .venv\Scripts\python.exe evals\product_lifecycle\run_m9r_direction_eval.py `
  --env-file <受控-env-file> --out <report-b.json>
```

## 8. 升级与备份策略

- v36 升 v39 前：由匹配 v36 的旧程序停写并生成、验证全量备份。
- v39 初始化：保留订单头、订单行、物流、售后和事件；库存迁移为 item 专属/未知身份两组部分唯一索引。
- 升级后恢复写入前：由 v39 程序生成并验证新的全量备份。
- 灾备 manifest 精确匹配当前 schema；v36 归档不得直接由 v39 程序恢复，只能在匹配旧 schema 的隔离环境恢复或先走受控升级。
- D-014 兼容只接受新增字段缺省的旧 payload hash：最早形态可同时缺订单头/订单行 item，
  `753ff15` 形态可保留订单头 item、缺订单行 item；任何其它同水位载荷差异仍冲突。

## 9. 未放行事项

- 本报告不是闫睿涵的 WP5 独立签署，也不替代其未见场景和 mutation。
- live gate 证明冻结场景上的模型方向，不证明真实店铺数据质量、平台因果或平台内部权重。
- 不包含真实淘宝/ERP 数据接入、生产发布、长稳、容量、异机灾备或任何平台写能力。
- M10-R v37/v38 及 PR #24 必须在 M9-R 合入后的 main 上重新基线，合并时保留 36、37、38、39 全部迁移，不能整体取 ours/theirs。

## 10. 正式 WP5 下一步

闫睿涵应从 PR #19 推送后的远端最终 Head 建立干净 detached worktree，先确认该 Head 是
`f23dba3` 的后代，再按第 7 节复跑。独立测试应至少覆盖旧 v36 公开写入后的同水位重放，
包括订单头 item 非空、旧订单行无 item 字段的 `753ff15` 公开形态；同时验证真实金额变化仍冲突、
退款未知对净销的阻断、移除 answer-free 业务事实后的方向失败、oracle 注入拒绝、具体证据
引用 DOM 可见，以及批准建议仍无平台写动作。失败需继续退回胡磊修复；全部通过后再由
闫睿涵写入正式签署结论。
