# M4 智能客服后端 WP1

- 分支：`feature/m4-customer-service`
- 范围：客服会话管理与上下文控制
- 执行计划：`docs/tasks_intro/M4_DAILY_PLAN.md` D01–D05

## D01 · Token 计数与历史截断

- 基线：全量测试 `302 passed in 130.28s`
- 新增确定性保守估算：中日韩字符按 1 token/字，其余字符按长度除以 4 向上取整
- 历史从最新消息向前保留；即使最近消息自身超出预算也至少保留该消息，并通过
  `over_budget` 标识
- 判据：构造 200 条超长历史，在预算内保留连续的最新窗口，最后一条原样保留

## D02 · 配置与知识预算

- 新增 `MODEL_CONTEXT_LIMIT_TOKENS=128000` 与 `CONTEXT_BUDGET_RATIO=0.7`；
  ratio 钳制在 0.1–0.9
- 生成与决策 Prompt 消费上游历史，不再自行截取 6 条
- 知识超过预算时按 score 从低到高移除，存在知识时至少保留最高分一条
- 四项定向测试通过
- 反证：临时把默认 `context_budget_ratio` 从 0.7 改为 0.99，同一历史截断
  用例按预期失败（保留数从 7 变为 9）；还原 0.7 后复验通过
