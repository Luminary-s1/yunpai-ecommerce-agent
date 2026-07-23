# 云湃电商一体机 Agent 0.22.3 测试报告

日期：2026-07-23

## 范围

本版本新增本机顾客对话测试入口：`GET /customer-test`、`GET /v1/test/customer-chat/cases` 与 `POST /v1/test/customer-chat`。页面提供五个静态、非敏感测试案例，并将实际客服回答、意图、风险、知识来源、转人工状态和原始 JSON 展示给操作者。

接口默认由 `CUSTOMER_TEST_ENABLED=false` 关闭。启用后仍只接受回环客户端，并使用已配置的 bootstrap client 构造受控主体。每个会话固定标记为 `source_type=simulation`、`source_reference=local-customer-test`；因此默认运营范围不会统计这类会话，也不会向外部渠道发送消息或执行退款、改址等业务动作。

## 验证结果

| 检查 | 结果 | 说明 |
|---|---|---|
| 测试入口定向测试 | 通过 | `py -3.12 -m pytest -q tests/test_api.py`，3 passed；覆盖默认关闭、非回环拒绝、案例接口、页面、实际对话、来源隔离和会话 ID 约束 |
| 全量回归 | 通过 | `py -3.12 -m pytest -q`，223 passed，449.94s，退出码 0 |
| Python 静态检查 | 通过 | `py -3.12 -m compileall -q src tests`，退出码 0 |
| 页面脚本检查 | 通过 | `docs/customer-test.html` 内联脚本经 Node 语法校验，退出码 0 |
| 本机 HTTP | 通过 | `127.0.0.1:8104` 返回 health `ok`、ready `ready`、schema v22；案例列表为 5 项；实际 POST 返回 `local_customer_simulation` 和 `simulation` 来源 |
| 数据范围 | 通过 | 实际测试会话写入模拟范围；默认运营会话数保持 0，模拟范围可见测试会话 |
| 浏览器 | 通过 | 顾客页面加载五个案例；保修案例返回实际知识库回答与 3 条来源；桌面页面无横向溢出，控制台 error/warning 为 0 |

## 交付入口

- 顾客页面：`http://127.0.0.1:8104/customer-test`
- 案例接口：`GET http://127.0.0.1:8104/v1/test/customer-chat/cases`
- 对话接口：`POST http://127.0.0.1:8104/v1/test/customer-chat`
- 配置与调用示例：[CUSTOMER_TEST_0.22.3.md](CUSTOMER_TEST_0.22.3.md)

## 结论与边界

本地顾客对话测试能力通过代码级验收，可用于演示和人工检查智能客服的实际输入输出。它不是客户渠道接入、真实店铺验收或生产自动回复的替代品。真实平台授权、真实客户脱敏标注集、真实模型基线、值守/SLA 签收、长稳、容量、安全、异机灾备和最终生产放行仍为阻塞项。
