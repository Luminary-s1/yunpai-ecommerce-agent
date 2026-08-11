# 预售商品的定金、尾款和预计发货时间以商品页预售规则为准。预计时间不是必然送达时间，订单异常时会转人工处理。

> **实体 ID**：`SCRIPT-018` ｜ **类型**：`script` ｜ **层级**：`seller`

## 当前结论

预售商品的定金、尾款和预计发货时间以商品页预售规则为准。预计时间不是必然送达时间，订单异常时会转人工处理。

## 属性

| 字段 | 值 |
|---|---|
| script_id | SCRIPT-018 |
| category | 预售 |
| intent | shipping |
| keywords | 预售 尾款 定金 发货 |
| questions | 预售什么时候发, 尾款什么时候付, 定金能退吗 |
| risk_level | low |
| layer | store |
| source | builtin:ecommerce-sop-v1 |


## 演化历史

- `2026-08-05T07:01:33.229469+00:00` **created** — 加载自任务6交付物：script.json（来源：builtin:ecommerce-sop-v1）
