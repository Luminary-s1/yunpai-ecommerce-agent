# 云湃电商 Agent 0.20.0 技术实现说明

日期：2026-07-22  
候选版本：`0.20.0`  
数据库：schema v20

## 1. 实现结论

当前系统不是把竞品、客服和后台页面堆在一个 Agent 中，而是采用单机优先的模块化单体：FastAPI 负责接口和身份边界，LangGraph 负责有界 Agent 规划，各业务服务负责确定性规则，SQLite 负责事务、版本和审计，后台只通过管理 API 操作业务。

0.20.0 在 0.19.0 人工队列和 SLA 基础上补齐坐席调度：管理员凭据、坐席档案、在线租约、队列权限、技能等级、全局/队列容量彼此分离；领取、转派和智能分配统一执行资格检查。它仍是本地代码级候选，不代表真实平台资质、真实客户排班或生产容量已经验收。

## 2. 总体技术路径

```text
合法渠道 / 可信业务适配器 / 管理后台
  -> FastAPI 鉴权、限流、正文限制、租户绑定
  -> 会话解析、HMAC 主体标识、输入脱敏
  -> ContextBuilder 固化可信事实和证据快照
  -> LangGraph：precheck -> RAG -> deliberate -> decision gate
       -> answer / clarify / observe / act / handoff / refuse / finish
  -> ToolRegistry + SOP Runtime：参数、权限、幂等、重试、后置验证
  -> 输出策略与高风险最终保护
  -> 消息、任务、事件、指标和审计持久化
  -> 渠道 Agent worker / outbox worker / SLA worker / 竞品 worker
```

核心原则是“模型负责理解，代码负责放行”。模型可以选择意图、工具和下一步，但不能决定租户权限、订单授权、退款是否成功、任务能否被领取、竞品证据是否可信或消息是否允许发送。

## 3. 运行与数据架构

### 3.1 API 和身份

- `api.py` 组装公开聊天、人工任务、平台接入和管理路由。
- `auth.py` 使用 PBKDF2 派生哈希保存客户端和管理员密钥；普通客户端、管理员和买家主体分别建模。
- 外部 `session_id` 与租户、客户端和主体 HMAC 绑定，跨租户或换主体复用同一会话会被拒绝。
- 管理请求的 actor 来自已认证管理员，不接受正文伪造操作人。
- 正文大小、速率、readiness、磁盘和 worker 存活由固定代码检查。

### 3.2 持久化

- 业务数据使用一个 SQLite 数据库，LangGraph checkpoint 使用独立 SQLite 文件。
- 所有共享写路径使用单进程写锁和数据库事务；并发业务对象再使用 `version` 或 `record_version` 乐观锁。
- schema v20 使用顺序迁移并在启动后校验物理表、列、索引和外键；迁移标记不能掩盖结构缺失。
- 单实例数据目录锁阻止两个进程同时写同一运行目录。
- 业务审计只保存结构化决策、状态和脱敏说明，不保存模型隐式推理。

### 3.3 后台

- `/admin` 是一个无构建步骤的响应式管理页面，所有数据来自同源管理 API。
- 管理员密钥仅保存在浏览器 `sessionStorage`；对话测试使用独立可信客户端凭据。
- 页面不直接访问 SQLite，也不在 JavaScript 中复制业务状态机。

## 4. 智能客服模块

### 4.1 会话和可信上下文

实现入口：`service.py`、`context_builder.py`、`policy.py`、`database.py`。

1. `AgentService.chat` 解析租户内会话并脱敏输入。
2. 只有具备 `can_supply_order_context` 的客户端才能提供订单和物流事实；正文中的 `authorized` 会被删除。
3. `ContextBuilder` 将身份、商品、订单、物流、售后和冲突结果写成不可变快照及 evidence 条目。
4. 快照校验和、父快照和 freshness 随回答持久化，后台可回放。

失败策略：身份或证据冲突时澄清/转人工；不会让模型用自然语言补齐权限。

### 4.2 Agent Runtime

实现入口：`graph.py`、`decision.py`、`llm.py`、`rag.py`、`prompts.py`。

- LangGraph 运行 `intake -> precheck -> retrieve -> deliberate -> decision_gate`。
- `AgentDecision` 只允许 `answer/clarify/observe/act/handoff/refuse/finish`。
- `observe/act` 进入有界 ReAct 循环，工具结果作为 Observation 返回模型，默认最多 4 步。
- 模型输出先过 Pydantic 结构检查，再过步骤预算、工具权限、SOP 和风险门。
- 最终回答再次执行敏感信息、来源、证据和高风险动作检查；高风险旁路会被改为人工任务。
- `MODEL_ENABLED=false` 是安全框架模式，需要规划的请求不调用网络模型并转人工。

### 4.3 工具和 SOP

实现入口：`tools.py`、`sops.py`、`business/*`。

- 工具注册项声明输入模型、读写类型、超时、重试、幂等字段和后置验证器。
- 只读异常可重试；写超时进入 `uncertain`，不能盲目重放。
- SOP 版本链为 `draft -> evaluated -> approved -> active -> retired`。
- 运行中会话固定 SOP 版本；步骤逐项持久化，读取可安全恢复，写入/补偿中断必须人工确认。

## 5. 人工接管与坐席调度

实现入口：`handoff.py`、`handoff_staffing.py`、`schemas.py`、schema v19/v20。

### 5.1 队列和任务

- 默认队列：投诉高风险、售后、技术异常、通用接管。
- 路由按显式队列、reason、intent、risk 和 catch-all 顺序确定；关键风险强制紧急优先级。
- 状态机：`proposed -> accepted -> working -> input_required/review -> completed`，另有拒绝、失败和取消终态。
- 任务写入首响/解决 SLA、负责人、重试预算、升级级别和乐观锁版本。
- 每次创建、领取、状态变化、转派、升级和备注都写不可变 `handoff_task_events`。
- SLA worker 将首响违约升级为 L1、解决违约升级为 L2；重复扫描和并发冲突不会覆盖人工操作。

### 5.2 坐席数据模型

`api_clients` 仍是登录凭据来源；schema v20 新增：

- `handoff_operator_profiles`：显示名、启用状态、配置在线状态、在线租约、全局容量、技能、版本。
- `handoff_operator_queue_memberships`：队列成员关系、1-5 熟练度、主队列标记。
- 触发器阻止跨租户档案和队列组合。

管理员可以存在但尚未配置为客服坐席。凭据停用、档案停用、租约过期、暂离、离线、非队列成员或达到任一容量时都不能领取任务。

### 5.3 在线租约

- `available` 和 `away` 必须带 60 秒至 8 小时 TTL。
- `effective_presence` 动态计算；租约过期立即表现为 `offline`，无需后台清理任务。
- 服务重启不会复活已过期租约。
- 坐席在线更新要求本人管理员凭据和档案乐观锁版本。

### 5.4 领取、转派和自动分配

领取与转派统一调用 `require_eligible`：

```text
凭据 active
  and 档案 active
  and effective_presence == available
  and 属于目标队列
  and 全局活动任务 < 档案上限
  and 队列活动任务 < 队列上限
```

智能分配只处理尚未认领的 `proposed` 任务。候选排序固定为：

1. 全局负载率低者优先；
2. 主队列成员优先；
3. 技能等级高者优先；
4. 活动任务少者优先；
5. `operator_id` 字典序作为稳定最终排序。

任务更新、负责人写入、首响确认和 claimed 事件在同一事务完成。版本不匹配、无可用坐席或并发抢先更新都返回冲突，不做降级分配。管理员凭据若仍有活动任务，必须先转派或结单才能停用。

### 5.5 管理 API 和页面

- `GET/PUT /v1/handoffs/operators...` 查询和配置坐席。
- `POST /v1/handoffs/operators/{id}/presence` 更新本人在线租约。
- `POST /v1/handoffs/{id}/assign-best` 执行确定性智能分配。
- 队列视图返回总坐席和可用坐席，任务视图返回负责人显示名和有效在线状态。
- 后台提供任务筛选、智能分配、完整状态处置、历史、SLA 扫描、队列策略和坐席调度表。

## 6. 竞品分析模块

实现入口：`business/competitive.py`、`operations_api.py`、竞品 worker。

### 6.1 数据进入

- 外部观察必须通过 Connector/导入 API，记录来源类型、来源引用、观察时间、估算标记和载荷哈希。
- 同一来源和载荷幂等；同版本冲突不会静默覆盖。
- 当前虚拟淘宝只生成可测试样本，不访问真实淘宝网络。

### 6.2 同款裁决

- 使用 GTIN、品牌、型号、标题和关键属性生成解释性匹配分数、缺失字段和硬冲突。
- 自动分数只产生 `pending` 建议，必须由管理员基于版本号批准或拒绝。
- 只有 `approved` 同款的价格、卖点和脱敏聚合口碑可进入 actionable 分析。
- 拒绝匹配会撤销相关告警资格，避免把不同商品用于调价建议。

### 6.3 监控和告警

- 每店铺/SKU 保存低价幅度、竞品降价、新鲜度、估算数据和 approved-only 策略。
- worker 和手工 API 使用同一原子评估服务。
- 告警支持 open/acknowledged/resolved；条件消失可清除，新证据再次满足可重开。
- 模块只给证据和建议，不自动修改售价。

## 7. 商品、库存、订单、指标模块

- 商品：`business/catalog.py` 保存 SPU/SKU、渠道状态、价格、属性、来源时间、哈希和版本。
- 库存：`business/inventory.py` 计算可售库存、覆盖天数、缺货/滞销风险和补货建议，不替代 WMS。
- 订单：`business/orders.py` 在同一事务写订单行、脱敏买家、物流、售后和不可变事件历史，不自动退款。
- 指标：`business/metrics.py` 只接受类型化 `QuerySpec`，不接受 SQL；返回定义版本、数据水位、质量和证据数量。
- `OperationsService` 统一注册只读 Agent 工具，业务域不直接访问第三方 HTTP。

营销和财务仍是规划模块，模块目录不会把它们报告成已完成。

## 8. 知识、自进化、质检和评测

- 分层知识：平台、行业、店铺、商品、进化五层；只检索当前租户/范围内已批准版本。
- 自进化：负反馈和修正答案形成 candidate，经静态安全、检索碰撞、回归、管理员批准后成为租户知识；不能修改权限、代码、Prompt 或工具。
- 质检/VOC：固定规则检测证据缺失、模型降级、漏转人工、敏感信息和渠道发送风险；人工确认或驳回。
- 客户评测：脱敏 case 集冻结后计算哈希，在隔离数据库快照运行真实多轮 Agent，主库不产生会话或任务污染。
- 发布回放：策略通过隔离回放、双人审批和运行预算后才可进入 shadow/assist/collaborative/automatic。

## 9. 渠道接待与可靠发送

- `taobao.py` 负责验签、重放保护、事件幂等、会话所有权和虚拟/真实能力边界。
- `channel_agent.py` 将入站事件与 Agent job 同事务落库，worker 用租约恢复。
- 人工接管后 ownership 变为 human/paused，发送前再次检查，阻止已排队的自动回复继续发送。
- `outbox.py` 先持久化加密载荷再派发；读回确认、指数退避、死信和 `uncertain` 状态都可审计。
- 不确定写入未经人工确认不会自动重发。

## 10. 发布、运维和灾备

- 发布策略按租户/平台/店铺不可变版本管理，双人审批后启用；稳定哈希分流保证同会话一致。
- 运行观测超过失败率或严重错误预算时自动暂停策略。
- `/health` 报告模型、数据库、队列和 worker；`/ready` 对生产所需 worker、发布策略、磁盘执行硬门禁。
- 灾备使用 SQLite online backup、一致性清单和 AES-256-GCM 加密归档；恢复先校验、覆盖前保留回滚副本。
- 数据目录只允许单实例；在线备份可并发，恢复和回滚要求服务停止。

## 11. 测试结构

- 单元/契约：决策、策略、RAG、工具、SOP、业务计算和模式校验。
- 集成：API 鉴权、租户隔离、数据库迁移、渠道/outbox worker、发布和评测。
- 并发/恢复：任务单赢家领取、租约、幂等、乐观锁、worker 重启、备份恢复。
- 端到端：实际 Agent 多轮用例、后台 API 和浏览器桌面/移动交互。
- 0.20.0 的最终通过数、覆盖率、运行时检查和剩余生产 Gate 见 `docs/TEST_REPORT_0.20.0.md`。

## 12. 当前生产边界

本版本可以作为隔离环境和客户 PoC 的本地候选，但以下证据缺失时不能宣称生产放行：

- 真实平台资质、测试店铺、消息读取/回复、订单和售后接口联调；
- 真实客户脱敏多轮标注集、固定模型/知识/SOP/事实版本基线；
- 客服主管确认的队列、技能、排班、容量、SLA 和升级责任；
- 目标一体机上的容量、24/72 小时长稳、断网/限流/重启和时钟漂移演练；
- 设备密钥托管、异机恢复、安全测试、业务验收和最终审批。

生产 `automatic` 模式继续保持关闭，不能用本地虚拟 Connector 或 mock 模型结果替代上述 Gate。
