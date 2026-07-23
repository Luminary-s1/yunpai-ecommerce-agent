# 淘宝客服接管联调手册

## 1. 当前实现边界

本项目已实现淘宝客服接管的本地执行链路：

1. 淘宝 OAuth 授权地址和一次性 `state`，有效期 10 分钟且只能消费一次。
2. OAuth token 通过 AES-256-GCM 加密后落 SQLite，密钥只从环境变量读取。
3. TOP/Qimen 的 MD5、HMAC-MD5、HMAC-SHA256 签名与验签。
4. `qimen.taobao.message.chatrobot.sync` 回调、10 分钟时间窗、防重放和事件幂等。
5. 买家标识 HMAC 化、昵称掩码、消息正文敏感数字脱敏、回复路由数据加密。
6. 渠道会话的 `bot/human/paused` 归属、乐观锁版本和审计记录。
7. `taobao.message.chatrobot.async` 异步回复、幂等发件箱和业务结果后置验证。
8. `taobao.message.chatrobot.assist.subscribe/query` 机器人订阅写入与读回验证。
9. 默认人工接管；自动回复必须显式开启，模型要求转人工时自动把会话切回 `human`。

真实收发仍依赖淘宝侧批准的机器人类目、奇门路由、数据平台分配的 `request_token/tenant_id` 和测试店铺授权。普通 AppKey 或千牛账号本身不能替代这些条件。

官方契约：

- [淘宝 OAuth 授权](https://developer.alibaba.com/docs/doc.htm?articleId=102635&docType=1&treeId=1)
- [TOP API 调用与签名](https://developer.alibaba.com/docs/doc.htm?articleId=101617&docType=1&treeId=780)
- [奇门签名说明](https://developer.alibaba.com/docs/doc.htm?articleId=118394&docType=1&treeId=813)
- [机器人消息同步 `qimen.taobao.message.chatrobot.sync`](https://developer.alibaba.com/docs/api.htm?apiId=72219)
- [机器人异步动作 `taobao.message.chatrobot.async`](https://developer.alibaba.com/docs/api.htm?apiId=48631)
- [机器人订阅 `taobao.message.chatrobot.assist.subscribe`](https://developer.alibaba.com/docs/api.htm?apiId=49455)
- [机器人订阅查询 `taobao.message.chatrobot.assist.query`](https://developer.alibaba.com/docs/api.htm?apiId=49454)
- [奇门官方场景接入流程](https://developer.alibaba.com/docs/doc.htm?articleId=106849&docType=1&treeId=285)
- [服务市场智能客服机器人管理规范](https://developer.alibaba.com/docs/doc.htm?articleId=121757&docType=1&treeId=253)

申请时直接使用项目内的[淘宝客服机器人 API 接入申请材料](taobao-api-access-application.md)。该材料列出了四个接口、三类平台分配参数、回调地址、测试资源和真实验收证据，避免把普通千牛登录误当成机器人通道授权。

## 2. 淘宝开放平台操作

### 2.1 应用与回调

1. 在淘宝开放平台创建或选定服务商应用，记录 `AppKey` 和 `AppSecret`。
2. OAuth 回调白名单填写：

   `https://你的公网域名/v1/integrations/taobao/oauth/callback`

3. 公网入口必须是有效 HTTPS 证书，反向代理应原样转发 POST 正文，服务器时间同步到 NTP。
4. 不要把 AppSecret、token、`request_token` 填入源码、前端或项目文档。

### 2.2 机器人资格与消息路由

向淘宝开放平台/服务市场对接人员明确申请以下完整能力，不要只申请普通订单 API。千牛商家登录不会自动产生这些凭证；`request_token` 和 `tenant_id` 必须由数据平台申请并分配：

| 项目 | 所需结果 | 本地变量/入口 |
|---|---|---|
| 机器人服务类目/应用权限 | 应用可调用机器人 API | `TAOBAO_APP_KEY/SECRET` |
| 奇门消息路由 | 买家消息推送到本机 HTTPS 回调 | `POST /v1/integrations/taobao/qimen` |
| 奇门客户标识 | 平台分配的 `customerId` | `TAOBAO_QIMEN_CUSTOMER_ID` |
| 数据平台令牌 | 平台分配的 `request_token` | `TAOBAO_CHATROBOT_REQUEST_TOKEN` |
| 机器人租户 | 平台分配/消息携带的 `tenant_id` | `TAOBAO_CHATROBOT_TENANT_ID` |
| 测试店铺 | 可完成 OAuth 且允许收测试消息 | OAuth 接口的 `shop_id` |

奇门路由的 API 名称为 `qimen.taobao.message.chatrobot.sync`。回写 API 为 `taobao.message.chatrobot.async`，订阅 API 为 `taobao.message.chatrobot.assist.subscribe`。申请时把三个名称一并给对接人员，要求确认应用是否具备调用权限和路由配置入口。

## 3. 本地配置与启动

项目要求 Python 3.11+。PowerShell 生成凭据加密密钥：

```powershell
py -3.12 -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

把输出写入本机 `.env` 或部署系统的 Secret，不要写回 `.env.example`。先使用以下安全配置：

```dotenv
TAOBAO_ENABLED=true
TAOBAO_AUTO_REPLY_ENABLED=false
TAOBAO_APP_KEY=平台提供的AppKey
TAOBAO_APP_SECRET=平台提供的AppSecret
TAOBAO_REDIRECT_URI=https://你的公网域名/v1/integrations/taobao/oauth/callback
TAOBAO_CREDENTIAL_KEY=上一步生成的值
TAOBAO_QIMEN_CUSTOMER_ID=平台提供的customerId
TAOBAO_QIMEN_ROUTE_VERIFIED=false
TAOBAO_CHATROBOT_REQUEST_TOKEN=数据平台提供的request_token
TAOBAO_CHATROBOT_TENANT_ID=平台提供的tenant_id
TAOBAO_TOP_GATEWAY=https://eco.taobao.com/router/rest
```

当前 CLI 不自动读取 `.env` 文件；使用容器 Secret、服务管理器环境变量，或在 PowerShell 中加载这些值后启动：

```powershell
py -3.12 -m pip install -e ".[dev]"
yunpai-agent init
yunpai-agent serve --host 127.0.0.1 --port 8080
```

## 4. 店铺授权

以下命令均需管理员头。先检查门禁：

```powershell
$admin = @{
  "X-Admin-Id" = "local-admin"
  "X-Admin-Key" = $env:ADMIN_API_KEY
}
Invoke-RestMethod http://127.0.0.1:8080/v1/integrations/taobao/capabilities -Headers $admin
```

创建授权地址：

```powershell
$auth = Invoke-RestMethod -Method Post `
  "http://127.0.0.1:8080/v1/integrations/taobao/authorize?shop_id=测试店铺的平台ID" `
  -Headers $admin
$auth.authorization_url
```

在浏览器打开返回地址，由测试店铺主账号登录授权。淘宝回跳后，本机会交换 token 并加密保存。不要把返回的 `state` 或授权码发送给其他人。

## 5. 奇门回调验证

1. 在淘宝/奇门配置页把机器人消息地址指向：

   `https://你的公网域名/v1/integrations/taobao/qimen`

2. 用测试买家向测试店铺发一条无敏感信息的消息。
3. 本地查询人工会话：

```powershell
Invoke-RestMethod `
  "http://127.0.0.1:8080/v1/integrations/taobao/conversations?owner_mode=human" `
  -Headers $admin
```

4. 只有真实消息已出现并且审计表有 `taobao.message.received` 后，设置：

   `TAOBAO_QIMEN_ROUTE_VERIFIED=true`

5. 重启服务，再查 capability；此时 `qimen_inbound.available` 才应为 `true`。

回调拒绝时看 `audit_log` 的 `taobao.qimen.rejected`，常见原因依次是服务器时间漂移、`app_key/customerId` 不匹配、反向代理改写表单正文、AppSecret 错误。

## 6. 人工接管与回复

查询会话后记录 `id` 和 `version`。切为人工归属：

```powershell
$ownership = @{ owner_mode="human"; expected_version=1; assigned_to="客服工号-01" } |
  ConvertTo-Json
Invoke-RestMethod -Method Post `
  "http://127.0.0.1:8080/v1/integrations/taobao/conversations/会话ID/ownership" `
  -Headers $admin -ContentType "application/json" -Body $ownership
```

人工回复：

```powershell
$reply = @{
  text = "您好，正在为您核实，请稍候。"
  idempotency_key = "reply:淘宝消息ID:客服工号-01"
} | ConvertTo-Json
Invoke-RestMethod -Method Post `
  "http://127.0.0.1:8080/v1/integrations/taobao/conversations/会话ID/messages" `
  -Headers $admin -ContentType "application/json" -Body $reply
```

重复提交同一个 `idempotency_key` 只返回原发件箱记录，不会再次调用淘宝。回复前系统要求会话处于 `human`；这阻止机器人和人工同时发言。

## 7. 自动回复灰度

自动回复不是首次联调步骤。必须先完成：

1. 入站消息连续验证通过。
2. 人工回复已在千牛买家会话中可见。
3. 重复消息和重复回复验证无二次发送。
4. 风险问法确实转人工。
5. 服务市场响应率、投诉、敏感词和留痕要求已确认。

之后只对测试店铺设置 `TAOBAO_AUTO_REPLY_ENABLED=true`。自动模式调用现有 Agent；Agent 返回 `requires_human=true` 时，本地会话自动切回 `human`，不会继续回写模型文本。

## 8. 验收记录

真实放行必须保存以下证据：

| Gate | 证据 |
|---|---|
| OAuth | 店铺授权记录、token 过期时间，无明文 token 截图 |
| 入站 | 淘宝消息 ID、本地 event ID、验签成功审计、重复投递仅一条事件 |
| 人工接管 | ownership 审计、版本冲突返回 409 |
| 回写 | 发件箱 ID、TOP request/response 脱敏摘要、买家端可见截图 |
| 风险 | 退款/赔付/投诉测试触发人工，不自动承诺 |
| 回滚 | `TAOBAO_AUTO_REPLY_ENABLED=false` 后只保留人工会话 |

严禁使用登录 Cookie、浏览器自动化、千牛客户端注入或逆向私有协议作为生产接管方案。这些方式无法满足稳定性、账号风控、授权范围和审计要求。
