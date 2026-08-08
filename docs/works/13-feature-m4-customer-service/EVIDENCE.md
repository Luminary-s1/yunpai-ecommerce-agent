# M4 ① 流式输出端到端实跑证据

> 对应：M4_HANDOFF 第 2 节第 1 项「流式输出端到端实跑证据」
> 首次实跑：2026-08-05；D22 浏览器证据：2026-08-07；D23 状态见下文

## 证据文件

| 文件 | 内容 | 性质 |
|---|---|---|
| `m4-stream-evidence.txt` | 74 事件完整流式输出 | **成功实跑证据**（真实模型） |
| `m4-browser-evidence.png` | 浏览器实测画面 | **D22 历史证据**；D23 已删除图中的旧路由语义，不作当前签署依据 |

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

## ② D22 历史浏览器佐证（m4-browser-evidence.png）

- **页面**：`/customer-test`（F-123 本机顾客直测入口）
- **环境**：2026-08-07，隔离临时数据目录，`MODEL_MOCK_MODE=true`，不出网
- **输入**：在晴川 AF5 非敏感店铺 / SKU 上下文中提交一条显式投诉回归消息
- **实际画面**：回答先给出共情说明，显示 `complaint_attention_required`、
  `已转人工`、`风险 medium`，并列出投诉 / 赔付 / 安全 3 条知识来源
- **原始响应复核**：`intent=complaint`、`requires_human=true`、
  `context_readiness=ready`、`model_fallback=false`，同时返回 `handoff_id` 与 3 条
  `sources`；浏览器 console `0 error / 0 warning`
- **意义**：证明 FIX-9 不再以无来源通用转人工话术替代答复；页面展示的是
  “检索证据 + 共情回复 + 人工标记”并存。`complaints / urgent` 队列由同链路集成测试
  `test_chat_complaint_answers_with_evidence_and_marks_urgent_handoff` 复核。

### D23 当前状态

D23 已删除分类标签强制写入的 `complaint_attention_required`，投诉是否 handoff 以及
`complaints / urgent` 优先级改由规划模型确认。所以上述 PNG 只能证明 D22，不再证明
当前路由语义。2026-08-07 尝试在隔离 mock 数据目录重新启动 `/customer-test`；服务正常
启动，但 Codex 内置浏览器与 Chrome 两个受支持表面均被客户端 localhost 策略拦截，未能
形成可核验新截图。当前浏览器证据标记为**未更新**，不计入 D23 签署；代码行为由聚焦
HTTP/Graph/queue 集成测试覆盖，仍不能替代 UI 实跑。

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

## D24 · FIX-11/12 复核证据（2026-08-08）

### 代码与回归

- `intent.py` 恢复配置模型下的规则零模型短路；规则未命中或窄口径业务责任冲突才调用分类模型。
- `graph.py` / `prompts.py` 对唯一目录候选且知识 ready 的商品问题设置
  `bounded_product_answer`，规划最多一步且不暴露工具；模型仍生成并经过 verify。
- 定向聚焦：`182 passed, 1 xfailed`；全量：`610 passed, 1 xfailed`；`compileall` 与
  `git diff --check` 通过。未改冻结 fixture、门禁阈值、schema v27、20/35 拓扑或
  `ChatResponse`。
- 计数反证：`我要退款`、`我要投诉`、`多少钱` 各为 `rule / 0.95` 且模型调用增量 0；
  `售后审核为什么还没有通过` 仍进入模型仲裁；责任追问 `退货运费明明该你们承担`、
  `保修责任明明该商家负责` 进入仲裁；`我要退货怎么弄` 保留规则快路径。

### WP4 评测复跑

`evals/customer_service/runs/20260808-m4-customer-eval-post-fix12-{mock,live}.json` 使用原冻结
fixture、同一 `env.md` 和临时隔离目录，主库新增均为 0。结果：

| answer_accuracy | hallucination_rate | pass_rate | severe_failures | gate |
|---:|---:|---:|---:|---|
| 0.940 | 0.020 | 0.940 | 3（≤5） | passed |

live `deepseek-v4-flash` 为 `answer_accuracy=0.820`、`hallucination_rate=0.060`、
`pass_rate=0.820`、`severe_failures=3`，gate passed；complaint `7/8`、product `14/15`、
after-sales `6/12`，`handoff_recall=1.000`、`evidence_coverage=1.000`。live 与 D23 的
`0.900` 不同，按 provider/run-to-run 波动如实保留，不能解释为能力提升。旧的 08-05/08-07
报告不冒充本轮结果。

### 意图泄漏回归

以下两个文件都明确标记 `leaked regression only; not generalization evidence`：

- `evals/intent/runs/20260808-m4-acceptance-post-fix11-live.json`：40 条中回答 32 条，
  `31/40=77.5%`，覆盖率 80%，作答子集 `31/32=96.875%`；投诉回答 3/9。
- `evals/intent/runs/20260808-m4-complaint-balanced-post-fix11-live.json`：20 正 + 20 负，
  precision 100%、recall 75%、负例误报 0/20、覆盖率 80%，既有 gate passed。

不能从这两次回归推出泛化结论；FIX-15 的密封留出集仍待验收人提供。

### 延迟阶段分解

`evals/performance/runs/20260808-m4-latency-post-fix12.json` 使用 `deepseek-v4-flash`、
`model_max_output_tokens=1600`、临时隔离目录和四条已泄漏场景；summary 为
`p50=16297.7ms / p95=33594.4ms`。K3 总耗时 20390.2ms，trace 含
`deliberate:bounded_product:answer`、工具调用 0，未出现 `react_step_limit_reached`；这不是
全量延迟分布，provider 尾延迟仍挂 P1。

### 证据边界

`m4-browser-evidence.png` 仍是 D22 历史截图，D24 未声称有新的浏览器实跑；FIX-14 gate
位置与 FIX-15 密封集仍属待裁定/待提供项。因此本证据支持“修复行为与 WP4 mock 回归通过”，
不支持 M4 整体签署。
