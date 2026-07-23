# 淘宝客服机器人 API 接入申请材料

## 1. 申请目标

云湃电商一体机拟以独立电商后台形式，为已授权的淘宝/天猫商家提供智能客服、人工接管、会话留痕和运营管理能力。生产链路仅使用淘宝开放平台、奇门和服务市场批准的接口，不读取登录 Cookie，不注入千牛客户端，不逆向旺旺私有协议，也不以浏览器自动化代替 API。

本次需要申请“客服机器人”服务商/应用能力，而不是普通订单 API 或单店千牛账号权限。

## 2. 请平台确认并开通的能力

| 能力 | 官方接口/场景 | 期望结果 |
|---|---|---|
| 买家消息入站 | `qimen.taobao.message.chatrobot.sync` | 奇门把授权店铺的机器人消息实时 POST 到我方 HTTPS 地址 |
| 机器人/人工回复 | `taobao.message.chatrobot.async` | 我方使用店铺 session 和数据平台令牌回写消息 |
| 客服账号订阅 | `taobao.message.chatrobot.assist.subscribe` | 为指定店铺客服子账号启用或停用机器人辅助 |
| 订阅读回 | `taobao.message.chatrobot.assist.query` | 返回当前生效的账号和租户，供上线后置校验 |
| 店铺授权 | 淘宝 OAuth 2.0 | 测试店铺可授权应用，并取得合法 TOP session |

请平台同时提供或确认以下配置：

1. 服务市场“客服机器人”类目准入路径和当前联系人。
2. 可创建对应应用标签的服务商主体和应用类型。
3. `AppKey`、`AppSecret` 及机器人 API 权限包。
4. 奇门官方场景是否对该应用可见，以及该场景的 `customerId`。
5. 数据平台分配的 `request_token` 和 `tenant_id`。这两项无法由千牛登录或普通 OAuth 生成。
6. 沙箱或测试店铺、测试客服子账号、测试买家账号及测试时间窗。
7. 生产发布、店铺授权、服务订购和月度考核的具体要求。

## 3. 我方接口信息表

提交前由项目负责人填写方括号内容：

| 字段 | 内容 |
|---|---|
| 公司主体 | `[营业执照主体]` |
| 开放平台开发者账号 | `[服务商淘宝账号]` |
| 应用名称 | `云湃电商一体机` |
| 应用模式 | `一站式电商后台 + 客服机器人服务` |
| 测试域名 | `[https://test.example.com]` |
| 生产域名 | `[https://api.example.com]` |
| OAuth 回调 | `[生产域名]/v1/integrations/taobao/oauth/callback` |
| 奇门消息地址 | `[生产域名]/v1/integrations/taobao/qimen` |
| 技术负责人 | `[姓名、手机号、邮箱]` |
| 业务负责人 | `[姓名、手机号、邮箱]` |
| 首个测试店铺 | `[店铺名称、seller_id、店铺主账号]` |
| 预计服务商家数 | `[试点/半年/一年数量]` |
| 峰值消息量 | `[QPS、日会话量、日消息量]` |

## 4. 可直接发送给平台小二的申请文本

> 我司正在开发“云湃电商一体机”，产品形态为独立的一站式电商后台，并为商家提供客服机器人和人工接管能力。我们只采用官方 OAuth、TOP 和奇门接口，不使用 Cookie、浏览器自动化、客户端注入或私有协议。现申请服务市场“客服机器人”类目及对应数据通道，请协助确认并开通 `qimen.taobao.message.chatrobot.sync`、`taobao.message.chatrobot.async`、`taobao.message.chatrobot.assist.subscribe`、`taobao.message.chatrobot.assist.query`，并分配或指导申请 `customerId`、`request_token`、`tenant_id`。我方已完成本地验签、防重放、消息幂等、人工/机器人归属、回复幂等、敏感信息脱敏和审计实现，希望获得测试店铺/测试租户完成真实联调。请告知当前准入材料、审核周期、应用类型、服务订购方式和技术对接人。

## 5. 平台审批后的执行顺序

1. 用服务商企业主体入驻开放平台和服务市场，申请客服机器人类目。
2. 创建获批类型的应用，配置 HTTPS OAuth 回调白名单。
3. 在 `qimen.taobao.com` 选择官方场景，实现并自测 `qimen.taobao.message.chatrobot.sync`，发布 API，再配置授权。
4. 将平台凭证写入部署 Secret；保持 `TAOBAO_AUTO_REPLY_ENABLED=false`。
5. 由测试店铺主账号完成 OAuth，调用 capability 接口确认授权连接数大于零。
6. 调用订阅接口，再调用查询接口读回；只有 `verified=true` 才进入消息联调。
7. 测试买家发送无敏感信息的消息，验证签名、时间窗、幂等和本地会话落库。
8. 先由人工接管并发送一条回复，确认买家端可见，再验证重复请求不二次发送。
9. 完成转人工、投诉、退款、敏感承诺和关闭自动回复的回滚测试。
10. 所有 Gate 通过后，仅对测试店铺小流量开启自动回复。

## 6. 不可替代的真实验收证据

| Gate | 必须留存的证据 |
|---|---|
| 资格 | 服务市场类目审批结果、应用权限页、对接工单编号 |
| 凭证 | `customerId/request_token/tenant_id` 已下发的脱敏记录 |
| OAuth | 店铺授权记录和 token 到期时间，不保存明文 token 截图 |
| 订阅 | subscribe 返回成功且 query 读回指定客服账号 |
| 入站 | 淘宝消息 ID、本地 event ID、验签审计、重复投递仅一条事件 |
| 回写 | 本地 outbox ID、脱敏响应摘要、买家端真实可见 |
| 接管 | 人工归属审计、版本冲突被拒、机器人停止发言 |
| 回滚 | 关闭自动回复后不再产生机器人出站消息 |

## 7. 官方依据

- [商家经营工具：千牛商家应用与一站式电商后台](https://developer.alibaba.com/docs/doc.htm?articleId=121810&docType=1&treeId=478)
- [客服机器人类目规则](https://developer.alibaba.com/docs/doc.htm?articleId=121757&docType=1&treeId=253)
- [奇门官方场景接入流程](https://developer.alibaba.com/docs/doc.htm?articleId=106849&docType=1&treeId=285)
- [消息入站接口](https://developer.alibaba.com/docs/api.htm?apiId=72219)
- [异步回复接口](https://developer.alibaba.com/docs/api.htm?apiId=48631)
- [订阅接口](https://developer.alibaba.com/docs/api.htm?apiId=49455)
- [订阅查询接口](https://developer.alibaba.com/docs/api.htm?apiId=49454)

说明：接口文档中的 `request_token` 和 `tenant_id` 明确标注由数据平台申请或分配；如奇门后台看不到该官方场景，应按奇门流程联系运营小二开通，不能用千牛页面抓包绕过。
