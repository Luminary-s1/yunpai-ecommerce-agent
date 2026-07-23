# 0.22.5 GLM Coding Plan 标准接口本机验证

日期：2026-07-23

## 范围

- 使用 OpenAI 兼容的 `POST /chat/completions` 调用 GLM Coding Plan 测试端点。
- 保持原智能客服页面的顾客测试入口、RAG/SOP 引用、结构化决策、工具门禁和人工接管链路。
- 不将 API 密钥写入仓库、页面、报告或审计记录。

## 修复

- Coding Plan 测试端点改用非流式 Chat Completions 调用，避免 SSE 读取超时。
- `AgentDecision` 仅将模型输出的空容器 `arguments: null` 和 `missing_fields: null` 归一为默认值；其他错误类型继续由 Pydantic 拒绝。
- 正常 GLM 端点仍保留可配置的流式模式。

## 验证结果

- 定向回归：`23 passed`，覆盖模型网关、决策结构、ReAct 图、API 和后台页面。
- 全量回归：`226 passed in 389.87s`，无 stderr。
- 健康检查：`mode=live_model`，模型为 `glm-4.7`，`streaming=false`。
- 原后台“智能客服 -> 对话测试”实际发送“保修 + 发货”问题，得到模型回答：整机保修 12 个月；发货需以商品页、订单和仓库处理状态为准。
- 对应审计轨迹包含 `deliberate:model:answer`、`generate:model`、`verify:passed`，无人工接管和 `model_unavailable`。

## 边界

这是一项本机 Coding Plan 测试验证，不等同于正式生产模型、真实渠道、真实客户数据、容量/长稳或生产放行验收。
