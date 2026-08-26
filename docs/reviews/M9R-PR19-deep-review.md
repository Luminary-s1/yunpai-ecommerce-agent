# M9-R PR #19 深度代码审查报告（Workflow 多维 + 对抗验证）

> **审查对象**：PR #19 `feature/m9r-read-model` @ `0302c1a`（第 5 轮修复后）
> **审查方式**：4 维度 Workflow（数据正确性 / D-034 语义边界 / 生产链路与验收符合性 / 测试防假绿）+ 逐发现对抗性验证
> **证据等级**：16 个发现中 14 个经对抗验证确认真实（isReal=true）

---

## 确认缺陷清单（14 个，按严重度排序）

### P0 — 数据正确性（会导致复验不通过或生产串数）

| # | 位置 | 缺陷 | 后果 |
|---|---|---|---|
| 1 | query.py:342 `_order_facts` CTE | **latest CTE 只按 item_id 过滤，无 sku_id、无单 item 时 NULL 共享行**；与主聚合（`l.sku_id=?` + `item_cond`）作用域不一致 | connector/source 可能取自同 item 其他 sku 的订单 → evidence_state（DEMO/ACTUAL）与 import_manifest_id 归属错误；单 item 时聚合含 NULL 共享行但 CTE 不认，同一 dict 描述不同订单 |
| 2 | query.py:347 `_order_facts` CTE | **ORDER BY 无唯一尾键**（仅 source_updated_at DESC, version DESC）——commerce_orders 无 (source_updated_at, version) 唯一约束，同批同戳同 version 平局，LIMIT 1 任取 | 来源归属不确定，违反注释声称的确定性 |
| 3 | query.py:273 `_inventory_facts` CTE | **ORDER BY 无唯一尾键**——inventory_balances 每仓一行，多仓同戳同 version 时平局 | 来源归属不确定 |
| 4 | query.py:525 `_product_mapping` | **不过滤 connector_id，跨连接器按 mapping_version DESC 取最大**——mapping_version 是每 (tenant,store,connector,sku) 独立序列 | sku-a 在 taobao(operational) v2 revoked，在 pdd(demo) v3 confirmed → 返回 pdd 映射，operational 的撤销被更高 version 掩盖；与 M7-R get_latest_mapping 带 connector 作用域语义偏离 |

### P1 — D-034 语义边界 / Eval 能力

| # | 位置 | 缺陷 | 后果 |
|---|---|---|---|
| 5 | engine.py:311 `_build_facts_snapshot` | **SELECTION/NEW_LAUNCH/CLEARANCE 非降级路径生产不可达**——透传 signal 键但生产链（diagnose/两解释器）从不注入 demand_signal 等；且透传只认裸布尔无来源校验 | 三类建议在生产恒降级，"发现真实方向"只在 eval 测试注入时成立；裸布尔即视为证据满足，无引用可追溯 |
| 6 | scenes.py:166 「缺数据」场景 | **expected.degraded=False 与生产默认路径矛盾**——生产 diagnose() 默认路径对 missing 输入走 _model_unavailable_diagnosis（degraded=True），但冻结场景锁 degraded=False | 场景预期与生产行为不一致，Eval 不能反映真实生产路径 |
| 7 | eval.py:99 `run_scene` | **对 frozen=True 的 Diagnosis 原地 update evidence_facts**——绕过冻结语义，把调用方注入的信号键混入"固化证据" | 冻结契约被破坏，证据来源标签被污染 |
| 8 | diagnosis.py:192 | **污染类型校验未锁定子类型与证据对应**——只要求 stockout 或 pollution 任一成立，不校验 STOCKOUT_POLLUTION 必须对应 stockout | 解释器可返回与证据不符的污染类型 |

### P2 — 生产链路 / 文档 / 测试质量

| # | 位置 | 缺陷 | 后果 |
|---|---|---|---|
| 9 | workbench_api.py:96 | **call() helper 死代码**——定义了异常包装 helper 但 11 个路由全部内联，未消费 | 死代码，误导后续开发者 |
| 10 | M9R-WP5-ACCEPTANCE-REPORT.md:1 | **验收报告无第 5 轮复验记录，head 落后**——标题仍"第 4 轮"，head=1d53871（0302c1a 前一提交） | 第 5 轮修复证据未落文档，复验不可复现 |
| 11 | tests:343 `test_order_source_deterministic_latest` | **测试无法区分排序键 bug**——种子数据 connector 相同、placed_at 与 source_updated_at 顺序相同 | 无法证明修复了来源同源（若实现回退 MAX 拼凑，测试可能仍绿） |
| 12 | tests:320 `test_multi_line_order...` | **多行订单测试数据不匹配语义**——两行均为同 sku-a 且无退款记录 | 没有真正测"多 SKU 订单退款无法归 SKU"的语义 |
| 13 | tests:276 `test_eval_direction_scenes_reachable` | **方向场景正向测试只证 plumbing 不证发现方向**——mock 建议解释器按 sku_id 硬编码返回期望类型 | "发现方向"仍未被真实语义层证明 |

### 被反驳（非缺陷，2 个）

- service.py:284 `degradation_reasons` 错标 evidence_insufficient——**反驳成立**（占位场景 diagnosis_type 本身即 evidence_insufficient，语义正确）
- inventory.py:84 冲突更新无条件写 item_id——**反驳成立**（真正共享 SKU 场景下不构成缺陷）

---

## 与负责人 7 阻断项 / 本轮修复的关系

| 缺陷 | 关联 | 影响评估 |
|---|---|---|
| #1 CTE 缺 sku 过滤 | 阻断项 2c（来源同源）**未完全修复** | 负责人可能复验再抓 |
| #2/#3 ORDER BY 平局 | 阻断项 2c 的"确定性"**未完全达成** | 平局场景下仍非确定 |
| #4 _product_mapping 跨 connector | 阻断项 2b（映射）**新暴露** | 多 connector 场景 revoked 仍复活 |
| #5 SELECTION 生产不可达 | 阻断项 5（Eval 假覆盖）**只修了 eval 测试，生产未达** | "发现真实方向"在真实生产仍不可达 |
| #6/#7/#8 | Eval 一致性/冻结契约/污染校验 | 生产链路语义边界 |
| #9-#13 | 死代码/文档/测试质量 | 代码可信度 |

---

## 结论

**判定：risky**（第 5 轮修复后）
- 负责人 7 阻断项**修复代码基本正确**（定向 35 passed + mutation 红绿 + 全量 1273 passed + 浏览器 4 passed 均为真）
- 但**深度审查暴露 14 个全量回归没抓到的缺陷**，其中 4 个（#1-#4）直接关联阻断项 2 的修复质量，可能被复验再抓；#5 表明阻断项 5 的"生产可达"未真正达成

**置信度**：high（对抗验证逐代码核实）
