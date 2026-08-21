# M8-R WP3 销售售后建议回复闭环交接说明

## 基本信息

- 模块：M8-R 销售与售后客服闭环
- 工作包：WP3 销售/售后语义策略与建议回复闭环
- 开发负责人：谢良璇
- 当前状态：开发侧代码、自动化和谢良璇 8 步人工黑盒验收完成；不替代 WP4 或缪海南 WP5 独立验收
- 基线：`454b35c9000ab279ffdbf115f80afdf3e031ee73`
- Schema：沿用现有 schema，无新增表、列或占号

## 本工作包解决的问题

WP3 把 WP1 的批准话术和 advisory-only 关键词、WP2 的可信销售/售后事实接入现有客服
Agent。模型继续决定 answer、clarify、observe、act、handoff、refuse 和 finish；确定性代码只处理
身份范围、工具 schema、事实来源、新鲜度、客户可见字段、影子写屏障和输出后置验证。

## 客户回复边界

- 默认库存回答只说明有货、缺货或暂时无法确认。
- 顾客明确询问数量，且事实仍为 current 时，才允许说明精确可售量。
- 在途数量只供内部判断，不对顾客披露；仓库明细永不进入客服回复。
- 过期事实必须显示 `data_as_of` 并使用“当时显示”等快照措辞，不能说成“目前有货”。
- 库存缺失保持 unknown/missing，不能补成 0。
- 未知来源或被阻断的事实不能生成确定业务结论。
- 退款、赔付、改订单等动作只有真实写工具通过权限、SOP、幂等和后置验证后才能声称完成。

## D-034 语义权边界

- 关键词、分类标签和风险信号只进入上下文，不直接决定语义路线。
- “我不是要退款”“如果买了不合适能否申请退款”“先查库存再说明退款规则”等请求继续由
  模型判断；旧的关键词命中不再把普通 answer/clarify 强制改为 handoff。
- 确定性 Gate 仍可因可信执行事实把路线推向更安全方向，例如范围冲突、工具结果未验证、
  来源 blocked、陈旧事实违规、影子模式写动作或输出泄露内部库存。

## 流式与非流式

`prepare_generation` 是两类接口唯一的生成准备入口，统一无证据降级、批准知识精确复用、
Prompt 变体、上下文预算和已验证工具结果。流式回复会先完成输出验证：通过后才发送原始
delta；失败时不发送危险草稿，只发送最终安全 handoff/retry 文案。正常回复中途断开仍不会
持久化半条助手消息。

## 建议证据契约

`ChatResponse.suggestion` 使用 `customer-service-suggestion-v1`，并随 `chat.completed` 审计事件
完整持久化，包含：

- 模型语义决定、意图、风险和原因；
- 批准话术 ID、关键词 signal ID 与 `advisory_only` 权威级别；
- 事实工具、证据 ID、`data_as_of`、freshness、source provenance 和回复策略；
- 模型 provider/name/enabled/mock/fallback 元数据；
- 上下文快照和证据 ID；
- 降级原因与人工任务是否实际持久化；
- 影子模式固定为 `delivery_status=suggestion_not_sent`。

## 影子写屏障

- 影子模式允许只读观察，但模型选择任何 `act` 时都会改为 `shadow_write_suppressed`。
- 不执行退款、赔付、改订单等写工具。
- 不创建真实人工任务，不写渠道 outbox，不向平台发送消息。
- 只保存本机建议、快照和审计证据，供 WP4 工作台展示与反馈。

## 开发侧验证

2026-08-20 当前代码的有效结果：

- WP3 快速契约 `11 passed`，整链/多轮/流式/影子 `6 passed`，合计 17 项。
- WP1/WP2 回归 `24 passed`。
- 安全策略 `10 passed`，上下文快照 `8 passed`，ReAct 工具循环 `4 passed`。
- 流式 service `4 passed`，HTTP/SSE `11 passed`。
- 意图路由集成拆组 `11 + 7 = 18 passed`，API `4 passed`。
- `compileall`、`git diff --check`、project-to-act validate 均退出 0。
- 仓库未配置 Ruff/mypy，本次不声称运行这些工具。
- 两次过大的组合回归和一次 WP3 组合运行达到工具时限，没有最终统计，均作废；上述数字只取
  得到明确退出码 0 的拆组结果。
- 8 步 `-AutoConfirm` 开发者演练通过，结果为 `developer_rehearsal_passed`；该结果只证明验收
  工具与本地链路可运行，不冒充谢良璇的正式人工确认。

## 人工验收

使用同目录：

- `WP3_人工验收指南.md`
- `WP3_人工验收助手.ps1`
- `WP3_人工验收场景.py`

2026-08-20，谢良璇使用无 `-AutoConfirm` 的助手逐项完成 8/8 人工确认。最终记录为
`confirmation_mode=human`、`automatic_contract_checks=passed`、
`human_observations_passed=true`、`final_status=human_accepted`，且明确记录未调用外部模型、
未执行平台写动作。结果和过程文件位于 F 盘 `wp3-manual-evidence`，台账证据为
E-20260820-007。

本结论只关闭 WP3 开发侧 Gate。WP4 和完整 M8-R PR #25 已形成开发侧候选；缪海南 WP5、
项目负责人审阅/合入、真实渠道/数据、真实模型质量、长稳和生产放行仍未完成。
