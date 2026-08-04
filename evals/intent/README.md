# 意图分类基准

`tests/` 回答「有没有回归」，这里回答「现在有多好、差在哪」。
两者的关键区别：**测试的样例是照着实现写的，基准的语料是照着现实写的。**

## 跑

```bash
.venv/bin/python evals/intent/run.py --mode rule    # 只测规则层（model=None）
.venv/bin/python evals/intent/run.py --mode mock    # 走 mock 网关
.venv/bin/python evals/intent/run.py --mode live    # 打真实模型
```

`live` 需要 `MODEL_ENABLED=true` 和有效的 `MODEL_API_KEY`，否则 `run.py` 会直接
退出而不是给你一份全 `default` 的假成绩单。

留档对比：

```bash
.venv/bin/python evals/intent/run.py --mode live --out evals/intent/runs/20260804-live.json
```

## 三个数字怎么读

| 指标 | 含义 |
|---|---|
| 端到端准确率 | `default` 兜底也算一次预测。反映用户实际体感 |
| 覆盖率 | 非 `default` 的比例。**它掉下去通常是故障，不是模型变笨** |
| 判定准确率 | 只统计真正给了答案的那些。反映分类能力本身 |

`default` 在这里被当作**弃权**而不是「预测为 chitchat」。混在一起统计，兜底会
把 chitchat 类的分数刷得很好看，掩盖掉「模型根本没被调用」这种故障。

## 标签体系

标签比总分有用——总分告诉你好不好，标签告诉你往哪修。

| 标签 | 含义 | 谁该负责 |
|---|---|---|
| `plain` | 直白表达，含关键词 | 规则层 |
| `keyword_free` | 语义清楚但不含任何关键词 | 模型层 |
| `negation` | 含否定（「不需要退款了」） | 模型层，但**规则层会先截胡** |
| `cross_domain` | 关键词出现在别的语境（相机的「曝光」） | 模型层，同样会被规则层截胡 |
| `typo` | 错别字 | 模型层 |
| `mixed` | 多意图混合 | 优先级策略 |
| `meta` | 关于 AI 本身的问题 | 待定义 |
| `ambiguous` | 标注本身有争议 | **人**——这些需要团队定调，不是模型的锅 |
| `escalation` | 有升级/维权信号 | 应触发人工接管 |
| `degenerate` | 空串 / 纯符号 / 纯 emoji | 输入校验 |

## 加语料的规矩

1. **来源要真**。`source` 字段区分 `probe-YYYYMMDD`（手工探针）、`handcraft`
   （凭经验编的）、`plan-d11`（照需求文档抄的）。第三类最不可信——用规格造的
   样例验规格，是循环论证。
2. **不要为了让分数好看而删语料**。分数低是信息，删掉就没了。
3. **`ambiguous` 标签是给人看的**。这类样例的 expected 只是当前约定，改标注要
   连带改需求文档，不要偷偷改。
4. 加完跑一遍 `--mode rule`，如果新语料让规则层分数掉了，**那是对的**——说明你
   找到了规则层的真实边界。

## 已知缺口（截至 2026-08-04）

- `keyword_free` 在规则层准确率 0%，符合预期；模型层（mock）只有 40%，
  说明 mock 那张表不能代表真实模型，**live 跑分之前不要下结论**。
- `negation` / `cross_domain` 全错，且**模型层救不了**：规则层命中即短路返回，
  模型没有机会介入。这是两级链的结构性弱点，见 `src/ecommerce_agent/intent.py`。
- `classify()` 的 `except Exception` 不记日志、不计数，`default` 无法区分
  「模型判定」与「模型故障」。覆盖率指标是目前唯一能发现这件事的手段。
