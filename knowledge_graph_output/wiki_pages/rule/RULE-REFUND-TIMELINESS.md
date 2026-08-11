# 电商平台退款时效规则

> **实体 ID**：`RULE-REFUND-TIMELINESS` ｜ **类型**：`rule` ｜ **层级**：`general`

## 当前结论

电商平台退款时效规则：买家申请退款后，商家应在规定时间内处理：未发货订单申请退款，商家应在48小时内处理；已发货订单申请仅退款，商家应在72小时内处理；退货退款申请，商家应在收到退货后48小时内确认退款。超时未处理系统自动退款。退款金额原则上原路退回。

## 属性

| 字段 | 值 |
|---|---|
| rule_code | RULE-REFUND-TIMELINESS |
| authority | 淘宝平台 |
| theme | 退款时效 |
| source | network |
| source_url | rule.taobao.com |
| captured_at | 2026-08-05 |
| raw_file | S14-refund.md |


## 演化历史

- `2026-08-05` **created** — 加载自任务6交付物：rule.json（来源：network）
