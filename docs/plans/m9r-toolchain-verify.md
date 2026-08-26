# M9-R 工具可执行性核查（m9r-toolchain-verify）

> 目的：本文件固化 `m9r-wp1-wp4-overall-plan.md` 中每条命令、接口、路径、版本的核实结果，
> 确保计划在执行时**不做猜测、每步可跑**。凡是标注「❓ 待确认」的，都是执行前必须先找负责人
> 对齐的项目；凡是「✅ 已核实」的，均为本次直接读 origin/main 代码确认。

---

## 一、命令可执行性

| 命令 | 核实 | 结果 |
|---|---|---|
| `cd /d/yunpai-ecommerce-agent` | 仓库根目录 | ✅ |
| `git checkout -b feature/m9r-read-model origin/main` | 仓库有 `feature/m9r-read-model` 吗？没有；`origin/main` 存在 | ✅ 可执行；**注意当前在 `feature/m3-knowledge-base`，切走前需确认无未提交改动** |
| `PYTHONPATH=src python -m pytest ...` | 见下方 pytest 配置 | ✅ |
| `python -m pytest tests/...` | `pyproject.toml` 有 `[tool.pytest.ini_options] testpaths=["tests"] pythonpath=["src"]` | ✅ `PYTHONPATH=src` 可省略，保留为兜底 |
| `git show origin/main:docs/tasks/M9R_PRODUCT_TRAFFIC_LIFECYCLE_WORKBENCH.md` | 文件存在 | ✅ |
| `git show origin/main:docs/tasks/M7R_READONLY_DATA_WORKBENCH.md` | 文件存在 | ✅ |
| `git show origin/main:src/ecommerce_agent/readonly_data/contracts.py` | 文件存在 | ✅ |
| `git show origin/main:src/ecommerce_agent/readonly_data/service.py` | 文件存在 | ✅ |
| `git show origin/main:src/ecommerce_agent/traffic_lab/__init__.py` | 文件存在 | ✅ |
| `git show origin/main:src/ecommerce_agent/traffic_lab/service.py` | 文件存在 | ✅ |
| `git show origin/main:src/ecommerce_agent/database.py` | 文件存在 | ✅ |
| `git show origin/main:pyproject.toml` | 文件存在 | ✅ |
| `git show origin/main:CONTRIBUTING.md` | 文件存在 | ✅ |
| `git show origin/main:docs/tasks/ECOMMERCE_CLOSED_LOOP_ROADMAP.md` | 文件存在 | ✅ |

---

## 二、版本与占号

| 项 | 核实 | 结果 |
|---|---|---|
| `SCHEMA_VERSION` | `database.py` 第 49 行 `SCHEMA_VERSION = 34` | ✅ =34，与计划一致 |
| M7-R 合入 commit | `git log` 有 `0b54a24 feat(readonly): establish M7-R WP1 data contracts` | ✅ |
| 迁移登记表 | `database.py` 有 `schema_migrations`，`_apply_vNN` 方法存在 | ✅ |
| CONTRIBUTING 占号规则 | 第 9 节「占号要提前说」，第 6 节「schema 版本号冲突（真实踩过的坑）」 | ✅ 需按此走 v35 |
| v35 占号 | 本计划**不预占**；需要落库时再按流程占号 | ✅ 符合任务书「本文不预占」 |

---

## 三、M7-R 契约接口（WP1 直接消费）

| 符号 | 核实 | 结果 |
|---|---|---|
| `EvidenceState` | contracts.py，枚举 `actual/manual/demo/missing` | ✅ |
| `SourceKind` | contracts.py，枚举 `actual/manual/demo` | ✅ |
| `ImportManifestInput` | contracts.py，含 `store_id/source_kind/source_system/report_type/report_period/exported_at/data_as_of/content_digest/mapping_version/parsed_rows/references` | ✅ **无 `tenant_id` 字段**（租户经 `record_import` 参数传入） |
| `ReportFieldPolicy` | contracts.py，含 `report_type/mapping_version/allowed_fields/required_fields/sensitive_fields/field_aliases` | ✅ |
| `sanitize_report_row(policy, row)` | contracts.py 返回 `SanitizedReportRow` | ✅ |
| `SanitizedReportRow.downstream_payload()` | contracts.py，返回 `dict` | ✅ |
| `FieldEvidenceInput` | contracts.py，MISSING 不得携带 import_id/data_as_of/source_reference | ✅ **v4 修正 2 已对齐** |
| `ReadonlyDataService.record_import(tenant_id, value)` | service.py，返回含 `import_id`（幂等复用） | ✅ |
| `ReadonlyDataService.get_import(tenant_id, import_id)` | service.py | ✅ |
| M7-R WP3 身份映射（料号/商家编码→内部料号） | main 上 `readonly_data/` 仅 contracts.py + service.py，**无映射表/接口**；数据库迁移仅 v32(身份列)/v33/v34 | ❌ **未交付**（问题①：料号引用无法填充，标 None） |
| M7-R WP2 数据域交付范围 | main 上未核实到 WP2 数据域对应代码/清单 | ⚠️ 待确认（问题②：缺竞品/退款边界依赖） |

---

## 四、M5-R 证据接口（WP2 桥接）

| 符号 | 核实 | 结果 |
|---|---|---|
| `TrafficLabService` | `traffic_lab/service.py` 第 77 行 `class TrafficLabService` | ✅ |
| `get_revision / list_revisions` | service.py 第 280/290 行 | ✅ |
| `create_revision / get_asset / list_assets` | service.py 第 177/147/157 行 | ✅ |
| `get_experiment / list_experiments / create_experiment / transition_experiment` | service.py 第 877/887/791/914 行 | ✅ |
| `get_analysis_run / list_analysis_runs` | service.py 第 1258/1271 行 | ✅ |
| `TrafficAnalysisEngine.analyze_experiment` | `traffic_lab/analysis.py` 第 191 行，含 A/A、样本量、控制变量、污染、lag 等实现 | ✅ **可复用，不重写统计** |
| `TrafficAnalysisEngine.analyze_experiment` | `traffic_lab/analysis.py` 第 191 行，含 A/A、样本量、控制变量、污染、lag 等实现 | ✅ **可复用，不重写统计** |
| freshness / provenance 查询 | service.py 未见独立 `get_freshness` / `get_provenance` 方法；freshness/provenance 藏在 revision/analysis 字段 | ⚠️ **问题⑤：桥接对象待核实**（WP2 设计期读代码定位载体） |
| `TrafficLabService` 构造参数 | `__init__`（第 93 行）需读实现确认（`db` + gateway？） | ⚠️ WP2 设计期读代码确认 |

---

## 五、WP1 代码关键校验（MISSING / 证据对齐）

| 校验点 | 核实 | 结果 |
|---|---|---|
| MISSING 时 `import_manifest_id`/`data_as_of` 必须 None | 与 `FieldEvidenceInput.validate_evidence_source` 一致 | ✅ |
| MISSING 时 `value` 必须 None | `project_evidenced_value` 同规则 | ✅ |
| 非 MISSING 时 `value`/`import_manifest_id`/`data_as_of` 必填 | `project_evidenced_value` 同规则 | ✅ |
| `extra="forbid"` 层拒绝店铺字段广播 | Pydantic 行为；测试 1/10/11 验证 | ✅ |

---

## 六、未决项清单（执行前必须对齐）

| # | 项 | 状态 | 需向谁确认 | 确认方式 |
|---|---|---|---|---|
| 1 | `get_freshness` / `get_provenance` 查询入口（问题⑤） | ❓ 未在 service.py 顶层找到独立方法 | M5-R 负责人（闫睿涵）+ 胡磊 | WP2 设计期读 revision/analysis 代码定位载体 |
| 2 | `TrafficLabService.__init__` 构造参数 | ❓ 未读全 | 同上（WP2 设计期读代码） | 读 `traffic_lab/service.py` 第 93 行 |
| 3 | F-310 前端基建就绪状态（问题④） | ❓ | 闫睿涵 / 前端 | 直接确认 |
| 4 | F-121/F-122 评测能力就绪状态（问题④） | ❓ | 评测平台负责人 | 直接确认 |
| 5 | D-037～D-040 Demo 数据域就绪状态（问题④） | ❓ | 数据/Demo 负责人 | 直接确认 |
| 6 | v35 落库占号 | ❓ 需要落库时再走流程 | 闫睿涵（占号协调） | CONTRIBUTING 流程 |
| 7 | **M7-R WP3 料号映射交付状态（问题①）** | ❌ main 上无映射代码 | 闫睿涵（M7-R 负责人） | 确认 WP3 交付时间点；交付前 material_code=None |
| 8 | **M7-R WP2 数据域交付范围（问题②）** | ❓ 未核实 | 闫睿涵（M7-R 负责人） | 索要 WP2 交付范围清单，确认是否含竞品/退款域 |
| 9 | **权威服务投影规则（问题③）** | ❓ 现为来源系统 best-effort | 胡磊（WP2 设计期自定） | WP2 桥接层确立规则后回填 |

---

## 七、复跑示例（WP1 落地后，一条条执行并贴输出）

```bash
# ① 建分支（当前在 feature/m3-knowledge-base，先确认工作区干净）
cd /d/yunpai-ecommerce-agent
git status --short            # 确认无未提交改动
git checkout -b feature/m9r-read-model origin/main

# ② 写代码 + 测试（m9r-wp1-read-model.md 文件 1~7）

# ③ 跑 WP1 测试
python -m pytest tests/test_m9r_read_model_isolation.py tests/test_m9r_readiness.py -q --no-header -p no:cacheprovider

# ④ 上游契约不回归
python -m pytest tests/test_readonly_data_contract.py -q --no-header -p no:cacheprovider
python -m pytest tests/test_traffic_lab.py -q --no-header -p no:cacheprovider

# ⑤ 全量回归（仓库既有 CI 范围内）
python -m pytest tests -q --no-header -p no:cacheprovider
```
