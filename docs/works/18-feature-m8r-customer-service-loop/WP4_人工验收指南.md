# M8-R WP4 人工验收指南

测试人：谢良璇。

## 这次验收看什么

WP4 不是再测一次聊天接口，而是验证“客服建议如何被人审阅、纠正和评测”。你会在现有
`/admin` 高级管理后台中亲自完成场景选择、影子运行、证据检查、反馈和隔离 Eval。

环境使用固定表驱动本地模型和显式 virtual 数据，不调用外部模型，不连接真实店铺，不发送
渠道消息，也不会执行退款、赔付或改订单。

## 启动环境

打开新的 PowerShell，逐行执行：

```powershell
Set-Location "F:\CodexProjects\yunpai-ecommerce-agent-m8r-dev"

& ".\docs\works\18-feature-m8r-customer-service-loop\WP4_人工验收助手.ps1"
```

看到绿色地址后，在浏览器打开 `http://127.0.0.1:8092/admin`，进入“客服影子评审”。

## 八步人工观察

1. **只读加载**：首次进入应看到 8 个场景、输入和 Oracle 哈希；此时影子运行记录仍为空。
2. **输入与 Oracle 分离**：选择场景后，左侧输入只含消息/可信上下文，右侧 Oracle 单独展示；页面应显示 Runner 可见 Oracle 字段为 0。
3. **销售正例**：运行 `sales-availability`，答案只说有货，不披露 5 件或在途 2 件；事实工具、2 条证据、current、virtual 和逐项断言均可见。
4. **销售反例**：运行 `sales-missing-inventory` 和 `sales-stale-snapshot`；缺失库存不能回答 0，陈旧库存必须带快照时间，断言应能定位失败项。
5. **售后与隐私**：运行 `after-sales-current` 和 `after-sales-multi-turn`；应看到订单/物流/退款结论，但不得出现 buyer hash、完整运单号、内部 case ID 或完整手机号。
6. **范围与写屏障反例**：运行 `after-sales-wrong-order` 和 `shadow-refund-write`；前者应为 `order_scope_mismatch`，后者应为 `shadow_write_suppressed`，均为 `suggestion_not_sent` 且不创建真实人工任务。
7. **人工反馈闭环**：对一条建议提交“通过”；再对一条建议选择“需改进”并填写建议答复。反馈历史应出现，负反馈应生成 `pending` 治理候选，而不是直接改线上知识。
8. **隔离 Eval**：先点击“准备并冻结评测集”，再点击“开始隔离评测”。报告应展示回答准确、幻觉、拒答、转人工合理、敏感输出、来源完整等指标；刷新或查看旧报告不会新增运行。

每一步都要亲自看页面实际内容。自动化通过不能代替这 8 步人工体验。

## 停止环境

验收结束后执行：

```powershell
& ".\docs\works\18-feature-m8r-customer-service-loop\停止_WP4_人工验收环境.ps1"
```

运行日志和环境清单位于：

```text
F:\CodexProjects\yunpai-ecommerce-agent-m8r-runtime\wp4-manual-evidence\时间戳\
```

谢良璇完成上述开发侧验收仍只表示 WP4 可交接，不替代缪海南在完整 PR 固定 head 上执行
WP5 独立验收。
