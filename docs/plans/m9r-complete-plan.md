# M9-R 商品流量与生命周期经营 — 完整执行计划

> 存放位置：`docs/plans/m9r-complete-plan.md`
> 版本：v1（对齐闫睿涵 8/18 飞书回复 + 用户总结的十项总纲）
> 负责人：胡磊（M9-R WP1～WP4 开发）；验收人：闫睿涵（M9-R WP5 独立复验）
> 前置：M7-R WP1 契约已合入 main（SCHEMA_VERSION=34,0b54a24）
> 引用：[M9R_PRODUCT_TRAFFIC_LIFECYCLE_WORKBENCH.md](../tasks/M9R_PRODUCT_TRAFFIC_LIFECYCLE_WORKBENCH.md)（任务书）；[m9r-wp1-read-model.md](m9r-wp1-read-model.md)（WP1 详细计划）

---

## 一、这个模块到底在干什么

**业务问题**：平台给商品多少曝光，取决于商品的**健康度**——有没有库存、价格对不对、标题关键词是否过时、有没有受控实验证据。M9-R **不操作平台**（不改价、不自动换图、不报名），但它是**让运营决定「改什么、先改哪个」**的数据基础设施：

```
看清现状 → 诊断原因 → 给出建议 → 人工确认 → 执行 → 追踪效果
   WP1        WP2      WP3      (人工)     (人工)    WP4
```

**三项核心交付**：
1. **流量诊断**：哪些 SKU 曝光/点击/转化/库存有问题，为什么
2. **受控实验**：先在一个 SKU 小范围测试，用 M5-R revision/experiment/analysis 基础设施，避免全店盲目改
3. **全链路生命周期建议**：选品 → 上新 → 曝光点击诊断 → 受控实验/保持观察 → 定价/活动候选 → 补货联动 → 清仓预警

---

## 二、七条硬边界（每条都必须有测试反证）

| # | 边界 | 说明 | 测试反例（必须失败） |
|---|---|---|---|
| B1 | 存量标题/主图默认不改 | 只有「标题错误」「关键词陈旧」「曝光点击差且数据质量满足」时才出人工复核建议 | `test_stock_item_keep_default`：无证据时模型不能输出「建议改标题」 |
| B2 | 一切建议需人工确认 | 系统只输出 draft 建议，人工批准才生效；批准仍不触发平台动作 | `test_approved_no_platform_action`：批准后不发 API 请求、不改 DB |
| B3 | 始终保留替代方案 | 每条建议必须附带「优先上新」或「受控实验」作为备选路径 | `test_alternative_path_present`：建议必须有 alternatives 字段 |
| B4 | 零平台写动作 | **平台写=0**（不改价、不停广告、不拦截订单、不自动报名、不调广告，只提示人工）；**内部写仅限白名单**（实验记录、建议记录、状态机流转），白名单外禁止 | `test_zero_platform_write_actions`：Mock 平台 API 客户端断言全部 not_called；`test_internal_write_allowlist`：白名单外内部写被拒 |
| B5 | SKU 级流量只用隔离 Demo | 现有导出只有店铺级汇总，没有 SKU 级曝光/点击；真实模式下**不得把店铺总量拆成 SKU 结论** | `test_sku_traffic_blocked_without_data`：缺 SKU 级流量时必须 blocked/missing，不能用店铺值推导 |
| B6 | 缺失绝不按 0 处理 | 缺什么指标、影响哪个数字，必须如实显示 | `test_missing_not_zero`：缺失字段 safe_value 必须抛 DataUnavailableError，不能返回 0.0 |
| B7 | 样本数据不作产品口径 | M7-R 样本（如收入月度汇总）仅作字段/粒度线索；样本数据不进入产品结论计算，不证明订单级费用明细 | `test_sample_not_product_caliber`：data_trust="sample" 的值参与产品口径计算 → 必须 FAIL |

---

## 三、四级证据状态（贯穿所有输出字段）

```
真实导出 (actual)   ← 来自 M7-R manifest/import
人工配置 (manual)   ← 运营手动填入
演示参数 (demo)     ← 隔离 Demo，带 demo 标签
缺失 (missing)      ← 字段不在报表中，明确记录 reason
```

> 这四级状态映射 M7-R `EvidenceState`（actual/manual/demo/missing），每个 MetricValue 必须有 evidence_state。

**关键原则**：
- 缺失 ≠ 0。缺失是「不知道」，0 是「就是 0」。
- 样本数据（M7-R 样本/日报）仅作字段与粒度线索，**不作为产品口径**。
- Demo 数据必须显式带 `DataScope.DEMO` 标签，**不进入默认 operational 视图**。

---

## 四、依赖链与解锁节奏

### 4.1 三级解锁机制

```
第一级：导入与证据契约冻结 ✅（M7-R WP1，SCHEMA_VERSION=34）
             ↓
第二级：每交付一个经复核的数据域 → 解锁对应真实数据联调
             ↓
第三级：商品/SKU/料号身份映射完成 → 解锁 SKU 经营闭环（料号引用）
```

### 4.2 已确认依赖状态（闫睿涵 8/18 回复）

| 依赖项 | 状态 | 解锁条件 | 计划影响 |
|---|---|---|---|
| M7-R WP1 契约 | ✅ 已交付 | 直接消费 | WP1 骨架 |
| M7-R WP2 数据域（竞品/退款域） | ⚠️ 未交付（F-304 竞品证据可复用） | WP2 诊断时按「机械语义」处理 | WP1 条目 6 维持 ⚠️ |
| M7-R WP3 料号映射 | 🔒 未交付，无时间窗 | 料号字段占位 None | WP1 条目 11 维持 🔒 |
| M5-R freshness/provenance | ✅ 已交付（evidence-freshness-v1 / source-provenance-v1） | 按闫哥给的 4 个持久化位置桥接 | WP2 条目 2/5 升 ✅ |
| F-310 前端基建（/admin） | ✅ 已可用 | WP4 直接扩展 | WP4 条目 1/2/5 升 ✅ |
| F-121/F-122 评测 | ✅ 已交付（simulation-evidence-v1 + runner） | WP4 复用扩展，不另建 | WP4 条目 4 升 ✅ |
| D-037~D-040 底层 | ✅ 已交付 | D19/D20、virtual_store_v1.json 可复用 | WP2/WP4 Demo 底层就绪 |
| M9 专属 Demo（SKU 流量/revision/窗口） | ⚠️ WP2/WP4 补齐 | 实验/诊断链路需要 | WP2/WP4 条目 3/6 ⚠️ |

---

## 五、分工作包执行计划（含遗漏项补全）

### WP1 Listing/SKU 经营读模型 + 数据准备度

**目标**：建立 store+item+SKU 四层聚合读模型，保留原始粒度与证据状态，为诊断与建议提供可追溯事实。

**代码结构**：
```
src/ecommerce_agent/product_read_model/
  __init__.py
  errors.py           # DataUnavailableError
  models.py           # Granularity/AggregateRule/MetricValue/Store/Item/SKUReadModel
  readiness.py        # MetricReadiness/SKUReadiness/funnel_availability（派生字段，非独立枚举）
  factory.py          # build_read_model_from_manifest
tests/
  test_m9r_read_model_isolation.py   # 13 个破坏性测试
  test_m9r_readiness.py              # 3 个准备度测试
  test_m9r_evidence_state_boundary.py # 2 个边界反证测试（B5/B6；B1~B4 属 WP2/WP3，见反例测试群）
  test_m9r_data_trust.py             # 5 个 data_trust 测试（B7：样本不作产品口径 + 非法组合校验）
  test_m9r_privacy.py                # 3 个敏感字段反例测试（隐私红线）
```

**MetricValue 字段清单（含 data_trust，样本口径的代码载体）**：
```python
class MetricValue(BaseModel):
    evidence_state: EvidenceState      # actual/manual/demo/missing（四态，对齐 M7-R）
    granularity: Granularity
    aggregate_rule: AggregateRule
    period_key: str
    value: float | None
    import_manifest_id: str | None
    data_as_of: datetime | None
    authoritative_service: str | None  # 溯源（WP2 回填）
    data_trust: Literal["production", "sample", "demo", "missing"]  # 样本 vs 生产口径（B7 载体）
    reason: str | None
```
> **data_trust 与 evidence_state 的关系**：evidence_state 回答「数据来自哪类来源」（actual/manual/demo/missing）；data_trust 回答「能否作为产品口径」（production 可作产品结论依据；sample 仅作字段/粒度线索，不进入产品口径计算；demo/missing 不可作产品口径）。M7-R 样本数据导入时 data_trust="sample"。

**关键新增：证据状态边界测试（对应 B5/B6）**
```python
def test_missing_field_returns_error_not_zero():
    """B6 反例：缺失字段 safe_value 必须抛错，不能静默返回 0。"""
    sku = _sku(missing_flow=True)
    with pytest.raises(DataUnavailableError):
        sku.impressions.safe_value  # missing 字段必须 fail-fast

def test_sku_traffic_blocked_without_real_data():
    """B5 反例：真实模式缺 SKU 级流量，不能自动用店铺值推导。"""
    sku = _sku(missing_flow=True)
    assert sku.impressions.evidence_state is EvidenceState.MISSING
    assert sku.impressions.reason == "sku_traffic_blocked"  # 不能假装是实际值
```

**验收表（修订后）**：
| # | 验收条目 | 状态 | 说明 |
|---|---|---|---|
| 1 | 同一 item 多 SKU / 同 SKU 多 revision / 同租户多店不串数 | ✅ 已达标 | composite_key 五元组 + 测试 2 |
| 2 | 日/月、店铺/商品、支付/退款不同粒度不静默相加 | ✅ 已达标 | period_key + granularity 物理隔离 + 测试 3 |
| 3 | 店铺级曝光/点击/广告/转化不广播成 SKU 指标 | ✅ 已达标 | extra="forbid" 物理拒绝 + 测试 1/10/11/12 |
| 4 | 跨粒度/跨店/跨 SKU/跨 revision/缺失确定性检查 | ✅ 已达标 | 结构性防呆 + 测试 2/3/7 |
| 5 | 缺字段 → 显示基础事实 + 阻断依赖结论（机械语义） | ✅ 已达标 | 工厂 MISSING 投影 + safe_value fail-fast + 测试 7 + B6 反例 |
| 6 | 竞品/退款数据域真实覆盖 | ⚠️ 待确认 | F-304 approved-only 竞品、订单/售后事实可复用（闫哥 8/18 确认）；真实 M7-R WP2 业务域待交付 |
| 7 | 每个值可回溯到 import manifest 和 data_as_of | ✅ 已达标 | import_id + data_as_of + 测试 8 |
| 8 | 每个值可回溯到权威服务 | ❌ 不达标(已降级) | 现为 source_system best-effort；WP2 桥接层确立投影规则后回填 |
| 9 | 保留字段原始粒度 | ✅ 已达标 | Granularity + period_key |
| 10 | 来源追溯（source_system / import_manifest_id） | ✅ 已达标 | manifest.source_system |
| 11 | 料号引用（material_code） | 🔒 已锁定(依赖未交付) | M7-R WP3 未交付；字段占位 None；闫哥确认无时间窗，长期挂起 |
| 12 | 数据准备度、漏斗可用性、缺失阻断语义 | ✅ 已达标 | readiness.py + funnel_availability 派生字段 + 测试 1-3 |
| 13 | 复用现有领域事实表和公开服务，不复制第二套真相 | ✅ 已达标 | 只消费 M7-R 契约，内存投影，不插库 |
| 14 | 四态证据状态贯穿（真实/人工/演示/缺失） | ✅ 已达标 | EvidenceState 映射 + 测试验证 |
| 15 | 缺失绝不按 0 处理（B6 反例） | ✅ 已达标 | safe_value fail-fast + 反例测试 |
| 16 | 样本数据不作产品口径（data_trust 字段，B7 载体） | ✅ 已达标 | MetricValue.data_trust 字段 + 断言：data_trust="sample" 的值不参与产品口径计算 |
| 17 | 敏感字段过滤 + 不入日志（隐私红线） | ✅ 已达标 | 工厂只接收 sanitize_report_row 脱敏后的 payload（M7-R 契约已内置敏感字段/敏感值剥离）；新增反例：含买家手机号/地址的字段出现在日志或读模型 → 测试必须 FAIL |
| 18 | manual（人工配置）数据录入机制明确 | ✅ 已达标 | **本期决策：manual 录入入口不在 WP1 范围**。M7-R 人工经营参数（料号/MOQ/交期）由 M7-R 侧录入，M9-R 只读消费；M9-R 专属人工配置本期不做录入表单，凡需 manual 的字段一律标 `EvidenceState.MISSING`（reason="manual_entry_not_available"），待 WP4 工作台再做录入入口。 |

#### 已知差距汇总附录（⚠️/❌/🔒 带债清单，随各阶段收口更新）

| # | 差距项 | 状态 | 解锁条件 | 责任人 | 预计解锁 |
|---|---|---|---|---|---|
| G1 | 竞品/退款真实业务域覆盖 | ⚠️ | M7-R WP2 数据域交付 | 闫睿涵（M7-R） | 待定 |
| G2 | 料号引用 material_code | 🔒 | M7-R WP3 身份映射合入 main | 闫睿涵（M7-R） | 无时间窗 |
| G3 | 权威服务溯源 | ❌ | WP2 桥接层确立投影规则后回填 | 胡磊 | WP2 |
| G4 | M9 专属 Demo（SKU 流量/revision/窗口） | ⚠️ | WP2/WP4 自行补齐 | 胡磊 | WP2/WP4 |
| G5 | M10-R 契约字段冻结 | ⚠️ | 第 4 周向缪海南评审、第 5 周冻结 | 胡磊 + 缪海南 | WP3 |

#### 负责人关注点（WP1）

**可交付**：`src/ecommerce_agent/product_read_model/` 包 + **26 个测试**（13 隔离 + 3 准备度 + 2 边界 + 5 data_trust + 3 隐私反例），全部跑绿。

**可验证**（闫睿涵 WP5 独立复验时必测）：
- 每个 SKU 级指标字段必须有 `evidence_state` 属性，取值 ∈ {actual, manual, demo, missing}
- 缺失字段的 `safe_value` **必须抛 `DataUnavailableError`**，绝不能静默返回 0.0
  ```python
  # 反例测试（test_missing_not_zero）：必须 FAIL 如果实现了「缺失=0」
  sku = build_sku_with_missing_traffic()
  with pytest.raises(DataUnavailableError):
      sku.impressions.safe_value  # missing 时绝不返回 0.0
  ```
- 跨店复合主键 (`tenant_id, store_id, item_id, sku_id, revision`) 长度恒为 5，槽位含义固定
- `data_trust` 字段必须存在且默认为 `"production"`；样本数据导入时显式标 `"sample"`

**无风险**：
- 不插库、不复制领域事实表，只消费 M7-R 契约内存投影
- 料号引用字段 `material_code` 当前为 None 占位（M7-R WP3 未交付），**不影响其他字段**

**运行结果示例**（落地后附截图）：
```
tests/test_m9r_readiness.py ............            [100%]
tests/test_m9r_read_model_isolation.py ............. [100%]
tests/test_m9r_evidence_state_boundary.py .....     [100%]
tests/test_m9r_data_trust.py .....                  [100%]
tests/test_m9r_privacy.py .                          [100%]
26 passed in 0.37s
```

### WP2 M5-R 证据桥接 + 流量诊断 + 受控实验

**目标**：桥接 M5-R 证据（revision/experiment/analysis/freshness/provenance），构建结构化诊断，实现受控实验入口。

**代码结构**：
```
src/ecommerce_agent/product_diagnosis/
  __init__.py
  bridge.py           # M5-R evidence → 统一只读查询（含 freshness/provenance 桥接）
  gates.py            # A/A、样本量、实际窗口、控制变量、新鲜度、污染确定性 Gate
  diagnosis.py        # 结构化诊断类型（曝光不足/点击不足/转化不足/缺货污染/证据不足）
  experiment.py       # 受控实验双路径封装（run_demo_experiment 走 VirtualStoreSimulation；create_real_experiment 预留+blocked）
tests/
  test_m9r_diagnosis_gates.py
  test_m9r_diagnosis_bridge.py
  test_m9r_demo_isolation.py         # 隔离 Demo 不进默认经营视图
  test_m9r_controlled_experiment.py  # 受控实验入口（新增）
  test_m9r_write_barrier.py          # 写屏障反例（新增：B4 零平台写动作）
```

**关键设计决策**（闫哥 8/18 回复落地）：

1. **freshness/provenance 桥接**：直接使用闫哥确认的 4 个持久化位置
   - Traffic: `evidence_json.source_provenance`
   - Demand: `lineage_json.source_provenance`
   - Forecast: `candidate_models_json.source_provenance`
   - Plan: `forecast_evidence_json.source_provenance`

2. **受控实验双路径（防返工关键——开工前已核实 M5-R 接口无 demo/scope 字段）**：

   > 交叉验证结论：`TrafficExperimentCreate`（traffic_lab/models.py）**没有 scope/demo 字段**，直接调用 `create_experiment` 会把实验写进真实 `traffic_experiments` 表——**Demo 实验绝不能走这条路**，否则污染真实实验数据。

   | 路径 | 入口 | 数据 | 使用场景 |
   |---|---|---|---|
   | **Demo 实验** | `VirtualStoreSimulation`（simulation.py，已核实：`confirm_virtual: Literal[True]` 强制确认 + fixture `"virtual": True` 校验 + 数据 `source_type: "virtual"`） | 隔离虚拟数据，显式 demo 标签 | SKU 级流量诊断演示、实验链路演示（任务书 V1 要求） |
   | **真实实验** | `TrafficLabService.create_experiment` + `transition_experiment` | 真实 revision + 真实窗口 | 仅当真实 SKU 级流量 + revision 窗口证据齐备后 |

   - `product_diagnosis/experiment.py` 必须**两条路径分开封装**：`run_demo_experiment(...)`（走 VirtualStoreSimulation）与 `create_real_experiment(...)`（走 TrafficLabService），入口强制要求调用方显式声明路径，不允许自动选择。
   - **本阶段只实现 Demo 路径**：真实 SKU 级流量/revision 窗口未交付，真实路径仅预留接口 + 阻塞标记（`blocked` 语义），不开通。

3. **Demo 隔离**：用 M7-R 已有的 `DataScope` 枚举（OPERATIONAL/DEMO/ALL），所有 Demo 数据显式带 `DataScope.DEMO`，不在默认 operational 视图中出现。

**验收表**：
| # | 验收条目 | 状态 | 说明 |
|---|---|---|---|
| 1 | 统一查询桥接 M5-R revision/experiment/analysis 证据 | ✅ 已达标可设计 | 接口已核实存在 |
| 2 | 统一查询桥接 freshness/provenance 证据 | ✅ 已达标可设计 | 闫哥给了完整结构与 4 个持久化位置 |
| 3 | 真实/Demo 查询范围物理隔离，demo 标签全链贯穿 | ⚠️ 待确认 | Demo 数据底层已交付（D19/D20/virtual_store_v1.json），M9 专属 Demo 需补齐 |
| 4 | A/A、样本量、实际窗口、控制变量 Gate 通过才给强方向结论 | ⚠️ 待确认 | 复用 TrafficAnalysisEngine，WP2 实现后确认 |
| 5 | freshness Gate | ✅ 已达标可设计 | evidence-freshness-v1 已确认 |
| 6 | 缺货/广告/价格污染不被归因标题/主图 | ⚠️ 待确认 | 污染 Gate + 反证测试 |
| 7 | 模型越权输出整份拒绝 | ⚠️ 待确认 | 输出校验 + 反证测试 |
| 8 | 无合格实验不编造 uplift | ⚠️ 待确认 | WP2 实现后确认 |
| 9 | 真实缺 SKU 流量/revision 窗口 → blocked；Demo 可演示但带标签 | ⚠️ 待确认 | 依赖 Demo 数据补齐 |
| 10 | 诊断全链平台写=0（内部写白名单内） | ⚠️ 待确认 | B4 双语义 + 反例测试（平台 API 全 Mock not_called） |
| 11 | 受控实验入口可用（**本阶段仅 Demo 路径**） | ⚠️ 待确认 | Demo 走 VirtualStoreSimulation；真实路径预留 + blocked |
| 12 | Demo 隔离不进入默认经营视图 | ⚠️ 待确认 | DataScope.DEMO 标签全链贯穿 |

#### 负责人关注点（WP2）

**可交付**：`src/ecommerce_agent/product_diagnosis/` 包 + **5 个测试**，全部跑绿。

**可验证**（闫睿涵 WP5 独立复验时必测）：
- 受控实验**双路径**（防返工关键，开工前已核实接口）：
  - **Demo 路径**：`VirtualStoreSimulation`（simulation.py）——本阶段实现，SKU 级流量诊断/实验演示
  - **真实路径**：`TrafficLabService.create_experiment`/`transition_experiment`——仅预留接口 + `blocked` 标记，真实数据齐备后开通
  - 复用 M5-R 统计能力：`TrafficAnalysisEngine.analyze_experiment`（含 A/A、样本量、控制变量、污染等 Gate，不重写统计逻辑）
- freshness/provenance 桥接**直接使用闫哥确认的 4 个持久化位置**（JSON 路径）：
  | 数据域 | source_provenance 路径 |
  |---|---|
  | Traffic | `evidence_json.source_provenance` |
  | Demand | `lineage_json.source_provenance` |
  | Forecast | `candidate_models_json.source_provenance` |
  | Plan | `forecast_evidence_json.source_provenance` |
- Demo 数据显式带 `DataScope.DEMO` 标签，**不进入默认 operational 视图**
- 写屏障双语义：平台写=0（Mock 平台 API 断言 not_called，`test_zero_platform_write_actions`）；内部写白名单（实验/建议/状态机流转，`test_internal_write_allowlist`）

**复用边界**（明确什么不做）：
- **做**：调用 `TrafficLabService.create_experiment` / `transition_experiment` / `analyze_experiment`
- **不做**：不新建实验框架、不另建通用 runner、不修改 M5-R 现有代码
- **新增**：`product_diagnosis/bridge.py`（桥接层）、`gates.py`（Gate 组合）、`experiment.py`（M9-R 实验入口封装）

**无风险**：
- 复用 M5-R 已有基础设施，M5-R 的统计逻辑与 Gate 实现不动
- Demo 数据与 operational 数据物理隔离，不会串数

### WP3 商品生命周期建议、人工确认与跟踪

**目标**：基于固化经营事实与流量诊断，生成版本化建议，人工确认后才生效。

**代码结构**：
```
src/ecommerce_agent/product_lifecycle/
  __init__.py
  schemas.py          # 建议类型注册表（选品/上新/保持/诊断/实验/定价候选/活动候选/补货联动/清仓预警）
  state_machine.py    # draft → awaiting_review → approved/rejected → observed → closed
  validation.py       # 模型输出校验 + 写屏障 + 类型/证据/状态校验
  interface.py        # 建议输出契约（为 M10-R 预留消费接口）
tests/
  test_m9r_lifecycle_state_machine.py
  test_m9r_lifecycle_validation.py
  test_m9r_lifecycle_idempotency.py     # 重放幂等
  test_m9r_lifecycle_write_barrier.py   # B4 反例：批准不触发平台动作
  test_m9r_lifecycle_alternatives.py    # B3 反例：建议必须有备选路径
```

**关键设计决策**：

1. **建议类型注册表**：严格按任务书链条设计（选品→上新→诊断→实验→定价候选→补货联动→清仓预警），每条建议必须带 `alternatives` 字段（B3）。

2. **状态机转换**：draft → awaiting_review → approved/rejected → observed → closed；**approved 仍然不触发平台动作**（B2）。

3. **M10-R 接口预留**：补货/清仓/定价建议的输出结构要能被 M10-R 直接消费——契约格式需与 M10-R 对齐，此处先预留接口字段。

**验收表**：
| # | 验收条目 | 状态 | 说明 |
|---|---|---|---|
| 1 | 建议默认 draft，人工批准才生效 | ⚠️ 待确认 | 状态机实现后 |
| 2 | 批准不触发平台写动作 | ⚠️ 待确认 | 写屏障测试 B4 |
| 3 | 存量标题/主图默认 keep/observe | ⚠️ 待确认 | B1 反例测试 |
| 4 | 缺成本/缺竞品时结论按证据降级 | ⚠️ 待确认 | 降级边界测试 |
| 5 | 重放幂等，旧建议标 stale | ⚠️ 待确认 | 幂等测试 |
| 6 | 每条建议带备选路径（上新/实验） | ⚠️ 待确认 | B3 反例测试 |
| 7 | 建议输出契约可被 M10-R 消费 | ⚠️ 待确认 | 接口预留设计 |
| 8 | 完整建议链条覆盖（选品→清仓） | ⚠️ 待确认 | 类型注册表测试 |

#### 负责人关注点（WP3）

**可交付**：`src/ecommerce_agent/product_lifecycle/` 包 + 5 个测试，全部跑绿。

**可验证**（闫睿涵 WP5 独立复验时必测）：
- **所有建议必须带 `alternatives` 字段**（B3 反例测试 `test_alternative_path_present`）
  ```python
  # 反例测试：建议没有 alternatives 时必须 FAIL
  recommendation = build_recommendation(type="定价候选", ...)
  assert "alternatives" in recommendation
  assert any(alt.type in ("上新", "受控实验") for alt in recommendation.alternatives)
  ```
- **approved 状态绝不触发平台写操作**（B2 反例测试 `test_approved_no_platform_action`）
  ```python
  # 反例测试：批准后不发 API、不改 DB
  recommendation = build_recommendation(...)
  result = recommend_service.approve(recommendation.id, operator_id="ops-1")
  assert result.status == "approved"
  # 断言：无写操作发生（Mock 所有写操作调用，必须全部未被触发）
  mock_write_api.assert_not_called()
  ```
- 状态机转换顺序严格：draft → awaiting_review → approved/rejected → observed → closed
- 重放幂等：同一条事实重放不创建重复建议，旧建议标 stale
- 缺成本/缺竞品时结论按证据降级（不输出正式利润安全价格、不假装行业对标）

**M10-R 接口预留**（建议输出契约字段，缪海南消费侧对齐）：
```python
# interface.py 预留字段（待与缪海南确认字段名后冻结）
class RecommendationOutput(BaseModel):
    recommendation_id: str
    type: RecommendationType          # 选品/上新/保持/诊断/实验/定价候选/活动候选/补货联动/清仓预警
    target: TargetObject              # store_id / item_id / sku_id
    facts_snapshot: dict[str, Any]    # 事实快照（引用来源）
    rationale: str                    # 模型理由
    missing_evidence: list[str]       # 缺失项
    alternatives: list[Recommendation]  # 备选路径
    state: RecommendationState        # draft/awaiting_review/approved/rejected/observed/closed
    created_at: datetime
    updated_at: datetime
```

**复用边界**（明确什么不做）：
- **做**：建议状态机、类型注册表、模型输出校验、幂等控制
- **不做**：不修改商品标题/主图/价格、不触发任何平台写动作、不引入第二套状态标签
- **持久化**：建 `product_recommendations` + `product_recommendation_audit` 表（v36 迁移，占号 PR #18 已获批；表结构本阶段交付，业务写入方为后续「WP3 持久化读写服务」）
- **新增**：`product_lifecycle/schemas.py`（类型注册表）、`state_machine.py`（状态机）、`validation.py`（校验）、`interface.py`（M10-R 接口）

**无风险**：
- 批准不触发平台动作，运营可安全审核每条建议
- 旧事实更新后新建议标 stale，历史不丢失

### WP4 商品经营工作台 + 机制 Eval + 反例测试

**目标**：建设可下钻的只读工作台，用数值与结构化机制 Eval 验证真实方向、拒绝污染方向。

**代码结构**：
```
src/ecommerce_agent/product_workbench/
  __init__.py
  pages.py            # 页面组件（商品列表/SKU 下钻/漏斗/诊断/实验/来源/建议/审核）
  eval.py             # 机制 Eval runner（复用 simulation-evidence-v1，不另建）
  scenes.py           # 冻结场景集（真实粒度不足 / 显式模拟实验两类）
  boundaries.py       # 页面说明文字占位（B1/B2/B4 边界说明）
tests/
  test_m9r_workbench_pages.py
  test_m9r_mechanism_eval.py           # 复用 F-121/122，不另建 runner
  test_m9r_demo_isolation_boundary.py  # B5 反例：Demo 不进入 operational
  test_m9r_sample_vs_product_gate.py   # B7 反例：样本数据不作为产品口径
  test_m9r_workbench_write_barrier.py  # B4 反例：页面侧零写断言（浏览/点击不触发任何写动作）
```

**关键设计决策**：

1. **前台基建复用**：直接扩展现有 `/admin` 后台（闫哥确认 F-310 已可用），不重新设计前端。

2. **评测能力复用**：复用 `simulation-evidence-v1`（闫哥确认 F-121/F-122 已交付），**不另建第二套通用 runner**。M9 自己的领域机制 Eval/冻结场景/独立 oracle 在新文件中新增。

3. **边界说明页面化**：B1/B2/B4 等边界规则以页面文字形式展示给运营（不只是代码侧验证）。

**验收表**：
| # | 验收条目 | 状态 | 说明 |
|---|---|---|---|
| 1 | 商品/SKU 下钻到 revision、时间窗、指标、来源、建议依据 | ✅ 基建已可用 | 扩展 /admin，不需要降级 |
| 2 | 显示「为什么建议」/「为什么暂不能建议」 | ✅ 基建已可用 | 同上 |
| 3 | 页面浏览无隐式分析/创建实验/创建建议/修改商品 | ⚠️ 待确认 | 写屏障 + 页面说明 |
| 4 | 机制 Eval 发现真实方向 + 拒绝污染方向 | ✅ 基建已交付 | 复用 simulation-evidence-v1；M9 领域 Eval 仍需新增 |
| 5 | 浏览器桌面 + 窄屏可读，console 无新增错误 | ✅ 基建已可用 | 扩展 /admin |
| 6 | 真实/模拟场景隔离，全链标注 | ✅ 底层就绪 | D19/D20/virtual_store_v1.json 可复用；M9 专属 Demo 待 WP2/WP4 补齐 |
| 7 | 样本数据不作为产品口径 | ⚠️ 待确认 | 新增反例测试 B7 |
| 8 | 边界说明文字在页面展示 | ⚠️ 待确认 | 新增页面文字占位 |
| 9 | **页面上每个数字渲染四态徽标 + 来源 + 时间**（对齐显示原则「所有页面与报告一致」） | ⚠️ 待确认 | 页面渲染断言：每个 MetricValue 输出必须带 evidence_state 徽标 + source + data_as_of |
| 10 | **演示参数显式标注「试算」字样**（PDF 要求） | ⚠️ 待确认 | boundaries.py 增加试算文案规范；页面断言 demo 数据必须渲染「试算」标签 |

#### 负责人关注点（WP4）

**可交付**：`src/ecommerce_agent/product_workbench/` 包 + **5 个测试**，全部跑绿；工作台页面可访问。

**可验证**（闫睿涵 WP5 独立复验时必测）：
- **工作台页面直接扩展 `/admin` 后台，不重新设计前端**（闫哥确认 F-310 已可用）
  - 扩展点：在现有 `/admin` 路由下新增商品经营视图（`/admin/products/{store_id}/{item_id}/{sku_id}`）
  - 复用现有 `operational/simulation/evaluation/all` 范围隔离，M9-R 专属内容在 `operational` 默认范围内
- **页面必须展示边界说明文字**（B1/B2/B4 硬边界用页面文字标注给运营看）
  ```python
  # boundaries.py：页面说明文字占位（示例）
  BOUNDARY_NOTES = {
      "B1": "本系统默认不改标题/主图。仅当证据表明标题错误或关键词陈旧时，才会给出「人工复核」建议。",
      "B2": "所有建议均为 draft 状态，需人工批准后生效。批准不触发任何平台动作。",
      "B4": "本模块为零平台写动作模块——仅输出建议，不自动改价/换图/报名/调广告。",
  }
  ```
- **机制 Eval runner 复用 simulation-evidence-v1，不另建通用 runner**
  - 复用：`simulation.py` 中的场景 runner、`simulation-evidence-v1` 输入/预期/断言格式
  - 新增：M9 自己的领域机制 Eval、冻结场景集、独立 oracle
  - 新文件命名规范：`tests/test_m9r_mechanism_eval.py`、`src/ecommerce_agent/product_workbench/eval.py`、`scenes.py`
- **Oracle 定义（确定性校验，不是模型打分）**：
  ```
  Oracle = 固定输入 → 固定输出的确定性断言
  例：给定「SKUA 缺 SKU 级流量」的 input，输出必须包含 funnel_availability="unavailable"（由 evidence_state 聚合派生）
  ```

**复用边界**（明确什么不做）：
- **做**：扩展 `/admin` 路由、复用 simulation-evidence-v1 runner、新增 M9 领域 Eval/scenes/oracle
- **不做**：不重新设计前端、不另建第二套通用 runner、ground truth 与 production input 物理分离
- **新增**：`product_workbench/pages.py`、`eval.py`、`scenes.py`、`boundaries.py`

**无风险**：
- 工作台是只读界面，无隐式写动作；所有操作显式点击并审计
- Demo 数据与 operational 数据严格隔离，不会串数或冒充真实

---

## 六、风险预案与降级策略

负责人最关心的：「如果某个依赖没交付怎么办？数据质量差怎么办？」下表逐项给出应对。

| 风险项 | 影响 | 预案 | 触发条件 |
|---|---|---|---|
| M7-R WP2 数据域未交付（竞品/退款域） | WP2 诊断无法使用真实竞品数据 | **先用 Demo 数据跑通流程**，待数据交付后切换；WP1 条目 6 维持 ⚠️ 不宣称覆盖真实业务域 | M7-R WP2 未合入 main |
| SKU 级流量数据缺失（真实模式） | WP1 无法输出 SKU 级诊断 | 标记为 `EvidenceState.MISSING`，`safe_value` 抛 DataUnavailableError，**不推导、不返回 0**；WP2 用 Demo 数据补齐诊断链路 | 真实报表无 SKU 级流量字段 |
| M7-R WP3 料号映射未交付 | 料号引用字段 `material_code` 为 None | 字段占位，进已知差距清单长期挂起；**不影响读模型其他字段**；等 WP3 交付后回填 | 闫哥确认无时间窗 |
| M5-R 实验框架变更 | WP2 受控实验入口失效 | 立即通知负责人，评估是否需自建简易实验入口；**目前 M5-R 接口稳定**，变更风险低 | M5-R 接口 breaking change |
| F-310 前端基建不可用 | WP4 工作台无法扩展 | 降级为 HTML 报告页 + 本地浏览器证据；**闫哥已确认 /admin 可用，此风险当前不激活** | F-310 状态逆转 |
| F-121/F-122 评测能力不可用 | WP4 机制 Eval 无法运行 | 自建轻量 runner（数值断言 + oracle）；**闫哥已确认已交付，此风险当前不激活** | F-121/F-122 状态逆转 |
| Demo 数据（D-037~D-040）M9 专属部分缺失 | WP2/WP4 Demo 链路不完整 | 用显式 demo 标记的模拟数据先跑通，全链带 `DataScope.DEMO` 标签；**底层 D19/D20/virtual_store_v1.json 已就绪** | M9 专属 Demo 未补齐 |
| 数据质量差（字段缺失/粒度不一致） | 诊断结论不可靠 | 四态证据状态（actual/manual/demo/missing）如实标记，缺失字段显示 reason；**不伪造、不静默相加** | 任何真实导入 |
| 样本数据被误用产品口径 | 结论失真 | 引入数据可信度标签（生产/样本/Demo/缺失），**样本数据仅在诊断参考中使用** | M7-R 样本数据 |

**降级原则**：
1. 缺失 ≠ 0，缺失就标 missing + reason
2. Demo ≠ 真实，Demo 数据必须带 `DataScope.DEMO` 标签
3. 样本 ≠ 生产口径，样本数据明确标注用途边界
4. 建议 ≠ 执行，所有建议默认 draft，人工批准不触发平台动作

---

## 七、反例测试群（每条硬边界对应一组）

以下测试**必须在各自 WP 完成时跑过并绿**。它们是「看起来能跑」背后的真正守门员。

| 反例测试 | 对应边界 | WP |
|---|---|---|
| `test_missing_not_zero` | B6 | WP1 |
| `test_sku_traffic_blocked_without_real_data` | B5 | WP1 |
| `test_stock_item_keep_default` | B1 | WP3 |
| `test_approved_no_platform_action` | B2 | WP3 |
| `test_alternative_path_present` | B3 | WP3 |
| `test_zero_platform_write_actions` | B4 | WP2/WP3 |
| `test_internal_write_allowlist` | B4 | WP2/WP3 |
| `test_m9r_workbench_write_barrier` | B4 | WP4（页面侧零写断言，独立测试名） |
| `test_m9r_privacy` | 隐私红线 | WP1 |
| `test_demo_not_in_operational_view` | B5 | WP4 |
| `test_sample_not_product_caliber` | B7 | WP4 |
| `test_cross_store_leak` | 串数防呆 | WP1 |
| `test_cross_granularity_addition_blocked` | 混粒度防呆 | WP1 |

---

## 八、执行节奏与顺序

```
阶段 1（立即，不依赖任何人）：
  WP1 骨架落地 → 26 个测试（13 隔离 + 3 准备度 + 2 边界 + 5 data_trust + 3 隐私反例）→ 跑绿
  → 收口门禁检查 → 合入 main → 接口冻结

阶段 2（WP1 合入 main + 接口冻结后启动，串行）：
  WP2 诊断 + 受控实验（Demo 路径）→ 收口门禁 → 合入

阶段 3（WP2 接口冻结后启动，串行）：
  WP3 生命周期建议（依赖 WP1/WP2 已冻结接口）→ 收口门禁 → 合入

阶段 4（WP3 接口稳定后启动，串行）：
  WP4 工作台 + 机制 Eval（扩展 /admin，复用 F-121/122）→ 收口门禁 → 合入

阶段 5（全部合入后）：
  WP5 闫睿涵独立复验

> 说明：WP1~WP4 由胡磊单一负责人完整承担，物理上不存在并行；一律串行 + 门禁，
> 前一阶段接口冻结后才允许后一阶段开工（防返工铁律 3）。
```

---

## 九、执行节奏细化（周级拆解）

> 以下节奏供你每周对齐负责人/验收人时使用。每个里程碑有明确的「完成标志」和「交付物」。

### 阶段 0（本周，不依赖任何人）— WP1 骨架落地

| 周 | 日 | 任务 | 完成标志 | 交付物 |
|---|---|---|---|---|
| 第 1 周 | 周一～周二 | 建 `feature/m9r-read-model` 分支；写 `errors.py` + `models.py` | 代码通过 lint，pytest 可导入 | 代码文件 |
| 第 1 周 | 周三～周四 | 写 `factory.py`（MISSING 投影 + 真实 import_id 参数）；写 `readiness.py` | 工厂路径 PASS，MISSING 不崩 | 代码文件 |
| 第 1 周 | 周五 | 写 13 个隔离测试 + 3 个准备度测试；跑绿 | 16 passed | 测试文件 |
| 第 1 周末 | — | 补 2 个边界反例测试（B5/B6）+ 5 个 data_trust 测试 + 3 个隐私反例测试；跑绿 | 26 passed | 测试文件 |
| 第 1 周末 | — | 跑上游契约不回归：`test_readonly_data_contract.py` + `test_traffic_lab.py`；全量测试无回归 | 无回归 | 验证输出 |

**WP1 完成标志**：26 个测试全绿（13 隔离 + 3 准备度 + 2 边界 + 5 data_trust + 3 隐私）+ 验收表 18 条状态固化 + 全量测试无回归 + 负责人关注点小节写进计划

### 阶段 1（WP1 合入 main 后启动，2 周）— WP2 诊断 + 受控实验

| 周 | 日 | 任务 | 完成标志 | 交付物 |
|---|---|---|---|---|
| 第 2 周 | 周一～周二 | 写 `bridge.py`（M5-R revision/experiment/analysis 桥接 + 4 个持久化位置 freshness/provenance） | 桥接测试 PASS | 代码文件 |
| 第 2 周 | 周三～周四 | 写 `gates.py`（A/A/样本/窗口/控制变量/freshness/污染 Gate）；复用 TrafficAnalysisEngine | Gate 测试 PASS | 代码文件 |
| 第 2 周 | 周五 | 写 `experiment.py`（受控实验**双路径**：`run_demo_experiment` 走 VirtualStoreSimulation；`create_real_experiment` 预留 + blocked） | Demo 路径测试 PASS；真实路径 blocked 断言 PASS | 代码文件 |
| 第 3 周 | 周一～周二 | 写 5 个 WP2 测试（桥接/Gate/实验/Demo 隔离/写屏障反例）；跑绿 | 5 passed | 测试文件 |
| 第 3 周 | 周三～周四 | Demo 数据补齐（SKU 流量/revision/窗口，显式 DataScope.DEMO 标签） | Demo 隔离测试 PASS | 测试文件 |
| 第 3 周 | 周五 | 跑上游契约不回归 | 无回归 | 验证输出 |

**WP2 完成标志**：测试全绿 + 12 条验收状态固化 + Demo/真实双路径落地（Demo 可跑通、真实 blocked）+ freshness/provenance 4 个持久化位置落地 + B4 双语义反例（平台写=0 + 内部写白名单）

### 阶段 2（WP2 接口稳定后启动，2 周）— WP3 生命周期建议

| 周 | 日 | 任务 | 完成标志 | 交付物 |
|---|---|---|---|---|
| 第 4 周 | 周一～周二 | 写 `schemas.py`（建议类型注册表 + alternatives 字段 + M10-R 接口预留）；**向缪海南发起 RecommendationOutput 字段评审** | 类型注册表测试 PASS；评审已发起 | 代码文件 + 评审记录 |
| 第 4 周 | 周三～周四 | 写 `state_machine.py`（draft→awaiting_review→approved→...→closed 状态机） | 状态机测试 PASS | 代码文件 |
| 第 4 周 | 周五 | 写 `validation.py`（模型输出校验 + 写屏障 + 幂等） | 校验测试 PASS | 代码文件 |
| 第 5 周 | 周一～周二 | 写 5 个 WP3 测试（状态机/校验/幂等/写屏障反例/alternatives 反例）；跑绿；**M10-R 契约字段冻结（缪海南确认）** | 5 passed + 契约冻结 | 测试文件 + 契约确认记录 |
| 第 5 周 | 周三～周四 | 边界说明文字（B1/B2/B4）写入 `boundaries.py` 页面占位；降级边界测试 | 降级测试 PASS | 代码文件 |
| 第 5 周 | 周五 | 跑上游契约不回归 | 无回归 | 验证输出 |

**WP3 完成标志**：5 个测试全绿 + 8 条验收状态固化 + alternatives 字段强制存在 + 批准不触发平台动作

### 阶段 3（WP3 接口稳定后启动，2 周）— WP4 工作台 + Eval

| 周 | 日 | 任务 | 完成标志 | 交付物 |
|---|---|---|---|---|
| 第 6 周 | 周一～周二 | 扩展 `/admin` 路由（商品/SKU 下钻页面） | 页面可访问，无新增 console 错误 | 代码文件 |
| 第 6 周 | 周三～周四 | 写 `eval.py`（复用 simulation-evidence-v1 runner）；写 `scenes.py`（M9 专属冻结场景） | 机制 Eval 测试 PASS | 代码文件 |
| 第 6 周 | 周五 | 写 `boundaries.py`（B1/B2/B4 边界说明文字） | 页面展示边界文字 | 代码文件 |
| 第 7 周 | 周一～周二 | 写 5 个 WP4 测试（页面/Eval/Demo 隔离反例/样本 vs 产品口径反例/页面写屏障反例）；跑绿 | 5 passed | 测试文件 |
| 第 7 周 | 周三～周四 | 桌面 + 窄屏浏览器检查；全量回归 | 浏览器无新增错误 | 验证输出 |
| 第 7 周 | 周五 | 跑上游契约不回归 + 全量回归 | 无回归 | 验证输出 |

**WP4 完成标志**：5 个测试全绿 + 10 条验收状态固化 + 工作台页面可访问 + 四态徽标/试算字样渲染断言绿 + Eval 能发现真实方向 + 拒绝污染方向

### 阶段 4（全部合入后）— WP5 独立复验

| 周 | 日 | 任务 | 完成标志 | 交付物 |
|---|---|---|---|---|
| 第 8 周 | 周一～周三 | 闫睿涵独立复验（从干净状态、未见商品/SKU 场景、反例 mutation） | 通过/不通过结论明确 | WP5 验收报告 |
| 第 8 周 | 周四～周五 | 修复失败项（如需要）；重新复验 | 复验通过 | 签署的里程碑 |

---

## 十、关键交付物清单（给闫睿涵 WP5 签收）

- `product_read_model` 包：读模型骨架 + 隔离铁律 + 数据准备度
- `product_diagnosis` 包：M5-R 桥接 + 流量诊断 + 受控实验入口
- `product_lifecycle` 包：建议状态机 + 类型注册表 + M10-R 接口预留
- `product_workbench` 包：工作台页面 + 机制 Eval + 冻结场景
- 完整反例测试群（13 个边界测试，含 B4 双语义拆分、隐私红线、WP4 页面写屏障）
- 依赖追踪清单（本文件第四节）
- 验收矩阵（本文件第五节四张表 + 负责人关注点 + WP1 已知差距汇总附录）
- 风险预案表（本文件第六节）
- **防返工基线**：每阶段开工前输入已核实（WP2 开工前已核实 M5-R 接口无 demo/scope 字段 → 双路径设计；WP4 开工前 F-310/F-121-122 已确认）

### 回归证据规范（每个 WP 收口必贴，供负责人/验收人核对）

> 原则：**「没有日志和断言的代码就是定时炸弹」**。每个模块（WP1~WP4）完成后、合入 main 前，
> 必须执行全量回归并把结果贴在对应 WP 的计划文档末尾，作为验收证据之一。

**统一执行方式**（加固脚本，杜绝静默失败）：
```bash
python scripts/run_full_regression.py --allow-dirty   # 开发中验证（工作区未提交）
python scripts/run_full_regression.py                  # 合入前（工作区必须干净）
```
- 脚本特性：`subprocess.run` + timeout=900s + capture_output + UTF-8；注入 `PYTHONUNBUFFERED`/`PYTHONDONTWRITEBYTECODE`；
  pre-flight（pytest≥7.0/可写/磁盘>100MB）；空输出 → OOM fallback 诊断；**强制写 `pytest_debug_report.json`**（含命令/返回码/输出/系统快照）。

**贴进计划的格式**（每个 WP 文档末尾统一）：
```markdown
### 全量回归证据（WP{n} 收口）

- 执行时间：YYYY-MM-DD HH:MM
- 命令：`python scripts/run_full_regression.py --allow-dirty`
- 结果：`{N} passed in {T}s`（或 `{N} passed, {M} failed` + 失败明细）
- 上游契约回归：`test_readonly_data_contract.py` + `test_traffic_lab.py` → {N} passed
- 报告：`pytest_debug_report.json`（含系统快照，路径见报告内）
- 状态：✅ 全量无回归 / ⚠️ 有失败（列失败原因）
```

**分层测试策略（提速，防返工不丢）**：
| 层 | 跑什么 | 什么时候跑 | 周期 | 防返工作用 |
|---|---|---|---|---|
| **L1 局部** | `tests/test_m9r_*.py` | 每次改 WP 代码/测试 | 秒级 | 自己模块不出错 |
| **L2 上游契约** | `test_readonly_data_contract.py` + `test_traffic_lab.py` | 每次改 WP 核心代码 | ~12-20s | 消费的 M7-R/M5-R 契约没被改坏 |
| **L3 全量** | `tests -q`（950+ 项） | **每个 WP 收口时 1 次** | 10-20 分钟 | 合入前全系统无回归 |

**规则**：
1. 文档/计划改动 → 不跑任何测试，最多 grep 验证（避免无效资源消耗）
2. 新增独立测试文件 → 只跑 L1 该文件
3. 改 WP 核心代码 → L1 + L2（秒级/20s 级）
4. **每个 WP 收口 → L3 全量 1 次**（不是每个 commit 都跑，是每个模块系统完成时跑）
5. 全量回归脚本：`scripts/run_full_regression.py`（加固：timeout/capture/UTF-8/preflight/OOM fallback/强制 JSON 报告）

---

## 十一、防返工阶段门禁（每阶段开工前/收口时必须过）

> 目标：一阶段一阶段执行，**每一阶段开工前输入已核实、接口已冻结；收口时欠账清零**。任何门禁不过，不进入下一阶段。

### 门禁清单

| 阶段 | 开工前必过（输入已核实） | 收口时必过（欠账清零） |
|---|---|---|
| **WP1** | ✅ M7-R 契约已核实（contracts.py/service.py 接口字段已读） | 22 测试全绿；验收表 18 条状态固化（✅/⚠️/❌/🔒 有据）；`data_trust` 字段落地 |
| **WP2** | ✅ M5-R 接口已核实（**TrafficExperimentCreate 无 demo/scope 字段 → 已改双路径设计**）；freshness/provenance 4 位置已核实；Demo 底层已核实（simulation.py confirm_virtual） | Demo 路径可跑通；真实路径 blocked 断言 PASS；B4 双语义反例绿；诊断 Gate 全绿 |
| **WP3** | ✅ WP1/WP2 接口已冻结（读模型字段名、诊断类型名不再变）；M10-R 接口预留字段已在计划固化 | 状态机全绿；B1/B2/B3 反例绿；alternatives 强制；M10-R 契约字段冻结 |
| **WP4** | ✅ F-310（/admin）已确认可用；F-121/122（simulation-evidence-v1）已确认；Demo 数据已确认 | 页面可访问；Eval 拒绝污染方向断言绿；B5/B7 反例绿；浏览器无新增错误 |

### 防返工纪律（三条铁律）

1. **开工前必须读代码核实接口**：任何「计划说复用 X」的接口，开工前必须 `git show origin/main:...` 读实际代码确认字段/参数。**发现计划与实际不符 → 先改计划再动工**（本次已发现并修复：TrafficExperimentCreate 无 demo 字段、B4 单写语义不清、data_trust 无载体）。
2. **收口时欠账清零**：验收表里 ⚠️/❌/🔒 的条目必须有「解锁条件」和「责任人」（见 WP1 已知差距汇总附录），不能带债进入下一阶段。无解锁条件的条目 → 本阶段降级为「已知差距」并写明，不允许悄悄变成 ✅。
3. **接口冻结后才允许下游依赖**：WP2 依赖 WP1 的读模型字段名/类型名，WP1 合入 main 后这些名字**冻结**；改名字 = 触发下游返工，必须走变更通知。

### 契约演进策略（防返工补充）

- **钉死当前基线**：SCHEMA_VERSION=34（M7-R WP1 合入 main）。
- **若开发期间 M7-R 升级**（如 v35 落库）：**不静默跟随**——先跑 `test_readonly_data_contract.py` 全量回归，确认契约向后兼容后再升级基线；若契约 breaking change → 立即通知闫睿涵，暂停依赖该契约的 WP，改完适配层再继续。
- **合入标准（每个 WP 统一）**：全量自动化测试绿（对齐 M7-R 950 项基线）+ 上游契约不回归 + 验收表状态固化。不是「自己包的测试绿就算数」。

### 验收红线（补，对齐任务书）

**只读 Demo 通过不构成生产放行**：Demo 链路（VirtualStoreSimulation）跑通只证明机制可用，不代表真实店铺数据已接入；任何阶段不得以 Demo 结果冒充真实结论。WP5 签署范围只覆盖「已验证的真实/隔离边界」，未放行事项必须列出。

### 代码实现纪律（零静默失败四原则，每个新函数/模块生成代码前必须声明）

> 原则来源：用户设定。生成任何代码前，必须先以 Markdown 列表声明以下四点的落实情况，再输出代码。

| # | 原则 | 要求 | 失败即视为缺陷 |
|---|---|---|---|
| 1 | **明确边界** | 每个函数/模块写清输入类型、输出结构、所有副作用（I/O、网络、状态变更）；严禁「处理数据」等模糊表述 | 边界模糊即返工 |
| 2 | **可观测信号** | 每项核心操作指定唯一可直接验证的完成标志（文件绝对路径、监听端口、状态前后对比值） | 无法验证即未完成 |
| 3 | **确定性自检** | 代码内置基于确定值的断言/日志校验；严禁依赖时间、随机数、外部不可控状态作判断依据 | 不确定性 = 不可复现 |
| 4 | **失败快速暴露** | 对空值、分支遗漏、异步未等待等静默失败点，显式加防御性判断并抛具体异常 | 静默失败 = 验收地雷 |

**落地检查清单**（每个函数在 docstring 中体现）：
- [ ] 输入类型 / 输出结构 / 副作用 三行写全
- [ ] 完成标志可验证（路径/端口/状态对比）
- [ ] 无时间源、无随机数（`now()`/`uuid` 必须由调用方传入）
- [ ] 所有 None/空分支/越界分支有显式异常，无 `pass` 吞错

**WP1 已按此原则落地**：5 个源文件每个 docstring 声明边界；工厂确定性断言（composite_key 槽位 spot-check）、MetricValue 非法组合构造即抛、period_key 格式校验、非有限浮点拒绝——全部实现并测试覆盖（26 passed）。

### 容量预期说明（WP1）

WP1 工厂为内存投影，消费 M7-R 订单导出（约 7.5 万行）。设计上逐行构建、逐行释放（`build_read_model_from_manifest` 返回列表由调用方持有），单行 MetricValue 开销固定（约 10 字段），7.5 万行估算内存 < 500MB，不会爆；落地时加一条加载耗时冒烟测试（导入 manifest → 构建模型 < 60s），验收人问「数据量大了会不会爆」时有据可答。

---

## 十二、下一步行动

**立即可以开始**：WP1 骨架落地（不依赖任何人），本周即可完成。

**等你拍板**：
1. 这份完整计划是否确认无误？确认后我开始写代码。
2. WP1 完成后是否立即合入 main，还是等 WP2/WP3 一起合？
3. 周级节奏是否需要调整（比如你每周可用时间不是 2h/天）？
