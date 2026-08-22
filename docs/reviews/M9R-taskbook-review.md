# M9-R 以任务书要求审查报告（agentops-awesome-list + Workflow 深度审查）

> **审查对象**：PR #19 `feature/m9r-read-model` @ `0302c1a`（第 5 轮修复后）
> **审查依据**：`docs/tasks/M9R_PRODUCT_TRAFFIC_LIFECYCLE_WORKBENCH.md` WP1-WP5 验收标准
> **审查方法**：agentops-awesome-list（T3 架构基线）+ 4 维度 Workflow 深度审查 + 对抗验证
> **审查时间**：2026-08-22

---

## 审查任务书

- **审查范围（含）**：`product_read_model/`、`product_diagnosis/`、`product_lifecycle/`、`product_workbench/`、`workbench_api.py`、`business/service.py` M9-R 部分、`docs/admin-console.html` 工作台
- **审查范围（不含）**：客服 Agent 拓扑、库存/订单写入路径、营销/财务模块、M10-R（并行工作流）
- **必查组件**：M9R 任务书 WP1-WP5 验收标准逐条 + `references/complete-agent-architecture.md` T3 基线
- **必跑测试（实际跑了）**：
  - 全量回归 **1273 passed**（21:02，串行单进程）`[test]`
  - 浏览器 **4 passed**（工作台渲染/生成建议落库/桌面窄屏无溢出）`[test]`
  - 定向 35 passed（R2/R3/R5 核心路径）`[test]`
  - R1 遗留 bug（SQL # 注释）修复后 58+39 passed `[test]`
- **基线对照**：`git diff 454b35c..HEAD` 净增（+16508/-34，无 merge-loss）`[baseline]`
- **已知决策（用户已拍板）**：net_sales 多行→MISSING；R5 方案 C-lite；R3 结构化 degradation_reasons；验收框架学闫睿涵思路不照搬 M10-R

---

## 体检结论

- **判定**：`risky`（第 5 轮修复后，距 ready 差 4 个 P0 缺陷修复）
- **适用模板**：**T3 Production Project**（多租户 + 人工审核状态机 + 业务写屏障 + 独立 WP5 验收门槛）
- **一句话结论**：负责人 7 个阻断项的**修复代码基本正确**（全量 1273 全绿 + 浏览器 4 passed + mutation 红绿均为真），但深度审查暴露 **14 个全量回归没抓到的缺陷**，其中 4 个（query.py 来源 CTE 口径/确定性、_product_mapping 跨 connector）直接关联阻断项 2 的修复质量，可能被复验再抓；阻断项 5 的"生产可达"未真正达成。
- **置信度**：`high`（对抗验证逐代码核实 + 全量回归真实运行）

---

## 任务书 WP1-WP5 验收标准逐条对照

### WP1 经营读模型（L138-144）

| 验收标准 | 状态 | 证据 | 问题 |
|---|---|---|---|
| 同一 item 多 SKU / 同 SKU 多 revision / 同租户多店不串数 | ⚠️ 部分 | `_order_facts` 用 `l.sku_id` 过滤聚合，但 **latest CTE 无 sku_id 过滤**（query.py:342） | 来源行可能取自同 item 其他 sku 的订单，evidence_state/来源归属错配 |
| 日/月、店铺/商品、支付/退款不同粒度不静默相加 | ✅ | period_key/granularity 物理隔离 | 无 |
| 店铺级指标不广播成 SKU | ✅ | `_sku_item_count` 多 item 不广播 | 无 |
| 缺广告/竞品/退款明细时阻断依赖结论 | ✅ | `_missing_*` + 独立 reason | 无 |
| 每值可回溯权威服务/import/data_as_of | ⚠️ 部分 | 来源同源 CTE 已改全局 LIMIT 1，但 **ORDER BY 无唯一尾键**（query.py:347/273） | 同戳同 version 平局时归属不确定 |
| **料号引用（L133）** | ⚠️ 部分 | `_product_mapping` 不带 connector_id 过滤（query.py:525） | 跨 connector 按 version 取最大，operational 撤销可能被 demo 高 version 掩盖 |

### WP2 证据桥接与流量诊断（L244-247）

| 验收标准 | 状态 | 证据 | 问题 |
|---|---|---|---|
| 统一查询经营事实 + M5-R revision/experiment/freshness | ✅ | EvidenceBridge + GateEngine | 无 |
| 物理区分真实与 Demo，标签贯穿 | ✅ | source_type_from_connector + DEMO/ACTUAL | 无 |
| 曝光/点击/转化不足 + 污染诊断 | ✅ | DiagnosisType 6 类 + 污染自动反推 | **污染类型校验未锁子类型与证据对应**（diagnosis.py:192） |
| 模型只解释证据不修改数值/Gate | ✅ | D-034 边界 | 无 |

### WP3 生命周期建议（L362-366）

| 验收标准 | 状态 | 证据 | 问题 |
|---|---|---|---|
| 建议默认 draft，人工批准/拒绝 | ✅ | 状态机 DRAFT→APPROVED/REJECTED | 无 |
| 存量标题/主图默认 keep/observe | ✅ | KEEP_OBSERVE 默认 + 备选 EXPERIMENT | 无 |
| 缺成本不出正式利润安全价格 | ✅ | REQUIRED_FACTS + PRICING 降级 | 无 |
| 重放不重复创建；旧建议 stale | ✅ | 幂等 + STALE 状态 | 无 |
| 语义建议由模型产生，代码不替代 | ⚠️ 部分 | **SELECTION/NEW_LAUNCH/CLEARANCE 生产不可达**（engine.py:311） | 三类建议在生产恒降级，"发现真实方向"只 eval 注入时成立 |

### WP4 工作台与机制 Eval（L473-477）

| 验收标准 | 状态 | 证据 | 问题 |
|---|---|---|---|
| 页面下钻到 revision/时间窗/指标/来源/建议依据 | ✅ | HTML/JS 已补 + 浏览器 4 passed | 无 |
| 显示为什么建议/为什么不建议 | ✅ | why_not_recommended + evidence_facts | 无 |
| 查看页面无隐式分析/写动作；显式点击审计 | ✅ | 浏览器操作契约 + db.audit | 无 |
| Eval 发现真实方向 + 拒绝污染方向 | ⚠️ 部分 | DIRECTION_SCENES + mock 解释器；**但测试只证 plumbing 不证发现**（test:276）；**frozen Diagnosis 原地修改**（eval.py:99） | "发现真实方向"仍未被真实语义层证明；冻结契约被破坏 |
| **缺数据场景 Eval 一致性** | ⚠️ | **「缺数据」expected.degraded=False 与生产默认路径矛盾**（scenes.py:166） | 场景预期与生产行为不一致 |

### WP5 独立验收（L585-588）

| 验收标准 | 状态 | 证据 | 问题 |
|---|---|---|---|
| WP1-WP4 完成条件、Eval、浏览器、隔离、回归均有独立证据 | ⚠️ 部分 | 全量 1273 + 浏览器 4 passed 真实运行 | **验收报告无第 5 轮复验记录，head 落后**（M9R-WP5-ACCEPTANCE-REPORT.md:1） |
| 至少一项关键反例或 mutation 破坏边界时失败、还原后恢复 | ✅ | R2-1/R2-2 mutation 红绿已验证 | 无 |
| 失败清单、修复差异、复验记录完整 | ⚠️ | 第 4 轮复验记录完整，第 5 轮未补 | 待补 |
| 不把模拟数据冒充真实 | ✅ | DEMO/ACTUAL 物理隔离 | 无 |

---

## 关键问题（按验收影响排序）

| 优先级 | 问题 | 为什么重要 | 关联 |
|---|---|---|---|
| **P0** | query.py:342 `_order_facts` latest CTE 无 sku 过滤 + NULL 行 | 来源行可能取自同 item 其他 sku → evidence_state/来源归属错误 | 阻断项 2c 未完全修复 |
| **P0** | query.py:347/273 CTE ORDER BY 无唯一尾键 | 同戳同 version 平局 → 来源归属不确定 | 阻断项 2c 的确定性未完全达成 |
| **P0** | query.py:525 `_product_mapping` 跨 connector 按 version 取最大 | operational 撤销被 demo 高 version 掩盖 | 阻断项 2b 新暴露 |
| **P0** | engine.py:311 SELECTION 生产不可达 | "发现真实方向"只在 eval 测试成立，生产恒降级 | 阻断项 5 未真正达成 |
| **P1** | scenes.py:166 缺数据场景 degraded=False 与生产矛盾 | Eval 不能反映真实生产路径 | 阻断项 5 相关 |
| **P1** | eval.py:99 frozen Diagnosis 原地修改 | 冻结契约被破坏 | 阻断项 5 相关 |
| **P1** | 验收报告无第 5 轮复验记录 | 复验不可复现 | 阻断项 7 未完全闭环 |
| **P2** | workbench_api.py:96 call() 死代码 | 代码可信度 | — |
| **P2** | 测试质量：#11 排序键无法区分、#12 多行订单数据不匹配、#13 只证 plumbing | 防假绿 | 阻断项 5 相关 |

---

## 优化建议

| 优先级 | 建议 | 预期收益 | 实施成本 | 验收方式 |
|---|---|---|---|---|
| P0 | `_order_facts` CTE 加 `sku_id` 过滤 + 单 item 时含 NULL 共享行（对齐聚合作用域） | 来源行与聚合行一致，证据归属正确 | 低 | 加跨 SKU 反例测试 |
| P0 | CTE ORDER BY 加唯一尾键（如 `id DESC`） | 来源归属确定 | 低 | 加同戳平局测试 |
| P0 | `_product_mapping` 按 connector_id 过滤（对齐 M7-R get_latest_mapping） | revoked 不被跨 connector 掩盖 | 低 | 加多 connector 测试 |
| P0 | 生产链注入 SELECTION 信号（模型解释器写回 evidence_facts）或明确标注"V1 仅 Eval 演示" | "发现真实方向"生产可达或诚实标注 | 中 | 生产链路探针 |
| P1 | 「缺数据」场景 expected 对齐生产 degraded=True | Eval 反映真实 | 低 | 改场景 + 断言 |
| P1 | eval.py 不原地改 frozen Diagnosis，改为构造新 Diagnosis | 冻结契约保持 | 低 | 测试断言 frozen |
| P1 | 补第 5 轮复验记录到验收报告 | 复验可复现 | 低 | 文档检查 |
| P2 | 删 call() 死代码 | 代码可信 | 低 | lint |

---

## 架构地图（delta）

| 模块 | 当前证据 | 评分 | 说明 |
|---|---|---|---|
| `product_read_model/query.py` | 全量回归绿 + 4 P0 缺陷确认 | **adequate** | R2 修复基本正确，但来源 CTE 口径/确定性/mapping 跨 connector 需修 |
| `product_diagnosis/diagnosis.py` | 污染自动反推 + fail-closed | **adequate** | 污染类型校验未锁子类型 |
| `product_lifecycle/engine.py` | REQUIRED_FACTS + 透传 | **adequate** | SELECTION 生产不可达 |
| `product_workbench/scenes.py + eval.py` | 方向场景 + mock 解释器 | **weak** | 缺数据场景矛盾 + frozen 原地修改 + 测试只证 plumbing |
| `workbench_api.py` | 生产链完整 | **strong** | call() 死代码小瑕疵 |
| `business/service.py` | degradation_reasons 结构化 | **strong** | 无 |

---

## 下一步（修复优先级）

1. **修 4 个 P0**（query.py CTE sku 过滤 + ORDER BY 尾键 + mapping 跨 connector + engine 生产注入）——直接影响复验是否再抓
2. **修 3 个 P1**（缺数据场景、frozen 原地修改、验收报告第 5 轮记录）
3. **补测试**（跨 SKU 反例、同戳平局、多 connector、生产可达探针）
4. 修复后**重跑全量回归 + 浏览器**，再更新 PR #19

---

**报告保存位置**：`docs/reviews/M9R-taskbook-review.md`
**源代码/配置/运行时文件未改动**（本报告是只读审查输出）
