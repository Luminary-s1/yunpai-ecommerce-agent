# 页面可下单通常表示当前系统有可售库存，但大促或并发下单时库存可能变化。最终以订单提交和仓库复核结果为准。

> **实体 ID**：`SCRIPT-011` ｜ **类型**：`script` ｜ **层级**：`seller`

## 当前结论

页面可下单通常表示当前系统有可售库存，但大促或并发下单时库存可能变化。最终以订单提交和仓库复核结果为准。

## 属性

| 字段 | 值 |
|---|---|
| script_id | SCRIPT-011 |
| category | 库存 |
| intent | inventory |
| keywords | 库存 现货 有货 |
| questions | 现在有货吗, 这是现货吗, 库存还有多少 |
| risk_level | low |
| layer | store |
| source | builtin:ecommerce-sop-v1 |


## 演化历史

- `2026-08-05T07:01:33.229469+00:00` **created** — 加载自任务6交付物：script.json（来源：builtin:ecommerce-sop-v1）
