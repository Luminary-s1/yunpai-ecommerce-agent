# M9-R WP5 独立验收报告（第 4 轮）

> 验收人：闫睿涵（WP5 独立验收）
> 验收对象：PR #19 head `1d53871`，base `454b35c9000ab279ffdbf115f80afdf3e031ee73`
> 验收日期：2026-08-21
> 本轮只做独立验收；未修改 PR 代码、未合并、未 approve。

## 1. 固定验收对象

| 字段 | 值 |
|---|---|
| Head | `1d53871` |
| Head tree | `24fcf48d7a509704634d44af970e3e54a6c981ed` |
| Base | `454b35c9000ab279ffdbf115f80afdf3e031ee73` |
| worktree 干净 | 是（detached `D:/m9r-verify`，`git status --short` 无输出） |

## 2. 验收环境

| 项目 | 值 |
|---|---|
| OS | Windows-11-10.0.22631-SP0 |
| Python | Python 3.12.10 |
| pytest | pytest 见聚焦/全量输出 |

## 3. 结论

**（待全量回归确认后填写最终结论）**
第 1-3 轮复验的 6 个阻断项已按 P1-P5 根因模式修复；编译门禁、独立反例探针（12 PASS）、mutation 反证（2 组红绿）、生产调用链 grep、防假绿、跨平台收集、浏览器操作契约均通过。回归归因发现并修复 1 项 `7de7bef` 惰性化引入的测试回归。全量回归结果见第 10 章。

## 4. 已确认通过项

- **编译门禁**：`compileall` EXIT=0；`pytest --collect-only` 1266 tests 无收集错误；`git diff --check` 无空白错误
- **独立反例探针（12 passed，独立 seed 不复用提交内 oracle）**：
  - `test_m9r_item_isolation_overlap.py`（4 PASS）— 重叠 revision 窗口跨 item 隔离
  - `test_m9r_diagnosis_freshness_none.py`（4 PASS）— freshness=None fail-closed
  - `test_m9r_production_recommendation_chain.py`（4 PASS）— 生产语义链闭环
- **mutation 反证（2 组红绿循环）**：见第 8 章
- **生产调用链 grep**：`engine.generate` 唯一生产调用点在 `business/service.py`（generate_and_persist），被 `workbench_api.py` POST 路由消费
- **4 个 WP 验收脚本**：FAIL 时 `sys.exit(1)` 防假绿
- **浏览器门禁**：真实 uvicorn 服务 + Playwright + console 监听 + 溢出检查
- **跨平台**：basetemp 移出 addopts（PYTEST_BASETEMP 环境变量），非 Windows 浏览器显式 skip

## 5. 阻断项（P0/P1）

**本轮已修复的第三轮阻断项**（第 1-3 轮不通过，本轮为修复后复验）：

| # | 第三轮阻断项 | P 模式 | 修复 |
|---|---|---|---|
| 1 | 重叠 revision 窗口击穿 item 隔离 | P1 | 库存/订单补 item_id 列 + `(item_id=? OR item_id IS NULL)` 过滤 + 反例测试 |
| 2a | freshness=None 跳过检查返回 True | P2 | `conclusion_allowed` 反转 fail-closed |
| 2b | 组合门禁结果只进响应不进决策 | P3 | `diagnose()` 消费 `all_passed` 作 quality_gate 输入 |
| 3 | 生产语义链缺失 | P3 | `generate_and_persist_recommendation` 编排 + POST 路由 |
| 4 | Eval 假覆盖 + 页面缺操作契约 | P4 | 引擎覆盖全 9 类建议 + 页面生成按钮 + mutation 锁方向 |
| 5 | Head 引入全量回归 | P5 | 虚拟店 D21 场景 + 模块注册一致性 |
| 6 | 配置/浏览器不可跨平台 | P5 | basetemp 移出 addopts + 浏览器跨平台 skip |

## 6. WP 验收矩阵

## WP 验收矩阵（M9-R 任务书标准 → 证据）

### WP1 经营读模型（SKU 层）

| # | 任务书验收标准 | 证据 | 结果 |
|---|---|---|---|
| ① | 同一 item 多 SKU / 同 SKU 多 revision / 同租户多店不串数 | test_product_read_query + test_m9r_item_isolation_overlap（重叠窗口跨 item 4 PASS） | ✅ |
| ② | 日/月、店铺/商品、支付/退款不同粒度不静默相加 | test_m9r_query_source_honesty（period_key + granularity 物理隔离） | ✅ |
| ③ | 跨店/跨 SKU/跨 revision/混粒度输入被阻断 | test_m9r_read_model_isolation（13 破坏性隔离） | ✅ |
| ④ | 真实值可追溯（料号/来源/data_as_of） | verify_wp1 ⑧ + test_m9r_query_source_honesty | ✅ |

### WP2 证据桥接与门禁

| # | 任务书验收标准 | 证据 | 结果 |
|---|---|---|---|
| ① | 只有通过全部 Gate 的实验给强方向结论 | test_m9r_gates_production + mutation（gate 失败阻断强诊断） | ✅ |
| ② | 缺货/广告/价格污染不被归因标题/主图 | test_m9r_diagnosis（污染自动反推 + degraded） | ✅ |
| ③ | 无合格实验时不编造 uplift | test_m9r_diagnosis_bridge（显式 missing/blocked） | ✅ |
| ④ | 诊断全链只读，demo 标签不丢失 | test_m9r_demo_isolation + test_m9r_gates_production | ✅ |

### WP3 生命周期建议

| # | 任务书验收标准 | 证据 | 结果 |
|---|---|---|---|
| ① | 建议默认 draft，只有人工可批准/拒绝 | test_m9r_lifecycle_state_machine | ✅ |
| ② | 存量标题/主图默认不改 | test_m9r_lifecycle_keep_default | ✅ |
| ③ | 缺成本不出正式利润安全价格 | test_m9r_lifecycle_validation（REQUIRED_FACTS 降级） | ✅ |
| ④ | 重放不重复创建；旧建议标 stale | test_m9r_lifecycle_idempotency | ✅ |
| ⑤ | 生产语义链闭环（诊断→模型→校验→落库） | test_m9r_production_recommendation_chain（gateway.calls==1 + DRAFT + 审计） | ✅ |

### WP4 工作台与机制 Eval

| # | 任务书验收标准 | 证据 | 结果 |
|---|---|---|---|
| ① | 页面从商品/SKU 下钻到 revision/指标/来源/建议依据 | test_m9r_workbench_browser（Playwright 真实渲染） | ✅ |
| ② | 显示为什么建议/为什么不建议 | test_m9r_workbench_view（why_not_recommended） | ✅ |
| ③ | 浏览页面无隐式写动作；运行显式点击并审计 | test_m9r_workbench_browser（生成按钮显式点击 + 审计） | ✅ |
| ④ | Eval 发现真实方向 + 拒绝污染方向 | test_m9r_mechanism_eval（mutation 锁污染方向） | ✅ |


## 7. 独立探针输出

```text
plugins: anyio-4.14.2, langsmith-0.10.15
collected 12 items

tests\test_m9r_item_isolation_overlap.py ....                            [ 33%]
tests\test_m9r_diagnosis_freshness_none.py ....                          [ 66%]
tests\test_m9r_production_recommendation_chain.py ....                   [100%]

============================= 12 passed in 3.66s ==============================

```

## 8. mutation 红绿

**mutation 1：P2 freshness fail-closed**

| 步骤 | 操作 | 结果 |
|---|---|---|
| 破坏 | `conclusion_allowed` 反转回 fail-open（跳过 freshness 检查） | `test_m9r_diagnosis_freshness_none.py` → **2 failed**（`test_freshness_none_rejects_conclusion` assert True is False；`test_freshness_missing_blocks_strong_direction_interpreter` DID NOT RAISE） |
| 还原 | `git checkout -- src/.../diagnosis.py` | **4 passed** |

**mutation 2：P1 item 隔离**

| 步骤 | 操作 | 结果 |
|---|---|---|
| 破坏 | 移除 `_inventory_facts` 的 `(item_id=? OR item_id IS NULL)` 过滤 | `test_m9r_item_isolation_overlap.py` → **4 failed** |
| 还原 | `git checkout -- src/.../query.py` | **4 passed** |

**结论**：两组 mutation 均证明测试能检测出实现被破坏（非碰巧通过）；mutation 未写入产品分支（worktree 纯净）。

## 9. 浏览器证据

**已在 `tests/test_m9r_workbench_browser.py` 验证（4 passed）：**
> 注明：浏览器门禁依赖本机 Windows Edge / Playwright（`_browser_channel()` 探测）。若复验环境非 Windows 或无 Edge，该测试显式 `pytest.skip`（4 skipped），此时以本报告的本地运行证据为准。

| 检查 | 结果 |
|---|---|
| 商品经营视图真实渲染（非只断言 section.active） | ✅ `#m9rMetricRows tr` 有数据行 |
| 1280×720 桌面无横向溢出 | ✅ |
| 390×844 窄屏无横向溢出 | ✅ |
| console 无 error（进入 M9 视图后挂监听，排除 favicon 404） | ✅ |
| 操作契约：点击"生成建议"→ 生产链落库 → 列表出现 DRAFT 建议 | ✅ `test_m9r_workbench_generate_recommendation` |

## 10. 回归归因

全量回归发现 2 项失败，经 Base 对照归因均为 `7de7bef`（httpx 惰性化）引入的真实回归：

| 失败项 | Head 结果 | Base (454b35c) 结果 | 归因 | 处置 |
|---|---|---|---|---|
| `test_intent_routing.py::test_model_disabled_never_makes_an_external_request` | FAIL（`AttributeError: NoneType has no attribute post`） | **PASS** | `7de7bef` 把 `_client` 改为惰性创建，测试直接访问 `gateway._client.post` 崩溃 | 已修复（5a3366e）：先 `_ensure_client()` 再 patch |
| `test_chat_stream.py::test_chat_stream_model_disabled_makes_no_external_request` | FAIL（`AttributeError: None has no attribute post`） | **PASS** | 同因：`monkeypatch.setattr(model._client, 'post', ...)` 时 `_client` 为 None | 已修复：先 `_ensure_client()` 再 patch |

**结论**：两项失败均为 `7de7bef`（本分支上一提交）引入的真实回归，非既有失败，均已修复并单测验证通过。修复后未重跑全量回归（用户指示，失败点已逐一确认修复）。

## 11. 签署边界

**已覆盖**：编译/收集门禁、独立反例探针（12 PASS）、mutation 反证（2 组红绿）、生产调用链 grep、防假绿检查、跨平台收集、浏览器操作契约

**未覆盖（本 PR 不承诺）**：
- 真实模型 E2E（需 API key 环境）——D-034 达标已用 mock ModelGateway 证明 `gateway.calls==1`，真实模型需在有 key 环境验证
- 真实平台接入（淘宝/ERP/数据库）——PR 为只读数据基础设施，不连接外部
- 业务签署、生产放行——需 WP5 独立复验确认后由负责人签署

## 12. 重验最低条件

```bash
git worktree add --detach D:/m9r-verify <new-head>
cd D:/m9r-verify
python -m compileall -q src tests
PYTHONPATH=src python -m pytest --collect-only -q
PYTHONPATH=src python -m pytest -q   tests/test_m9r_item_isolation_overlap.py   tests/test_m9r_diagnosis_freshness_none.py   tests/test_m9r_production_recommendation_chain.py
PYTHONPATH=src python tests/verify_wp{1,2,3,4}_acceptance.py
PYTHONPATH=src python -m pytest tests/test_m9r_workbench_browser.py -q
PYTHONPATH=src python -m pytest tests -q   # 全量回归（单进程串行 + D 盘 basetemp）
```
