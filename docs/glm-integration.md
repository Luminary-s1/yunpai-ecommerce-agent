# GLM 接入说明

## 标准 Chat Completions 接口

客服运行时只通过 OpenAI 兼容的 HTTP Chat Completions 接口调用模型：

```text
POST {MODEL_BASE_URL}/chat/completions
Authorization: Bearer {MODEL_API_KEY}
Content-Type: application/json
```

默认生产配置使用标准 GLM 服务：

```text
MODEL_PROVIDER=glm
MODEL_BASE_URL=https://open.bigmodel.cn/api/paas/v4
MODEL_NAME=glm-4.7-flash
MODEL_ENABLED=true
```

模型用于两类任务：先返回受 `AgentDecision` 校验的 JSON 决策，再依据检索到的知识、SOP 和工具结果生成顾客回答。模型不能绕过工具参数校验、权限门、SOP 或人工接管规则。

## Coding Plan 本机测试

可将 Coding Plan 作为显式本机测试模型，仍使用相同的 Chat Completions 协议：

```text
MODEL_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4
MODEL_NAME=glm-4.7
MODEL_ALLOW_CODING_PLAN=true
MODEL_STREAMING=false
```

`MODEL_ALLOW_CODING_PLAN` 默认关闭，避免误把测试订阅配置带入正式环境。Coding Plan 测试端点会固定使用非流式请求，规避 SSE 长连接超时；常规 GLM 端点仍可按 `MODEL_STREAMING=true` 使用流式响应。

在当前 PowerShell 会话内设置密钥后可启动测试服务，密钥不写入仓库、页面、审计记录或测试报告：

```powershell
$env:MODEL_API_KEY = "your-api-key"
.\scripts\start-glm-coding-test.ps1
```

## 运行参数

- `MODEL_THINKING_ENABLED=false`：客服短答默认不启用深度思考。
- `MODEL_MAX_OUTPUT_TOKENS=240`：限制单次输出长度和成本。
- `RAG_TOP_K=3`：只向模型提供最相关的三条知识。
- `RAG_DIRECT_APPROVED_ANSWER=true`：高置信度已批准答案可直接复用，仍经过确定性安全检查。
- `MODEL_RETRY_ATTEMPTS=1`：仅对可重试的服务端或瞬时连接错误重试一次；账户限流不重试。
- `MODEL_TIMEOUT_SECONDS=45`：读取超时进入受控人工接管，不重复占用窗口。

`yunpai-agent model-probe` 只发送最小连通性请求并输出模型、延迟和 usage，不输出密钥或响应头。通过探测后再开放真实客服流量。
