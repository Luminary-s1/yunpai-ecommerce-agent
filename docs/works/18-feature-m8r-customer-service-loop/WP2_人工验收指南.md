# M8-R WP2 人工验收指南

测试人：谢良璇。

## 这次验收看什么

你不用阅读代码。助手会在 F 盘创建一套全新的虚拟商品、库存和订单数据，然后真实调用 WP2
的两个 Agent 只读工具。你只需要逐步核对屏幕上的业务结果。

这次不调用外部模型、不连接真实店铺、不修改真实订单，也不需要启动网页或服务端。

## 开始操作

打开一个新的 PowerShell，逐行执行：

```powershell
Set-Location "F:\CodexProjects\yunpai-ecommerce-agent-m8r-dev"

& ".\docs\works\18-feature-m8r-customer-service-loop\WP2_人工验收助手.ps1"
```

不要加 `-AutoConfirm`。该开关只供开发者检查脚本能否跑通，不能形成你的人工验收结论。

## 每一步观察位置

| 步骤 | 屏幕上应看到的关键结果 |
|---|---|
| 1. 销售事实 | 商品为“恒温水壶”，售价 129.00，可用库存 5.00，在途 2.00 |
| 2. 缺失语义 | 第二个商品有商品事实但库存为 missing/null，不能显示库存 0 |
| 3. 新鲜度 | 五天前快照显示 stale、usable_as_current=false，并显示 data_as_of |
| 4. 来源 Gate | 虚拟来源明确为 virtual；未知来源返回 blocked 且 facts 为空 |
| 5. 范围 Gate | 错订单号和错店铺号分别被 order_scope_mismatch/store_scope_mismatch 阻断 |
| 6. 隐私投影 | 能看到订单、物流、退款事实；看不到买家 hash、运单号、内部行号和完整手机号，只允许脱敏形式 |
| 7. 历史更正 | 版本 1 仍可读但为 superseded；版本 2 为 current，订单状态为 delivered |
| 8. 租户隔离 | tenant-b 查询不到 tenant-a 的商品，结果为 missing，不泄漏商品详情 |

每一步后输入 `Y` 表示实际观察与预期一致；输入 `N` 表示不一致。输入 `N` 后仍可继续，
但最终状态必须保持失败，不能把 WP2 标记为人工验收通过。

## 证据位置

助手会在以下目录生成中文结果文件：

```text
F:\CodexProjects\yunpai-ecommerce-agent-m8r-runtime\wp2-manual-evidence\
```

文件包括：

- `谢良璇_WP2人工验收过程_时间.txt`
- `谢良璇_WP2人工验收结果_时间.json`

结果中 `automatic_contract_checks=passed`、`human_observations_passed=true` 和
`final_status=human_accepted` 同时成立，才表示谢良璇完成 WP2 开发侧人工验收。

这只关闭 WP2 开发侧人工验收，不替代 WP3/WP4 的独立开发证据，也不替代未参与开发的
缪海南在完整 PR 固定 head 上执行 WP5 独立验收。
