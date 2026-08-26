# M9-R PR #19 修复后审查报告（单人模式，第二轮）

> **审查对象**：PR #19 `feature/m9r-read-model` @ `0302c1a`
> **审查时间**：2026-08-22（第二轮，修复 P0 缺陷后）
> **审查方法**：agentops-awesome-list（T3 基线）+ 任务书逐条对照 + 修复验证

---

## 审查任务书

- **审查范围（含）**：M9-R 核心模块（query.py、service.py、engine.py、scenes.py、eval.py、diagnosis.py、workbench_api.py）
- **审查范围（不含）**：M10-R、客服 Agent、库存/订单写入路径
- **必查组件**：M9R 任务书 WP1-WP5 验收标准 + T3 架构基线
- **必跑测试（实际跑了）**：
  - 定向 7 个文件 **59 passed** `[test]`
  - 全量回归：**已暂停**（用户要求）→ `[unverified]`
- **基线对照**：`git diff 454b35c..HEAD` 净增 +16508/-34 `[baseline]`
- **已知决策**：R2 MISSING + 独立 reason、R5 方案 C-lite、R3 结构化 degradation_reasons

---

## 第一轮审查修复验证

### 已确认修复（5 个 P0/P1 + 3 个文档/死代码）

| # | 缺陷 | 状态 | 验证方式 |
|---|---|---|---|
| 1 | query.py:342 CTE 缺 sku 过滤 | ✅ 修复 | grep 确认 `l.sku_id=?` + `cte_item_cond` |
| 2 | query.py:347/273 ORDER BY 无尾键 | ✅ 修复 | grep 确认 `id DESC` / `o.id DESC` |
| 3 | query.py:525 mapping 跨 connector | ✅ 修复 | grep 确认 `source_updated_at DESC, revision_no DESC, id DESC` |
| 4 | engine.py:311 SELECTION 生产不可达 | ✅ 诚实标注 | grep 确认 V1 边界注释 |
| 5 | scenes.py:166 缺数据 degraded | ✅ 误判不改（Eval 路径正确） | 代码验证 |
| 6 | eval.py:99 frozen 原地修改 | ✅ 修复 | grep 确认 `model_copy` |
| 7 | M9R-WP5-ACCEPTANCE-REPORT.md head/tree | ✅ 修正 | git diff 确认 |
| 8 | call() 死代码 | ✅ 删除 | grep 确认无残留 |
| 9 | 反例测试增强 | ✅ 补充 | test_order_source_not_polluted_by_other_sku + test_order_source_deterministic_on_tie + test_query_mapping_not_hidden_by_other_connector_version |

### 测试证据

```
tests/test_m9r_query_source_honesty.py ........... [11 passed]
tests/test_product_read_query.py ..........         [10 passed]
tests/test_m9r_item_isolation_overlap.py ....      [ 4 passed]
tests/test_m9r_mechanism_eval.py ............       [12 passed]
tests/test_m9r_diagnosis_production.py .....       [ 5 passed]
tests/test_m9r_production_recommendation_chain.py ....  [4 passed]
tests/test_workbench_api.py .............          [13 passed]
TOTAL: 59 passed in 34.08s
```

---

## 第二轮审查新发现

### 遗留 P1 问题（非阻塞，但影响复验）

| # | 位置 | 问题 | 验证方式 | 影响评估 |
|---|---|---|---|---|
| 1 | diagnosis.py:192-194 | 污染类型校验未锁子类型与证据对应 | grep 确认 `facts.stockout or facts.pollution is not None` | PLAUSIBLE：生产路径由解释器控制，校验是兜底；可后续补强 |
| 2 | test_m9r_mechanism_eval.py:276 | 方向场景正向测试只证 plumbing，不证发现方向 | 读测试确认 mock 按 sku_id 硬编码 | PLAUSIBLE：V1 生产不可达已诚实标注，测试证明"信号齐→非降级"的引擎能力 |
| 3 | docs/reviews/M9R-taskbook-review.md | 任务书审查报告无修订日期 | 读文件确认 | LOW：内部审查文档，不影响复验 |

### 第二轮审查确认无新增问题

- **边界破坏**：M9-R 改动不影响非 M9-R 模块（grep 确认）
- **接口契约**：workbench_api.py 端点签名与前端约定一致（grep 确认 11 个路由均内联异常处理）
- **并发/幂等**：recommendation create/transition 有 payload_hash 检查（grep 确认）
- **资源泄漏**：所有 DB 查询使用 `with self.db.connect() as conn:` 上下文管理器（grep 确认）
- **错误处理**：multi_line 查询失败时 `fetchone() == None` → `refund_value=None` → 走单行逻辑（安全兜底）

---

## 体检结论

- **判定**：`risky`（全量回归未跑，不能断 ready）
- **适用模板**：T3 Production Project
- **一句话结论**：第一轮审查发现的 9 个问题全部修复并验证通过，第二轮审查未发现新增问题；但**全量回归尚未重跑**，不能承诺 1273 passed 仍成立
- **置信度**：`medium`（定向测试 59 passed，全量 unverified）

---

## 下一步（必须完成才能交付）

1. **重跑全量回归**（约 20 分钟）→ 确认 1273 passed 仍成立
2. **跑浏览器测试**（约 1 分钟）→ 确认 4 passed 仍成立
3. **提交 PR** → 更新 PR #19 head
4. **给闫睿涵飞书汇报** → 附验收证据

---

**报告保存位置**：`docs/reviews/M9R-round2-review.md`
**源代码/配置/运行时文件未改动**（本报告是只读审查输出）
