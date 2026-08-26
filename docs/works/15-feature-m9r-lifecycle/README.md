# M9-R 商品流量与生命周期经营 — 交付与 WP5 验收证据包

日期：2026-08-18
分支：`feature/m9r-read-model`
PR：#19（base=a1024053774/main，head=hlanan886:feature/m9r-read-model）
Schema 占号：#18（v36=M9-R WP3 生命周期建议，已合并）

## 一、交付范围

| 工作包 | 交付物 | 验收 |
|---|---|---|
| WP1 商品经营读模型 | `product_read_model/` 5 文件（models/readiness/factory/errors/__init__） | 18 条固化 |
| WP2 流量诊断 + 受控实验 | `product_diagnosis/` 5 文件（bridge/gates/diagnosis/experiment/__init__） | 12 条固化 |
| WP3 生命周期建议 | `product_lifecycle/`（schemas/state_machine/validation/interface/service/__init__） | 8 条固化 |
| WP4 经营工作台 | `product_workbench/`（pages/boundaries/scenes/eval/__init__） | 10 条固化 |
| v36 迁移 | `product_recommendations` + `product_recommendation_audit`（database.py） | T1-T8 |
| WP3 持久化读写服务 | `RecommendationPersistenceService`（service.py） | 10 用例 |

## 二、Commit 链（origin/main..HEAD，13 个）

```
8ded743 feat(lifecycle): add recommendation persistence read/write service
5ff97b7 docs(plans): freeze WP2/WP4 acceptance tables to verified
de92ab0 docs(plans): freeze WP3 acceptance table to verified
6ad329a docs(plans): sync v36 schema reservation and persistence boundary
c7cd99f feat(db): add v36 lifecycle recommendation persistence tables
4779c6b Merge origin/main into feature/m9r-read-model
ba0e5b8 feat(lifecycle): M10-R 契约按缪海南评审 5 点对齐
df443dc docs(plans): M9-R WP4 详细规划
896f27f docs(plans): M9-R WP3 详细规划
3952e59 docs(plans): M9-R WP2 详细规划
72e9b87 docs(plans): M9-R WP1-WP4 总纲
3c4ea94 docs(plans): M9-R WP1 详细计划 + 收口回归证据
ee827a9 feat(read-model): M9-R WP1-WP4 骨架
```

## 三、测试证据（WP5 复验必测项）

### 3.1 v36 迁移（8 条）

```bash
pytest tests/test_m9r_lifecycle_persistence_v36.py tests/test_disaster_recovery.py
# 8 passed in 6.20s（T1-T8：幂等/列存在/enum CHECK/audit 不可变/状态可迁移/FK/v34 升级/灾备拒绝）
```

### 3.2 WP3 持久化读写服务（10 条）

```bash
pytest tests/test_m9r_lifecycle_persistence_service.py
# 10 passed in 9.70s（幂等/冲突/事务/FK/round-trip）
```

### 3.3 WP3 生命周期（原 37 + 新 = 54 全绿）

```bash
pytest tests/test_m9r_lifecycle_*.py tests/test_m9r_m10_contract.py
# 54 passed in 27.06s（状态机 8/校验 5/幂等 3/写屏障 2/alternatives 3/keep_default 1/M10 契约 8/持久化 10/迁移 7+）
```

### 3.4 影响面完整（26 文件）

```bash
pytest tests/test_m9r_*.py tests/test_migrations.py tests/test_readonly_data_contract.py tests/test_disaster_recovery.py
# 160 passed in 182s
```

### 3.5 全量回归说明

900s 窗口超时（项目既有慢测试 `test_actual_agent_20_case_semantic_gate` 单测 16.59s，`test_agent.py` 整文件 7 测试 61.44s）。
分区实测排除 test_agent.py 前 41% 全绿、无 v36 引发回归。**与本次变更无关**（未触碰 test_agent.py 任何代码）。

## 四、反证记录（CONTRIBUTING 第 6 节门禁）

- **T8（required 登记）**：临时从 `_validate_schema.required` 删 `product_recommendations` 条目 →
  T8 如期失败（`_validate_schema` 不再报 missing table）→ 还原 → 复验 8 passed。反证写进 commit `c7cd99f`。
- **T4（防误建 readonly）**：recommendations 状态可 UPDATE 落库正例（防 state 被触发器锁死）。
- **B1-B7 反例测试群**：keep_default / write_barrier / alternatives / data_trust / demo_isolation 等全部 PASS。

## 五、跨负责人协调

| 事项 | 负责人 | 状态 |
|---|---|---|
| M10-R 契约评审 | 缪海南 | ✅ **已冻结（2026-08-18）**：5 点对上、无额外调整、契约按 v1 冻结、M10-R 消费侧照此（interface.py + 8 测试） |
| v35/v36 占号 | 闫睿涵 | ✅ #18 已合并；v35 归 M7-R WP3，v36 归 M9-R WP3 |
| WP5 独立验收 | 闫睿涵 | ⏳ 待 PR #19 合并后 |

## 六、v35/v36 合并纪律（合 #19 时必读）

1. database.py 三处冲突：SCHEMA_VERSION / initialize if 链 / 两个 `_apply_vNN` 方法。
2. **两块都保留按号排序，SCHEMA_VERSION 取较大者 36，不用 ours/theirs 整体覆盖**（v25 事故）。
3. 合完自检：`git grep "def _apply_v3[456]"` 必须 34/35/36 三号并存。
4. 灾备：版本提到 36 作废全部历史备份，合后立即全量新备份。

## 七、已知边界

- **WP3 持久化读写服务**：薄 service，业务逻辑（状态机/B3）仍在内存模块，不在 service 搬移。
- **AuditRecord.target 有损**：audit 表无 target 列，落库丢弃（未来需追溯需 v37 加列）。
- **payload_hash 裁剪清单**：排除身份/作用域/可变状态/生命周期，避免 transition 后失真。
- **WP4 读侧**：独立方法 `recommendations`/`recommendation_audit_trail`，不改 `product_detail` 返回键。
