# M9-R PR #19 WP5 独立验收协议

> 任务书唯一权威源：`docs/tasks/M9R_PRODUCT_TRAFFIC_LIFECYCLE_WORKBENCH.md`。
> 开发负责人：胡磊；独立验收负责人：闫睿涵。

## 1. 固定验收对象

- PR：#19，`feature/m9r-read-model`。
- Base：`1ee68cb686fd4f3c86c22c51b6f57c84042d6d45`。
- 已验证实现提交：`f23dba31de755ee7df5dbc0cde894d41c06b47ad`。
- 验收时必须从 GitHub 回读最新 Head，并确认它是 `f23dba3` 的后代。
- 使用干净 detached worktree；开发者工作区和临时报告不作为独立证据。

```powershell
gh pr view 19 --repo a1024053774/yunpai-ecommerce-agent `
  --json headRefOid,baseRefOid,mergeStateStatus,state
git merge-base --is-ancestor f23dba3 <remote-head>
git worktree add --detach D:\m9r-wp5 <remote-head>
```

## 2. 先决门禁

1. `git status --short` 无输出。
2. `compileall` exit 0。
3. `pytest --collect-only` exit 0，记录真实计数和 skip。
4. `git diff --check <base>..<head>` exit 0。
5. `_apply_v36`、`_apply_v39` 各一份；M9 提交不含 `_apply_v37/_apply_v38`。
6. 报告不得把开发侧候选写成 WP5 已签署。

## 3. WP 验收矩阵

| WP | 必验边界 | 主要证据 |
|---|---|---|
| WP1 | tenant/store/item/SKU/revision 原始粒度；店铺指标不拆 SKU；来源与缺失诚实 | `test_m9r_read_model_isolation.py`、`test_m9r_query_source_honesty.py` |
| WP2 | M5-R revision/window/analysis/provenance；A/A、样本、控制变量、freshness、污染 Gate | `test_m9r_gates_production.py`、`test_m9r_diagnosis_bridge.py` |
| WP3 | 模型语义、可信前提反馈、draft/人工审核、幂等/stale/不可变历史、零平台写 | `test_m9r_production_recommendation_chain.py`、`test_m9r_lifecycle_*` |
| WP4 | 真实/Demo 隔离、answer-free Eval、live 方向、浏览器下钻到具体证据值 | `test_m9r_direction_live_gate.py`、`test_m9r_workbench_browser.py` |

## 4. 最新负责人反例

必须独立重建，不只复跑提交内测试：

1. 分别用最早全空 item 形态和 `753ff15` 公开服务可达形态写入 v36；后者必须是订单头
   item 非空、旧订单行无 item 字段。升 v39 后以相同 source time 和业务载荷重放，库存与
   订单均为 idempotent；改变真实金额/数量或订单头 item 仍 conflict。
2. 无可用退款来源时，`refunds` 与 `net_sales` 同时 MISSING，payments 保持可用。
3. 方向场景输入删除原始关键事实后应失败；不能依赖 SKU、场景名、`required_signals` 或建议类型值。
4. oracle key、oracle value 或建议类型进入生产 input 时 preflight 必须拒绝；preflight 不能访问 `scene.expected`。
5. 建议 DOM 必须显示实际 revision/source/mapping 引用值，不接受只显示顶层 key。
6. 模型三次持续返回缺可信前提的类型或禁用理由时，必须安全降级并保留审计来源。

## 5. Eval 证据分级

- `/v1/admin/evaluations/mechanism` 是 `fixed_ruleset_mechanics`，只证明离线机械回归和污染拒绝，`direction_discovery_claim=false`。
- 固定 mock 可以证明 schema、生产输入形态和 facts snapshot 接线，不能证明模型发现方向。
- 方向发现必须运行 `run_m9r_direction_eval.py`，要求 `MODEL_ENABLED=true`、`MODEL_MOCK_MODE=false`，强制评测温度 0。
- oracle 隔离预检先于模型；oracle 评分晚于对应模型调用。报告必须保存输入与 oracle hash。
- 至少连续两轮全部通过；失败或波动须如实记录，不得删除场景或把 mock 结果替代 live。

## 6. 浏览器 Gate

使用项目 dev 依赖和真实浏览器：

- 1280x720 与 390x844 无页面级横向溢出。
- console 无新增 warning/error。
- 页面真实显示 revision 时间窗、实验/来源、诊断、语义来源和具体 evidence reference 值。
- 纯查看不创建分析、实验、建议或修改商品。
- 生成、提交和审计必须由显式点击触发；批准仍无平台写动作。
- skip 不计通过，必须在具备 Playwright 和 Edge/Chrome 的环境补跑。

## 7. 回归命令

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

真实模型使用验收人的受控配置，连续运行两次：

```powershell
& .venv\Scripts\python.exe evals\product_lifecycle\run_m9r_direction_eval.py `
  --env-file <受控-env-file> --out <report-a.json>
& .venv\Scripts\python.exe evals\product_lifecycle\run_m9r_direction_eval.py `
  --env-file <受控-env-file> --out <report-b.json>
```

## 8. 迁移与灾备

- v36/v39 属于 M9-R；v37/v38 属于 M10-R。
- v39 不重建订单头，只回填订单行 item 归属；订单自然键仍为
  `(tenant, connector, store, external_order_id)`。
- 升级前用 v36 程序停写并验证旧备份；升级后恢复写入前用 v39 程序生成、验证新全量备份。
- 灾备 manifest 精确匹配 schema；旧 v36 归档不能直接由 v39 恢复。
- 后续合并 M10-R 必须保留 36、37、38、39 全部方法与初始化成员，禁止整块取 ours/theirs。

## 9. 签署边界

正式结果只能是“通过”或“不通过”，并固定远端 Head、Base、环境、命令、计数、skip、
mutation、浏览器和 live 报告。通过只覆盖 M9-R 代码级、本机/受控模型范围，不代表真实
平台数据、平台因果、平台权重、长稳、异机灾备、生产发布或任何平台写能力已放行。
