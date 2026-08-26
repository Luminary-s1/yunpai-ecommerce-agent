# M9-R WP1～WP4 整体任务计划（胡磊开发范围）

> 存放位置：项目目录 `docs/plans/`
> 状态：待批准执行
> 分支：`feature/m9r-read-model`（新建，基于 origin/main）
> 前置：M7-R WP1 已合入 main（0b54a24，SCHEMA_VERSION=34）
> 分工：WP1～WP4 开发 = **胡磊**；WP5 独立验收 = **闫睿涵**；M10-R 开发 = 缪海南（下游消费方）
> 依赖关系：WP2～WP4 各自可在前置 WP 落地后**并行**推进，不受 M8-R/M10-R 阻塞
> 关联文件：[m9r-wp1-read-model.md](m9r-wp1-read-model.md)（WP1 详细代码/测试）、[m9r-wp2-wp5-next-steps.md](m9r-wp2-wp5-next-steps.md)（WP2～WP5 依赖清单）、[m9r-toolchain-verify.md](m9r-toolchain-verify.md)（工具可执行性核查）

---

## 一、目的（总目标，贯穿全程不丢失）

在**纯电商闭环真实经营场景**中，让链接和 SKU 的经营问题可被观测、可被诊断、可被安全推进：

1. **每个 store+item+SKU 有统一经营读模型**，指标保留真实粒度与证据状态（真实/人工/演示/缺失），绝不串数、绝不静默相加、绝不广播、逐值可回溯。
2. **真实缺数据时不伪造结论**：SKU 级流量缺失时漏斗为 missing/blocked，只用店铺级背景流量做上下文，不推导成 SKU 指标。
3. **隔离 Demo 可完整跑通** M5-R 诊断/实验/生命周期建议全链，但**不进入默认经营视图、不冒充真实店铺证据**，全链带 demo 标签。
4. **商品生命周期建议**默认 draft，人工确认才生效，批准也不触发任何平台写动作；存量标题/主图默认不改。
5. **WP1～WP4 由胡磊完整交付并自测**；WP5 由闫睿涵从干净状态独立复验后签署。

> 边界红线（贯穿全部 WP，违反即整体不通过）：不自动发布/改价/换图/报名/调广告/下架；不宣称平台权重或逆向算法；不把演示数据当真实因果；缺成本时不出正式利润安全价格。

---

## 二、里程碑结构

| 里程碑 | 负责人 | 交付物 | 外部依赖 | 前置 |
|---|---|---|---|---|
| M1 WP1 读模型 + 数据准备度 | 胡磊 | `product_read_model` 包 + 15 测试 | **无（唯一输入 M7-R 契约，已就位）** | 完成 |
| M2 WP2 M5-R 证据桥接 + 流量诊断 | 胡磊 | `diagnosis` 包 + Gate + 测试 | M5-R 证据接口（已就位）+ D-037～D-040 Demo 数据 | M1 |
| M3 WP3 生命周期建议 + 状态机 | 胡磊 | `lifecycle` 包 + 状态机 + 测试 | M10-R 成本/补货接口（可选增强，缺则降级） | M1/M2 |
| M4 WP4 工作台 + 机制 Eval | 胡磊 | 页面 + 冻结场景 + Eval runner | F-310 前端 / F-121/F-122 评测（需确认） | M1～M3 |
| M5 WP5 独立验收（**闫睿涵**） | 闫睿涵 | 验收矩阵 + 反例 + 签署 | 胡磊 M1～M4 完整交付 | M1～M4 |

> 并行策略：M1 落地后，M2/M3 不依赖 M4，可并行推进；M4 需 M1～M3 的接口稳定。

---

## 三、M1：WP1 读模型 + 数据准备度（先行，唯一不卡任何人的里程碑）

> 详细代码与测试见 [m9r-wp1-read-model.md](m9r-wp1-read-model.md)（v4）。此处为执行摘要。

### 任务
- `src/ecommerce_agent/product_read_model/`：`errors.py` / `models.py` / `factory.py` / `readiness.py` / `__init__.py`
- 四条隔离铁律 + 工厂 MISSING 投影 + 数据准备度（FunnelState）
- 测试：`tests/test_m9r_read_model_isolation.py`（12 个）+ `tests/test_m9r_readiness.py`（3 个）

### 验证命令（已核实可执行）
```bash
cd /d/yunpai-ecommerce-agent
git checkout -b feature/m9r-read-model origin/main        # 建分支（当前在 feature/m3，先切走）
PYTHONPATH=src python -m pytest tests/test_m9r_read_model_isolation.py tests/test_m9r_readiness.py -q --no-header -p no:cacheprovider
PYTHONPATH=src python -m pytest tests/test_readonly_data_contract.py -q --no-header -p no:cacheprovider   # M7-R 契约不回归
PYTHONPATH=src python -m pytest tests/test_traffic_lab.py -q --no-header -p no:cacheprovider              # M5-R 不回归
```
> 注：pyproject.toml 已配 `pythonpath = ["src"]`，`PYTHONPATH=src` 前缀可省略；保留仅为兜底。

### 验收（WP1，对齐任务书，诚实标注状态）

> 状态取值：✅ 已达标 / ⚠️ 待确认 / ❌ 不达标（已降级）/ 🔒 已锁定（依赖未交付）

| # | 验收条目 | 状态 | 落地/说明 |
|---|---|---|---|
| 1 | 同一 item 多 SKU、同 SKU 多 revision、同租户多店不串数 | ✅ 已达标 | composite_key 五元组；测试 2 |
| 2 | 日/月、店铺/商品、支付/退款不同粒度不静默相加 | ✅ 已达标 | period_key + granularity 物理隔离；测试 3 |
| 3 | 店铺级曝光/点击/广告/转化不广播、均摊、推导成 SKU 指标 | ✅ 已达标 | extra="forbid" 类型层拒绝；测试 1/10/11/12 |
| 4 | 跨粒度/跨店/跨 SKU/跨 revision/缺失确定性检查 | ✅ 已达标 | 结构性防呆；测试 2/3/7 |
| 5 | 缺字段 → 显示基础事实 + 阻断依赖结论（机械语义） | ✅ 已达标 | 工厂 MISSING 投影 + safe_value fail-fast；测试 7 |
| 6 | 竞品/退款数据域缺失场景覆盖 | ⚠️ 待确认 | 依赖 M7-R WP2 交付范围；确认后补场景测试 |
| 7 | 每个值可回溯到 import manifest 和 data_as_of | ✅ 已达标 | import_id 接入；测试 8 |
| 8 | 每个值可回溯到权威服务 | ❌ 不达标（已降级） | 现为 source_system best-effort；投影规则待 WP2 桥接层 |
| 9 | 保留字段原始粒度 | ✅ 已达标 | Granularity + period_key |
| 10 | 来源追溯（source_system / import_manifest_id） | ✅ 已达标 | manifest.source_system |
| 11 | 料号引用（material_code） | 🔒 已锁定（依赖未交付） | M7-R WP3 未合入 main；字段占位 None |
| 12 | 数据准备度、漏斗可用性、缺失阻断语义 | ✅ 已达标 | readiness.py + FunnelState；readiness 测试 1-3 |
| 13 | 复用现有领域事实表和公开服务，不复制第二套真相 | ✅ 已达标 | 只消费 M7-R 契约，内存投影，不插库 |

#### WP1 依赖追踪清单

| 依赖项 | 当前状态 | 提供方 | 解锁条件 | 预计确认日期 |
|---|---|---|---|---|
| M7-R WP3 canonical 料号/商家编码/内部料号映射 | 🔒 已锁定（依赖未交付） | 闫睿涵（M7-R） | WP3 合入 main 且有权威查询接口 → 填充 material_code → 条目 11 转 ✅ | 待定（需闫睿涵确认交付时间点） |
| M7-R WP2 数据域交付范围（含竞品/退款域） | ⚠️ 待确认 | 闫睿涵（M7-R） | 索要 WP2 范围清单 → 确认竞品/退款域 → 条目 6 转 ✅（或维持机械语义） | 待定（需闫睿涵确认） |
| 权威服务投影规则 | ❌ 不达标（已降级） | 胡磊（WP2 设计期） | WP2 桥接层确立投影规则并回填 authoritative_service → 条目 8 转 ✅ | WP2 设计期 |
| M7-R record_import 真实 import_id | ✅ 已达标（接口已就位） | M7-R（已交付） | WP2 接入即用，替换占位 id | WP2 开工 |

---

## 四、M2：WP2 M5-R 证据桥接 + 流量诊断（胡磊）

> 任务书 `M9R-PRODUCT-TRAFFIC-LIFECYCLE` WP2；前置 `M9R-WP1`。

### 任务
- **统一只读证据查询接口**：把 M9-R 读模型 + M5-R 证据（revision/experiment/analysis/freshness/provenance）桥接成统一只读查询。真实/Demo 查询范围物理隔离，demo 标签贯穿 API、页面、报告。
- **确定性 Gate**：A/A、样本量、实际窗口、控制变量、新鲜度、污染六类 Gate（复用 M5-R `TrafficAnalysisEngine`，不另建旁路）。
- **结构化诊断输出**：曝光不足/点击不足/转化不足/缺货污染/广告或价格变更污染/证据不足。
- **模型解释契约**：输入固化证据 → 输出结构化诊断；越权输出（改 effect/区间/样本量/Gate、声称平台权重）整份拒绝。

### 已核实的 M5-R 接口（供桥接使用，均存在）
```python
# src/ecommerce_agent/traffic_lab/service.py
TrafficLabService.get_experiment(tenant_id, experiment_id)
TrafficLabService.list_experiments(...)
TrafficLabService.get_revision(tenant_id, revision_id)
TrafficLabService.list_revisions(...)
TrafficLabService.get_analysis_run(tenant_id, analysis_run_id)
TrafficLabService.list_analysis_runs(...)
TrafficLabService.get_freshness(...)          # 若存在；否则读 freshness 视图
TrafficLabService.get_provenance(...)         # 若存在；否则读 provenance 字段
# TrafficAnalysisEngine.analyze_experiment(...)  # 复用，不重写统计
```
> 核查备注：analysis.py 的 `TrafficAnalysisEngine.analyze_experiment` 已提供 A/A、样本量、控制变量、污染等 Gate 实现；桥接层**调用它**，不复制统计逻辑。

### 新增结构（建议）
```
src/ecommerce_agent/product_diagnosis/
  __init__.py      # 导出
  bridge.py        # read_model + M5-R evidence → 统一只读查询
  gates.py         # A/A/样本/窗口/控制变量/新鲜度/污染确定性 Gate
  diagnosis.py     # 结构化诊断类型 + blocked/missing 语义
tests/test_m9r_diagnosis_gates.py
tests/test_m9r_diagnosis_bridge.py
tests/test_m9r_demo_isolation.py        # demo 标签贯穿 + 不进默认经营视图
```
> 边界：本包不写任何平台数据；只读 + Gate + 诊断；模型越权由输出校验拒绝。

### 验证命令（M1 合入后）
```bash
PYTHONPATH=src python -m pytest tests/test_m9r_diagnosis_gates.py tests/test_m9r_diagnosis_bridge.py tests/test_m9r_demo_isolation.py -q --no-header -p no:cacheprovider
```

### 验收（WP2，对齐任务书，诚实标注状态）

> 状态取值：✅ 已达标 / ⚠️ 待确认 / ❌ 不达标（已降级）/ 🔒 已锁定（依赖未交付）
> 说明：WP2 尚未实现，表中无 ✅ 行；每行是「承诺目标 + 解锁条件」，依赖解锁后转 ✅。

| # | 验收条目 | 状态 | 落地/说明 |
|---|---|---|---|
| 1 | 统一查询桥接 M5-R revision / experiment / analysis 证据 | ⚠️ 待确认 | 接口已核实存在（get_experiment/get_analysis_run 等）；WP2 实现后确认 |
| 2 | 统一查询桥接 M5-R freshness / provenance 证据 | ❌ 不达标（已降级） | M5-R 无独立查询方法（问题⑤）；降级为「先桥接已核实的 revision/experiment/analysis，载体待定位」 |
| 3 | 真实/Demo 查询范围物理隔离，demo 标签全链贯穿 | ⚠️ 待确认 | 依赖 Demo 数据域（D-037~D-040）就绪确认 |
| 4 | A/A、样本量、实际窗口、控制变量 Gate 通过才给强方向结论 | ⚠️ 待确认 | 复用 TrafficAnalysisEngine；WP2 实现后确认 |
| 5 | freshness Gate | ⚠️ 待确认 | 依赖 freshness 载体定位（问题⑤）；定位后实现 |
| 6 | 缺货/广告/价格污染不被归因标题/主图 | ⚠️ 待确认 | 污染 Gate + 反证测试；WP2 实现后确认 |
| 7 | 模型越权输出（effect/区间/样本量/Gate/平台权重）整份拒绝 | ⚠️ 待确认 | 输出校验 + 反证测试；WP2 实现后确认 |
| 8 | 无合格实验不编造 uplift，返回观察/补数/实验建议 | ⚠️ 待确认 | WP2 实现后确认 |
| 9 | 真实缺 SKU 流量/revision 窗口 → blocked；Demo 可演示但带标签 | ⚠️ 待确认 | 依赖 Demo 数据域确认 + blocked 语义由 WP2 定义 |
| 10 | 诊断全链只读，无任何 listing/图片/广告/活动写动作 | ⚠️ 待确认 | 写屏障；WP2 实现后确认 |

#### WP2 依赖追踪清单

| 依赖项 | 当前状态 | 提供方 | 解锁条件 | 预计确认日期 |
|---|---|---|---|---|
| M5-R revision/experiment/analysis 接口 | ✅ 已达标（已合入 main） | 闫睿涵（M5-R，已交付） | 直接复用 | 已就位 |
| M5-R TrafficAnalysisEngine（A/A/样本/污染等 Gate） | ✅ 已达标（已合入 main） | 闫睿涵（M5-R，已交付） | 直接复用，不重写统计 | 已就位 |
| M5-R freshness/provenance 载体 | ⚠️ 待确认 | 闫睿涵 + 胡磊 | WP2 设计期读代码定位载体（问题⑤）→ 条目 2/5 转 ✅ | WP2 设计期 |
| Demo 数据域 D-037~D-040 | ⚠️ 待确认 | 数据/Demo 负责人 | 确认就绪 → 条目 3/9 转 ✅ | 待定（需确认） |

---

## 五、M3：WP3 商品生命周期建议、人工确认与跟踪（胡磊）

> 任务书 WP3；前置 `M9R-WP1` / `M9R-WP2`。

### 任务
- **建议权威 schema**：类型注册表（选品/上新/保持/诊断/实验/定价候选/活动候选/补货联动/清仓预警）+ 事实快照 + 假设 + 收益/风险 + 缺失项 + 版本。
- **状态机**：draft → awaiting_review → approved/rejected → observed → closed；批准不触发平台动作；事实更新标 stale，不原地改写历史。
- **模型语义建议接入**：代码只校验类型/必需证据/状态/幂等/写屏障；模型决定语义下一步、解释证据与反证。
- **降级边界**：缺成本时不出正式利润安全价格；缺竞品时不出行业对标。

### 新增结构（建议）
```
src/ecommerce_agent/product_lifecycle/
  __init__.py
  schemas.py        # 建议类型注册表 + 事实快照 + 缺失项
  state_machine.py  # draft→…→closed 状态机 + stale 规则
  validation.py     # 模型输出校验 + 写屏障
tests/test_m9r_lifecycle_state_machine.py
tests/test_m9r_lifecycle_validation.py
tests/test_m9r_lifecycle_idempotency.py   # 重放不重复创建
```

### 验证命令
```bash
PYTHONPATH=src python -m pytest tests/test_m9r_lifecycle_state_machine.py tests/test_m9r_lifecycle_validation.py tests/test_m9r_lifecycle_idempotency.py -q --no-header -p no:cacheprovider
```

### 验收（WP3，对齐任务书）
- 建议默认 draft，只有人工可批准/拒绝；批准仍不触发平台动作。
- 存量标题/主图默认 keep/observe；满足条件才显示人工复核优化，保留优先上新/实验替代。
- 缺成本/缺竞品时结论按证据降级。
- 重放不重复创建；事实更新后旧建议标 stale，不原地改写历史。
- 语义建议由模型产生，确定性代码不以标签或关键词替代模型判断。

---

## 六、M4：WP4 商品经营工作台 + 机制 Eval（胡磊）

> 任务书 WP4；前置 `M9R-WP1` ~ `M9R-WP3`。

### 任务
- **只读工作台页面**：商品列表 / SKU 下钻 / 漏斗 / 流量诊断 / 实验 / 来源 / 建议 / 审核页面。
- **冻结场景集**：真实粒度不足 + 显式模拟实验两类；独立 oracle；ground truth 不进入生产输入。
- **数值/机制 Eval**：不以文案关键词或表面措辞作裁判；Eval 同时能发现真实方向、拒绝污染方向。
- **浏览器检查**：桌面 + 窄屏可读，console 无新增错误；浏览页面无隐式动作，运行操作显式点击并审计。

### 依赖确认（需向闫睿涵核实）
| 依赖 | 谁提供 | 就绪状态 | 缺时方案 |
|---|---|---|---|
| F-310 前端基建 | 闫睿涵/前端 | ❓ 待确认 | 用现有 admin 模板 + HTML 报告页 |
| F-121/F-122 评测能力 | 评测平台 | ❓ 待确认 | 自建轻量 runner（数值断言 + oracle） |
| 落库（v35） | 胡磊 + 闫睿涵占号 | 待确认 | 纯内存骨架先跑，工作台再落库 |

### 验证命令（M1～M3 合入后）
```bash
PYTHONPATH=src python -m pytest tests/test_m9r_workbench_eval.py -q --no-header -p no:cacheprovider
# 浏览器证据（如已有 E2E 框架；否则人工截图）
```

### 验收（WP4，对齐任务书，诚实标注状态）

> 状态取值：✅ 已达标 / ⚠️ 待确认 / ❌ 不达标（已降级）/ 🔒 已锁定（依赖未交付）
> 说明：WP4 尚未实现，且前端/Eval/落库依赖均未确认；全部行「待确认后承诺」，确认方见依赖清单。

| # | 验收条目 | 状态 | 落地/说明 |
|---|---|---|---|
| 1 | 商品/SKU 下钻到 revision、时间窗、指标、来源、建议依据 | ⚠️ 待确认 | 依赖 F-310 前端基建确认（问题④） |
| 2 | 显示「为什么建议」/「为什么暂不能建议」，不只给红黄绿 | ⚠️ 待确认 | 依赖 WP3 建议输出 + 前端 |
| 3 | 页面浏览无隐式分析/创建实验/创建建议/修改商品；运行显式点击并审计 | ⚠️ 待确认 | 写屏障 + 审计；WP4 实现后确认 |
| 4 | 机制 Eval 发现真实方向 + 拒绝污染方向；ground truth 物理隔离 | ⚠️ 待确认 | 依赖 F-121/F-122 评测能力确认（问题④） |
| 5 | 浏览器桌面 + 窄屏可读，console 无新增错误 | ⚠️ 待确认 | 依赖 F-310 前端基建确认（问题④） |
| 6 | 真实/模拟场景隔离，全链标注 | ⚠️ 待确认 | 依赖 Demo 数据域确认 |

#### WP4 依赖追踪清单

| 依赖项 | 当前状态 | 提供方 | 解锁条件 | 预计确认日期 |
|---|---|---|---|---|
| F-310 前端基建 | ⚠️ 待确认 | 闫睿涵/前端 | 确认就绪 → 条目 1/2/5 转 ✅；否则备选方案（现有 admin 模板）不视为承诺 | 待定（需确认） |
| F-121/F-122 评测能力 | ⚠️ 待确认 | 评测平台负责人 | 确认就绪 → 条目 4 转 ✅；否则自建轻量 runner（备选） | 待定（需确认） |
| Demo 数据域 D-037~D-040 | ⚠️ 待确认 | 数据/Demo 负责人 | 确认就绪 → 条目 6 转 ✅ | 待定（需确认） |
| WP1~WP3 读模型/诊断/建议接口 | ✅ 已达标（计划锁定） | 胡磊 | WP4 消费前接口冻结 | WP1~WP3 完成时 |
| v35 落库占号 | ⚠️ 待确认 | 闫睿涵（占号协调） | 工作台需跨请求查询时按 CONTRIBUTING 占号 → 落库转 ✅ | WP4 设计期 |

---

## 七、边界与不变量（贯穿全部 WP）

| 边界 | 说明 |
|---|---|
| 数据源 | 只消费 M7-R 契约（manifest/policy/sanitize）与 M5-R 权威证据；**不得直接插库**绕过 service |
| 真实/Demo | 隔离；demo 标签全链贯穿；模拟数据不冒充真实 |
| 写屏障 | 读模型、诊断、建议全链只读；不自动发布/改价/换图/报名/调广告/下架 |
| 证据 | 缺成本不出正式利润安全价格；缺竞品不出行业对标；缺数据不伪造漏斗/趋势 |
| 统计 | 复用 M5-R `TrafficAnalysisEngine`，不另建简化统计旁路 |
| 人工 | 建议默认 draft，人工批准才生效；批准不触发平台动作 |
| schema | 涉及数据库改动先按 CONTRIBUTING 占号（v35），在群说一声，合并后跑全量 |

---

## 八、验证与完成声明（每里程碑都必须走）

1. 每条验证命令**真实执行**并贴出通过输出（`verification-before-completion`）。
2. 跑 `tests/test_readonly_data_contract.py` + `tests/test_traffic_lab.py` 确认上游契约不回归。
3. 全量回归：`PYTHONPATH=src python -m pytest tests -q --no-header -p no:cacheprovider`（或按仓库既有 CI 配置）。
4. 完成声明只覆盖「已执行且通过」的部分；未执行/跳过/失败的部分如实说明，不假装完成。

---

## 九、已知差距与诚实降级（5 项，负责人明确要求但本计划不达标/待确认）

> 原则：计划只标真实状态。以下每一项都写清「当前状态 → 降级措辞 → 解锁条件」。

### 差距① 料号引用（问题①）
- 要求：WP1「保留字段原始粒度、料号引用、来源和 data_as_of」。
- 当前：`material_code` 已加入 Item/SKU 模型，默认 None；M7-R WP3 映射**未合入 main**，无法从权威服务填充。
- 降级：`material_code=None` = 料号引用不可用（M7-R WP3 未交付）；不当作已达标。
- 解锁：**M7-R WP3 canonical 映射合入 main 且有权威查询接口** → 从该服务填充并补断言 → 升级已达标。

### 差距② 权威服务回溯（问题③）
- 要求：「读模型每个值可回溯到权威服务、import manifest 和 data_as_of」。
- 当前：`authoritative_service` 已加字段，现以 `manifest.source_system` 作来源系统 best-effort；权威服务投影规则未实现。
- 降级：验收条目只对「import manifest + data_as_of」标已达标；权威服务标 best-effort。
- 解锁：**WP2 桥接层确立权威服务投影规则**（每个读模型值绑定产出领域服务）→ 回填并补断言 → 升级已达标。

### 差距③ 缺竞品/退款边界（问题②）
- 要求：缺广告/竞品/退款明细时仍显示基础流量事实。
- 当前：工厂 MISSING 投影保证机械语义（行缺字段不崩、缺失可安全读取）；竞品/退款业务域真实边界**取决于 M7-R WP2 交付范围，未核实**。
- 降级：只断言机械事实；不声称覆盖竞品/退款业务域。
- 解锁：**确认 M7-R WP2 交付范围清单** → 按实际范围补场景 → 升级已达标（或维持机械语义）。

### 差距④ WP4 浏览器/Eval（问题④）
- 要求：WP4 浏览器桌面+窄屏、机制 Eval。
- 当前：依赖 F-310 / F-121-F122 / Demo 数据就绪状态，**均未确认**。
- 降级：WP4 浏览器/Eval 能力标为「待确认后承诺」；缺时方案（现有 admin 模板 + 自建轻量 runner）仅为备选，不视为已承诺能力。
- 解锁：**确认 F-310/F-121-F122/Demo 数据就绪**（确认方：闫睿涵 / 评测平台负责人）→ 按实际基建承诺。

### 差距⑤ freshness/provenance 桥接（问题⑤）
- 要求：WP2「统一只读查询」桥接 freshness/provenance。
- 当前：M5-R `TrafficLabService` **无独立 `get_freshness`/`get_provenance` 方法**；freshness/provenance 藏在 revision/analysis 字段。
- 降级：桥接对象标「待核实」；WP2 先桥接已核实的 revision/experiment/analysis，freshness/provenance 待读代码定位。
- 解锁：**WP2 设计期读 M5-R 代码定位 freshness/provenance 载体**（确认方：胡磊读代码 + 闫睿涵）→ 补桥接 → 升级已达标。

---

## 十、风险与待确认项

| 风险/待确认 | 影响 | 缓解 |
|---|---|---|
| F-310 / F-121/F-122 就绪状态未知 | WP4 前端/Eval 基建 | 提前向闫睿涵确认；缺时自建轻量 runner |
| D-037～D-040 Demo 数据域就绪状态未知 | WP2 演示链路 | 用显式 demo 标记的模拟数据先跑通；任务书允许 |
| v35 落库占号 | 工作台跨请求查询 | 内存骨架先跑，工作台再占号；CONTRIBUTING 流程 |
| 存量标题/主图「默认不改」被误触发 | 商品原则 | WP3 反证测试锁 keep/observe 默认 |

---

## 十一、完成即交付（handoff）

- `product_read_model` / `product_diagnosis` / `product_lifecycle` 包 + 测试，接口与查询示例。
- 数据准备度、漏斗可用性、缺失阻断语义、真实/Demo 隔离说明。
- 跨店/跨 SKU/跨 revision/混粒度/店铺指标拆分反例证据。
- 固定消费的 M7-R 输出版本（SCHEMA_VERSION=34）与 M5-R 证据接口版本。
- WP4 工作台页面、冻结场景、Eval 门禁报告、浏览器证据。
- 完整回归通过输出 + 未完成项清单（若有）。
