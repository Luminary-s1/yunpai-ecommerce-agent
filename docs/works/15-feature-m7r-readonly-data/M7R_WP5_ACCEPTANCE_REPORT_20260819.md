# M7-R WP5 独立验收报告（2026-08-19）

## 验收对象
- PR #20：feat(readonly-data) 交付 M7-R WP2–WP4 只读数据链路
- head：a1024053774:feature/m7r-wp4-readiness @ ece61e1；base：48013b1；mergeable/clean
- 范围：WP2 报表适配/文件解析/入库、WP3 商品身份（schema v35）、WP4 readiness 投影/API/Demo

## 验收人与方法
- 验收人：缪海南（WP5 独立验收，未参与 WP1–WP4 功能实现）
- 方法：干净状态 checkout 独立 worktree；独立黑盒探针；全量回归；静态审阅（D-014 版本契约 / D-035 单一权威源）

## 结果
| 项目 | 结果 |
|---|---|
| PR 自带测试（readiness/product_identity/ingestion） | 85 passed |
| 独立探针（6 项） | 全部通过 |
| 全量回归（含探针） | 1041 passed / 0 failed（27:10；PR 基线 1035 复现） |
| compileall | 退出 0 |

### 独立探针覆盖
1. 同一内部料号跨店不串（canonical_product_id 带 tenant+store 范围）
2. decision 重放幂等；同 decision_key 不同载荷拒绝
3. 未知商品身份策略版本 fail-closed
4. 空 readiness 投影全 missing、不造假 0、只读零写入
5. 敏感手机号不进导入输出与 catalog 载荷
6. demo 事实与 operational 投影隔离

### 静态审阅要点
- 策略版本单一权威源：PRODUCT_IDENTITY_POLICY_VERSION / READINESS_POLICY_VERSION 各一处
- 版本冲突守卫：mapping_version_conflict、mapping_decision_key_conflict、stale/source_version_conflict
- v35 新表（canonical_products / mapping_events / reconciliation_*）已登记 _validate_schema，并带不可变触发器
- 新模块无 TODO/FIXME

## 非阻塞观察
- traffic_lab_api 存在 OpenAPI Operation ID 重复 warning（存量，非 PR #20 引入）

## 未放行事项
- 真实平台字段全覆盖、生产放行、M7-R 最终签署
- 下游联动（M9-R v36 生命周期建议等）不在本报告范围内

## 结论
- WP1–WP4 在本验收范围内通过（开发自测 + 独立探针 + 全量全绿），建议合入 PR #20
- 最终签署与放行由负责人按项目流程执行
