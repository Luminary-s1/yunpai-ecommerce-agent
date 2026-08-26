# M9-R AgentOps 健康检查报告（第 5 轮修复前基线）

> **生成时间**：2026-08-21
> **审查者**：Claude（agentic review，agentops-awesome-list 技能）
> **目标版本**：HEAD `1d53871` + 工作区未提交改动（第 5 轮修复前）
> **基线对照**：`454b35c9000ab279ffdbf115f80afdf3e031ee73`（负责人 WP5 固定 Base）
> **前置报告**：[agentops-health-check-M9R-2026-0819.md](agentops-health-check-M9R-2026-0819.md)（8-19 复审修复后）

---

## 审查任务书

- **审查范围（含）**：`product_read_model/query.py`、`product_diagnosis/`、`product_lifecycle/`、`product_workbench/`、`workbench_api.py`、`business/service.py` M9-R 部分、`docs/admin-console.html` 工作台
- **审查范围（不含）**：客服 Agent 拓扑、库存/订单写入路径、营销/财务模块、M10-R（并行工作流）
- **必查组件**：`references/complete-agent-architecture.md` 全部基线组件（T3 Production 档）
- **必跑测试（实际跑了）**：
  - 5 个核心 M9-R 文件 + mechanism_eval：`pytest test_m9r_query_source_honesty test_m9r_item_isolation_overlap test_m9r_diagnosis_production test_m9r_production_recommendation_chain test_product_read_query test_m9r_mechanism_eval` → **36 passed** `[test]`
  - 全量 collect：`pytest --collect-only` → **1266 tests** `[test]`
  - 生产调用链 grep：diagnose / generate_and_persist / build_diagnosis_facts / quality_gate 消费点 `[trace]`
  - 基线 diff：`git diff 454b35c..HEAD` → 108 文件 +15689 行，净增无 merge-loss `[baseline]`
- **必跑测试（未跑）**：全量回归 `pytest tests`（190s，复验基线 `1260 passed/1 failed/1 skipped`；第 5 轮修复后必须重跑全绿）
- **基线对照**：`454b35c..HEAD` 净增，无 merge-loss 回归
- **已知决策（用户已拍板，不重审）**：
  1. net_sales 多行订单 → MISSING + 独立 reason
  2. R5 Eval → 方案 C-lite（facts_snapshot 透传信号 + mock 解释器，非降级真实方向）
  3. R3 → 结构化 `degradation_reasons`，reason 保持稳定码
  4. 验收框架学闫睿涵思路，不照搬 M10-R
  5. 优先级：贴合任务书要求 + 负责人发现的错误必须解决

---

## 体检结论

- **判定**：`risky`（第 5 轮修复前）
- **适用模板**：**T3 Production Project**
- **一句话结论**：M9-R 生产调用链完整接线、36 定向测试全绿、基线净增无 merge-loss；但负责人第 4 轮 7 个阻断项中 **R2 三缺陷、R3 部分、R5、R7 仍未修**，且其中 R2 的 net_sales 冒充、R5 的 Eval 假覆盖是"测试绿但能力未证明"的假绿残余——第 5 轮修复后需全量回归全绿才算通过验收。
- **置信度**：`high`（36 passed [test] + diff [baseline] + 生产链 [trace] + 负责人复验原文对照）

---

## 完整架构基线（T3 对齐，delta 自 8-19 报告）

| 基线组件 | T3 要求 | 状态 | 当前证据 | 缺口 / 理由 |
|---|---|---|---|---|
| System boundary | required | **strong** | 任务书明确四边（L86-95）+ .project-to-act | 无 |
| Task intake | required | **strong** | `business/service.py` 工具注册表 + `kind="read"` 过滤 | 无 |
| Agent loop | lightweight | **not-needed** | 无 LangGraph 主循环，HTTP 路由消费领域服务 | T3 不要求独立 loop |
| Tool layer | required | **strong** | `BusinessModuleRegistry` + `ToolSpec(kind/policy/handler)` | 无 |
| State machine | required | **strong** | `state_machine.py` `_TRANSITIONS` 完整图 | 无 |
| Project ledger | required | **strong** | `.project-to-act/` 五文档 | 无 |
| Evidence system | required | **strong** | `MetricValue.import_manifest_id/data_as_of/authoritative_service` 强制；来源诚实 | **但 R2-2 映射复活、R2-3 来源非确定待修** |
| Gate system | required | **adequate** | `GateEngine.run_all` + `EvidenceBridge.run_gates` | **R3 blocked 信息只在 evidence_facts 内，顶层不区分（待修）** |
| Session/users | required | **strong** | `AdminPrincipal.tenant_id` 隔离 + store_id 归属 409 | 无 |
| Memory | lightweight | **not-needed** | 领域事实表即权威存储 | T3 不强制 |
| Retrieval/context | lightweight | **adequate** | `_traffic_facts`/`_inventory_facts`/`_order_facts` 按 revision 窗口过滤 | `_order_facts` 来源非确定（R2-3 待修） |
| Artifacts/workspace | not-needed | **not-needed** | 无 artifact 产出 | T3 不涉及 |
| Registry/routing | required | **strong** | `business/registry.py` 三字段 | 无 |
| Multi-agent handoff | lightweight | **not-needed** | 无跨 agent 移交 | T3 轻量级 |
| Coordination/conflict | lightweight | **strong** | SQLite WAL + `_write_lock` + `BEGIN IMMEDIATE` | 无 |
| A2A/MCP | not-needed | **not-needed** | 无 MCP | 无 |
| Observability | lightweight | **weak** | `db.audit` 落痕；缺 trace/cost/latency | 无工具调用时延统计 |
| Evaluation | required | **weak** | `MechanismEvalRunner` 有机制 Eval；**但选品/上新/清仓场景全锁 EVIDENCE_INSUFFICIENT + 保持观察（假覆盖，R5 待修）** | **负责人第 4 轮抓的阻断项 5** |
| Guardrails/security | required | **strong** | `validate_full_recommendation` 越权扫描 + `FORBIDDEN_OUTPUT_KEYS` | 无 |
| Self-evolution | not-needed | **not-needed** | 无自进化 | 目标不要求 |

---

## 缺失/待修组件清单（负责人 7 阻断项 → 组件）

| 优先级 | 阻断项 | 组件 | 为什么重要 | 建议修法（已探究） |
|---|---|---|---|---|
| **高** | 2a net_sales=gross | Evidence system | 多行订单退款无法归 SKU 时用 GMV 冒充净销，任务书 L66 禁止 | 多行订单→MISSING + 独立 `net_sales_reason`（已拍板） |
| **高** | 2b 映射复活 | Evidence system | revoked 映射被忽略，回落到旧 confirmed | 取最新事件，revoked→None（对齐 M7-R） |
| **高** | 2c 来源非确定 | Retrieval/context | CTE 按 external_order_id 分区，rn=1 子查询多行任取 | 去分区全局 ORDER BY + LIMIT 1 取整行 |
| **高** | 3 D-034 默认路径 | Gate system | 默认路径 blocked 信息被 model_unavailable 吞并，顶层不区分 | 结构化 `degradation_reasons`（reason 保持稳定码，不引越权词） |
| **高** | 5 Eval 假覆盖 | Evaluation | 选品/上新/清仓场景全锁"保持观察"，场景名通过能力未证明 | 方案 C-lite：facts_snapshot 透传信号 + mock 解释器 → 非降级真实方向 |
| **高** | 7 文档不可复现 | Project ledger/文档 | Base 写错、计数错、EOF 空白、浏览器 skip 未注明 | 4 处修正（Base→454b35c、collect→1266、3 EOF、浏览器注明） |

---

## 架构地图（delta）

| 模块 | 当前证据 | 评分 | 说明 |
|---|---|---|---|
| `product_read_model/query.py` | revision 隔离 + 来源诚实 + 跨仓汇总；**R2 三缺陷待修** | **adequate**（修复后→strong） | `[test]` test_m9r_query_source_honesty 7 passed + test_product_read_query 7 passed；R2-1/2/3 未修 |
| `product_diagnosis/diagnosis.py` | conclusion fail-closed + 污染自动反推 | **strong** | `[test]` test_m9r_diagnosis_production 4 passed |
| `product_lifecycle/engine.py` | `_TYPE_BY_DIAGNOSIS` + `REQUIRED_FACTS` + `_build_facts_snapshot`（SELECTION 类 return {}） | **adequate** | `[read]` 引擎 V1 已诚实降级；但方向永不产出（R5 待修） |
| `product_workbench/scenes.py + eval.py` | 10 冻结场景 + MechanismEvalRunner；**选品/上新/清仓假覆盖待修** | **weak** | `[test]` test_m9r_mechanism_eval 10 passed；R5 待修 |
| `workbench_api.py` | read-model + workbench + recommendations CRUD + diagnose | **strong** | `[trace]` api.py:197 挂载 router；L187/L208 生产消费 |
| `business/service.py` | EvidenceBridge + _model_unavailable_diagnosis + generate_and_persist | **strong** | `[trace]` L257 build_diagnosis_facts、L319 diagnose 消费 |
| `docs/admin-console.html` | 工作台视图 + 审核操作；**R4 下钻 HTML/JS 已改待浏览器验证** | **adequate** | `[read]` L1673+ revision/insights/诊断面板；浏览器待跑 |

---

## 功能审查报告（delta，聚焦负责人 7 阻断项）

| 功能/能力 | 是否存在 | 完整度 | 主要问题 | 建议 |
|---|---|---|---|---|
| 经营读模型（SKU 层） | ✅ | 85% | R2-1 net_sales 多行冒充 GMV；R2-2 revoked 映射复活；R2-3 来源非确定 | 三项按已探究方案修 |
| 证据桥接（门禁） | ✅ | 85% | R3 blocked 信息只在 evidence_facts，顶层 reason 不区分 | 结构化 degradation_reasons |
| 生命周期建议 | ✅ | 95% | create/transition 生产入口齐全；但选品/上新/清仓方向永不产出 | R5 方案 C-lite |
| 工作台 JSON view | ✅ | 90% | 下钻 HTML/JS 已改；浏览器待验证 | 跑 test_m9r_workbench_browser |
| 机制 Eval | ✅ | 70% | 选品/上新/清仓全锁"保持观察"，场景名通过能力未证明 | R5 方案 C-lite |
| 文档可复现 | ✅ | 60% | Base 写错、计数错、EOF 空白、浏览器 skip 未注明 | R7 4 处修正 |

---

## 关键问题（按验收影响排序）

| 优先级 | 问题 | 为什么重要 | 建议修复 |
|---|---|---|---|
| **P0** | R2 三缺陷（net_sales 冒充/映射复活/来源非确定） | 负责人阻断项 2，任务书 L66 明确禁止 GMV 冒充净销 | 按已探究方案（MISSING + 独立 reason / 最新事件判定 / 全局 LIMIT 1） |
| **P0** | R5 Eval 假覆盖 | 负责人阻断项 5，任务书 L476"发现真实方向"未达标 | 方案 C-lite（透传信号 + mock 解释器 → 非降级方向） |
| **P0** | R3 blocked 信息被吞并 | 负责人阻断项 3，D-034 默认路径应区分阻塞原因 | 结构化 degradation_reasons |
| **P1** | R7 文档不可复现 | 负责人阻断项 7，Base/计数/EOF/浏览器 skip | 4 处修正 |
| **P1** | R1/R4/R6 已修项需复验确认 | 负责人阻断项 1/4/6，已修但未在复验环境确认 | 跑 item_isolation_overlap / workbench_browser / production_recommendation_chain |
| **P2** | 全量回归未重跑 | 复验基线 1260/1/1，第 5 轮修复后必须全绿 | 批次 F 全量 pytest |

---

## 优化建议（生产落地，非本轮必须）

| 优先级 | 建议 | 预期收益 | 实施成本 | 验收方式 |
|---|---|---|---|---|
| 中 | 诊断工具接入时强制传 `model_output` 给 `run_gates` | 输出侧越权拦截生产可触达 | 低 | 加测试断言 |
| 中 | 机制 Eval 建 golden regression 集 | 冻结场景可回归，防 scenes.py 改动静默失败 | 中 | 定期跑 |
| 中 | workbench view 统一 revision 参数 | metrics 与 gates 同 revision，避免归因误导 | 低 | test 断言 |
| 低 | 审计 log 加 latency 追踪 | 观察 create/transition 耗时 | 低 | 单元测试 |

---

## 测试证据

| 测试集 | 命令 | 结果 | 证据类型 |
|---|---|---|---|
| 核心 M9-R + mechanism_eval | `.venv python -m pytest test_m9r_query_source_honesty test_m9r_item_isolation_overlap test_m9r_diagnosis_production test_m9r_production_recommendation_chain test_product_read_query test_m9r_mechanism_eval` | **36 passed** | `[test]` |
| 全量 collect | `.venv python -m pytest --collect-only` | **1266 tests** | `[test]` |
| 基线 diff | `git diff 454b35c..HEAD` | 108 文件 +15689 行，净增 | `[baseline]` |
| 生产链 grep | `diagnose/generate_and_persist/build_diagnosis_facts/quality_gate` 消费点 | 全接线 | `[trace]` |
| 全量回归 | `pytest tests` | 未跑（修复后必须全绿） | `[unverified]` |

---

## 下一步（按用户优先级：任务书 + 负责人错误）

1. **批次 A**：R2-1 net_sales else 分流 + R2-3 CTE 确定性（同批，避免中间态红）
2. **批次 B**：R2-2 revoked 映射复活
3. **批次 C**：R3 结构化 degradation_reasons
4. **批次 D**：R5 Eval 方案 C-lite（facts_snapshot 透传信号 + mock 解释器 → 非降级真实方向）
5. **批次 E**：R7 文档修正（Base/计数/EOF/浏览器 skip）
6. **批次 F**：全量验证（compileall + collect + git diff --check + 全量 pytest + 浏览器）
7. **提交**：按批次提交，更新 PR #19，出第 5 轮验收汇报

---

**报告保存位置**：`docs/reviews/agentops-health-check-M9R-2026-0819-5th-round.md`
**源代码/配置/运行时文件未改动**（本报告是只读审查输出；修复按已批准计划执行）
