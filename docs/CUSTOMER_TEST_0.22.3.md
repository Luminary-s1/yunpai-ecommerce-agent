# 顾客对话测试接口 0.22.3

本地开发环境可设置 `CUSTOMER_TEST_ENABLED=true` 后，打开 `/customer-test` 以顾客身份和智能客服对话。页面加载五个测试案例，并展示实际接口返回的回答、意图、风险、知识来源、转人工结果与原始 JSON。

接口仅接受回环客户端；默认关闭；它复用正式 `AgentService.chat`，但所有会话都带 `source_type=simulation` 和 `source_reference=local-customer-test`，因此不会出现在默认运营看板，也不会写入外部渠道。

```powershell
$env:CUSTOMER_TEST_ENABLED = "true"
yunpai-agent serve --host 127.0.0.1 --port 8080
```

```powershell
$body = @{
  session_id = "customer-test:manual-001"
  message = "晴川 AF5 空气炸锅保修多久？"
  context = @{ shop_id = "qingchuan-flagship-001"; sku_id = "QC-AF5-WHITE" }
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/v1/test/customer-chat `
  -ContentType "application/json" -Body $body
```

案例列表可通过 `GET /v1/test/customer-chat/cases` 获取。非回环请求会被拒绝；生产环境应保持 `CUSTOMER_TEST_ENABLED=false`。
