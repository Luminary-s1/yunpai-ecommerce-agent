# 电商平台发货时效与延迟发货规则

> **实体 ID**：`RULE-SHIPPING-TIMELINESS` ｜ **类型**：`rule` ｜ **层级**：`general`

## 当前结论

电商平台发货时效与延迟发货规则：延迟发货：消费者付款后24小时内未上传运单号，或48小时内查不到揽件跟踪信息。店铺周度延迟发货率≤5%每单扣1分；＞5%每单扣1分周不超8分；≥50%且≥50单视为情节严重扣12分。赔付：订购延迟发货补贴商家自动赔付每单5元无敌券；未订购按支付金额5%赔付（500-5000云钻）。

## 属性

| 字段 | 值 |
|---|---|
| rule_code | RULE-SHIPPING-TIMELINESS |
| authority | 苏宁易购 |
| theme | 发货时效 |
| source | network |
| source_url | rule.suning.com/ruleInfo/ruleInfoDetail/GZ100004233.htm |
| captured_at | 2026-08-03 |
| raw_file | S5.md |


## 演化历史

- `2026-08-03` **created** — 加载自任务6交付物：rule.json（来源：network）
