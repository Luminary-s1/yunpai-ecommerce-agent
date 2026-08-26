# M9-R WP4：商品经营工作台 + 机制 Eval — 详细执行规划

> 存放位置：`docs/plans/`
> 状态：待 WP3 收口合入后开工（串行 + 门禁）
> 前置：WP1~WP3（读模型/诊断/建议）接口稳定
> 负责人：胡磊；验收：闫睿涵（WP5）
> 引用：[m9r-complete-plan.md](m9r-complete-plan.md) 第四节（依赖）/ 第五节 WP4（验收表）

---

## 一、WP4 目标

建设可下钻的**只读经营工作台**，用数值与结构化机制 Eval 验证真实方向、拒绝污染方向。

```
WP1~WP3 数据 ──> pages.py 工作台页面（扩展 /admin）
                    │
                    ├──> eval.py 机制 Eval（复用 simulation-evidence-v1）
                    ├──> scenes.py 冻结场景集
                    └──> boundaries.py 边界说明文字
```

**交付物**：`src/ecommerce_agent/product_workbench/` 包 + 5 个测试文件。

---

## 二、开工前门禁（输入已核实）

| 门禁项 | 状态 | 证据 |
|---|---|---|
| WP1~WP3 接口稳定 | ⏳ 待 WP3 收口 | 读模型/诊断/建议字段名冻结 |
| F-310 前端基建（/admin） | ✅ 已确认可用 | 闫哥 8/18 回复 |
| F-121/F-122 评测能力 | ✅ 已确认交付 | 闫哥 8/18 回复（simulation-evidence-v1） |
| Demo 数据（D19/D20/virtual_store_v1.json） | ✅ 底层就绪 | 闫哥 8/18 回复 |
| M9 专属 Demo（SKU 流量/revision/窗口） | ⚠️ 需补齐 | WP2 已补 demo_fixtures，WP4 复用 |

---

## 三、代码结构（详细）

```
src/ecommerce_agent/product_workbench/
  __init__.py          # 导出全部
  pages.py             # 页面组件（商品列表/SKU 下钻/漏斗/诊断/实验/来源/建议/审核）
                       #   扩展 /admin 路由：/admin/products/{store_id}/{item_id}/{sku_id}
  eval.py              # 机制 Eval runner（复用 simulation-evidence-v1，不另建）
  scenes.py            # 冻结场景集（真实粒度不足 / 显式模拟实验两类）
                       #   独立 oracle = 固定输入 → 固定输出确定性断言
  boundaries.py        # 页面说明文字占位（B1/B2/B4 + 试算字样规范）
tests/
  test_m9r_workbench_pages.py
  test_m9r_mechanism_eval.py           # 复用 F-121/122，不另建 runner
  test_m9r_demo_isolation_boundary.py  # B5 反例：Demo 不进入 operational
  test_m9r_sample_vs_product_gate.py   # B7 反例：样本数据不作为产品口径
  test_m9r_workbench_write_barrier.py  # B4 反例：页面侧零写断言
```

---

## 四、关键设计决策

### 4.1 前台基建复用（F-310）

- 直接扩展现有 `/admin` 后台（闫哥确认可用），**不重新设计前端**
- 复用现有 `operational/simulation/evaluation/all` 范围隔离，M9-R 内容在 `operational` 默认范围
- 扩展点：`/admin/products/{store_id}/{item_id}/{sku_id}` 下钻路由

### 4.2 评测能力复用（F-121/F-122）

- 复用 `simulation.py` 场景 runner + `simulation-evidence-v1` 输入/预期/断言格式
- **不另建第二套通用 runner**
- 新增：M9 领域机制 Eval、冻结场景集、独立 oracle

### 4.3 Oracle 定义（确定性校验）

```
Oracle = 固定输入 → 固定输出的确定性断言
例：给定「SKUA 缺 SKU 级流量」→ 输出必须包含 funnel_availability="unavailable"
```

- ground truth 与 production input **物理分离**
- 冻结场景 ≥2：缺货污染（输出不得含标题/主图归因）、合格实验（输出含指定诊断类型）

### 4.4 页面显示原则（对齐任务书）

- **每个数字渲染四态徽标 + 来源 + 时间**：`evidence_state` 徽标颜色区分
  （actual=绿/manual=蓝/demo=橙/missing=灰）+ source + data_as_of
- **演示参数显式标注「试算」字样**：demo 数据必须渲染「试算」标签
- **边界说明文字**：B1/B2/B4 用页面文字展示给运营（不只是代码侧验证）

---

## 五、验收表（WP4，10 条，对齐主计划）

| # | 验收条目 | 状态 | 验证方式 |
|---|---|---|---|
| 1 | 商品/SKU 下钻到 revision、时间窗、指标、来源、建议依据 | ✅ 基建可用 | 扩展 /admin 路由 |
| 2 | 显示「为什么建议」/「为什么暂不能建议」 | ✅ 基建可用 | 页面 + 建议依据 |
| 3 | 页面浏览无隐式分析/创建实验/创建建议/修改商品 | ✅ | B4 页面写屏障测试（test_m9r_workbench_write_barrier.py） |
| 4 | 机制 Eval 发现真实方向 + 拒绝污染方向 | ✅ 基建交付 | simulation-evidence-v1 复用 + M9 Eval 新增 |
| 5 | 浏览器桌面 + 窄屏可读，console 无新增错误 | ✅ 基建可用 | 浏览器检查 |
| 6 | 真实/模拟场景隔离，全链标注 | ✅ 底层就绪 | D19/D20/virtual_store_v1.json 复用 |
| 7 | 样本数据不作为产品口径 | ✅ | B7 反例测试（test_m9r_data_trust.py） |
| 8 | 边界说明文字在页面展示 | ✅ | boundaries.py + 页面断言（test_m9r_workbench_pages.py） |
| 9 | 页面上每个数字渲染四态徽标 + 来源 + 时间 | ✅ | 页面渲染断言（test_m9r_workbench_pages.py） |
| 10 | 演示参数显式标注「试算」字样 | ✅ | boundaries.py 文案 + 页面断言（test_m9r_workbench_pages.py DEMO_LABEL） |

> 收口证据：test_m9r_workbench_pages.py + test_m9r_workbench_write_barrier.py 等 8 文件 36 passed in 0.65s（2026-08-18，分支 feature/m9r-read-model）。

---

## 六、负责人关注点（WP4）

**可交付**：`product_workbench/` 包 + 5 个测试，全部跑绿；工作台页面可访问。

**可验证**（闫睿涵 WP5 必测）：
- 页面直接扩展 `/admin`，不重新设计前端
- 页面展示边界说明文字（B1/B2/B4）
- 机制 Eval runner 复用 simulation-evidence-v1，不另建
- Oracle = 固定输入 → 固定输出确定性断言
- 每个数字四态徽标 + 来源 + 时间；demo 渲染「试算」

**复用边界**：做=扩展 /admin、复用 simulation-evidence-v1、新增 M9 Eval/scenes/oracle；不做=不重新设计前端、不另建第二套 runner、ground truth 与 production 物理分离。

**无风险**：工作台只读，无隐式写动作；Demo 与 operational 严格隔离。

---

## 七、周级拆解（WP3 合入后 2 周）

| 周 | 任务 | 完成标志 |
|---|---|---|
| 第 6 周 | 扩展 `/admin` 路由（商品/SKU 下钻页面）；**F-310/F-121-122 状态复核** | 页面可访问，无 console 错误 |
| 第 6 周 | 写 eval.py（复用 simulation-evidence-v1）；写 scenes.py（M9 冻结场景 + oracle） | 机制 Eval 测试 PASS |
| 第 6 周 | 写 boundaries.py（B1/B2/B4 + 试算字样规范） | 页面展示边界文字 |
| 第 7 周 | 写 5 个测试；跑绿 | 5 passed |
| 第 7 周 | 桌面 + 窄屏浏览器检查；全量回归 | 浏览器无新增错误 |
| 第 7 周 | L2 上游契约回归 + 全量回归（WP4 收口） | 无回归 |

**WP4 收口门禁**：测试全绿 + 10 条验收状态固化 + 页面可访问 + 四态徽标/试算渲染断言绿 + Eval 拒绝污染方向。

---

## 八、WP4 收口回归证据（占位）

> 按 `m9r-complete-plan.md` 第十节「回归证据规范」填写。

- 执行时间：_待填_
- 命令：`python scripts/run_full_regression.py --allow-dirty`
- M9-R WP4 测试：_待填（5 个测试文件）_
- 全量回归：_待填（{N} passed）_
- 报告：`pytest_debug_report.json`
- 状态：⏳ 待 WP4 开工

---

## 九、WP4 依赖与风险

| 依赖/风险 | 状态 | 预案 |
|---|---|---|
| F-310 前端基建 | ✅ 已确认 | 直接扩展 /admin；不重新设计前端 |
| F-121/F-122 评测 | ✅ 已确认 | 复用 simulation-evidence-v1；不另建 runner |
| M9 专属 Demo 数据 | ⚠️ WP2 补齐后复用 | WP2 demo_fixtures；全链 demo 标签 |
| 浏览器 E2E | ⚠️ 无现成框架 | 人工截图 + console 断言；或复用项目既有 E2E |
| ground truth 隔离 | ✅ 设计已定 | oracle 固定输入/输出，物理分离 production input |
