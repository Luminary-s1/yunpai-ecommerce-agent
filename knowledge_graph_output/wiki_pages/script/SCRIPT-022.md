# 取消订单会改变交易状态，不能由智能客服直接执行。请先在平台订单页尝试取消；若无法操作，我会转人工核对发货状态。

> **实体 ID**：`SCRIPT-022` ｜ **类型**：`script` ｜ **层级**：`seller`

## 当前结论

取消订单会改变交易状态，不能由智能客服直接执行。请先在平台订单页尝试取消；若无法操作，我会转人工核对发货状态。

## 属性

| 字段 | 值 |
|---|---|
| script_id | SCRIPT-022 |
| category | 订单 |
| intent | order |
| keywords | 取消 订单 |
| questions | 怎么取消订单, 不想要了能取消吗, 帮我关掉订单 |
| risk_level | low |
| layer | store |
| source | builtin:ecommerce-sop-v1 |


## 演化历史

- `2026-08-05T07:01:33.229469+00:00` **created** — 加载自任务6交付物：script.json（来源：builtin:ecommerce-sop-v1）
