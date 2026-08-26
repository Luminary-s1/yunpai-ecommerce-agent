## WP 验收矩阵（M9-R 任务书标准 → 证据）

### WP1 经营读模型（SKU 层）

| # | 任务书验收标准 | 证据 | 结果 |
|---|---|---|---|
| ① | 同一 item 多 SKU / 同 SKU 多 revision / 同租户多店不串数 | test_product_read_query + test_m9r_item_isolation_overlap（重叠窗口跨 item 4 PASS） | ✅ |
| ② | 日/月、店铺/商品、支付/退款不同粒度不静默相加 | test_m9r_query_source_honesty（period_key + granularity 物理隔离） | ✅ |
| ③ | 跨店/跨 SKU/跨 revision/混粒度输入被阻断 | test_m9r_read_model_isolation（13 破坏性隔离） | ✅ |
| ④ | 真实值可追溯（料号/来源/data_as_of） | verify_wp1 ⑧ + test_m9r_query_source_honesty | ✅ |

### WP2 证据桥接与门禁

| # | 任务书验收标准 | 证据 | 结果 |
|---|---|---|---|
| ① | 只有通过全部 Gate 的实验给强方向结论 | test_m9r_gates_production + mutation（gate 失败阻断强诊断） | ✅ |
| ② | 缺货/广告/价格污染不被归因标题/主图 | test_m9r_diagnosis（污染自动反推 + degraded） | ✅ |
| ③ | 无合格实验时不编造 uplift | test_m9r_diagnosis_bridge（显式 missing/blocked） | ✅ |
| ④ | 诊断全链只读，demo 标签不丢失 | test_m9r_demo_isolation + test_m9r_gates_production | ✅ |

### WP3 生命周期建议

| # | 任务书验收标准 | 证据 | 结果 |
|---|---|---|---|
| ① | 建议默认 draft，只有人工可批准/拒绝 | test_m9r_lifecycle_state_machine | ✅ |
| ② | 存量标题/主图默认不改 | test_m9r_lifecycle_keep_default | ✅ |
| ③ | 缺成本不出正式利润安全价格 | test_m9r_lifecycle_validation（REQUIRED_FACTS 降级） | ✅ |
| ④ | 重放不重复创建；旧建议标 stale | test_m9r_lifecycle_idempotency | ✅ |
| ⑤ | 生产语义链闭环（诊断→模型→校验→落库） | test_m9r_production_recommendation_chain（gateway.calls==1 + DRAFT + 审计） | ✅ |

### WP4 工作台与机制 Eval

| # | 任务书验收标准 | 证据 | 结果 |
|---|---|---|---|
| ① | 页面从商品/SKU 下钻到 revision/指标/来源/建议依据 | test_m9r_workbench_browser（Playwright 真实渲染） | ✅ |
| ② | 显示为什么建议/为什么不建议 | test_m9r_workbench_view（why_not_recommended） | ✅ |
| ③ | 浏览页面无隐式写动作；运行显式点击并审计 | test_m9r_workbench_browser（生成按钮显式点击 + 审计） | ✅ |
| ④ | Eval 发现真实方向 + 拒绝污染方向 | test_m9r_mechanism_eval（mutation 锁污染方向） | ✅ |
