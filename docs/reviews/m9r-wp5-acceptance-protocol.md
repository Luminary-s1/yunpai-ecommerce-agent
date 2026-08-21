# M9-R 商品流量与生命周期经营 — 独立验收协议

> **定位**：通用验收手册（[acceptance-playbook.md](acceptance-playbook.md)）在 M9-R 上的实例化。
> **被测对象**：PR #19 `feature/m9r-read-model`（统一 PR，WP1–WP4）
> **验收人**：闫睿涵（WP5 独立验收）；**负责人**：胡磊
> **任务书唯一权威源**：`docs/tasks/M9R_PRODUCT_TRAFFIC_LIFECYCLE_WORKBENCH.md`

---

## 1. 固定验收对象

| 字段 | 值 |
|---|---|
| Head | 每轮固定（当前 `ae7d97a`） |
| Base | `8de48c35` |
| PR | #19 OPEN / head `feature/m9r-read-model` |
| 工作包 | WP1 读模型 · WP2 证据桥接/门禁 · WP3 生命周期建议 · WP4 工作台/Eval |

```bash
git worktree add --detach D:/m9r-verify <head>
cd D:/m9r-verify && git status --short  # 必须干净
```

## 2. 编译与收集门禁

```bash
python -m compileall -q src tests        # exit 0
python -m pytest --collect-only -q       # 无收集错误
git diff --check <base>..HEAD            # exit 0
```

注意：迁移拼接是语法错误高发区（闫睿涵抓过 `_apply_v35/v36` 缺三引号）。

## 3. WP 验收矩阵（任务书验收标准 → 证据）

### WP1 经营读模型（SKU 层）

| # | 任务书验收标准 | 证据类型 | 验证方式 | 测试/脚本 |
|---|---|---|---|---|
| ① | 同一 item 多 SKU / 同 SKU 多 revision / 同租户多店不串数 | 探针 | composite_key 五元组 + 重叠窗口跨 item | test_product_read_query + test_m9r_item_isolation_overlap |
| ② | 日/月、店铺/商品、支付/退款不同粒度不静默相加 | 断言 | period_key + granularity 物理隔离 | test_m9r_query_source_honesty |
| ③ | 跨店/跨 SKU/跨 revision/混粒度输入被阻断 | 探针 | 范围隔离检查 | test_m9r_read_model_isolation |
| ④ | 真实值可追溯（料号/来源/data_as_of） | 断言 | authoritative_service + import_manifest_id | verify_wp1 ⑧ |

### WP2 证据桥接与门禁

| # | 任务书验收标准 | 证据类型 | 验证方式 | 测试/脚本 |
|---|---|---|---|---|
| ① | 只有通过全部 Gate 的实验给强方向结论 | mutation | gate 失败阻断强诊断 | test_m9r_gates_production + mutation |
| ② | 缺货/广告/价格污染不被归因标题/主图 | 探针 | 污染自动反推 + degraded | test_m9r_diagnosis |
| ③ | 无合格实验时不编造 uplift | 探针 | 显式 missing/blocked | test_m9r_diagnosis_bridge |
| ④ | 诊断全链只读，demo 标签不丢失 | 断言 | 零写动作 | test_m9r_demo_isolation |

### WP3 生命周期建议

| # | 任务书验收标准 | 证据类型 | 验证方式 | 测试/脚本 |
|---|---|---|---|---|
| ① | 建议默认 draft，只有人工可批准/拒绝 | 断言 | 状态机 DRAFT 强制 | test_m9r_lifecycle_state_machine |
| ② | 存量标题/主图默认不改 | 探针 | keep/observe 默认 | test_m9r_lifecycle_keep_default |
| ③ | 缺成本不出正式利润安全价格 | 探针 | REQUIRED_FACTS 降级 | test_m9r_lifecycle_validation |
| ④ | 重放不重复创建；旧建议标 stale | 断言 | 幂等 + stale | test_m9r_lifecycle_idempotency |
| ⑤ | 生产语义链闭环（诊断→模型→校验→落库） | 静态 grep + 探针 | generate_and_persist + POST 路由 | test_m9r_production_recommendation_chain |

### WP4 工作台与机制 Eval

| # | 任务书验收标准 | 证据类型 | 验证方式 | 测试/脚本 |
|---|---|---|---|---|
| ① | 页面从商品/SKU 下钻到 revision/指标/来源/建议依据 | 浏览器 | Playwright 真实渲染 | test_m9r_workbench_browser |
| ② | 显示为什么建议/为什么不建议 | 断言 | why_not_recommended | test_m9r_workbench_view |
| ③ | 浏览页面无隐式写动作；运行显式点击并审计 | 浏览器 + 探针 | 生成按钮显式点击 + 审计 | test_m9r_workbench_browser |
| ④ | Eval 发现真实方向 + 拒绝污染方向 | mutation | mutation 锁污染方向 | test_m9r_mechanism_eval |

## 4. 独立反例探针清单

| # | 探针 | 对齐反例类型 | 状态 |
|---|---|---|---|
| 1 | 重叠 revision 窗口跨 item 击穿隔离 | 重叠窗口/范围击穿 | test_m9r_item_isolation_overlap（4 PASS） |
| 2 | freshness=None 跳过检查返回 True | 缺必需输入 fail-closed | test_m9r_diagnosis_freshness_none（4 PASS） |
| 3 | 生产语义链缺失（engine.generate 无生产调用点） | 生产调用链 grep | test_m9r_production_recommendation_chain（4 PASS） |
| 4 | 越权输出递归绕过（嵌套 effect/自然语言） | 伪造引用 | test_m9r_forbidden_output_recursive |
| 5 | demo 数据污染 formal 查询 | demo 污染 formal | test_m9r_demo_isolation |
| 6 | 缺失变 0 绕过 Gate | 缺失变 0 | test_m9r_write_barrier |

## 5. mutation 反证

| mutation | 破坏点 | 红灯 | 还原 | 绿灯 |
|---|---|---|---|---|
| P2 freshness fail-open | `conclusion_allowed` 跳过 freshness | 2 failed | `git checkout` | 4 passed |
| P1 item 过滤移除 | 聚合 SQL 去 item_id 过滤 | 4 failed | `git checkout` | 4 passed |

## 6. 可复现命令

```bash
git worktree add --detach D:/m9r-verify ae7d97a
cd D:/m9r-verify

python -m compileall -q src tests
PYTHONPATH=src python -m pytest --collect-only -q

# 独立反例（3 组核心探针）
PYTHONPATH=src python -m pytest -q \
  tests/test_m9r_item_isolation_overlap.py \
  tests/test_m9r_diagnosis_freshness_none.py \
  tests/test_m9r_production_recommendation_chain.py

# 4 个 WP 验收脚本（FAIL 时 sys.exit(1)）
PYTHONPATH=src python tests/verify_wp{1,2,3,4}_acceptance.py

# 浏览器门禁（Windows Edge；非 Windows 显式 skip）
PYTHONPATH=src python -m pytest tests/test_m9r_workbench_browser.py -q

# 全量回归（单进程串行，防 SQLite 锁竞争）
PYTHONPATH=src python -m pytest tests -q
```

## 7. 报告模板

用 `scripts/acceptance/make_report.py` 生成，对齐验收手册第 9 章。

## 8. 已验收记录

| 轮次 | head | 结果 | 存档 |
|---|---|---|---|
| 1 | `6511cdc` | 不通过（scope policy/证据桥/M5-R 契约/越权输出/页面/模块目录） | PR#19 comment |
| 2 | `2da07f9` | 不通过（语法缺口/v36→v37 外键/item 串 revision/验收脚本假绿/PII/页面/D-034） | PR#19 comment |
| 3 | `421f969` | 不通过（重叠窗口击穿/freshness fail-open/生产链缺失/Eval 假覆盖/全量回归/跨平台） | PR#19 comment |
| **4** | **`ae7d97a`** | **按 P1-P5 根因模式修复后待复验** | 本协议 + 报告 |
