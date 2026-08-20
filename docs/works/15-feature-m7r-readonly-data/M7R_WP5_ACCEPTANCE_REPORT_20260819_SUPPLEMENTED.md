# M7-R WP5 正式验收补充报告（2026-08-19）

> 本文件是对缪海南出具的《M7-R WP5 独立验收报告》的范围与证据补充，不覆盖、不改写原件，也不代替独立验收人签署。

## 1. 报告身份与证据来源

| 项目 | 说明 |
|---|---|
| 原独立验收人 | 缪海南；未参与 WP1～WP4 功能实现 |
| 原独立报告 | `/Users/luckye/Downloads/M7R_WP5_ACCEPTANCE_REPORT_20260819.md` |
| 原报告 SHA-256 | `007edc5001f39e4aba26e6361b75152625a52a7f61ac5281699d33e5580aa794` |
| 原独立结论 | PR #20 范围通过；自带测试 85 passed；独立探针 6/6；含探针全量 1041 passed / 0 failed；PR 基线 1035 复现；compileall 退出 0；建议合入 PR #20 |
| 本补充执行方 | Codex（WP1～WP4 实施方之一） |
| 本补充性质 | 补齐精确验收对象、WP1～WP4 矩阵、环境和命令、mutation 红→绿、桌面/窄屏浏览器与失败/复验记录 |
| 独立性边界 | 本补充不冒充缪海南执行新增步骤，不替代其独立签署；原报告中的独立探针、静态审阅和全量复跑仍是 WP5 的独立证据来源 |

## 2. 口径校正：WP1 已先行合入 main

WP1 是 M7-R 以及下游 M8-R～M10-R 共用的导入、隐私、来源和证据基建。为解除 WP2～WP4 的开发阻塞，WP1 已先行合入 `main`；因此 PR #20 的差异只包含 WP2～WP4，并不表示 WP1 不在 M7-R WP5 的验收范围内。

本次 WP5 的正式代码对象是一个组合范围：

| 范围 | 精确对象 | 说明 |
|---|---|---|
| WP1 | `main@48013b1d3b29a810288c32f73df028c69070064c` | PR #20 的 base；已包含 WP1 公共基建 |
| WP2～WP4 | PR #20 head `ece61e14fb9c326b38dcde084513494147c508e8` | 分支 `feature/m7r-wp4-readiness`；PR 当前为 OPEN、Ready、MERGEABLE/CLEAN |
| 集成验收快照 | `ece61e14fb9c326b38dcde084513494147c508e8` | 该 head 以包含 WP1 的 main 为基线，因此在此快照复跑即覆盖 WP1～WP4 集成态 |

对象指纹：

- base tree：`79f6e720c67f018ffe4e6c15f9f15587211d3906`
- head tree：`0be58a78c40657e13db3281bfacdf9bd38d73e1a`
- WP1 功能提交：`0b54a2475a0b152583a47c4c4ebedffca8293a23`（`feat(readonly): establish M7-R WP1 data contracts`）
- WP1 台账/集成提交：`e127c397f7e291248990b0006ca4876d7e20a075`（`docs(project): align M10 ownership and v34 integration`）
- 上述两个 WP1 提交均经 `git merge-base --is-ancestor <commit> 48013b1...` 验证为 base 的祖先，退出码均为 0。

PR：<https://github.com/a1024053774/yunpai-ecommerce-agent/pull/20>

## 3. 验收环境

| 项目 | 值 |
|---|---|
| 日期/时区 | 2026-08-19 / Asia/Shanghai |
| 操作系统 | macOS 27.0，Build 26A5388g |
| Python | 3.11.14 |
| pytest | 8.4.2 |
| Node.js | v26.5.0 |
| Git | 2.52.0 |
| 干净验收 worktree | `/tmp/m7r-wp5-supplement.ZRBGr1/repo` |
| detached commit | `ece61e14fb9c326b38dcde084513494147c508e8` |
| Python 环境 | 项目 `.venv`，显式设置验收 worktree 的 `PYTHONPATH=.../src` |
| 网络代理 | 复跑与本地浏览器服务均清除大小写 HTTP(S)/ALL proxy 变量 |

所有补充复跑均在 detached、干净 worktree 完成；结束时 `git status --short` 无输出，未把临时 mutation 或浏览器数据写回产品分支。

## 4. 可复跑命令

### 4.1 固定验收对象并建立干净 worktree

```bash
REPO=/Users/luckye/Documents/Code/yunpai-ecommerce-agent
TARGET=ece61e14fb9c326b38dcde084513494147c508e8
BASE=48013b1d3b29a810288c32f73df028c69070064c
CHECKOUT="$(mktemp -d /tmp/m7r-wp5-replay.XXXXXX)/repo"

git -C "$REPO" worktree add --detach "$CHECKOUT" "$TARGET"
git -C "$CHECKOUT" status --short
git -C "$CHECKOUT" rev-parse HEAD HEAD^{tree} "$BASE" "$BASE^{tree}"
git -C "$CHECKOUT" merge-base --is-ancestor 0b54a2475a0b152583a47c4c4ebedffca8293a23 "$BASE"
git -C "$CHECKOUT" merge-base --is-ancestor e127c397f7e291248990b0006ca4876d7e20a075 "$BASE"
```

### 4.2 WP1～WP4 聚焦复跑

```bash
PY=/Users/luckye/Documents/Code/yunpai-ecommerce-agent/.venv/bin/python

env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy PYTHONPATH="$CHECKOUT/src" "$PY" -m pytest -q "$CHECKOUT/tests/test_readonly_data_contract.py" "$CHECKOUT/tests/test_readonly_data_ingestion.py" "$CHECKOUT/tests/test_product_identity.py" "$CHECKOUT/tests/test_m7r_wp4_readiness.py"
```

结果：`104 passed in 29.55s`。

### 4.3 全量、静态和台账门禁

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy PYTHONPATH="$CHECKOUT/src" "$PY" -m pytest -q "$CHECKOUT/tests"

PYTHONPATH="$CHECKOUT/src" "$PY" -m compileall -q "$CHECKOUT/src" "$CHECKOUT/tests"

git -C "$CHECKOUT" diff --check "$BASE"..HEAD

"$PY" /Users/luckye/.codex/skills/project-to-act/scripts/init_project_management.py --project-root "$CHECKOUT" --validate

perl -0777 -ne 'while (m{<script(?:\s[^>]*)?>(.*?)</script>}sg) { print $1, qq{\n} }' docs/admin-console.html | node --check -
```

结果：

- 全量：`1035 passed, 24 warnings in 741.36s (0:12:21)`，退出码 0。
- 24 条 warning 均为存量 `traffic_lab_api.py` FastAPI Duplicate Operation ID，集中于 `tests/test_forecasting_wp4.py` 与 `tests/test_traffic_lab_api.py`；无 failed/skipped/xfailed。
- `compileall`：退出码 0。
- `git diff --check`：退出码 0。
- project-to-act 校验：`valid`，退出码 0。
- admin-console 内联 JavaScript：`node --check` 退出码 0。

### 4.4 本地浏览器复验

```bash
BROWSER_DATA="$(mktemp -d /tmp/m7r-wp5-browser-data.XXXXXX)"

env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy PYTHONPATH="$CHECKOUT/src" DATA_DIR="$BROWSER_DATA" ADMIN_AUTH_REQUIRED=false MODEL_ENABLED=false KG_IMPORT_ENABLED=false "$PY" -m ecommerce_agent.cli serve --host 127.0.0.1 --port 53176
```

然后访问 `http://127.0.0.1:53176/admin`。免管理员认证仅限本机 loopback 验收环境，不是生产配置。

## 5. WP1～WP4 验收矩阵

证据标记：

- **I**：缪海南原独立验收报告中的独立探针、全量复跑或静态审阅。
- **S**：本补充在固定 commit 的干净 worktree 中复跑或浏览器实测。
- **M**：本补充的临时 mutation 红→绿证据。

| WP | 任务书完成条件 | 证据与结果 | 结论 |
|---|---|---|---|
| WP1 | 敏感姓名、手机号、地址和非白名单列不进入规范化载荷、模型或普通日志 | I：独立探针确认敏感手机号不进入导入输出与 catalog 载荷；S：`test_readonly_data_contract.py`、`test_readonly_data_ingestion.py` 纳入 104 项聚焦全绿 | 通过 |
| WP1 | 相同文件重放幂等；同来源版本不同内容拒绝 | I：静态审阅确认 `stale/source_version_conflict` 守卫；S：契约与 ingestion 聚焦测试全绿 | 通过 |
| WP1 | `missing` 不生成导入记录且不转成数值 0；Demo 不进入默认 operational | I：独立探针确认空 readiness 全 missing、不造假 0，且 demo/operational 隔离；S：空库与 Demo 浏览器/数据库实测；M：破坏 demo 隔离时测试失败、还原后通过 | 通过 |
| WP1 | import manifest、actual/manual/demo、四态证据、隔离错误和 v34 兼容边界可追溯 | I：完整差异与 D-014/D-035 静态审阅；S：契约测试、compileall、schema 校验和集成全量通过 | 通过 |
| WP2 | 已登记适配器覆盖正常、字段缺失、非法类型、重复、乱序和跨店反例 | S：`test_readonly_data_ingestion.py` 聚焦通过；八类 Demo manifest 在端到端装载中生成且可下钻 | 通过 |
| WP2 | 日/月/订单行粒度不混算；金额单位和时区显式 | S：适配器契约与 ingestion 测试在固定快照通过；来源、水位和字段证据可由 readiness 投影追溯 | 通过 |
| WP2 | 单域失败保留其他成功事实并报告 partial | S：ingestion 契约测试通过；全量回归无失败 | 通过 |
| WP2 | 仅经既有领域公开服务写入，无第二套订单、库存、财务真相 | I：D-014/D-035 静态审阅无违规；S：端到端数据库只出现既有领域事实表和 readonly 证据表，页面 GET 零写入 | 通过 |
| WP3 | 租户/店铺范围隔离，同料号不串联 | I：独立探针 1 通过；S：`test_product_identity.py` 全绿 | 通过 |
| WP3 | 标题相似但 SKU/商家编码冲突进入歧义，不自动绑定 | S：product identity 聚焦测试全绿；策略版本单一权威源经 I 静态审阅确认 | 通过 |
| WP3 | 人工裁决重放稳定，冲突拒绝，撤销后不消费旧映射且历史保留 | I：独立探针 2、3 通过，确认 decision 重放幂等、同 key 异载荷拒绝、未知策略 fail-closed；S：映射事件链测试全绿 | 通过 |
| WP3 | 每一输入行进入 matched/ambiguous/unmapped/rejected 可解释终态 | S：product identity 对账测试通过；Demo 生成 1 个 reconciliation run 和 3 条 row，页面可下钻 | 通过 |
| WP3 | v35 迁移、升级边界、不可变历史和 schema 校验 | I：v35 新表已登记 `_validate_schema` 且有不可变触发器；S：全量、compileall、project-to-act 校验全绿 | 通过 |
| WP4 | API/页面统一展示来源、覆盖、水位、新鲜度、质量、映射和缺失，并可追到 manifest | S：readiness 聚焦测试和桌面浏览器实测通过；Demo 显示八域 8/8、manifest 8、四项真实缺口明确 | 通过 |
| WP4 | 只打开页面不触发导入、重建、模型或平台写 | I：独立探针确认空 readiness 只读零写入；S：打开页面并执行首屏 GET 前后逐表计数完全一致 | 通过 |
| WP4 | Demo 显式装载且重放幂等，默认 operational 不显示 demo/simulation | I：独立探针 6 通过；S：二次装载经营事实计数不变，仅审计日志按显式动作 +1；operational 仍为 0 manifest/0 可用域；M：隔离破坏测试红、还原绿 | 通过 |
| WP4 | API 与页面对采购成本、订购单、运输周期、整备成本给出相同缺口 | S：桌面 Demo 页面四项缺口均保持明确；`test_m7r_wp4_readiness.py` 验证服务/API 共用语义 | 通过 |
| WP4 | 桌面/窄屏可读，console 无新增错误 | S：1280×720 与 390×844 均无页面级横向溢出；窄屏关键控件可见可用；console error/warning 为 0 | 通过 |

## 6. 原独立探针（缪海南，6/6）

以下内容来自原独立报告，不归属本补充执行方：

1. 同一内部料号跨店不串，`canonical_product_id` 带 tenant + store 范围。
2. decision 重放幂等；同 `decision_key` 不同载荷拒绝。
3. 未知商品身份策略版本 fail-closed。
4. 空 readiness 投影全 missing、不造假 0、只读零写入。
5. 敏感手机号不进入导入输出与 catalog 载荷。
6. Demo 事实与 operational 投影隔离。

结果：`6/6` 通过。

## 7. 关键 mutation：破坏时红，还原后绿

### 7.1 破坏内容

在独立临时 worktree 中，仅把 `src/ecommerce_agent/readonly_data/service.py` 的 operational source scope 从：

```python
{"actual", "manual"}
```

故意错误扩为：

```python
{"actual", "manual", "demo"}
```

### 7.2 红灯证据

运行：

```bash
pytest -q tests/test_m7r_wp4_readiness.py::test_demo_load_is_sanitized_idempotent_and_scope_isolated
```

结果：`1 failed`。精确失败点为：

```text
first["verification"]["operational_scope_unchanged"] is True
实际值：False
```

这证明测试确实能够检测 Demo 混入 operational，而不是只证明当前实现“碰巧通过”。

### 7.3 还原与绿灯证据

用反向 patch 精确还原上述一处 mutation，重跑同一测试：`1 passed in 1.98s`。

还原后：

- `git diff --check` 退出码 0。
- `git status --short` 无输出。
- mutation 未进入 PR #20，也未写入原工作区。

独立性说明：该 mutation 是 Codex 补充证据，不冒充缪海南执行；原独立报告已另以黑盒探针验证同一 demo/operational 隔离边界。

## 8. 浏览器与端到端证据

### 8.1 空库、首屏与写屏障

在全新数据目录、`MODEL_ENABLED=false`、`KG_IMPORT_ENABLED=false`、loopback 免登录环境启动服务。打开后台“数据准备度”并触发首屏 GET 前后，逐表计数完全一致：

- 所有 `readonly_*` 表均为 0。
- 所有经营事实表均为 0。
- `audit_log` 前后均为 9。
- 没有隐式导入、重建、模型调用或平台写入。

空库 operational 页面：八个数据域全部为 missing，可用数据域 0，四项真实缺口全部保持缺失，manifest 0。即 missing 没有被伪造成数值 0。

桌面 1280×720：`documentElement.scrollWidth == clientWidth == 1265`，无页面级横向溢出。

### 8.2 显式 Demo 装载

首次点击“显式装载安全 Demo”后：

- 页面自动切换为 `scope=demo`。
- 八个数据域可用 `8/8`。
- import manifest 为 8。
- 缺失采购成本、订购单、运输周期、整备成本仍明确显示，未用 Demo 伪造真实能力。
- 默认 operational 范围不受影响。

首次装载后的数据库计数：

```json
{
  "audit_log": 10,
  "catalog_items": 1,
  "commerce_after_sale_cases": 1,
  "commerce_order_lines": 1,
  "commerce_order_logistics": 1,
  "commerce_orders": 1,
  "inventory_balances": 1,
  "marketing_campaign_metrics": 1,
  "ops_operation_records": 1,
  "readonly_canonical_products": 1,
  "readonly_field_evidence": 66,
  "readonly_import_manifests": 8,
  "readonly_product_mapping_events": 1,
  "readonly_product_reconciliation_rows": 3,
  "readonly_product_reconciliation_runs": 1,
  "settlement_statements": 1
}
```

### 8.3 Demo 幂等重放与 operational 隔离

第二次重复装载同一 Demo：

- 上述全部经营事实和 readonly 事实计数不变。
- `readonly_import_row_issues = 0`。
- 仅 `audit_log` 从 10 增至 11，符合“每次显式管理员动作均审计”。
- 再次查询 operational：可用数据域 0、manifest 0、八域全部 missing。

因此“重复事实不增加”和“显式动作可审计”同时成立。

### 8.4 窄屏 390×844

- `innerWidth=390`，`documentElement.clientWidth=375`，`scrollWidth=375`。
- 页面级横向溢出为 false。
- 43 个可见 `.table-wrap` 中，6 个按设计在 panel 内部横向滚动；最大内部 overflow 394px，没有把页面整体撑宽。
- 店铺 ID、scope、只读查询、显式装载安全 Demo 四个关键控件均可见且 enabled。
- 浏览器 console：error 0，warning 0。

### 8.5 截图与哈希

证据目录：`/Users/luckye/Downloads/m7r-wp5-evidence-20260819`

| 文件 | SHA-256 |
|---|---|
| `01-desktop-empty-operational-1280x720.png` | `860e01ba853d1f31fe86c9bea3bb8bb1aebc09096b66f32b3cc106a17dee8682` |
| `02-desktop-demo-1280x720.png` | `81fdac7fb9585e1aa55e3515f4a8f4809a2af5ccaa2506f82d9b97ba843cdbb3` |
| `03-narrow-demo-390x844.png` | `4b65ce845dc5089ec9b70d380e40580d58523a0a03b568d2381ad393224bcdcd` |

## 9. 隐私、隔离、幂等、冲突和写屏障证据汇总

| 关键边界 | 独立证据 | 补充证据 | 结论 |
|---|---|---|---|
| 敏感字段不进入规范化输出 | 独立探针 5 | WP1/WP2 聚焦测试 | 通过 |
| 跨租户/跨店隔离 | 独立探针 1 | WP3 聚焦测试 | 通过 |
| missing 不转 0 | 独立探针 4 | 空库浏览器投影 | 通过 |
| Demo 不混入 operational | 独立探针 6 | 浏览器双向查询 + mutation 红绿 | 通过 |
| 重放幂等 | 独立探针 2（decision） | Demo 二次装载事实计数不变；导入契约测试 | 通过 |
| 同版本/同 key 异载荷冲突 | 独立探针 2 + 静态审阅 | WP1/WP2/WP3 聚焦测试 | 通过 |
| 页面只读写屏障 | 独立探针 4 | 首屏 GET 前后全库逐表计数一致 | 通过 |
| 未知策略/版本 fail-closed | 独立探针 3 | v34/v35 schema 与兼容测试 | 通过 |

## 10. 失败清单、修复和复验记录

### 10.1 产品失败

本次固定对象 `ece61e1` 上：**0 项产品测试失败，0 项阻断缺陷**。因此没有需要退回 WP1～WP4 负责人修复的责任包。

### 10.2 人为 mutation

- 性质：为证明测试有效而故意破坏 operational/demo 隔离，不是产品缺陷。
- 失败：目标测试按预期 `1 failed`。
- 处理：反向 patch 精确还原。
- 复验：同一测试 `1 passed`；worktree 恢复干净。

### 10.3 验收命令修正

- 初次直接执行 `node --check docs/admin-console.html` 时，Node 因 `.html` 扩展不受支持而拒绝。
- 该项是验收命令构造错误，不是页面 JavaScript 失败。
- 修正为先提取内联 `<script>` 再管道给 `node --check -`，退出码 0。

### 10.4 非阻塞存量告警

- 全量回归有 24 条 FastAPI Duplicate Operation ID warning。
- 均来自存量 `traffic_lab_api.py`，与 PR #20/M7-R 功能无关。
- 不影响本次 1035 项测试全绿结论；后续应由对应流量实验 API 责任包处理，不在 M7-R WP5 范围扩修。

## 11. 正式结论与签署边界

在以下精确范围内：

- WP1：已先行合入的 `main@48013b1d3b29a810288c32f73df028c69070064c`；
- WP2～WP4：PR #20 head `ece61e14fb9c326b38dcde084513494147c508e8`；
- 集成验收快照：`ece61e14fb9c326b38dcde084513494147c508e8`；

原独立验收报告、原 6 项独立探针、原含探针全量结果，以及本补充的 104 项聚焦复跑、1035 项全量复跑、mutation 红→绿、桌面/窄屏浏览器和零隐式写证据共同支持以下技术结论：

> **M7-R WP1～WP4 在上述固定代码对象上通过 WP5 技术 Gate；PR #20 可以合入 main。**

本结论只覆盖已经验证的只读导入契约、报表规范化、商品身份、准备度 API/页面和隔离 Demo。以下事项仍未放行，且不得由本报告冒充完成：

- 未经授权的真实平台接入与真实平台字段全覆盖。
- 基于真实经营数据的业务结论。
- 生产发布、生产权限或平台写能力。
- M7-R 最终业务签署及后续 M8-R～M10-R 交付。

签署边界：缪海南原报告是 WP5 独立验收签署来源；本文件由实施方补齐证据和口径，不代签独立验收。建议负责人把本补充报告连同三个截图哈希转交缪海南确认归档。PR #20 合入后，再在 `main` 上单独登记 WP5 Gate 和更新 F-323，避免改变已验收的 PR head。
