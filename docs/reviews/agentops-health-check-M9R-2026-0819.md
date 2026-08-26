# M9-R AgentOps 体检报告（复审修复后）

> **生成时间**：2026-08-19
> **审查者**：Claude (agentic review)
> **目标版本**：HEAD `652de44`（未提交工作区）
> **基线对照**：`origin/main`（M9-R 相对 main 全是新增，3738 insertions, 0 deletions）

---

## 审查任务书

- **审查范围（含）**：`product_read_model/`、`product_diagnosis/`、`product_lifecycle/`、`product_workbench/`、`workbench_api.py`、`business/service.py` M9-R 部分、`evaluation_api.py`
- **审查范围（含依赖边界）**：`traffic_lab/service.py`、`traffic_lab/freshness.py`、`readonly_data/contracts.py`、`database.py` 相关 schema
- **审查范围（不含）**：客服 Agent 拓扑、库存/订单写入路径、营销/财务模块、全文 M9-R 代码改动逻辑（这是复审修复，不是从零实现）
- **必查组件**：`references/complete-agent-architecture.md` 全部基线组件（按 T3 逐分类）
- **必跑测试（实际跑了）**：
  - M9 定向 200 passed
  - 相邻回归（M9+迁移+traffic_lab+readonly_data 契约）218 passed
  - WP1-4 验收脚本 4 个全部 PASS
  - agentops 3 个修复验证 31 passed
- **必跑测试（未跑）**：全量回归 `tests/`（900s 超时，仓库既有约束；M9-R 改动不触及全量公共路径，相邻回归已覆盖依赖域）
- **基线对照**：`git diff origin/main` — M9-R 全是新增，无 merge 丢失
- **已知决策（用户已拍板，不重审）**：
  1. query 路径消费领域事实表不经过 M7-R `readonly_import_manifests`（批次 1 已拍板为"字段语义诚实化"）
  2. D-041 evidence_state 四态（actual/manual/demo/missing）
  3. B1/B2/B3/B4 写屏障硬边界
  4. D-034 确定性只答能不能
  5. 浏览器证据由闫睿涵 WP5 复验（不引入 playwright）
  6. C2 幂等按 id 不按内容（已回滚原计划）

---

## 体检结论

- **判定**：`risky`
- **适用模板**：**T3 Production Project**（多租户 SQLite + 人工审核状态机 + 业务写屏障 + 独立 WP5 验收门槛）
- **一句话结论**：M9-R 核心读写链路接线完整、M9 定向 + 相邻回归全绿，但门禁越权拦截在输出侧潜伏、读侧完整性 list 路径曾缺覆盖——**建议把 3 项中优先级 gap 补完后交闫睿涵 WP5 独立复验**。
- **置信度**：`high`（实测数据 200+218+4 PASS + 双 agentops 审查 agent 交叉确认）

---

## 完整架构基线（T3 对齐）

| 基线组件 | T3 要求 | 状态 | 当前证据 | 缺口 / 理由 |
|---|---|---|---|---|
| System boundary | required | **adequate** | [PROJECT_OVERVIEW.md](.project-to-act/PROJECT_OVERVIEW.md) 有 M9-R 范围/D-041/B1-B4；[M9R_WORKBENCH](docs/tasks/M9R_PRODUCT_TRAFFIC_LIFECYCLE_WORKBENCH.md) 明确四边 | 无 |
| Task intake | required | **strong** | `business/service.py` 工具注册表 + `kind="read"` 过滤 | 无 |
| Agent loop | lightweight | **not-needed** | M9-R 无 LangGraph 主循环，仅 HTTP 路由消费领域服务 | T3 不要求独立 loop |
| Tool layer | required | **strong** | `BusinessModuleRegistry` + `ToolSpec(kind/policy/handler)` 显式声明；read 类 13 个已登记 | 无 |
| State machine | required | **strong** | `state_machine.py` 完整 `_TRANSITIONS` 图（含 STALE/CLOSED 终态 + REJECTED→MARK_STALE→STALE→CLOSED） | 无 |
| Project ledger | required | **strong** | `.project-to-act/PROJECT_{OVERVIEW,PROGRESS,FEATURES,VERSIONS,ACCEPTANCE}.md` 五文档 + E-20260819-001 已登记 | 无 |
| Evidence system | required | **strong** | `readonly_data.contracts.FieldEvidenceInput` + `MetricValue.import_manifest_id/data_as_of/authoritative_service` 强制非空；M9-R query 路径来源诚实化（批 1 修复） | 无 |
| Gate system | required | **adequate** | `GateEngine.run_all`（evidence/freshness/quality_gate）+ `EvidenceBridge.run_gates`（加 model_output 越权门）；**但输出侧越权拦截潜伏态**（见关键问题） | run_all 不含 output gate（B5 修复后已接） |
| Session/users | required | **strong** | `AdminPrincipal.tenant_id` 作用域隔离；工具侧 `trusted_context.store_id` 回落；HTTP 路由必填 `store_id` 跨店 409 | 无 |
| Memory | lightweight | **not-needed** | M9-R 无 agent 记忆机制；领域事实表即权威存储 | T3 不强制独立 memory |
| Retrieval/context | lightweight | **adequate** | `ProductReadQuery._traffic_facts`/`_inventory_facts`/`_order_facts` 查询按 revision 窗口过滤 | `_order_facts` 的 period_key 用窗口起始（已知近似，文档声明） |
| Artifacts/workspace | not-needed | **not-needed** | 无文件系统 artifact 产出 | T3 不涉及 |
| Registry/routing | required | **strong** | `business/registry.py` 模块注册表 + 工具名/入参 schema/策略三字段 | 无 |
| Multi-agent handoff | lightweight | **not-needed** | M9-R 无跨 agent 移交；仅 admin 视角工具/路由 | T3 轻量级 |
| Coordination/conflict | lightweight | **strong** | SQLite WAL + `_write_lock` + `BEGIN IMMEDIATE` 事务；`payload_hash` 不可变触发器 | 无 |
| A2A/MCP | not-needed | **not-needed** | 无 MCP 调用 | M9-R 无 MCP 接入 |
| Observability | lightweight | **weak** | `db.audit("recommendation.create/state_transition")` 落审计日志；缺 trace/cost/latency 指标 | 无工具调用时延统计 |
| Evaluation | required | **adequate** | `MechanismEvalRunner` 有机制 Eval（9 冻结场景）+ `verification_api.py mechanism` 端点；缺 golden task 集 + regression | **见关键问题** |
| Guardrails/security | required | **strong** | `validate_full_recommendation` 越权扫描（5 处 FORBIDDEN_OUTPUT_KEYS）+ `contains_forbidden_token` DFS + read-model 来源诚实 | 无 |
| Self-evolution | not-needed | **not-needed** | 无自进化能力 | 目标系统不要求 |

---

## 缺失组件清单

| 优先级 | 缺失组件 | 为什么重要 | 建议补齐方式 |
|---|---|---|---|
| **高** | **`run_all` 组合门禁不含 `check_no_forbidden_output`**（B5 修复后已接） | 如果 `run_gates` 作为唯一门禁入口，越权拦截不在组合结果中；本次已修复 | ✅ 已在 `bridge.py:237-253` 接 model_output 可选参数；后续工具接入时传 |
| **中** | **get_experiment_view / ExperimentGateway 零生产消费者** | WP4 交接内容明确"真实/演示双路径"；demo 实验路径目前不可达 | 在 workbench 暴露 demo 实验端点（强制 confirm_virtual），或模块 docstring 显式标注"本阶段仅修订" |
| **中** | **`validate_model_output` 双实现**（导出但实际内联在 full_validation） | dead code 信号；后续开发者可能改旧入口忽略新内联 | 删除 `validate_model_output` 导出，改为内部函数或显式标记 deprecated |
| **低** | **read-model/workbench/evidence-gates 仅 HTTP 无 agent 工具** | D-034 确定性能力仅限 admin 视图，LLM agent 运行时无法 consult gates | 若 agent 需 runtime consult，注册只读工具；否则文档明确边界 |
| **低** | **workbench view metrics(revision=1) 与 gates(latest) 可能不同 revision** | 多 revision SKU 下"为什么暂不能建议"归因误导 | workbench view 暴露 revision 参数并保持同 revision |

---

## 架构地图

| 模块 | 当前证据 | 评分 | 说明 |
|---|---|---|---|
| `product_read_model/query.py` | revision 隔离 + 粒度诚实 + 来源诚实 + demo/actual 派生 + 跨仓汇总 | **strong** | `[test]` test_m9r_query_source_honesty 9 passed + verify_wp1 ⑧ ✅ 升级 |
| `product_diagnosis/bridge.py` | freshness 修正 + revision quality_gate + provenance 格式 + latest_revision_view | **strong** | `[test]` test_m9r_diagnosis_bridge + test_m9r_gates_production 9 passed |
| `product_diagnosis/diagnosis.py` | conclusion fail-closed + 污染自动反推 | **strong** | `[test]` test_m9r_diagnosis 23 passed |
| `product_diagnosis/gates.py` | run_all 组合三关 | **adequate** | `[test]` test_m9r_diagnosis_gates 8 passed；输出侧 gate 潜伏态（见关键问题） |
| `product_lifecycle/service.py` | create 审计 + transition 审计 + read-side hash（list 已补） | **strong** | `[test]` test_m9r_lifecycle_persistence_service 15 passed |
| `product_lifecycle/state_machine.py` | STALE 补全 + REJECTED→MARK_STALE + STALE→CLOSE | **strong** | `[test]` test_m9r_lifecycle_state_machine 6 passed + verify_wp3 t05 ✅ |
| `product_lifecycle/validation.py` | validate_full_recommendation 越权扫描 + FORBIDDEN_OUTPUT_KEYS 扩展 | **strong** | `[test]` test_m9r_lifecycle_validation 9 passed + test_m9r_forbidden_output_recursive |
| `product_workbench/eval.py + scenes.py` | 9 冻结场景 + MechanismEvalRunner | **adequate** | `[test]` test_m9r_mechanism_eval 6 passed；缺 regression 集 |
| `workbench_api.py` | read-model + insights + evidence-gates + workbench JSON view + recommendations CRUD | **strong** | `[test]` test_workbench_api 9 passed（含 transition 归属 409） |
| `evaluation_api.py` | mechanism 端点 | **adequate** | `[test]` test_m9r_workbench_view 4 passed（含 mechanism） |
| `business/service.py` | EvidenceBridge 接线 + _list_recommendations_tool 回落 | **strong** | `[trace]` service.py:160-165 + service.py:894-903 |
| `readonly_data/contracts.py` | source_type_from_connector + evidence_state_from_source_type 上提 | **strong** | `[trace]` contracts.py:93-115 + query.py:78-79 + bridge.py:47-48 |

---

## 功能审查报告

| 功能/能力 | 是否存在 | 完整度 | 主要问题 | 建议 |
|---|---|---|---|---|
| 经营读模型（SKU 层） | ✅ 存在 | 95% | 流量/订单混源（orders 来自流量桶、payments 来自 commerce）；`Period_key` 粒度标记正确但 `period_key` 格式有 HOURLY 新扩展 | 加漏斗一致性校验（orders ≤ payments） |
| 证据桥接（M5-R） | ✅ 存在 | 90% | experiment_view freshness 已修正；revision quality_gate 已补；但 get_experiment_view/list_analysis_runs_view 无 HTTP 路由 | 补 experiment evidence 路由或标注 deferred |
| 门禁 Gate | ✅ 存在 | 85% | run_all 三关 + run_gates 可选 output gate；结论 allowed fail-closed；污染自动反推 | 诊断工具接入时强制传 model_output |
| 生命周期建议 | ✅ 存在 | 95% | create/transition POST 生产入口齐全；归属校验；STALE 闭环；validator 越权扫描；审计落痕 | 无 |
| 工作台 JSON view | ✅ 存在 | 90% | workbench 视图含 metrics + evidence_gates + why_not_recommended；无 HTML 渲染（已拍板） | WP5 浏览器复验 |
| 机制 Eval | ✅ 存在 | 75% | 9 冻结场景 + oracle 确定性断言；缺 golden 回归集 + mutation 测试 | 建 evals/m9r/golden/ + 定期跑 |
| Demo/Actual 隔离 | ✅ 存在 | 95% | virtual connector → DEMO/DEMO；operational → ACTUAL/PRODUCTION；`_TRUST_BY_STATE` 约束合法组合 | 无 |
| 幂等 | ✅ 存在 | 90% | create 按 (id, payload_hash)；record_transition 按 (action, actor, occurred_at)；C2 幂等按内容已回滚 | 无 |
| 跨店铺归属 | ✅ 存在 | 95% | tool 侧 policy；HTTP detail/audit/transition 必填 store_id + 409 | 无 |

---

## 关键问题

| 优先级 | 问题 | 为什么重要 | 建议修复 |
|---|---|---|---|
| **中** | **`output_scope` 越权 Gate 在生产潜伏态** | `run_gates` 两处生产调用均不传 `model_output`；诊断越权扫描仅在 eval 路径 | 诊断/工作台工具接入时**强制** `run_gates(model_output=...)` |
| **高** | **`list()` 读路径不校验 payload_hash** | 篡改行可在列表中被列出而不报错（本次 **已修复**，见 E-20260819-001） | ✅ 已补 |
| **中** | **transition 路由无 store_id 归属校验** | 租户管理员可跨店铺流转建议（本次 **已修复**，见 E-20260819-001） | ✅ 已补 |
| **中** | **query 路径 connector_id=None 时 value 非空会抛异常** | 有值但来源未知时，当前靠 schema NOT NULL 兜底（本次 **已修复**，见 E-20260819-001） | ✅ 已补 |
| **中** | **机制 Eval 无 golden task 集 + regression 集** | `MechanismEvalRunner` 目前仅 run FROZEN_SCENES 一次；无回归集、无 mutation 测试 | 建 `evals/m9r/golden/` + 定期跑 `verify_m9r_mechanism_regression.py` |
| **低** | **全量回归 900s 超时** | 单进程 + SQLite 慢（仓库既有约束）；M9-R 改动不触及公共路径 | 分区跑或加 timeout 配置；相邻回归 218 passed 已覆盖 |
| **中** | **get_experiment_view / ExperimentGateway 零生产消费者** | WP4 交接明确"真实/演示双路径"；demo 实验路径目前不可达 | 暴露 demo 实验端点或 docstring 标注本阶段仅修订 |

---

## 优化建议

| 优先级 | 建议 | 预期收益 | 实施成本 | 验收方式 |
|---|---|---|---|---|
| **高** | 诊断工具接入时强制 `run_gates(model_output=...)` | 输出侧越权拦截在生产可触达 | 低（1 行签名改动 + 1 处调用点） | 加 `test_m9r_gates_output_scope.py` 断言 |
| **中** | 机制 Eval 建 golden regression 集 | 冻结场景可回归；避免修改 scenes.py 后静默失败 | 中（建目录 + 写 3-5 个黄金用例） | `python tests/verify_m9r_mechanism_regression.py` 跑通 |
| **中** | workbench view 统一 revision 参数 | metrics 与 gates 同 revision，避免"为什么暂不能建议"归因错误 | 低（1 个 Query 参数 + 路由透传） | test 断言 metrics + gates 同 revision |
| **低** | 读模型 `orders ≤ payments` 漏斗一致性校验 | 避免流量桶 orders 与 commerce payments 混源导致逆漏斗 | 中（query.py 加一次 SUM 比对） | test 断言逆漏斗报 reason |
| **低** | 审计 log 加 latency 追踪 | 观察 create/transition 耗时 | 低（2 行 timing） | 单元测试 |

---

## 测试证据

| 测试集 | 命令 | 结果 | 证据类型 |
|---|---|---|---|
| M9 定向 | `pytest tests/test_m9r_* tests/test_workbench_api.py tests/test_product_read_query.py tests/test_m9r_gates_production.py tests/test_m9r_query_source_honesty.py tests/test_m9r_workbench_view.py` | **200 passed** | `[test]` |
| 相邻回归 | `pytest tests/test_m9r_* tests/test_workbench_api.py tests/test_product_read_query.py tests/test_migrations.py tests/test_traffic_lab.py tests/test_readonly_data_contract.py` | **218 passed** | `[test]` |
| WP1-4 验收脚本 | `python tests/verify_wp{1,2,3,4}_acceptance.py` | **4/4 PASS** | `[test]` |
| agentops 修复验证 | `pytest tests/test_workbench_api.py tests/test_m9r_lifecycle_persistence_service.py tests/test_m9r_query_source_honesty.py tests/test_product_read_query.py` | **31 passed** | `[test]` |
| agentops 审查（M9-R 内部） | agent `a4b5e294` 审查 + 34 测试跑 | **187 passed** | `[test]` |
| agentops 审查（依赖边界） | agent `a68f3996` 审查 + 76 测试跑 | **76 passed** | `[test]` |
| 全量回归 | `scripts/run_full_regression.py --allow-dirty` | **900s 超时，6% 处 kill** | `[unverified]` 仓库既有约束 |

---

## 本次复审修复一览（E-20260819-001）

| 批次 | 修复内容 | 测试证据 |
|---|---|---|
| 批 1 WP1 | revision 隔离 / 粒度诚实 / 来源诚实化 / demo-实际派生 / 跨仓汇总 / Granularity.HOURLY 扩展 / Period key 格式 | test_m9r_query_source_honesty 9 + test_product_read_query 原用例 + verify_wp1 ⑧ 升级 |
| 批 2 WP2 | evidence-gates 路由 + bridge EvidenceBridge 实例化 + experiment freshness 修正 + revision quality_gate + conclusion fail-closed + 污染反推 | test_m9r_gates_production 9 |
| 批 3 WP3 | POST create/transition + 店铺归属 409 + state 400 + list 回落 trusted store + STALE 状态机补全 + 竞品防线 + validator 越权扫描 + 审计落痕 + 读侧 hash + t05 CLOSED→STALE | test_workbench_api 9 + test_m9r_lifecycle_persistence_service 15 |
| 批 4 WP4 | workbench JSON view + 机制 Eval 端点 + revision 下钻 + reason_not_recommended | test_m9r_workbench_view 4 |
| agentops 修复 | transition 归属 409 + list hash + query connector None 防御 | 31 passed |
| 验收脚本修复 | WP1 ⑧ 真实断言 / WP2 ⑫ `_bridge`→`_real_bridge` + 正确 tenant / WP3 ④ degraded missing_evidence | 4 个脚本全 PASS |

---

## 下一步

1. **交闫睿涵 WP5 独立复验**——这是最终交付边界；上述所有修复和证据登记（E-20260819-001）已完成
2. **补机制 Eval regression 集**（优先级中，非阻塞）：在 `evals/m9r/golden/` 建 3-5 个 gold 场景，定期跑
3. **诊断工具接入时** 强制传 `model_output` 给 `run_gates`（避免 output_scope gate 潜伏态）
4. **demo 实验路径** 在 WP5 浏览器复验时由闫睿涵决定：要么 workbench 暴露 demo 端点，要么模块 docstring 显式标注本阶段仅修订

---

**报告保存位置**：`agentops-health-check-M9R-2026-0819.md`（本报告）
**源代码/配置/运行时文件未改动**（报告是纯文档输出）
