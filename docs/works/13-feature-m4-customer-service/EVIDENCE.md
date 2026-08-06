# M4 ① 流式输出端到端实跑证据

> 对应：M4_HANDOFF 第 2 节第 1 项「流式输出端到端实跑证据」
> 日期：2026-08-05

## 证据文件

| 文件 | 内容 | 性质 |
|---|---|---|
| `m4-stream-evidence.txt` | 74 事件完整流式输出 | **成功实跑证据**（真实模型） |
| `m4-browser-evidence.png` | 浏览器实测画面 | 补充佐证（降级转人工场景） |

## ① 成功实跑证据（m4-stream-evidence.txt）

- **接口**：`POST /v1/chat/stream`（SSE）
- **模型**：GLM `glm-4.7-flash`（真实模型，`MODEL_ENABLED=true`）
- **请求**：`晴川 AF5 空气炸锅保修多久？`（context 含 shop_id + sku_id）
- **结果**：`meta(1) → delta(71) → citations(3) → done(1)`，共 74 事件
- **关键字段**：
  - `intent: 查询商品保修信息`（意图识别正常）
  - `risk_level: low`
  - `model_fallback: false`（**真实模型调用，非降级**）
  - `citations` 3 个知识来源（RAG 检索生效）
- **服务日志佐证**：`POST /v1/chat/stream 200 OK`

## ② 浏览器佐证（m4-browser-evidence.png）

- **页面**：`/customer-test`（F-123 本机顾客直测入口）
- **内容**：输入保修问题后，系统安全降级转人工（`model_unavailable`）
- **意义**：同时佐证了模型不可用时的**安全转人工降级**（对应 D18 低置信度转人工的降级路径）

## 复现方式

```bash
# 起服务（真实模型）
source env.md && .venv/Scripts/python.exe -m ecommerce_agent.cli serve --host 127.0.0.1 --port 8080

# 调流式接口
curl -N -X POST http://127.0.0.1:8080/v1/chat/stream \
  -H "Content-Type: application/json" \
  -H "X-Client-Id: local-adapter" \
  -H "X-Client-Key: <BOOTSTRAP_CLIENT_KEY>" \
  -H "X-Subject-Id: m4-stream-evidence" \
  -d '{"session_id":"repro-001","message":"晴川 AF5 空气炸锅保修多久？","context":{"shop_id":"qingchuan-flagship-001","sku_id":"QC-AF5-WHITE"}}'
```

> 注：本次实跑涉及 GLM `glm-4.7-flash` 偶发限流（429）与 `glm-4-flash`/`deepseek-v4-flash` 决策格式兼容问题，均已通过降级路径安全处理（转人工），未产生错误业务结论。M4 ② D17 的确定性场景断言不依赖模型措辞，已验证通过。
