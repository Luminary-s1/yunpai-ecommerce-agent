# M9-R WP2：M5-R 证据桥接 + 流量诊断 + 受控实验 — 详细执行规划

> 存放位置：`docs/plans/`
> 状态：待 WP1 收口合入后开工（串行 + 门禁）
> 前置：WP1（读模型骨架，27 passed）合入 main、接口冻结
> 负责人：胡磊；验收：闫睿涵（WP5）
> 引用：[m9r-complete-plan.md](m9r-complete-plan.md) 第四节（依赖）/ 第五节 WP2（验收表）

---

## 一、WP2 目标

基于 WP1 读模型（store+item+SKU），桥接 M5-R 权威证据，构建**结构化流量诊断** + **受控实验入口**。

```
WP1 读模型 ──> bridge.py 桥接 M5-R 证据 ──> gates.py 确定性 Gate ──> diagnosis.py 结构化诊断
                                                    │
                                                    └──> experiment.py 受控实验（Demo 路径）
```

**交付物**：`src/ecommerce_agent/product_diagnosis/` 包 + 5 个测试文件 + Demo 数据补齐。

---

## 二、开工前门禁（输入已核实）

| 门禁项 | 状态 | 证据 |
|---|---|---|
| WP1 合入 main + 接口冻结 | ⏳ 待 WP1 收口 | `product_read_model` 字段名/类型名冻结 |
| M5-R `TrafficExperimentCreate` 无 demo/scope 字段 | ✅ 已核实 | traffic_lab/models.py：无 scope/demo |
| freshness/provenance 4 个持久化位置 | ✅ 已核实 | 闫哥 8/18 回复 + evidence_freshness.py / provenance.py |
| Demo 底层 `VirtualStoreSimulation` 强制隔离 | ✅ 已核实 | simulation.py：`confirm_virtual: Literal[True]` + fixture `"virtual": True` |
| `TrafficAnalysisEngine.analyze_experiment` 含 Gate | ✅ 已核实 | traffic_lab/analysis.py 第 191 行 |
| M9 专属 Demo 数据（SKU 流量/revision/窗口） | ⚠️ 需补齐 | WP2 第 3 周补 demo_fixtures |

---

## 三、代码结构（详细）

```
src/ecommerce_agent/product_diagnosis/
  __init__.py          # 导出全部
  bridge.py            # M5-R evidence → 统一只读查询
                       #   TrafficLabService: get_revision/list_revisions/get_experiment/list_analysis_runs
                       #   freshness: evidence-freshness-v1（Forecast/Plan 读模型根字段）
                       #   provenance: 4 个持久化位置（见下）
  gates.py             # 确定性 Gate 组合
                       #   A/A / 样本量 / 实际窗口 / 控制变量 / freshness / 污染
                       #   复用 TrafficAnalysisEngine 统计，本层只做确定性判定
  diagnosis.py         # 结构化诊断类型
                       #   曝光不足 / 点击不足 / 转化不足 / 缺货污染 / 广告价格污染 / 证据不足
  experiment.py        # 受控实验双路径
                       #   run_demo_experiment()  走 VirtualStoreSimulation
                       #   create_real_experiment() 预留 + blocked（不开通）
tests/
  test_m9r_diagnosis_gates.py
  test_m9r_diagnosis_bridge.py
  test_m9r_demo_isolation.py          # Demo 不进默认经营视图
  test_m9r_controlled_experiment.py   # 双路径入口
  test_m9r_write_barrier.py           # B4 平台写=0 + 内部写白名单
```

**freshness/provenance 桥接的 4 个持久化位置**（闫哥确认）：
| 数据域 | source_provenance 路径 |
|---|---|
| Traffic | `evidence_json.source_provenance` |
| Demand | `lineage_json.source_provenance` |
| Forecast | `candidate_models_json.source_provenance` |
| Plan | `forecast_evidence_json.source_provenance` |

---

## 四、关键设计决策

### 4.1 受控实验双路径（防返工核心）

> 交叉验证结论（开工前已核实）：`TrafficExperimentCreate` 无 scope/demo 字段，
> 直接调用 `create_experiment` 写真实 `traffic_experiments` 表——Demo 实验绝不能走这条路。

| 路径 | 入口 | 数据 | 场景 |
|---|---|---|---|
| **Demo** | `VirtualStoreSimulation`（confirm_virtual 强制） | 隔离虚拟数据 + demo 标签 | SKU 级流量诊断/实验演示（任务书 V1） |
| **真实** | `TrafficLabService.create_experiment` | 真实 revision + 窗口 | 真实数据齐备后 |

- `experiment.py` 两条路径分开封装，**入口强制显式声明路径**，不自动选择
- **本阶段只实现 Demo 路径**；真实路径仅预留接口 + `blocked` 标记

### 4.2 Gate 确定性

- 复用 `TrafficAnalysisEngine.analyze_experiment`（统计逻辑不重写）
- `gates.py` 只做确定性判定：`GateResult(passed: bool, reason: str | None)`，无模型调用
- 越权输出禁止键清单：`{effect, interval, sample_size, gate, "平台权重", "平台算法"}` → 命中即整体拒绝

### 4.3 Demo 隔离

- 用 M7-R `DataScope`（OPERATIONAL/DEMO/ALL），Demo 数据显式带 `DataScope.DEMO`
- 所有查询默认 operational 范围，不返回 demo 数据

---

## 五、验收表（WP2，12 条，对齐主计划）

| # | 验收条目 | 状态 | 验证方式 |
|---|---|---|---|
| 1 | 桥接 M5-R revision/experiment/analysis 证据 | ✅ 可设计 | bridge.py 复用接口测试 |
| 2 | 桥接 freshness/provenance 证据 | ✅ 可设计 | 4 个持久化位置读取测试 |
| 3 | 真实/Demo 查询物理隔离 | ⚠️ | demo 隔离测试 |
| 4 | A/A、样本量、实际窗口、控制变量 Gate | ⚠️ | gates 确定性测试 |
| 5 | freshness Gate | ✅ 可设计 | evidence-freshness-v1 |
| 6 | 缺货/广告/价格污染不归因标题/主图 | ⚠️ | 污染 Gate 反证测试 |
| 7 | 模型越权输出整份拒绝 | ⚠️ | 禁止键清单测试 |
| 8 | 无合格实验不编造 uplift | ⚠️ | 无合格实验→观察/补数建议 |
| 9 | 缺 SKU 流量/revision → blocked | ⚠️ | 真实路径 blocked 断言 |
| 10 | 诊断全链平台写=0（内部写白名单） | ⚠️ | B4 反例测试 |
| 11 | 受控实验入口可用（Demo 路径） | ⚠️ | run_demo_experiment 测试 |
| 12 | Demo 隔离不进入默认视图 | ⚠️ | DataScope.DEMO 测试 |

---

## 六、负责人关注点（WP2）

**可交付**：`product_diagnosis/` 包 + 5 个测试 + Demo 数据补齐，全部跑绿。

**可验证**（闫睿涵 WP5 必测）：
- 受控实验**双路径**：Demo 走 `VirtualStoreSimulation`（隔离），真实走 `TrafficLabService`（预留+blocked）
- freshness/provenance 桥接用 4 个持久化位置
- Demo 数据带 `DataScope.DEMO`，不进 operational 视图
- 写屏障双语义：平台写=0 + 内部写白名单

**复用边界**：做=调用 M5-R 接口；不做=不新建实验框架、不重写统计、不改 M5-R 代码；新增=bridge/gates/experiment。

**无风险**：M5-R 统计与 Gate 不动；Demo 与 operational 物理隔离。

---

## 七、周级拆解（WP1 合入后 2 周）

| 周 | 任务 | 完成标志 |
|---|---|---|
| 第 2 周 | 写 bridge.py（4 个持久化位置 freshness/provenance 桥接） | 桥接测试 PASS |
| 第 2 周 | 写 gates.py（复用 TrafficAnalysisEngine，确定性 Gate） | Gate 测试 PASS |
| 第 2 周 | 写 experiment.py（双路径：Demo + 真实 blocked） | Demo 测试 PASS |
| 第 3 周 | 写 5 个测试；跑绿 | 5 passed |
| 第 3 周 | 补 demo_fixtures（≥2 冻结场景：缺货污染/合格实验，复用 virtual_store_v1.json） | Demo 隔离测试 PASS |
| 第 3 周 | L2 上游契约回归 + 全量回归（WP2 收口） | 无回归 |

**WP2 收口门禁**：测试全绿 + 12 条验收状态固化 + Demo/真实双路径落地 + freshness/provenance 落地 + B4 双语义反例绿。

---

## 八、WP2 收口回归证据（占位）

> 按 `m9r-complete-plan.md` 第十节「回归证据规范」填写。

- 执行时间：_待填_
- 命令：`python scripts/run_full_regression.py --allow-dirty`
- M9-R WP2 测试：_待填（5 个测试文件）_
- 全量回归：_待填（{N} passed）_
- 报告：`pytest_debug_report.json`
- 状态：⏳ 待 WP2 开工

---

## 九、WP2 依赖与风险

| 依赖/风险 | 状态 | 预案 |
|---|---|---|
| M9 专属 Demo 数据（SKU 流量/revision/窗口） | ⚠️ 需补齐 | WP2 第 3 周补 demo_fixtures（复用 virtual_store_v1.json 扩展） |
| `TrafficLabService.__init__` 构造参数 | ⚠️ 未读全 | WP2 设计期读代码确认（db + gateway） |
| freshness/provenance 载体 | ✅ 已确认 | evidence_freshness.py + provenance.py |
| M5-R 接口变更 | ✅ 当前稳定 | 若 breaking change → 通知闫睿涵评估 |
