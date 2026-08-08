# FIX-14 · 投诉分类 Gate 位置决策包

日期：2026-08-08
状态：等待负责人裁定；代码和门槛均未据此修改

## 数据边界

本文件使用的 40 条投诉平衡集、原 M4 40 条意图集和 WP4 冻结 50 例均已公开，全部只能
作为回归证据，不能作为新的泛化成绩。FIX-15 的密封留出集仍须由外部验收人在运行时解封。

## 同一实现的两层结果

| 层级 | 当前结果 | 结论 |
|---|---:|---|
| 投诉平衡分类集 | coverage 33/40 = 82.5%；precision 13/13 = 100%；recall 13/20 = 65%；负例误报 0/20 | 分类 recall gate failed |
| 原 40 条意图回归 | 31/40 = 77.5%；coverage 34/40 = 85%；作答子集 31/34 = 91.2% | 总体跨过 75%，投诉覆盖仅 5/9 |
| WP4 端到端 live | complaint 8/8；handoff TP=9、FN=1、FP=0、TN=40；recall 90%、precision 100% | 客服 gate passed |
| WP4 回答质量 live | answer_accuracy 92%；hallucination 0%；severe 2；after-sales 9/12；product 15/15 | 客服 gate passed |

分类层仍直接影响知识检索范围、回复 Prompt 变体和 F-117 追溯字段，所以它不是无用指标；
但 `complaints / urgent` 是否建立由规划模型最终决定，所以分类 recall 也不再等于 SLA 路由
recall。集成测试继续验证规划确认投诉后进入 `complaints / urgent`。

## 2 秒预算下的波动

| 平衡集运行 | coverage | complaint coverage | complaint recall | deadline 弃权 |
|---|---:|---:|---:|---:|
| FIX-11 回归 | 80% | 16/20 = 80% | 15/20 = 75% | 8 |
| FIX-13 回归 | 82.5% | 15/20 = 75% | 13/20 = 65% | 7 |

FIX-13 的 7 条弃权全部为约 1.98 秒的 `model_deadline_exceeded`，其中 5 条为投诉、2 条为
售后。另有 2 条已作答投诉被判成售后：1 条来自模型，1 条仍是 after-sales 规则直返的
反方向仲裁。当前实现没有改动 WP3 写死的 2 秒预算，也没有降低任何 gate。

## 待裁定方案

### A · 分类 Gate 继续阻塞发布

- 保留 `complaint_recall >= 0.75` 为签署硬门；当前 65% 因而继续阻塞。
- 优点：检索域、Prompt 变体和意图追溯字段的退化会被及时拦住。
- 代价：即使端到端 `complaints / urgent` 路由和 WP4 客服 gate 已通过，provider 的 2 秒
  尾部仍可单独阻塞 M4；需要通过 provider 容量、分类模型或 Prompt 进一步治理，不能自行
  放宽预算。

### B · 端到端 SLA Gate 阻塞，分类 Gate 保留为质量告警

- 发布硬门改为“真实投诉最终进入 `complaints / urgent`”的 precision/recall；分类 coverage、
  precision/recall 继续逐项报告并设置告警，不删除指标。
- 优点：门禁直接对应顾客 SLA 后果，避免把已被规划模型纠正的分类错误重复计为发布失败。
- 代价：分类退化仍会改变检索范围、Prompt 与追溯字段；必须另定告警升级条件，不能把 65%
  写成通过或从报告中隐藏。

## 负责人需要签的两项

1. 选择 A，或批准 B 并给出端到端 `complaints / urgent` precision/recall 阈值。
2. 明确 WP3 的 2 秒预算继续固定，还是进入单独产品变更；实现者不得自行放宽。

裁定前保持现状：分类 recall gate 仍为 failed，WP4 客服 gate 如实为 passed，M4 不因本文件
自动签署。

## 证据

- `evals/intent/runs/20260808-m4-complaint-balanced-fix13-live.json`
- `evals/intent/runs/20260808-m4-acceptance-fix13-live.json`
- `evals/customer_service/runs/20260808-m4-customer-eval-fix13-live-run2.json`
- `tests/test_intent_routing.py`
- `tests/test_intent_routing_integration.py`
