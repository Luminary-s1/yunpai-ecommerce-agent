# 独立验收手册（Acceptance Playbook）

> **固化对象**：负责人闫睿涵对 M9-R 三轮独立复验的方法论（`hlanan886` 学习提炼）。
> **用途**：任何交付物（M7-R / M8-R / M9-R / M10-R / M11-R…）的 WP5 独立复核都照此执行。
> **定位**：本手册与具体模块无关；每个交付物落地时，在 `docs/reviews/<模块>-acceptance-protocol.md` 中实例化。
> **验收人纪律**：独立验收人不得参与实现（WP5 纪律）；只做验证与留痕；不修改 PR 代码、不合并、不 approve、不替开发修复缺陷。

---

## 第 0 章 验收角色与纪律

- 独立验收人不得参与 WP1–WP4 功能实现。
- 每轮只做独立检查；不修改 PR 代码、不合并、不 approve、不执行灾备（除非任务书要求）。
- 结论只覆盖**已验证的固定代码对象**；未验证的边界（真实平台接入、生产发布、业务签署）必须显式列在"签署边界"里，不得冒充完成。

---

## 第 1 章 固定验收对象

**铁律：只在干净 detached worktree + 固定 Head/Base SHA 上复验，绝不信任开发者工作区。**

| 字段 | 值 | 验证命令 |
|---|---|---|
| Head SHA | `<head>` | `git rev-parse HEAD` |
| Head tree | `<tree>` | `git rev-parse HEAD^{tree}` |
| Base SHA | `<base>` | `git rev-parse <base>` |
| PR 状态 | OPEN / MERGEABLE / CLEAN | `gh pr view <n>` |
| 工作包矩阵 | WP1–WP4 各自范围提交 | `git log --oneline <base>..<head>` |

```bash
git worktree add --detach D:/<module>-verify <head>
cd D:/<module>-verify
git status --short   # 必须无输出（干净）
```

---

## 第 2 章 编译与收集门禁

目的：确保"声称能跑"的东西真的能跑（闫睿涵第二轮抓过 `compileall` 语法错误 + pytest 收集 ImportError）。

```bash
python -m compileall -q src tests   # exit 0 才继续
python -m pytest --collect-only -q  # 无收集错误
git diff --check <base>..HEAD       # exit 0
```

注意：迁移拼接（`_apply_v3X`）是语法错误高发区——SQL 三引号、`@staticmethod` 缺失、方法漏包。

---

## 第 3 章 独立最小反例探针（核心）

**不复用提交内 oracle；独立 seed 真实数据；每个探针 `构造反例 → assert 应失败`。**

结构（模板见 `scripts/acceptance/probe_template.py`）：

```python
def probe_xxx(tmp) -> dict:
    db = new_db(tmp)                    # 独立 tmp SQLite
    seed_xxx(db)                        # 独立 seed（不复用提交内 fixture）
    service = XxxService(db)
    try:
        result = service.do_bad_thing() # 构造反例
        return {"verdict": "FAIL", "actual": "本应拒绝却成功：..."}
    except XxxError as exc:
        return {"verdict": "PASS", "actual": f"被拒绝：{exc}"}
```

每个探针输出 `[PASS]/[FAIL] + 预期/实际`，汇总后 FAIL 时 `sys.exit(1)`（防假绿）。

### 反例类型清单（通用）

| # | 类型 | 说明 | M9-R 实例 |
|---|---|---|---|
| 1 | 跨租户/跨店隔离 | 同 key 跨 scope 不串 | trusted store 读跨店 |
| 2 | 重叠窗口/范围击穿 | 两个相同 scope 记录，查询不串 | 重叠 revision 窗口击穿 item 隔离 |
| 3 | 缺必需输入 fail-closed | 缺必需输入 → 必须被拒（不跳过检查返回 True） | freshness=None 返回 True |
| 4 | 伪造引用 | 伪造外部引用（料号/run_ref/policy_ref/order_id）→ 必须被拒 | 料号伪造绕过 Gate |
| 5 | demo 污染 formal | demo 数据进正式范围 → 必须隔离 | demo 进 operational |
| 6 | 缺失变 0 绕过 | 把 missing 改成 0 → Gate 仍不过 | 缺失费用置 0 不能过 Gate |
| 7 | 写屏障 | 读/浏览操作后业务表计数不变（不隐式写） | 页面 GET 零写入 |
| 8 | 审计完整性 | 每个写操作后 audit_log 有对应条目；失败不落审计 | ordering/profit 无审计 |
| 9 | 状态机非法转换 | 跳过中间态 → 必须拒绝 | draft→confirmed 绕过人工 |
| 10 | 金额方向/精度 | 负数金额、超大金额、非法单位 → 被拒 | 金额方向校验 |
| 11 | 并发旧版本冲突 | 旧版本推进 → version_conflict | 并发旧版本确认 |
| 12 | 幂等 | 相同输入重放 → 结果不变，不重复入账 | 重放幂等 |

---

## 第 4 章 mutation 反证

目的：证明"测试能检测出实现被破坏"，而非"碰巧通过"。

```bash
# 1. 在独立 worktree 故意破坏一个实现点（如把 operational scope 扩为含 demo）
# 2. 跑目标测试 → 必须红（红灯证据）
# 3. 反向 patch 精确还原 → 重跑 → 必须绿（绿灯证据）
git diff --check  # exit 0
git status --short  # 无输出（mutation 未写入产品分支）
```

记录：破坏内容、红灯输出、还原后绿灯输出、精确失败点。

---

## 第 5 章 生产调用链 grep（静态检查）

对每个"能力"，确认其生产消费点存在，而不是只有 eval/test 在用。

```bash
grep -rn "XxxService.method" src/ | grep -v test
# 反例：generate() 唯一调用在 eval.py → 生产链路不存在（即使实现存在）
```

闫睿涵实例：`recommendation_engine.generate()` 唯一调用在 `product_workbench/eval.py` → "诊断→模型建议→校验→持久化"生产链不存在。实现存在 ≠ 已接线。

---

## 第 6 章 防假绿检查

| 假绿形态 | 要求 |
|---|---|
| 验收脚本"输出 FAIL 退出码 0" | 脚本 FAIL 时必须 `sys.exit(1)` |
| dict 断言冒充浏览器 Gate | 浏览器必须真实起服务 + Playwright + console 监听 + 溢出检查 |
| 门禁函数缺输入跳过检查返回 True | 缺输入时 fail-closed（返回 False / 阻断） |
| Eval 只验证 degraded 不验证方向 | 断言锁定 diagnosis_type + recommendation_type（不只 degraded） |
| 断言 section.active 而非真实渲染 | 等真实数据行渲染（如 `#m9rMetricRows tr` 有数据） |

---

## 第 7 章 跨平台/跨环境可复现

- 不用硬编码平台路径（`--basetemp` 固定 Windows D 盘 → macOS/Linux 失败）。
- 提供干净环境可跑的命令前缀：代理屏蔽（`NO_PROXY=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 ...`）、`PYTHONPATH=src`。
- 浏览器测试不要只认 Windows Edge 路径，缺失时显式 skip 并注明。

---

## 第 8 章 回归归因

全量回归失败项的处理：

```bash
# 1. 记下 head 上失败的测试
# 2. 在固定 Base 上单独复跑同一批 → 区分"既有失败"vs"本 PR 引入"
# 3. 超时问题：给出真实耗时，不用固定 timeout 掩盖（闫睿涵抓的 900s 超时掩盖 939s）
```

判定：Base 上复跑通过 = 本 PR 引入（阻断）；Base 上也失败 = 既有失败（不阻断，但如实记录）。并行（xdist）对 SQLite 单文件库会引入假失败——优先串行，或用模板库提速（见 conftest `_migration_template`）。

---

## 第 9 章 报告模板

`scripts/acceptance/make_report.py` 生成，结构：

```markdown
# <模块> WP5 独立验收报告（第 N 轮）

## 1. 固定验收对象     # Head/Base/tree/PR 状态/工作包矩阵
## 2. 验收环境         # OS/Python/pytest/git/浏览器/代理/cleanup
## 3. 结论             # 通过 / 不通过（列阻断项）
## 4. 已确认通过项     # 编译门禁 + 聚焦测试 + 独立探针 PASS 项
## 5. 阻断项（P0/P1）  # 每项：反例场景 + 独立复现 + 影响
## 6. WP1–WP4 验收矩阵 # 任务书标准逐条 → 证据/探针/mutation/断言 + 结果
## 7. mutation 红绿    # 破坏→红→还原→绿
## 8. 浏览器证据       # 桌面/窄屏截图 + 溢出/console/写屏障 + SHA-256
## 9. 回归归因         # 失败项在 Base 上的复跑结果
## 10. 签署边界        # 已覆盖 / 未覆盖（真实平台、生产、业务签署）
## 11. 重验最低条件    # 修复后固定新 head 再申请复验
```

---

## 迁移方法（如何套用新交付物）

1. 读任务书 → 把验收标准逐条抄进"WP 验收矩阵"表（标准原文 + 证据类型 + 验证方式）
2. 跑第 1–2 章（固定对象 + 编译收集门禁）
3. 按第 3 章反例类型清单写独立探针（复用 `probe_template.py`）
4. 挑 1–2 个关键边界做 mutation 反证（第 4 章）
5. 对核心能力做生产调用链 grep（第 5 章）
6. 检查防假绿（第 6 章）→ 跑全量回归归因（第 8 章）
7. 用 `make_report.py` 生成报告，按第 9 章模板输出
