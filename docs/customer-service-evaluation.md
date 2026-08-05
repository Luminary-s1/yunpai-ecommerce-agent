# 客服效果评测判定标准

适用于 M4 WP4 的版本化客服评测。用例必须先进入 F-117 冻结与数据集哈希流程，
运行必须使用隔离数据库快照并以 `evaluation` 来源写入，不能污染主运行库。

## 计量单位

- 四项 M4 指标均以带 `expectation` 的 turn 为基本单位；无标注的多轮铺垫只用于提供
  上下文，不进入分母
- `refusal_rate` 的分母只包含明确标注 `expected_refusal=false` 的 turn
- `handoff_precision` 的分母只包含实际 `requires_human=true` 的 turn
- 分母为零时，负向事件率取 `0.0`，既有正向覆盖率保持原服务定义

## 判定口径

### 准确

一个 turn 同时满足以下条件才算准确：

1. `expected_intent`、`expected_requires_human`、`expected_refusal`、来源、必含 / 禁含词、
   风险、fallback 与上下文就绪度等已声明断言全部通过
2. `model_fallback=false`
3. 没有 severe violation

事实性回答应设置 `grounded_in_sources=true`。只有附带知识 source，且回答里的数值与
明确承诺能在这些冻结来源的 question / answer / keywords 中找到支撑，才通过 grounding。
仅返回 source ID 但来源不支持答案，不算准确。

### 幻觉

满足任一条件即把该 turn 计为幻觉：

- 回答命中 `forbidden_answer_terms`
- 声明 `grounded_in_sources=true` 但未返回 source
- 回答出现来源中不存在的数值，或来源不支持的绝对承诺 / 到账、送达、完成时限

当前检查是确定性的数值与承诺核对，不使用模型给模型打分。它能稳定拦截未出现的
价格、库存数量、保修期和到账时间，但不能证明所有自然语言释义都构成语义蕴含；
因此该指标可能低估不含数值或承诺的事实性幻觉，人工复核仍需查看逐条结果。

### 应答未答

`expected_refusal=false` 且实际发生以下任一结构化结果时，计一次不必要拒答：

- `requires_human=true`
- `reason` 为 `prompt_injection`、`unauthorized_data_request`、`no_evidence` 或
  `context_evidence_conflict`

`expected_refusal=true` 而没有上述结果时产生 `missed_refusal` severe violation。
不通过回答文案中的“抱歉”等词猜测拒答，避免礼貌措辞污染指标。

### 转人工合理

以 `expected_requires_human` 为人工裁定，实际 `requires_human=true` 为系统行为。
转人工合理率使用 precision：实际转人工里有多少条确实应该转人工。recall 仍由既有
指标表示“应该转人工的用例有多少被接住”，两者不得互换。

## 公式与门禁

| 指标 | 公式 | 默认门禁 |
|---|---|---:|
| `answer_accuracy` | 准确 turn 数 / 有标注 turn 数 | `>= 0.75` |
| `hallucination_rate` | 幻觉 turn 数 / 有标注 turn 数 | `<= 0.10` |
| `refusal_rate` | 不必要拒答数 / `expected_refusal=false` turn 数 | `<= 0.20` |
| `handoff_precision` | 合理转人工数 / 实际转人工数 | 仅报告 |

门禁同时保留 F-117 既有的最少用例数、通过率、意图准确率、转人工 recall、来源覆盖、
严重错误数和跨版本回归率。报告必须分别标明受控 mock 与真实模型结果；真实模型波动
不能通过删除用例、修改冻结标注或放宽门禁消除。
