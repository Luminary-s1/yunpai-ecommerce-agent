# 实际支付金额由商品价、店铺优惠、平台优惠和账号可用券共同计算，请以结算页实时展示为准。客服不能绕过平台修改成交价。

> **实体 ID**：`SCRIPT-013` ｜ **类型**：`script` ｜ **层级**：`seller`

## 当前结论

实际支付金额由商品价、店铺优惠、平台优惠和账号可用券共同计算，请以结算页实时展示为准。客服不能绕过平台修改成交价。

## 属性

| 字段 | 值 |
|---|---|
| script_id | SCRIPT-013 |
| category | 价格 |
| intent | price_promo |
| keywords | 价格 到手价 标价 |
| questions | 实际多少钱, 到手价是多少, 为什么结算价不一样 |
| risk_level | low |
| layer | store |
| source | builtin:ecommerce-sop-v1 |


## 演化历史

- `2026-08-05T07:01:33.229469+00:00` **created** — 加载自任务6交付物：script.json（来源：builtin:ecommerce-sop-v1）
