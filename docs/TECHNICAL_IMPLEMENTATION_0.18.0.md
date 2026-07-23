# 云湃电商 Agent 0.18.0 技术实现说明

## 1. 当前技术实现路径

本项目采用“业务事实层 + 受控 Agent + 持久工作流 + 管理控制面”的单机模块化架构。模型负责理解、规划和生成，代码负责身份、数据权限、工具参数、业务规则、幂等、后置验证、发布门禁和审计。任何模型输出都不能直接提升权限或证明外部动作成功。

当前主链如下：

```text
合法平台/ERP/人工文件/虚拟样本
  -> Connector 验签、归一、去重和来源版本
  -> 商品/订单/库存/竞品等租户事实层
  -> ContextBuilder 固定装配可信上下文和证据
  -> LangGraph 有界 ReAct 选择知识、SOP 或已注册工具
  -> 代码化权限门、风险门和工具后置验证
  -> 回答、人工任务、草稿或加密 outbox
  -> 渠道回执、运行观测、质检/VOC 和审计
  -> 版本化客户评测集
  -> 隔离运行真实 Agent、多轮指标和回归比较
  -> 发布策略评测、双人审批、shadow/assist/协同/自动灰度
```

单机 V1 使用 FastAPI、Pydantic、LangGraph 和 SQLite WAL。业务库与 checkpoint 库分离，后台 worker 使用数据库租约恢复任务。多进程或多节点阶段再迁移 PostgreSQL，不在当前一体机版本提前引入分布式基础设施。

## 2. 0.18.0 解决的问题

此前发布门禁可以读取一次性 JSON 做隔离回放，但客户用例没有持久版本、数据集完整性、跨版本基线和独立评测账本，难以回答“这次发布究竟用哪批客户样本、哪些场景退化、结果是否可重复”。

0.18.0 增加版本化客户 Agent 评测中心：脱敏标注集从草稿、原子换版、冻结、运行、回归比较到发布关联形成完整证据链。运行器不是静态字符串比较，而是在临时数据库快照中执行实际多轮 Agent，同时保证主运行库不产生评测会话、消息、人工任务或 checkpoint。

## 3. 客户评测端到端设计

```text
客户脱敏标注 / 文件导入 / 人工或合成样本
  -> 创建 draft suite 和阈值
  -> 原子替换全部 cases（乐观锁）
  -> 校验必需场景和最少用例数
  -> 逐 case SHA-256 + dataset SHA-256
  -> frozen 不可变版本
  -> SQLite online backup 到临时目录
  -> 隔离 AgentService 执行同会话多轮对话
  -> 回合断言、用例结果、场景和总体指标
  -> 同 suite_key 基线回归（仅比较同 case_key + case_hash）
  -> gate passed/failed
  -> 可选关联 release policy（乐观锁）
  -> 双人审批和灰度运行
```

评测集生命周期为 `draft -> frozen -> retired`。冻结版本不能修改；需要调整用例或阈值时创建新版本并保留 `previous_suite_id`。用例批量替换在单事务内完成，`expected_record_version` 防止多人编辑互相覆盖。

## 4. schema v18

| 表或字段 | 职责 | 核心约束 |
|---|---|---|
| `evaluation_suites` | 评测集版本、来源、阈值和数据集哈希 | 租户内 `suite_key + version` 唯一；冻结后不可变；版本链可追溯 |
| `evaluation_cases` | 场景、多轮输入、逐回合期望和 case 哈希 | 租户隔离；suite 内 case_key 唯一；级联归属固定 |
| `evaluation_runs` | 运行幂等、总体指标、Gate、基线和发布关联 | `tenant + suite + run_key` 唯一；请求哈希冲突拒绝 |
| `evaluation_case_results` | 用例/回合结果、违规码和脱敏摘要 | 只保存受控结果，不保存未脱敏运行输出 |
| `release_policies.latest_evaluation_run_id` | 发布策略引用最新版本化评测 | 只能引用同租户、同 release 且已完成的 run |

v17 到 v18 为前向增量迁移，不改变历史竞品、渠道、发布或会话数据。启动时同时校验 `PRAGMA user_version`、迁移账本、物理表和关键列。

## 5. 用例与断言模型

每个 case 包含稳定 `case_key`、场景、脱敏来源引用和 1 至 12 个 turns。至少一个回合必须有标签。单回合可以断言：

- 意图 `expected_intent`；
- 是否应转人工 `expected_requires_human`；
- 是否必须提供来源 `require_sources`；
- 回答必含/禁含词；
- 最高风险级别；
- 是否允许模型降级；
- 上下文就绪状态 `ready/degraded/blocked`。

未知字段由 Pydantic `extra=forbid` 拒绝。客户标注集必须声明 `deidentified=true`；输入和 context 在写库前再执行标准化、敏感信息清理和白名单化。来源引用含个人信息时直接拒绝，而不是先落库再补救。

## 6. 指标和 Gate

默认阈值为至少 20 个用例、通过率 95%、意图准确率 95%、转人工召回率 100%、证据覆盖率 95%、严重失败 0、回归率 0。阈值随 suite 版本保存，发布审批使用当次冻结版本，不能被后来编辑静默改变。

| 指标 | 计算方式 |
|---|---|
| 用例通过率 | 全部回合无违规的 case 数 / case 总数 |
| 意图准确率 | 有意图标签的回合中匹配数 / 意图标签数 |
| 转人工召回/精确率 | 基于期望与实际 `requires_human` 的 TP/FN/FP/TN |
| 证据覆盖率 | 要求来源的回合中实际有来源的比例 |
| 严重失败 | 敏感输出、漏转人工、禁用词、无证据直接回答、风险越界等 |
| 回归率 | 基线曾通过且定义未变的可比用例中，本次失败的比例 |

没有对应标签时，该指标不虚构失败：准确率/覆盖率/召回率按 1.0 处理，并同时返回实际标签分母供后台识别。基线只比较相同 `suite_key` 下 `case_key + case_hash` 都一致的用例；定义已变更的 case 单独列出，不能把改题造成的差异伪装成模型回归。

## 7. 隔离运行、幂等和恢复

`AgentService.run_evaluation_suite` 先用 SQLite online backup 创建临时业务库，再创建关闭常驻 worker 和自动回复的隔离 `AgentService`。每个 case 使用独立稳定 session，多轮 turn 复用该 session；运行结束只把脱敏评测结果写回主库，临时目录随即清理。

关键故障语义：

- 同一个 `run_key` 和相同请求重复提交返回原 run；请求不同则冲突；
- 并发提交由数据库唯一约束收敛为单个 run；
- 运行前重新计算每个 `case_hash` 和整个 `dataset_hash`，数据库被绕过服务层篡改时拒绝执行；
- 输出先脱敏再保存摘要，runner 异常只记录稳定错误类别，不保存原始异常文本；
- 服务启动将遗留 `running` 评测标记为 `error/interrupted_by_restart`，并只审计一次，不把不完整运行当作通过。

## 8. 与发布门禁的结合

运行可携带 `release_id + expected_release_record_version`。评测服务会同时应用发布策略的意图白名单、最大风险、来源和模型降级限制；通过后由 `ReleaseService.apply_evaluation` 在乐观锁条件下把结构化结果写入策略。

发布策略保存 suite ID/key/version、dataset hash、runner version、metrics 和 gate。失败评测同样进入证据链，但不能审批；策略在评测期间被修改时，应用结果失败并要求重新评测。历史一次性 replay 仍兼容，审批统一要求“回放或版本化评测通过”。

## 9. 各功能模块的设计与实现

### 9.1 智能客服 Agent

`graph.py` 用 LangGraph 实现有界 ReAct：预检、检索、结构化决策、工具门、执行、后置验证、回答/转人工和持久化。`ContextBuilder` 在模型前按固定顺序装配会话、商品/订单、SOP、知识、工具和输出限制，并生成不可变 decision/generation 父子快照。身份冲突、授权不足和证据缺失在模型前降级；模型不能把顾客正文变成可信订单权限。

### 9.2 人工客服和渠道运行时

渠道会话使用 `bot/human/paused` 所有权与乐观锁。入站事件和 Agent job 同事务落库，租约 worker 支持恢复、退避和死信；每个事件的 invocation 幂等并精确关联消息、上下文和下游动作。shadow 不产生草稿、人工任务、SOP 或 outbox；assist 生成草稿；collaborative/automatic 在策略和所有权二次检查后才可能发送。

### 9.3 可靠发送

发送请求先持久化 AES-GCM 加密载荷，再由 outbox worker 外呼。能证明发生在调用前的失败才自动重试；调用已开始但回执不确定时进入 `uncertain`，必须人工读回核对。`confirmed/rejected/dead_letter/cancelled` 全部带尝试、版本、原因和审计。

### 9.4 竞品分析 Agent

`business/competitive.py` 先以 GTIN、品牌、型号、类目、标题和关键属性生成确定性、可解释的同款候选，再由人工乐观锁批准/拒绝。只有批准匹配关联的价格、卖点和至少 5 个样本的脱敏聚合口碑能进入 Agent；策略 worker 形成低价、降价和新鲜度持久告警。模块只读，不抓取未授权数据，不自动改价。

### 9.5 商品、订单、库存和经营指标

商品与订单使用统一来源时间、载荷哈希和单调版本，重复回放幂等，旧版本和同版本不同载荷拒绝。订单同时保存行项目、脱敏物流、售后和不可变历史。库存计算覆盖天数、缺货/滞销和补货建议，不执行采购。指标模块只接受严格 `QuerySpec`，六项公式由代码定义，不允许模型提交 SQL。

### 9.6 知识、SOP、质检和进化

知识按平台、行业、店铺、商品和租户进化分层，采用不可变版本、评测、批准、激活、退役和回滚。SOP DSL 把读取、评估、提案、动作、审批、补偿和后置条件持久化到逐步骤账本；写入中断进入 unknown/uncertain，禁止盲目重试。质检把证据缺失、漏转人工、敏感信息、模型降级和渠道故障代码化，结果需人工复核；自进化只产生知识候选，不自动修改权限、代码、Prompt 或模型权重。

### 9.7 Connector 与平台接入

统一 Connector SDK 约束能力声明、连接测试、分页拉取、Webhook 验签、动作幂等和读回验证。虚拟淘宝连接器明确 `virtual=true` 且不访问外网。真实淘宝路径为服务市场客服机器人资格、店铺 OAuth、奇门入站和 TOP 异步回写；在正式权限和测试店铺到位前不冒充已接通。

### 9.8 管理后台

`/admin` 是本地运营控制面，覆盖经营总览、客服会话/任务/证据、渠道队列、商品库存、订单售后、竞品质量和告警、知识/SOP、质检/VOC、发布策略、客户评测、模块状态与审计。0.18.0 的“客服评测”支持创建 suite、JSON 批量换版、冻结、新版本、退役、实际 Agent 运行、基线选择、发布关联、Gate/场景/失败用例查看。所有管理 API 使用管理员身份和租户隔离，密钥只保存在浏览器 sessionStorage。

### 9.9 运维与灾备

同一数据目录使用进程锁；业务库和 checkpoint 库以 SQLite online backup 生成快照，使用 AES-256-GCM 认证加密，支持验证、staging 恢复、自动回滚、换钥和 dry-run 保留清理。`/health` 暴露 schema、worker 和恢复统计，`/ready` 检查数据库、checkpoint、磁盘、发布门和 worker。当前仍是单写实例边界。

### 9.10 其他模块

营销/内容、利润/对账和多模态客服通过模块注册表声明为规划或候选状态，API 不虚报完成。后续模块继续复用租户、Connector、事实版本、任务、工具、审批、审计和评测底座，不另建旁路权限系统。

## 10. 管理 API

评测路由前缀为 `/v1/admin/evaluations`：

```http
GET  /overview
GET  /suites
POST /suites
GET  /suites/{suite_id}
PUT  /suites/{suite_id}/cases
POST /suites/{suite_id}/freeze
POST /suites/{suite_id}/versions
POST /suites/{suite_id}/retire
POST /suites/{suite_id}/runs
GET  /runs
GET  /runs/{run_id}
```

鉴权失败返回 401/403，跨租户对象按不存在处理；输入错误返回 422，状态/乐观锁/幂等冲突返回 409。接口不会把 runner 原始异常或未脱敏用户数据透出。

## 11. 生产边界与实施顺序

0.18.0 是本地代码级候选，不等于可以直接打开自动回复：

1. 获取平台/服务商正式权限、测试店铺和真实回执能力；
2. 导入客户确认脱敏的代表性多轮标注集，冻结数据集哈希；
3. 固定真实模型、知识、SOP、商品/订单数据版本运行评测；
4. 在目标一体机完成容量、安全、24/72 小时长稳和异机恢复；
5. 以真实渠道执行 shadow，验证零副作用、无跨会话和无静默丢失；
6. 业务和安全负责人签收后进入 assist，再逐版本推进协同；
7. automatic 必须有双人审批、白名单、错误预算、停止和人工补偿证据。

在这些 Gate 完成前，合理的生产实践范围是隔离 PoC、真实只读导入、客户标注评测、shadow 或坐席辅助，不是无人值守自动接管。
