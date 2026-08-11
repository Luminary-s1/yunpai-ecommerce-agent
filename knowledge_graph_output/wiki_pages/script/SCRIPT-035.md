# 能否换货取决于平台入口、商品状态和目标规格库存。实际换货会改变订单履约，请从售后页申请或转人工确认。

> **实体 ID**：`SCRIPT-035` ｜ **类型**：`script` ｜ **层级**：`seller`

## 当前结论

能否换货取决于平台入口、商品状态和目标规格库存。实际换货会改变订单履约，请从售后页申请或转人工确认。

## 属性

| 字段 | 值 |
|---|---|
| script_id | SCRIPT-035 |
| category | 售后 |
| intent | return_exchange |
| keywords | 换货 尺码 颜色 |
| questions | 可以换个尺码吗, 颜色买错了怎么换, 能直接换货吗 |
| risk_level | low |
| layer | store |
| source | builtin:ecommerce-sop-v1 |


## 演化历史

- `2026-08-05T07:01:33.229469+00:00` **created** — 加载自任务6交付物：script.json（来源：builtin:ecommerce-sop-v1）
