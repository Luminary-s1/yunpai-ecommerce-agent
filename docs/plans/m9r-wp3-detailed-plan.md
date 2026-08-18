# M9-R WP3：商品生命周期建议、人工确认与跟踪 — 详细执行规划

> 存放位置：`docs/plans/`
> 状态：待 WP2 收口合入后开工（串行 + 门禁）
> 前置：WP1（读模型，27 passed）+ WP2（诊断/实验）接口冻结
> 负责人：胡磊；验收：闫睿涵（WP5）
> 引用：[m9r-complete-plan.md](m9r-complete-plan.md) 第四节（依赖）/ 第五节 WP3（验收表）

---

## 一、WP3 目标

基于固化经营事实（WP1 读模型）与流量诊断（WP2），生成**版本化生命周期建议**，人工确认后才生效。

```
WP1 读模型 + WP2 诊断 ──> schemas.py 建议类型注册表 ──> state_machine.py 状态机
                              │                              │
                              └──> validation.py 校验/写屏障 └──> interface.py M10-R 契约
```

**交付物**：`src/ecommerce_agent/product_lifecycle/` 包 + 6 个测试文件。

---

## 二、开工前门禁（输入已核实）

| 门禁项 | 状态 | 证据 |
|---|---|---|
| WP1 + WP2 接口冻结 | ⏳ 待 WP1/WP2 收口 | 读模型字段名、诊断类型名不再变 |
| M10-R 契约字段评审 + 冻结 | ✅ 已冻结 | 缪海南 5 点评审落地（test_m9r_m10_contract.py，8 用例） |
| v36 schema 占号获批 | ✅ 已获批 | 占号 PR #18 已合并（v35 归 M7-R WP3，v36 归 M9-R WP3） |
| B1/B2/B3 反例测试承载文件 | ✅ 计划已定 | keep_default / write_barrier / alternatives |

---

## 三、代码结构（详细）

```
src/ecommerce_agent/product_lifecycle/
  __init__.py          # 导出全部
  schemas.py           # 建议类型注册表
                       #   选品/上新/保持/诊断/实验/定价候选/活动候选/补货联动/清仓预警
                       #   每条建议必须带 alternatives（B3）
                       #   required_facts: dict[RecommendationType, list[str]] 前置依赖
  state_machine.py     # draft → awaiting_review → approved/rejected → observed → closed
                       #   approved 不触发平台动作（B2）
                       #   事实更新 → 旧建议标 stale
  validation.py        # 模型输出校验 + 写屏障 + 类型/证据/状态校验
                       #   禁止键清单 + alternatives 强制 + 幂等
  interface.py         # 建议输出契约（M10-R 消费侧）
tests/
  test_m9r_lifecycle_state_machine.py
  test_m9r_lifecycle_validation.py
  test_m9r_lifecycle_idempotency.py
  test_m9r_lifecycle_write_barrier.py      # B4 反例：批准不触发平台动作
  test_m9r_lifecycle_alternatives.py       # B3 反例：建议必须有备选路径
  test_m9r_lifecycle_keep_default.py       # B1 反例：存量标题/主图默认不改
```

---

## 四、关键设计决策

### 4.1 建议类型注册表（任务书链条）

```
选品候选 → 上新准备 → 曝光/点击诊断 → 受控实验或保持观察
  → 定价/活动候选 → 补货联动 → 清仓预警或退役建议
```

- 每种建议类型带 `required_facts` 前置依赖（如：补货建议需库存事实；定价建议需成本准备度）
- 缺前置事实 → 建议降级（`degraded: true` + `missing_evidence`），不输出具体结论

### 4.2 状态机（人工确认门）

```
draft → awaiting_review → approved / rejected → observed → closed
```

- **approved 不触发平台动作**（B2 反例测试锁死）
- 事实更新 → 旧建议标 stale，不原地改写历史（重放幂等）
- 每次状态流转写审计记录：`(actor, at, action, target, from_state, to_state)`

### 4.3 M10-R 接口预留

```python
class RecommendationOutput(BaseModel):
    recommendation_id: str
    type: RecommendationType          # 9 种建议类型
    target: TargetObject              # store_id / item_id / sku_id
    facts_snapshot: dict[str, Any]    # 事实快照（引用来源）
    rationale: str                    # 模型理由
    missing_evidence: list[str]       # 缺失项
    alternatives: list[Recommendation]  # 备选路径（B3）
    state: RecommendationState        # 状态机
    created_at: datetime
    updated_at: datetime
```

- **第 4 周向缪海南发起字段评审，第 5 周冻结**；超时未回复 → 单方冻结 V0（标注 unconfirmed-by-consumer）

### 4.4 持久化（v36，质量红线）

- 建议记录 + 审计记录**必须持久化**（服务重启不丢、工作台跨请求可查）
- 走 v36 迁移（`product_recommendations` + `product_recommendation_audit` 表）
- **WP3 开工前必须占号获批**；未获批不开工（不降级为内存）
- 已获批（占号 PR #18）：v35 归 M7-R WP3 Product Identity，v36 归 M9-R WP3。**本任务交付表结构（`_apply_v36`），业务写入方为后续独立工作包「WP3 持久化读写服务」**（接表后 recommendations 落库 / audit 落库，读侧工作台与复验查询）

---

## 五、验收表（WP3，8 条，对齐主计划）

| # | 验收条目 | 状态 | 验证方式 |
|---|---|---|---|
| 1 | 建议默认 draft，人工批准才生效 | ✅ | 状态机测试（test_m9r_lifecycle_state_machine.py，8 用例） |
| 2 | 批准不触发平台写动作 | ✅ | B2 反例测试（test_m9r_lifecycle_write_barrier.py） |
| 3 | 存量标题/主图默认 keep/observe | ✅ | B1 反例测试（test_m9r_lifecycle_keep_default.py） |
| 4 | 缺成本/缺竞品时结论按证据降级 | ✅ | degraded + missing_evidence 断言（test_m9r_lifecycle_validation.py，5 用例） |
| 5 | 重放幂等，旧建议标 stale | ✅ | 幂等测试（test_m9r_lifecycle_idempotency.py，3 用例） |
| 6 | 每条建议带备选路径（上新/实验） | ✅ | B3 反例测试（test_m9r_lifecycle_alternatives.py，3 用例） |
| 7 | 建议输出契约可被 M10-R 消费 | ✅ | interface.py + 缪海南 5 点评审落地（test_m9r_m10_contract.py，8 用例） |
| 8 | 完整建议链条覆盖（选品→清仓） | ✅ | 类型注册表测试（test_m9r_lifecycle_validation.py） |

> 收口证据：上述 7 个测试文件 37 passed in 8.81s（2026-08-18，分支 feature/m9r-read-model）。持久化表结构 v36 已交付（PR #19），业务写入方为后续独立工作包「WP3 持久化读写服务」。

---

## 六、负责人关注点（WP3）

**可交付**：`product_lifecycle/` 包 + 6 个测试，全部跑绿。

**可验证**（闫睿涵 WP5 必测）：
- 所有建议带 `alternatives` 字段（B3 反例 `test_alternative_path_present`）
- approved 状态不触发平台写操作（B2 反例 `test_approved_no_platform_action`）
- 状态机顺序严格；重放幂等；缺成本降级
- 每条建议可追溯到事实快照 + 审计记录

**复用边界**：做=状态机/注册表/校验/幂等；不做=不改标题/主图/价格、不触发平台动作；新增=schemas/state_machine/validation/interface。

**无风险**：批准不触发平台动作；事实更新旧建议标 stale，历史不丢失。

---

## 七、周级拆解（WP2 合入后 2 周）

| 周 | 任务 | 完成标志 |
|---|---|---|
| 第 4 周 | 写 schemas.py（注册表 + alternatives + required_facts）；**向缪海南发起 RecommendationOutput 评审** | 注册表测试 PASS；评审已发起 |
| 第 4 周 | 写 state_machine.py（状态机 + 审计） | 状态机测试 PASS |
| 第 4 周 | 写 validation.py（校验 + 写屏障 + 幂等） | 校验测试 PASS |
| 第 5 周 | 写 6 个测试；跑绿；**M10-R 契约字段冻结** | 6 passed + 契约冻结 |
| 第 5 周 | 边界说明文字（B1/B2/B4）写入 boundaries.py 占位 | 降级测试 PASS |
| 第 5 周 | L2 上游契约回归 + 全量回归（WP3 收口） | 无回归 |

**WP3 收口门禁**：测试全绿 + 8 条验收状态固化 + alternatives 强制 + 批准不触发平台动作 + M10-R 契约冻结。

---

## 八、WP3 收口回归证据（占位）

> 按 `m9r-complete-plan.md` 第十节「回归证据规范」填写。

- 执行时间：_待填_
- 命令：`python scripts/run_full_regression.py --allow-dirty`
- M9-R WP3 测试：_待填（6 个测试文件）_
- 全量回归：_待填（{N} passed）_
- 报告：`pytest_debug_report.json`
- 状态：⏳ 待 WP3 开工

---

## 九、WP3 依赖与风险

| 依赖/风险 | 状态 | 预案 |
|---|---|---|
| v36 schema 占号 | ✅ 已获批 | 占号 PR #18 已合并；未获批不开工（质量红线） |
| M10-R 契约字段冻结 | ✅ 已冻结 | 缪海南 5 点评审落地（test_m9r_m10_contract.py，8 用例） |
| 缺成本/缺竞品 | ✅ 已实现 | 降级：`degraded: true` + `missing_evidence`，不出具体数字（验收表 4 已固化） |
| 存量标题/主图默认不改 | ✅ 计划已锁 | B1 反例测试 `test_m9r_lifecycle_keep_default.py` |
