# 项目验收

> 执行测试、交付或声明完成前必须读取本文件。没有新鲜证据时不得写成通过。
> 不粘贴密钥、完整个人信息、原始顾客对话或未脱敏工具输出。

## 当前验收结论

- 结论：2026-08-07 的 D23 修复候选暂不签署 M4 本机独立验收，证据 ID 为 E-20260807-002。D-034 语义边界已落实：分类与规则仅为 advisory signal，投诉 handoff / `complaints / urgent` 由规划模型确认，普通商品问题不再以目录候选或高检索分数绕过模型，流式与非流式共用生成计划。冻结 50 例未改，mock `answer_accuracy=0.940 / hallucination_rate=0.020 / severe_failures=3 / passed`，live `deepseek-v4-flash` `0.900 / 0.020 / severe_failures=1 / passed`，商品 `15/15`、投诉 `7/8`。阻塞项是当前代码的 40 条泄漏意图回归总体 `29/40=72.5% < 75%`，投诉平衡回归 recall `45%` 且 gate failed；现有浏览器 PNG 也仅代表 D22，D23 localhost 实跑被客户端策略拦截，未更新。不得以作答子集 `29/33=87.9%`、挑选较好 run 或放宽 2 秒预算替代门槛；下一轮需全新未泄漏留出集与 provider 容量方案。D22 的四场景 p50/p95 已被当前语义路径变更失效，当前全量延迟未知；生产放行继续阻塞。
- 结论：0.23.0 已完成营销与利润模块本机候选。schema v23 的营销日指标、内容草稿有限事实检查、来源费用、结算单和对账任务均带租户边界与来源/版本契约；营销只生成诊断和不可直接发布的草稿，利润仅为管理估算，对账只生成或人工流转差异任务。D14/D15 连同既有场景共 15/15 通过，两个 Agent 工具均按只读方式执行。2026-07-24 的隔离 SQLite 单机并发压测在 16 线程完成 818 次操作，验证来源幂等、任务乐观锁和租户隔离；完整输入输出可在网页与原始 JSON 中复核。该验证不替代真实广告平台、财务系统、总账、税务、结算资金、容量、长稳或生产放行验收。
- 结论：0.22.6 已完成管理后台视觉与响应式布局优化；原后台“智能客服 -> 对话测试”继续保留 0.22.5 的真实 `glm-4.7` 本机验证能力。桌面和 390px 下的长列表、会话和操作控件均有受限尺寸与内部滚动。该验证不替代正式模型、真实渠道、真实客户数据、容量/长稳或生产放行验收。
- 结论：0.22.4 原后台智能客服无客户端密钥顾客直测与 0.22.2 后台运营/模拟/评测数据隔离通过本地代码级候选验收；顾客测试默认关闭、仅回环可用，实际调用会话固定归入 simulation，原后台页面可显示实际回答、来源、风险、转人工、会话/追踪，默认运营范围不被污染；正式 `/v1/chat` 客户认证保持；真实客户脱敏标注集、真实模型/渠道、客服主管组织/周期班次/技能/队列/SLA 签收、真实授权竞品/口碑数据、24/72 小时长稳、容量、安全、异机灾备、业务验收和最终生产放行未验收
- 验收范围：原后台智能客服本机顾客直测、回环拒绝、默认关闭、simulation 来源隔离，以及 schema v22 会话来源分类、后台 overview/conversations/handoffs/dispatch scope 过滤、智能客服决策详情、Mock 状态展示、场景验收真实输入输出和既有全量回归
- 最后检查：2026-08-07
- 遗留问题：真实淘宝/ERP 权限、合法竞品/口碑数据源、客户同款标注集、脱敏客户多轮评测集、真实模型基线、数据字典和客服组织/班次/技能/队列/SLA 口径待提供；真实业务工具/读回补偿、语义 VOC、真实广告与财务数据接入、目标移动设备、渠道任务 24/72 小时长稳、容量、安全、异机介质、设备密钥托管和业务 RPO/RTO 待完成；虚拟数据不得替代上述证据

## 验收标准

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| 项目目标达到可验证结果 | 待检查 | 对照 `PROJECT_OVERVIEW.md` | 无 |
| 范围内功能满足完成条件 | 待检查 | 对照 `PROJECT_FEATURES.md` | 无 |
| 项目约定的测试全部通过 | 待检查 | 运行完整测试命令 | 无 |
| 阻塞与重大遗留问题已处理 | 待检查 | 对照 `PROJECT_PROGRESS.md` | 无 |

### 0.23.0 营销与利润并发压测验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| 来源版本重放在并发下保持幂等 | 通过 | 16 线程各重放 128 次营销、费用和结算来源事件 | 三类事件各 1 applied + 127 idempotent |
| 内容和财务边界不因并发失效 | 通过 | 64 个草稿并发写入、240 次并发查询 | 草稿均不可发布；利润响应 `financial_statement=false` |
| 对账任务保持单记录与乐观锁 | 通过 | 64 次并发对账、2 次同版本人工流转 | 仅创建 1 条任务；1 次成功、1 次版本冲突 |
| 租户隔离保持有效 | 通过 | 64 次另一租户并发读取 | 营销、费用、结算、任务和草稿均为空 |
| 完整输入输出可复核 | 通过 | 浏览器页面与静态 JSON 路由返回真实压测证据 | E-20260724-002；`/reports/marketing-finance-pressure` |
| 容量、长稳与生产放行 | 未验收 | 多进程、目标硬件、24/72 小时、真实数据与安全测试 | 不在 E-20260724-001 范围内 |

### 历史文档交付验收（原 Agent 路线）

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| 路线覆盖架构、模块、阶段、Gate 和验收 | 通过 | 检查 Markdown 标题结构 | 15 个二级章节、21 个三级章节 |
| Markdown 代码围栏闭合 | 通过 | 统计以三反引号开头的行 | 32 个围栏，数量为偶数 |
| 关联文档存在 | 通过 | `Test-Path` 检查三个引用 | 三个引用均为 `True` |
| 无乱码和未处理占位符 | 通过 | 搜索替换字符、`TODO`、`TBD`、`尚未定义` | 无命中 |
| 项目状态未被误写为已完成 | 通过 | 对照当时总览和进度 | 当时保留待决策阻塞项，项目功能未验收 |

### 后台接入与客服接管调研验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| 明确区分后台数据与客服 IM 权限 | 通过 | 检查结论、平台矩阵和执行路线 | 新报告第 1、3、4、6 章 |
| 提供可执行的申请、授权、同步、接管和验收步骤 | 通过 | 检查操作清单、状态机、计划和 Gate | 新报告第 4 至 6、10 至 14 章 |
| 竞品机制区分官方证据、厂商自述和工程推断 | 通过 | 检查证据等级与竞品章节 | 新报告第 2、7 章 |
| 未把未获权限写成已开放能力 | 通过 | 搜索“待验证/待审批/专项权限/书面权限”并核对平台矩阵 | 新报告第 4、9、11 章 |
| Markdown 结构完整且无乱码/占位符 | 通过 | 统计标题、围栏、链接和异常字符 | 761 行、1 个 H1、16 个 H2、45 个 H3、18 个闭合围栏、63 个链接、0 个乱码字符、0 个 TODO/TBD |
| 项目文件与新开发顺序一致 | 通过 | 交叉检查总览、进度、功能和验收 | F-201 至 F-210 为当前主线；Agent 内部能力已后移 |

### 淘宝客服接管本地 PoC 验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| OAuth 状态与凭据保护 | 通过 | 模拟 token 交换、state 重放、AES-GCM 篡改测试 | `tests/test_taobao.py` |
| 奇门入站安全与幂等 | 通过 | MD5 验签、时间窗、重复事件、敏感信息脱敏测试 | `tests/test_taobao.py` |
| 会话归属与人工回复 | 通过 | `human/paused` 乐观锁、人工发送门、幂等发件箱和 TOP 表单测试 | `tests/test_taobao.py` |
| 能力不得虚报 | 通过 | 管理 API 权限、缺少 route/request_token 时门禁测试 | `tests/test_taobao_api.py` |
| 全量代码回归 | 通过 | `py -3.12 -m pytest -q` | 当前工作区 53 passed，1 个上游弃用 warning |
| 离线信任边界与安全评测 | 通过 | `py -3.12 -m ecommerce_agent.cli eval`（隔离 DATA_DIR） | 20/20 passed |
| 淘宝真实消息收发 | 阻塞 | 真实测试店铺 OAuth、买家消息、人工回复和截图 | 缺淘宝专属权限与凭证 |

### 淘宝官方机器人 API 契约复核

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| 不依赖千牛页面自动化 | 通过 | 检查架构决策、运行手册和申请材料 | 独立后台通过 OAuth、TOP、奇门接入；明确禁用 Cookie/客户端注入 |
| 官方接口和字段对齐 | 通过 | 对照四个官方 API 文档检查方法名、网关、签名、用户字段和业务响应 | `src/ecommerce_agent/taobao.py`、`tests/test_taobao.py` |
| 订阅具备后置验证 | 通过 | 模拟 subscribe 后调用 query 并校验目标客服账号集合 | `tests/test_taobao.py` |
| 能力状态不虚报 | 通过 | capability 返回官方契约和缺失条件测试 | `tests/test_taobao_api.py` |
| 准入材料可执行 | 通过 | 检查申请对象、所需参数、回调、执行顺序和真实 Gate | `docs/taobao-api-access-application.md` |
| 全量回归与离线安全评测 | 通过 | `pytest`、隔离 DATA_DIR eval、`compileall` | 54 passed；20/20 passed；compileall 退出 0 |
| 真实淘宝联调 | 阻塞 | 平台审批、专属凭证、测试店铺真实收发 | 千牛登录不能替代客服机器人类目和数据平台授权 |

### 0.6.0 全域业务模块首批切片验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| 统一连接器契约 | 通过 | 能力、连接测试、分页拉取、Webhook 验签、动作幂等和回执验证测试 | `src/ecommerce_agent/connectors/`、`tests/test_operations_modules.py` |
| 虚拟接口不得冒充真实平台 | 通过 | 检查 `virtual=true`、无外部 HTTP、固定虚拟回执和 UI/README 边界 | `VirtualTaobaoConnector`、`/v1/connectors/catalog` |
| 仓储模块可运行 | 通过 | 虚拟库存同步、版本化余额、缺货/滞销和补货公式测试 | `business/inventory.py`、`GET /v1/inventory/risks` |
| 竞品模块来源可追溯 | 通过 | 虚拟来源强制估算标识、价格差和证据返回测试 | `business/competitive.py`、`GET /v1/competitive/analysis` |
| Agent 可调用经营工具 | 通过 | 动态目录选择、参数校验、执行及 postcondition 测试 | `get_inventory_risk`、`get_competitor_price_analysis` |
| 全量代码回归 | 通过 | `py -m pytest -q -p no:cacheprovider` | 54 passed，1 个上游弃用 warning |
| 离线安全评测 | 通过 | 隔离 DATA_DIR 运行 `ecommerce_agent.cli eval` | 20/20 passed |
| 架构页响应式与交互 | 通过 | 本地浏览器 1440×900、390×844；检查无 body 横向溢出、4 个标签和标签切换 | `docs/architecture-inspector.html` |

### 功能台账汇总验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| 客服技术路线已映射到功能清单 | 通过 | 检查 F-001 至 F-116 与技术路线章节/Epic 的对应关系 | `.project-to-act/PROJECT_FEATURES.md` |
| 已有与目标能力状态未混淆 | 通过 | 对照当前源码、测试、目标文档和阻塞项 | 已有基线标记已完成；未实现能力标记已规划/已阻塞 |
| 功能项可直接用于后续拆 Issue | 通过 | 检查每项优先级、依赖、完成条件和证据 | 功能表字段完整，无“暂无/未定义”项 |
| 总览、进度、功能和验收一致 | 通过 | 交叉检查当前焦点、当前任务、阻塞和结论 | M0 仍阻塞；真实渠道和读写动作未误报完成 |

### 0.7.0 经营与客服管理 V1 验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| 竞品洞察可追溯 | 通过 | 虚拟同步后检查总览、价格位置、历史趋势、风险、建议和来源/估算标识 | `tests/test_operations_modules.py`、浏览器竞品页交互 |
| 客服管理按租户隔离 | 通过 | 创建会话后检查管理总览、列表、详情、消息来源及未认证访问 | `tests/test_admin_console.py` |
| 后台可操作且不绕过鉴权 | 通过 | 管理员登录、竞品同步、对话测试、会话回放、人工任务和审计 API 检查 | `docs/admin-console.html`、浏览器实际交互 |
| 响应式布局 | 通过 | 默认桌面及 390×844 视口检查 body 无横向溢出、导航可滚动、卡片重排 | 浏览器截图与 DOM 像素检查 |
| 全量回归与离线安全评测 | 通过 | `pytest`、隔离 DATA_DIR eval、`compileall`、project-to-act `--validate` | 56 passed；20/20 passed；其余退出 0 |
| 真实平台与生产放行 | 阻塞 | 真实店铺、消息、客户数据和部署 Gate | 不在本地 V1 验收范围内，无豁免 |

### 0.8.0 经营事实与受控指标候选验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| 商品来源版本与租户隔离 | 通过 | 首写、幂等、旧版本、同版本冲突、新版本、跨租户与 16 次并发重放 | `tests/test_catalog_orders_metrics.py` |
| 订单聚合与不可变历史 | 通过 | 订单行、物流、售后、版本历史、旧数据保护和注入失败事务回滚 | `tests/test_catalog_orders_metrics.py` |
| 订单 Agent 对象权限 | 通过 | 缺可信上下文、订单号不匹配、店铺号不匹配和正常读回 | `get_order_facts` 工具测试 |
| 受控指标可复算 | 通过 | 六项固定指标、定义版本、水位、质量、证据和额外 SQL 字段拒绝 | `business/metrics.py`、测试报告第 6 节 |
| 四资源安全回放 | 通过 | 商品、订单、库存、竞品首次同步与完整重复同步 | 首次各 2 条；重放各 0 条实际写入 |
| 工具执行边界 | 通过 | 只读异常重试、读超时、写超时不确定态、后置验证失败 | `tests/test_tools.py` |
| 数据迁移 | 通过 | legacy v1 到 v8、带历史数据 v7 到 v8 | `tests/test_migrations.py` |
| 管理后台 | 通过 | 登录、商品/库存同步与筛选、订单/售后同步与筛选、指标显示 | 1440×900 与 390×844 浏览器检查；0 控制台错误 |
| 全量回归与覆盖率 | 通过 | pytest + branch coverage | 67 passed；83% total；核心模块 83% 至 94% |
| 离线信任边界与安全评测 | 通过 | 隔离 DATA_DIR、模型禁用运行 eval | 20/20 passed |
| 完整测试报告 | 通过 | 复核环境、矩阵、覆盖率、浏览器和未通过 Gate | `docs/TEST_REPORT_0.8.0.md` |
| 真实平台与生产放行 | 阻塞 | 真实淘宝/ERP、真实模型、客户数据、性能长稳、灾备与设备安全 | 无豁免；测试报告第 10 节 |

### 0.9.0 Agent 治理与客服操作闭环本地候选验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| 分层知识生命周期 | 通过 | 创建、修订、评测、批准/激活、退役、回滚、乐观锁、跨租户隔离和店铺/SKU 检索过滤 | `tests/test_governance.py`、`knowledge_management.py`、`rag.py` |
| SOP DSL、生命周期与会话固定版本 | 通过 | 类型化 DSL、非法定义、评测/批准/激活/退役/回滚、必需上下文、外部写门、工具白名单和同会话版本固定 | `tests/test_governance.py`、`sops.py`、`graph.py` |
| 质检复核与 VOC | 通过 | 事实证据、模型降级、漏转人工、敏感信息、发送失败、高风险发送规则；待复核/确认/驳回及汇总 | `tests/test_governance.py`、`quality.py` |
| 客服操作闭环 | 通过 | 暂停、接管、恢复、人工归属门、草稿创建/编辑、结构化 diff、证据/SOP/风险、人工发送 | `tests/test_taobao.py`、`taobao.py`、`docs/admin-console.html` |
| 暂停与发送竞态 | 通过 | 发送前再次读取会话归属；人工接管后旧自动回复不得发出 | `tests/test_taobao.py` |
| 渠道投递状态 | 通过 | 成功为 `confirmed`，平台业务拒绝为 `rejected`，传输异常为 `uncertain`；相同幂等键失败后禁止盲重试 | `tests/test_taobao.py` |
| 数据迁移与 API 错误契约 | 通过 | legacy/v8 到 schema v9；治理资源不存在、版本冲突、非法状态和租户隔离映射为确定 HTTP 错误 | `tests/test_migrations.py`、`tests/test_governance_api_errors.py` |
| 管理后台 | 通过 | 浏览器实际创建并激活知识、运行并复核质检；桌面与 390×844 响应式检查；0 控制台错误 | `/admin`、`docs/admin-console.html` |
| 全量回归与源码分支覆盖率 | 通过 | `coverage run --branch -m pytest -q`、`coverage report --include="src/*" -m` | 74 passed；源码 branch coverage 84% |
| 离线安全评测 | 通过 | 隔离 `DATA_DIR` 且模型禁用运行 eval | 20/20 passed |
| SQLite 备份恢复冒烟 | 通过（仅本地） | 在线 backup、恢复初始化、schema v9、`integrity_check` 和关键表计数 | `integrity_check=ok`；不替代加密备份、断电和整机恢复演练 |
| 本机性能冒烟 | 通过（仅框架/模拟） | health 100 次、chat 30 次、8 worker/20 次并发 | 详细 p50/p95/max 见 `docs/TEST_REPORT_0.9.0.md`；不替代生产负载/长稳 |
| 生产放行 | 阻塞 | 真实淘宝/ERP、脱敏客户回放、持久 outbox、24 小时长稳、加密灾备和设备安全 Gate | 无豁免；测试报告第 10 节 |

### 0.10.0 可靠发送本地候选验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| 持久、加密和幂等入队 | 通过 | 回复入队、重复键、数据库明文搜索、API 响应字段检查 | `tests/test_outbox.py`、`tests/test_outbox_api.py` |
| 跨连接原子租约 | 通过 | 8 个独立数据库实例并发领取同一记录 | 只有一个 owner，见 `test_outbox_claim_is_atomic_across_database_instances` |
| 崩溃边界 | 通过 | 分别模拟平台调用前和调用后租约过期 | 调用前安全回队；调用后 `uncertain` 且不自动重发 |
| 重试、死信与核对 | 通过 | 建连失败、指数退避、最大尝试、三种核对、死信回队约束和版本冲突 | `tests/test_outbox.py`、`tests/test_outbox_api.py` |
| 会话归属和凭证实时复核 | 通过 | 入队后切换人工 owner；worker 发送前重读 owner/connection | 旧自动回复被取消；不使用陈旧凭证 |
| 草稿和出站事件一致性 | 通过 | 异步入队、平台确认、失败和人工核对 | `sending -> sent/failed`；事件 `queued -> sent/failed` |
| schema 迁移与物理校验 | 通过 | v1/v7/v9 -> v11、遗留在途记录、伪造迁移标记 | `tests/test_migrations.py` |
| worker 生命周期和就绪状态 | 通过 | FastAPI lifespan 启停、health/readiness、运行服务重启 | schema 11；worker enabled/running；ready checks 全 true |
| 管理 API 与租户隔离 | 通过 | 未认证、租户范围、密文隐藏、摘要、执行、核对和版本冲突 | `tests/test_outbox_api.py` |
| 管理后台 | 部分通过 | 桌面浏览器登录、渠道页、队列执行、DOM 溢出和控制台日志 | 桌面 1280×720 通过、0 error/warning；本轮移动视口工具未生效，未计实测通过 |
| 全量回归与源码分支覆盖率 | 通过 | branch coverage 全量 pytest | 83 passed；全部 `src/` 84%；outbox 85% |
| 离线安全评测 | 通过 | 隔离 `DATA_DIR`、模型禁用 | 20/20；三类 failure 均为 0 |
| SQLite 恢复冒烟 | 通过（仅本地） | 在线 backup、恢复初始化、schema 11、完整性和关键表计数 | `integrity_check=ok`；不替代加密/断电/整机恢复 |
| 本机性能冒烟 | 通过（仅框架/模拟） | health/outbox summary/chat 顺序与 8 worker 并发 | 0 错误；详细 p50/p95/max 见 `docs/TEST_REPORT_0.10.0.md` |
| 生产放行 | 阻塞 | 真实淘宝/ERP、客户回放、24 小时长稳、故障注入、加密灾备和设备安全 | 无豁免；测试报告第 11 节 |

### 0.11.0 加密灾备本地候选验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| 运行目录互斥 | 通过 | 第二服务启动、在线服务下离线备份/恢复 | 同一目录只能有一个服务 owner；活动目录恢复被拒绝 |
| 在线/离线双库快照 | 通过 | SQLite online backup、离线 `--require-stopped`、session/checkpoint 关系 | 在线逐库原子且身份一致；离线为锁定维护点，不虚报跨库同一事务 |
| 认证加密和归档边界 | 通过 | 错误密钥、篡改、截断、错误 header、恶意 ZIP、清单/哈希/schema 破坏 | AES-256-GCM/HKDF；所有不可信输入在恢复前被拒绝 |
| 恢复和自动回滚 | 通过 | 新目录、force、sidecar、提交故障注入和恢复启动 | staging 复验；失败恢复原数据库；成功生成 receipt |
| 手工回退 | 通过 | 成功 rollback、无 rollback、rollback 故障注入 | 原版本可恢复，当前版进入 forward；失败时两套数据均保留 |
| 换钥和保留策略 | 通过 | rekey、旧/新 key 验证、prune dry-run/apply/无效文件 | 新 key 生效、旧 key 失效；默认不删除且至少保留一份 |
| CLI 安全错误 | 通过 | 子进程执行全命令及错误路径 | JSON stderr、非零退出、无 traceback/密钥泄漏 |
| 全量回归与覆盖率 | 通过 | branch coverage 全量 pytest | 99 passed；全部 `src/` 84%；disaster recovery 85% |
| 离线评测与静态检查 | 通过 | 隔离 eval、compileall、JS、editable 版本 | 20/20；版本 0.11.0；全部检查通过 |
| 真实运行目录演练 | 通过（仅本机） | 活动服务在线备份/验证/恢复；停止服务后离线维护点备份 | schema 11；100/100 和 150/150 session/checkpoint 一致，见完整报告 |
| 管理后台 | 沿用 0.10.0 桌面证据 | 本版只改版本标签并执行 JS 静态检查 | 未新增浏览器或移动端实测声明 |
| 生产放行 | 阻塞 | 真实淘宝/ERP、客户回放、24 小时长稳、故障注入、异机/设备密钥/RPO-RTO | 无豁免；`docs/TEST_REPORT_0.11.0.md` 第 10 节 |

### 0.12.0 发布门禁本地候选验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| 策略版本和生命周期 | 通过 | 版本递增、范围固定、乐观锁、启用新版本退役旧版本 | `tests/test_releases.py` |
| 完整 Agent 隔离回放 | 通过 | online backup 到临时双库、真实图调用、源 session/message/handoff 不变 | 1/1 专项隔离测试；`releases.py` 86% |
| 回放隐私和 CLI | 通过 | 原始问答不落回放表、数据集哈希、非法输入脱敏错误、模型禁用失败 | `tests/test_releases.py`、`tests/test_cli.py` |
| 双人审批和管理员 | 通过 | 自动策略创建人自审批拒绝、第二凭据审批、停用和自停用保护 | API 端到端通过 |
| 四级运行模式 | 通过 | no-policy、shadow、assist、automatic 和 owner/outbox 断言 | 奇门后台任务集成测试通过 |
| 运行观测和自动停止 | 通过 | 幂等 event、缺证据、发送异常、错误预算和自动暂停审计 | 故障注入通过 |
| 管理 API 和后台 | 通过（弹窗点击除外） | 租户鉴权、状态/版本错误、策略创建/回放、桌面/390px 页面和控制台日志 | 浏览器 0 error、无溢出/重叠；原生 prompt 由自动化取消，API 和回显通过 |
| 全量回归与覆盖率 | 通过 | branch coverage 全量 pytest | 111 passed；全部 `src/` 85%；发布模块 86% |
| 离线评测与静态检查 | 通过 | 隔离 eval、compileall、JS、editable 版本 | 20/20；版本 0.12.0；全部检查通过 |
| 运行服务与性能 | 通过（仅本机/mock） | health/ready/OpenAPI 和顺序冒烟 | schema 12、ready 200；详细 p50/p95/max 见报告 |
| 生产放行 | 阻塞 | 真实淘宝/ERP、客户回放、24 小时长稳、异机/设备密钥/RPO-RTO、真实灰度 | 无豁免；`docs/TEST_REPORT_0.12.0.md` 第 10 节 |

### 0.22.2 后台数据隔离与真实输入输出展示

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| 数据来源分类 | 通过 | schema v22 迁移、新库、历史 virtual/evaluation/taobao 回填 | `sessions.source_type/source_reference`；`tests/test_migrations.py` |
| 默认运营隔离 | 通过 | 8104 `/v1/admin/overview` 与模拟范围对比 | 默认运营 0 会话、0 消息、0 人工任务、0 指标请求；模拟 17 会话、34 消息、13 人工任务、17 指标请求 |
| 智能客服可复核 | 通过 | Edge 打开 `/admin`，切换模拟范围，选择会话详情 | 来源标签“虚拟验收”、证据按钮、决策详情、工具/上下文/轨迹可见 |
| 场景验收真实输出 | 通过 | Edge 场景验收页运行全部场景 | 13/13 通过；D01-D13 可查看真实调用输入、断言和 JSON 输出；HTTP 响应约 442,944 bytes |
| 全量回归 | 通过 | `pytest -q` | 221 passed；退出码 0；日志 `tmp/pytest-full-0.22.2-20260722-221156.out.log` |
| 静态与安全 Gate | 通过 | compileall、JS、默认模型关闭 eval | compileall 0；`validated 1 inline script(s)`；20/20 eval |
| 运行服务 | 通过（仅本机/mock） | health/ready、Edge 页面、SQLite scope 查询 | health ok；ready 200；schema 22；管理员本机免登录；客户认证仍开启；Mock 模型显示 |
| 生产放行 | 阻塞 | 真实平台/客户/模型/竞品源、营销/利润、长稳、容量、安全和异机灾备 | `docs/TEST_REPORT_0.22.2.md`；无豁免 |

### 0.22.1 虚拟店铺逐场景输入输出证据

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| 报告数据契约 | 通过 | GET 检查固定输入/预期，POST 检查 `input/expected/assertions/output` 和兼容 `detail` | `simulation-evidence-v1`；13 项均完整 |
| 实际输出覆盖 | 通过 | 检查商品/订单/风险/指标、竞品工具/告警、客服/派单、租户探测、连接器和评测 | D01-D13；真实 HTTP 110,780 bytes |
| 后台场景验收 | 通过 | 登录、运行、查看 D01/D07、模块筛选、模块覆盖 | 13 通过、0 失败、0 跳过；智能客服筛选仅 D07 |
| 响应式与可读性 | 通过 | 1280×720 和 390×844 截图、宽度/定位、console | 无全局横向溢出；移动详情标题不被 sticky 导航遮挡；0 error/warning |
| 全量回归与覆盖率 | 通过 | branch coverage 全量 pytest | 218 passed；失败 0；跳过 0；生产源码 86%；simulation 94%；API 100% |
| 静态与安全 Gate | 通过 | compileall、JS、JSON、editable 版本、默认模型关闭 eval | 源码/包 0.22.1；20/20；三类失败数组为空 |
| 运行服务与数据库 | 通过（仅本机/虚拟） | health/ready/OpenAPI、SQLite integrity/foreign key | schema 21；135 paths；integrity ok；外键错误 0 |
| 生产放行 | 阻塞 | 真实平台/客户/模型/竞品源、营销/利润、长稳、容量、安全和异机灾备 | `production_claim=false`；无豁免；`docs/TEST_REPORT_0.22.1.md` |

### 0.22.0 虚拟店铺运营模拟与跨模块验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| 数据包边界与关联数据 | 通过 | 强制 `virtual=true`；6 商品、10 库存、8 订单、3 竞品候选、4 店铺知识经 Pydantic 和公开领域服务导入 | `fixtures/virtual_store_v1.json`、`simulation.py` |
| 领域质量门 | 通过 | 来源版本、同款评分/人工裁决、approved-only 证据、知识评测/批准、租户隔离和工具权限均不可绕过 | D01-D06、D08、D11；首次运行修复 3 个被正确拒绝的数据问题 |
| 当前可用模块覆盖 | 通过 | 报告与业务模块注册表交叉核对 | 7/7 `available` 模块通过；营销/财务为 `planned_not_executed` |
| 客服与人工接管 | 通过 | 店铺知识回答或模型关闭安全转人工；可信订单范围；建单、持久 job 和自动派单 | D07-D10；来源和上下文快照存在 |
| 客服 Agent 评测 | 通过 | 冻结合成标注集、数据集哈希、临时 SQLite 快照实际 Agent 回放、主库计数前后对比 | D13；run passed；主库 session/message/handoff 不变 |
| 重放与审计 | 通过 | CLI 和真实 HTTP 各立即重放；检查来源状态、稳定评测 key 和审计 | 商品 6、库存 10、订单 8、匹配 3、观察 2、信号 3 均幂等；知识 4 复用 |
| API 与显式确认 | 通过 | GET 摘要、POST 缺确认 422、管理员租户运行、审计查询 | `tests/test_virtual_store_simulation.py`；135 paths |
| 全量回归与覆盖率 | 通过 | branch coverage 全量 pytest | 218 passed；失败 0；跳过 0；全项目 90%；生产源码 86%；simulation 95% |
| 静态与实际 Agent Gate | 通过 | compileall、JSON、editable 版本、默认模型关闭 eval | 源码/包 0.22.0；20/20，三类失败数组为空 |
| 运行服务与数据库 | 通过（仅本机/虚拟） | health/ready、HTTP 双重放、SQLite integrity/foreign key、后台桌面/390px | schema 21；integrity ok；外键错误 0；`http://127.0.0.1:8104/admin` |
| 生产放行 | 阻塞 | 真实平台/客户/模型/竞品源、营销/利润、长稳、容量、安全和异机灾备 | `production_claim=false`；无豁免；`docs/TEST_REPORT_0.22.0.md` 第 8 节 |

### 0.21.0 值守排班与持久自动派单本地代码级候选验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| schema v21 与前向迁移 | 通过 | 新库、v20 升级、历史 proposed 补种、跨租户触发器、伪造标记和物理结构 | `tests/test_migrations.py`；schema 21 |
| 值守心跳与排班 | 通过 | session、连续/幂等 sequence、跳号/旧号拒绝、租约过期、UTC 班次、重叠/取消和班次内外 | `tests/test_handoff_dispatch.py` |
| 自动/人工派单隔离 | 通过 | automatic worker 候选、manual 人工路径、队列/技能/容量和 fail closed | 专项正负向测试通过 |
| 作业租约与恢复 | 通过 | 任务/job 同事务、多 worker 单赢家、lease 过期恢复、启动补种和状态对账 | dispatch 86% branch coverage |
| 等待、失败和告警 | 通过 | 无坐席 waiting/occurrence、恢复唤醒/自动解决、技术错误预算、版本重试/确认和脱敏 | 专项与浏览器闭环通过 |
| 管理 API 与后台 | 通过（本地） | 班次、心跳、作业、重试、告警、冲突错误、桌面/390px 和 console | 修复后新增 error/warning 为 0 |
| 全量回归与覆盖率 | 通过 | branch coverage 全量 pytest | 215 passed；全项目 89%；生产源码 86% |
| 实际 Agent Gate | 通过 | 模型禁用隔离运行真实 Agent 图和安全链 | 20/20，失败数组为空 |
| 运行服务 | 通过（仅本机/隔离） | health/ready/OpenAPI 和运行 worker | schema 21；133 paths；`http://127.0.0.1:8103/admin` |
| 生产放行 | 阻塞 | 真实客户/模型/渠道、周期排班/SLA、长稳、容量、时钟、安全和异机灾备 | 无豁免；`docs/TEST_REPORT_0.21.0.md` 第 8 节 |

### 0.20.0 坐席调度与智能分配本地代码级候选验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| schema v20 与前向迁移 | 通过 | 新库、v19 升级、补种、跨租户触发器、伪造标记和物理 schema | `tests/test_migrations.py`；integrity ok；外键错误 0 |
| 坐席身份与运营档案 | 通过 | 管理员凭据、档案启停、显示名、技能、全局容量和乐观锁 | `tests/test_handoff_staffing.py` |
| 在线租约与重启语义 | 通过 | available/away TTL、过期离线、本人更新和服务重启不复活 | 专项测试全部通过；staffing 96% |
| 队列成员、技能与容量 | 通过 | L1-L5、主队列、非成员、全局满载、队列满载和凭据/档案停用 | 负向策略全部 fail closed |
| 智能分配与原子性 | 通过 | 负载率/主队列/技能/任务数/ID 稳定排序，任务/首响/事件/审计同事务 | 自动化和浏览器 `created -> claimed` 闭环通过 |
| 管理 API 与后台 | 通过（本地） | 配置、暂离/上线、队列可用数、智能分配、负载、历史和响应式 | 1280 x 720、390 x 844，0 console error/warning |
| 全量回归与覆盖率 | 通过 | branch coverage 全量 pytest | 203 passed；全项目 89.65%；生产源码 85.86%；staffing 96% |
| 静态检查与包版本 | 通过 | compileall、管理页 JS、editable 元数据 | 源码/包 0.20.0；全部退出 0 |
| 实际 Agent Gate | 通过 | 模型禁用隔离环境运行真实 Agent 图和安全链 | 20/20，2.5 秒，失败数组为空 |
| 运行服务与数据库 | 通过（仅本机/隔离） | health/ready/OpenAPI、SQLite 完整性和浏览器运行库 | schema 20；123 paths；独立 `tmp/runtime-0.20.0` |
| 生产放行 | 阻塞 | 真实客户/模型/渠道、业务组织/排班/SLA、长稳、容量、安全、异机灾备 | 无豁免；`docs/TEST_REPORT_0.20.0.md` 第 10 节 |

### 0.19.0 人工接管队列与 SLA 本地代码级候选验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| schema v19 与前向迁移 | 通过 | 新库、v18 活动已分配任务、历史升级、伪造标记和物理 schema | `tests/test_migrations.py`；integrity ok；外键错误 0 |
| 路由、优先级和队列策略 | 通过 | 默认四队列、原因/意图/风险、显式覆盖、兜底、自升级和坏 token | `tests/test_handoff_workbench.py` |
| 原子认领、容量与租户隔离 | 通过 | 12 路并发单赢家、坐席上限、跨队列转派和跨租户所有操作 | 专项测试全部通过 |
| 状态机与事件历史 | 通过 | 负责人门、待补充预算、复核/退回/完成、终态说明、连续版本事件 | 自动化和浏览器五事件闭环通过 |
| SLA 与 worker | 通过 | 首响 L1、解决 L2、重复扫描幂等、冲突、health/ready 和手工扫描 | worker 专项、运行 health/ready 通过 |
| 高风险 Agent 最终保护 | 通过 | 真实 Agent 12 检索、5 高风险动作、3 前置检查/拒绝/接管 | 20/20，1.119 秒，17.87 cases/s |
| 管理 API 与后台 | 通过（本地） | 鉴权、404/409/422、筛选、全处置、历史、策略、SLA 扫描和响应式 | 1280 x 720、390 x 844，0 console error/warning |
| 全量回归与覆盖率 | 通过 | branch coverage 全量 pytest | 195 passed；全项目 89.45%；生产源码 85.63%；handoff 87.94% |
| 静态检查与包版本 | 通过 | compileall、管理页 JS、editable 元数据 | 源码/包 0.19.0；全部退出 0 |
| 运行服务与数据库 | 通过（仅本机/隔离） | health/ready/OpenAPI、SQLite 完整性和浏览器运行库 | schema 19；119 paths；4 queues/1 task/5 events |
| 生产放行 | 阻塞 | 真实客户/模型/渠道、业务 SLA/排班、长稳、容量、安全、异机灾备 | 无豁免；`docs/TEST_REPORT_0.19.0.md` 第 10 节 |

### 0.15.0 可信上下文客服 Agent 本地代码级候选验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| schema v15 与前向迁移 | 通过 | 新库、历史升级、伪造迁移标记和物理 schema 校验 | `tests/test_migrations.py`；integrity ok；外键错误 0 |
| ContextBuilder 固定装配与证据 | 通过 | 会话、商品/订单、SOP、知识、工具和约束；authority/freshness/checksum | `tests/test_context_builder.py`；ContextBuilder 93% |
| 冲突、幂等、并发与完整性 | 通过 | 模型前店铺冲突、16 路并发、重放变化和持久数据篡改 | 专项测试全部通过 |
| ReAct 父子快照与工具验证 | 通过 | decision #0 -> decision #1 -> generation #1；verified_tool | `tests/test_react_graph.py` |
| API、消息、审计与后台 | 通过 | 租户鉴权、消息关联、证据接口、桌面真实查看和 390px | 0 console 错误；弹窗无横向溢出 |
| 隐私与留存 | 通过 | 脱敏、普通到期、反馈保留、活动人工任务保护 | `tests/test_privacy_metrics.py`；maintenance 100% |
| 全量回归与覆盖率 | 通过 | branch coverage 全量 pytest | 149 passed；全部 `src/` 85%；ContextBuilder 93% |
| 离线评测与包版本 | 通过 | 隔离 eval、compileall、JS、editable 安装 | 20/20；包/应用 0.15.0 |
| 运行服务、数据库与性能 | 通过（仅本机/隔离） | health/ready/OpenAPI、SQLite、80 次顺序请求 | schema 15、ready；详细 p50/p95/max 见报告 |
| 生产放行 | 阻塞 | 真实平台/业务工具、客户回放、长稳、容量、异机/设备密钥/RPO-RTO | 无豁免；`docs/TEST_REPORT_0.15.0.md` 第 12 节 |

### 0.14.0 竞品监控 Agent 本地代码级候选验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| schema v14 与前向迁移 | 通过 | 新库、v13 升级、历史迁移、伪造标记和物理 schema 校验 | `tests/test_migrations.py`；quick_check ok；外键错误 0 |
| 策略与告警状态机 | 通过 | 阈值版本、低价/降价/过期、确认/解决/清除/复发 | `tests/test_competitive_monitoring.py` |
| 幂等、并发与租户隔离 | 通过 | 相同证据重评、12 次并发、跨租户读取/评估拒绝 | occurrence 不重复；租户越权无结果 |
| worker 生命周期 | 通过 | 启动、周期、逐租户故障隔离、readiness 和关闭 | health/ready 与专项测试通过 |
| Agent/API/后台 | 通过 | 工具证据、8 条竞品路径、乐观锁、桌面真实解决、390px | 0 console 错误；无页面级横向溢出 |
| 全量回归与覆盖率 | 通过 | branch coverage 全量 pytest | 142 passed；全部 `src/` 85%；竞品 91% |
| 离线评测与包版本 | 通过 | 隔离 eval、JS 语法、editable 安装 | 20/20；包/应用 0.14.0 |
| 运行服务、数据库与性能 | 通过（仅本机/隔离） | health/ready/OpenAPI、SQLite、400 次只读请求 | schema 14、ready 200；详细 p50/p95/max 见报告 |
| 生产放行 | 阻塞 | 真实平台/生产数据、长稳、容量、安全、部署灾备、业务验收 | 无豁免；`docs/TEST_REPORT_0.14.0.md` 第 10 节 |

### 0.13.0 SOP 持久执行本地候选验收

| 标准 | 状态 | 验证方法 | 证据 |
|---|---|---|---|
| schema v13 与前向迁移 | 通过 | 新库、v12 活动运行迁移、伪造迁移标记、物理 schema 校验 | `tests/test_migrations.py`；quick_check ok；外键错误 0 |
| 类型化 DSL 与发布评测 | 通过 | 步骤操作互斥、稳定 ID、重试/审批/工具类型约束 | `tests/test_sop_execution.py` |
| 多步运行与并发 | 通过 | 上下文、顺序、单执行者抢占、逐步推进、完成门 | SOP 专项与 ReAct 集成通过 |
| 失败、未知态与恢复 | 通过 | 读取重试/耗尽、写入未知、补偿成功/失败/未知、重启故障注入 | `tests/test_sop_execution.py` |
| 脱敏与可信上下文 | 通过 | 模型参数不得冒充可信上下文；敏感键、号码和深层输出脱敏 | 专项安全断言通过 |
| 管理 API 与后台 | 通过 | 租户/404/409、运行查询、审批/裁决/补偿、页面内对话框真实提交 | 桌面推进 step_02 到 step_03；390px 无页面溢出；0 控制台错误 |
| 全量回归与覆盖率 | 通过 | branch coverage 全量 pytest | 136 passed；全部 `src/` 85%；SOP 85% |
| 离线评测与静态检查 | 通过 | 隔离 eval、compileall、JS、editable 版本 | 20/20；版本 0.13.0；全部检查通过 |
| 运行服务、数据库与性能 | 通过（仅本机/隔离） | health/ready/OpenAPI、SQLite 完整性、300 次只读请求 | schema 13、ready 200；详细 p50/p95/max 见报告 |
| 生产放行 | 阻塞 | 真实淘宝/ERP、客户回放、真实业务补偿、24 小时长稳、异机/设备密钥/RPO-RTO | 无豁免；`docs/TEST_REPORT_0.13.0.md` 第 9 节 |

## 证据索引

- E-20260807-002：M4 D23 独立报告修复候选。删除分类 complaint 强制 handoff、目录候选/高分商品快答与来源拼接话术；规划模型决定语义步骤，执行层只保留安全门，人工批准知识只允许标准化问法完全相等时直返；流式/非流式共用 `prepare_generation`。聚焦 `199 passed / 1 xfailed`、全量 `603 passed / 1 xfailed`，compileall 与 whitespace 通过。冻结 50 例 mock/live gate 均 passed：mock `0.940 / hallucination 0.020 / severe 3`，live `deepseek-v4-flash` `0.900 / 0.020 / severe 1`，商品 `15/15`、投诉 `7/8`、after-sales `8/12`。但泄漏意图回归为 `29/40=72.5%`，投诉平衡回归 recall `45%`，因此 M4 暂不签署；旧浏览器 PNG 降级为 D22 历史证据，D23 页面因 localhost 客户端策略未能形成新截图。证据：`evals/customer_service/runs/20260807-customer-service-{mock,live}.json`、`evals/intent/runs/20260807-m4-{acceptance,complaint-balanced}-post-d034-live.json`、`tests/test_intent_routing*.py`、`tests/test_intent_guardrails.py`、`tests/test_service_stream.py`、`docs/works/13-feature-m4-customer-service/README.md` D23。无新依赖、schema、迁移或非流式 API 字段变化；生产放行不豁免。
- E-20260807-001：M4 智能客服后端本机独立验收。FIX-9 投诉链路同时返回检索证据、共情答复和人工标记；FIX-10 目录快答限定为问题所问且被检索证据支持的事实。冻结 50 例 mock A/B 为 `0.820 / severe 7 / failed → 0.940 / severe 3 / passed`，投诉 `6/8`、商品 `14/15`；真实 `deepseek-v4-flash` 为 `answer_accuracy=0.860 / hallucination_rate=0.060 / refusal_rate=0.067 / severe_failures=4 / gate passed`。D18 低置信度人工兜底及阈值反证使 `simulation-evidence-v1` 达 `18/18`；浏览器 console 0 error/warning；独立验收 `27 passed, 1 xfailed`、全量 `597 passed, 1 xfailed`、compileall 与 whitespace 检查通过。FIX-10 后四条已泄漏场景延迟为 `p50=10.87s / p95=36.51s`，作为非阻塞 P1 残留。证据：`evals/customer_service/runs/20260807-customer-service-{postfix-red-mock,mock,live}.json`、`evals/performance/runs/20260807-m4-latency-post-fix10.json`、`tests/test_m4_acceptance.py`、`tests/test_virtual_store_simulation.py`、`docs/works/13-feature-m4-customer-service/EVIDENCE.md`、`docs/works/13-feature-m4-customer-service/README.md`。没有新增依赖、schema、迁移或非流式 API 变化；生产放行不豁免。
- E-20260731-002：F-124 SSE 流式客服接口。`stream_generate` 只产出 delta；`verify`/`persist` 抽为可复用步骤，图内节点与流式路径共用同一实现；两段式生成后 LangGraph 节点与边零变化；`POST /v1/chat/stream` 事件协议 meta/delta/citations/handoff/done/error，error 后紧跟 done 关流；同一 `Idempotency-Key` 断连重发返回同一 message_id 且不重新调模型；`MODEL_ENABLED=false` 时零外部请求。证据：`tests/test_chat_stream.py`、`tests/test_service_stream.py`、`tests/test_llm.py`、`docs/works/13-feature-m4-customer-service/SSE_EVENT_PROTOCOL.md`。生产放行不豁免。
- E-20260731-001：F-125 会话 Token 预算与生命周期。超长历史截断后 token 不超阈值且保留最近一轮；截断元信息作为 `history_window` 证据进入上下文快照；会话 CRUD 四端点具备鉴权、409、422、404，55 条消息按 limit=20 翻页无重复无遗漏；空闲 121 分钟自动关闭且带未结人工任务的会话不被关闭。反证：`context_budget_ratio` 由 0.7 临时调至 0.99 后保留消息数由 7 升至 9，截断断言如期失败，还原后四项复验通过。定向 16 项、回归 40 项、全量 318 项通过。证据：`tests/test_tokens.py`、`tests/test_context_budget.py`、`tests/test_chat_sessions_api.py`、`tests/test_session_idle.py`、`docs/works/13-feature-m4-customer-service/SESSION_DATA_MODEL_AND_API.md`。生产放行不豁免。
- E-20260730-001：F-311 运营辅助与文案生成模块。CSV/JSON/表单三条录入链路按租户、数据集、日期、渠道幂等写入；五风格小批量文案与确定性模板降级，生成方式显式标记；分析报告统计值由代码计算；501 行数据集报告合计正确不被列表上限截断。门禁双反证：移除注册表模块覆盖映射后 `report["passed"]` 由 True 变 False；将 fixture 坏日期改为合法日期后场景断言失败，两处均已还原。全量 313 项通过。证据：`tests/test_ops_assistant.py`、`tests/test_virtual_store_simulation.py`、`docs/works/12-feature-m5-operations-assistant/README.md` 及 11 张实跑截图。开发数据仅用于本地验收，不构成生产经营结论。

| 证据 ID | 时间 | 方法或命令 | 退出状态 | 版本或文件哈希 | 结果摘要 | 证据位置 | 有效期 |
|---|---|---|---|---|---|---|---|
| E-20260807-002 | 2026-08-07 | D-034 语义边界反例、冻结 50 例 mock/live、两份泄漏意图 live 回归、全量 pytest、compileall、whitespace；浏览器 localhost 受策略拦截 | 客服 gate 0；意图 gate 1；浏览器未完成 | working tree；schema v27 不变 | 聚焦 `199 passed / 1 xfailed`；全量 `603 passed / 1 xfailed`；mock `0.940 / severe 3 / passed`；live `0.900 / severe 1 / passed`；意图 `29/40=72.5%`；投诉 recall `45% / failed`；M4 暂不签署 | `evals/customer_service/runs/20260807-customer-service-{mock,live}.json`；`evals/intent/runs/20260807-m4-*-post-d034-live.json`；`docs/works/13-feature-m4-customer-service/` | M4 代码、provider、意图新留出集或浏览器证据变化前 |
| E-20260807-001 | 2026-08-07 | 冻结 50 例 mock 单变量 A/B、同口径 live、真实延迟剖析、M4 独立验收、18 项场景契约与阈值反证、全量 pytest、compileall、whitespace、隔离浏览器实跑 | 0 | working tree；schema v27 不变 | mock `0.940 / 0.020 / severe 3 / passed`；live `0.860 / 0.060 / severe 4 / passed`；四场景延迟 `p50=10.87s / p95=36.51s`；18/18；27 passed + 1 xfailed；全量 597 passed + 1 xfailed；生产 Gate 不豁免 | `evals/customer_service/runs/20260807-customer-service-{mock,live}.json`；`evals/performance/runs/20260807-m4-latency-post-fix10.json`；`docs/works/13-feature-m4-customer-service/` | M4 代码、fixture、门禁、场景契约或证据变化前 |
| E-20260721-001 | 2026-07-21 | 文档结构、围栏、引用和占位符检查 | 0 | 文件大小 28,911 字节 | 原客服产品技术路线文档交付通过；实施未完成 | `云湃电商AI客服产品技术路线_20260721.md` | 文档内容变化前 |
| E-20260721-002 | 2026-07-21 | 功能表结构、ID、状态和证据路径检查 | 0 | 21 个唯一功能 ID | 功能台账汇总通过；规划功能未验收 | `PROJECT_FEATURES.md` | 功能台账变化前 |
| E-20260721-003 | 2026-07-21 | 文档结构、链接、异常字符和项目一致性检查 | 0 | 文件大小 50,362 字节 | 后台接入与客服接管调研交付通过；真实权限和 PoC 未验收 | `云湃电商后台接入与客服接管执行调研报告_20260721.md` | 文档内容变化前 |
| E-20260721-004 | 2026-07-21 | 32 项 skill 单元测试、`project-to-act --check`、`--validate`、官方快速校验、隔离前向测试和远端 blob 对比 | 0 | config SHA256 `6B418036...BB2E47A`；SKILL SHA256 `CFF9E2B5...F0F77EC`；脚本 SHA256 `444E53EC...50EA6B`；提交 `bdc69fa` | managed schema v1 有效；旧账本仅为受控跳转；源包、安装包和 GitHub 分支逐文件一致 | `.project-to-act/PROJECT_CONFIG.json`、`C:/Users/zzg/.codex/skills/project-to-act/`、分支 `codex/project-to-act-hardening-20260721` | 配置、skill、脚本或管理结构变化前 |
| E-20260721-005 | 2026-07-21 | `py -3.12 -m pytest -q`、隔离 DATA_DIR 离线评测、project-to-act `--validate` | 0 | schema v6、项目 0.5.0 | 淘宝本地 PoC 合并当前运营模块后 53 tests、20/20 eval、managed schema 校验通过；真实淘宝 Gate 未执行 | `src/ecommerce_agent/taobao.py`、`tests/test_taobao.py`、`docs/taobao-customer-service-runbook.md` | 淘宝代码、配置或测试变化前 |
| E-20260721-006 | 2026-07-21 | 32 项 skill 单元测试、官方快速校验、当前项目 `--check`/`--validate`、GitHub 引用与递归 tree/blob 对比 | 0 | 提交 `e05d84dc9a262934c3b5c988e2b2bd76b5c53c3e`；README SHA256 `27F7B490...CC0B3F`；测试 SHA256 `CD6E0099...FCAC0` | Windows 编码兼容修正验证通过；GitHub `main` 与修复分支均指向发布提交；远端 14 个文件与本地逐一一致 | `https://github.com/redmaplewww/project-to-act/commit/e05d84dc9a262934c3b5c988e2b2bd76b5c53c3e`、`F:/opencode/云湃智算/.codex_tmp/project-to-act/` | GitHub 引用或仓库文件变化前 |
| E-20260721-007 | 2026-07-21 | `pytest` 全量回归、`compileall`、隔离 DATA_DIR 离线评测、桌面/窄屏浏览器检查、project-to-act `--validate` | 0 | 0.6.0 关键文件聚合 SHA256 `48C6C2A6858F5A0825AFC43261E780B047CF430355D2AA7287B42B5A1183DC15` | 54 tests、20/20 eval、schema v6、虚拟连接器/仓储/竞品/Agent 工具及架构页通过；真实平台未验收 | `src/ecommerce_agent/connectors/`、`src/ecommerce_agent/business/`、`tests/test_operations_modules.py`、`docs/architecture-inspector.html` | 上述代码、测试或架构页变化前 |
| E-20260721-008 | 2026-07-21 | 官方 API 文档复核、`py -3.12 -m pytest -q -p no:cacheprovider`、隔离 DATA_DIR eval、`compileall`、project-to-act `--validate` | 0 | `taobao.py` SHA256 `523A3155...686824B0`；申请材料 SHA256 `9E0D721A...03D127B6` | 淘宝真实适配已校准为服务市场机器人资格 + OAuth + 奇门入站 + TOP 回写；54 tests、20/20 eval、compileall 通过；真实平台 Gate 未执行 | `src/ecommerce_agent/taobao.py`、`tests/test_taobao.py`、`docs/taobao-api-access-application.md` | 淘宝代码、配置、测试或准入材料变化前 |
| E-20260721-009 | 2026-07-21 | `py -3.12 -m pytest -q -p no:cacheprovider`、隔离 DATA_DIR eval、`compileall`、桌面/390px 浏览器登录与交互、project-to-act `--validate` | 0 | 0.7.0 关键文件聚合 SHA256 `DACB6E0075698D19B2EFA4F9D4D28086D4FB3B3897EF63EF0372F17B4E160649` | 56 tests、20/20 eval；竞品洞察、客服管理聚合 API、后台登录/同步/对话/回放及响应式布局通过；真实平台未验收 | `src/ecommerce_agent/admin.py`、`business/competitive.py`、`docs/admin-console.html`、`tests/test_admin_console.py` | 上述代码、测试或后台页面变化前 |
| E-20260721-010 | 2026-07-21 | branch coverage 全量 pytest、隔离 DATA_DIR eval、`compileall`、JS 语法、editable 安装、桌面/390px 浏览器登录/同步/筛选/像素检查 | 0 | 0.8.0；schema v8 | 67 tests、83% coverage、20/20 eval；商品/订单/库存/竞品回放、指标、工具超时/权限、迁移与后台通过；生产 Gate 未执行 | `docs/TEST_REPORT_0.8.0.md`、`tests/test_catalog_orders_metrics.py`、`tests/test_tools.py`、`tests/test_migrations.py` | 0.8.0 代码、测试、依赖或后台页面变化前 |
| E-20260721-011 | 2026-07-21 | branch coverage 全量 pytest、隔离 DATA_DIR eval、compileall、editable 安装、桌面/390px 浏览器交互、SQLite 备份恢复、本机性能冒烟、project-to-act `--validate` | 0 | 0.9.0；schema v9 | 74 tests、84% source branch coverage、20/20 eval；知识/SOP/质检/客服操作和治理后台本地候选通过；生产 Gate 阻塞 | `docs/TEST_REPORT_0.9.0.md`、`tests/test_governance.py`、`tests/test_taobao.py`、`tests/test_governance_api_errors.py` | 0.9.0 代码、测试、依赖、管理页或 Gate 变化前 |
| E-20260721-012 | 2026-07-21 | branch coverage 全量 pytest、隔离 eval、compileall/JS、editable 版本、运行服务重启与 health/ready/OpenAPI、桌面浏览器、SQLite 恢复、本机性能、project-to-act `--validate` | 0 | 0.10.0；schema v11 | 83 tests、84% source branch coverage、20/20 eval；持久 outbox、worker、崩溃边界、重试/死信/核对、API 和后台桌面候选通过；生产 Gate 阻塞 | `docs/TEST_REPORT_0.10.0.md`、`tests/test_outbox.py`、`tests/test_outbox_api.py`、`tests/test_migrations.py` | 0.10.0 代码、测试、配置、后台或 Gate 变化前 |
| E-20260721-013 | 2026-07-21 | branch coverage 全量 pytest、隔离 eval、compileall/JS、editable 版本、在线/离线加密备份、验证、恢复/回滚/换钥/清理、运行服务重启、性能和 project-to-act `--validate` | 0 | 0.11.0；schema v11 | 99 tests、84% source branch coverage、灾备 85%、20/20 eval；运行锁和完整本地灾备候选通过；生产 Gate 阻塞 | `docs/TEST_REPORT_0.11.0.md`、`tests/test_disaster_recovery.py`、`tests/test_cli.py` | 0.11.0 代码、测试、配置、灾备格式或 Gate 变化前 |
| E-20260721-014 | 2026-07-21 | branch coverage 全量 pytest、隔离 eval、compileall/JS、editable 版本、health/ready/OpenAPI、桌面/390px 浏览器、发布创建/回放/API 审批启用、故障注入和性能冒烟 | 0 | 0.12.0；schema v12 | 111 tests、85% source branch coverage、发布模块 86%、20/20 eval；回放/灰度/自动停止本地候选通过；生产 Gate 阻塞 | `docs/TEST_REPORT_0.12.0.md`、`tests/test_releases.py`、`tests/test_cli.py` | 0.12.0 代码、测试、配置、发布策略或后台变化前 |
| E-20260721-015 | 2026-07-21 | branch coverage 全量 pytest、隔离 eval、compileall/JS、editable 版本、health/ready/OpenAPI、SQLite 完整性、桌面/390px 浏览器真实审批、恢复故障注入和性能冒烟 | 0 | 0.13.0；schema v13 | 136 tests、85% source branch coverage、SOP 85%、20/20 eval；多步执行/补偿/恢复和管理处置本地候选通过；生产 Gate 阻塞 | `docs/TEST_REPORT_0.13.0.md`、`tests/test_sop_execution.py`、`tests/test_react_graph.py`、`tests/test_admin_console.py` | 0.13.0 代码、测试、SOP schema、工具契约或后台变化前 |
| E-20260721-016 | 2026-07-21 | branch coverage 全量 pytest、隔离 eval、JS、editable 版本、health/ready/OpenAPI、SQLite 完整性、worker、桌面/390px 浏览器真实告警处置和性能验证 | 0 | 0.14.0；schema v14 | 142 tests、85% source branch coverage、竞品 91%、20/20 eval；策略/告警/幂等重评/worker/Agent/API/后台本地候选通过；生产 Gate 阻塞 | `docs/TEST_REPORT_0.14.0.md`、`tests/test_competitive_monitoring.py`、`tests/test_migrations.py`、`tests/test_admin_console.py` | 0.14.0 代码、测试、竞品 schema/策略/告警、worker、Agent 工具或后台变化前 |
| E-20260722-001 | 2026-07-22 | branch coverage 全量 pytest、隔离 eval、compileall/JS、editable 版本、health/ready/OpenAPI、SQLite 完整性、桌面/390px 浏览器真实证据查看和性能验证 | 0 | 0.15.0；schema v15 | 149 tests、85% source branch coverage、ContextBuilder 93%、数据库 94%、留存 100%、20/20 eval；不可变上下文/冲突降级/ReAct 证据/API/后台/留存本地候选通过；生产 Gate 阻塞 | `docs/TEST_REPORT_0.15.0.md`、`tests/test_context_builder.py`、`tests/test_react_graph.py`、`tests/test_privacy_metrics.py` | 0.15.0 代码、测试、context.v1/schema、Graph、留存或后台变化前 |
| E-20260722-002 | 2026-07-22 | branch coverage 全量 pytest、渠道故障/并发专项、隔离 eval、compileall/JS、editable 版本、health/ready/OpenAPI、真实 HTTP Qimen shadow、SQLite 完整性、桌面/390px 浏览器账本/证据和性能验证 | 0 | 0.16.0；schema v16 | 161 tests、全项目 89%/源码 85% branch coverage、渠道 Agent 85%、数据库 95%、ContextBuilder 93%、竞品 91%、20/20 eval；持久入站/幂等/租约/四模式/异步熔断/API/后台本地候选通过；生产 Gate 阻塞 | `docs/TEST_REPORT_0.16.0.md`、`tests/test_channel_agent.py`、`tests/test_releases.py`、`tests/test_migrations.py`、`tests/test_admin_console.py` | 0.16.0 代码、测试、schema、渠道/发布/outbox 契约或后台变化前 |
| E-20260722-003 | 2026-07-22 | branch coverage 全量 pytest、竞品实体/并发/迁移/API 专项、隔离 eval、compileall/JS、editable 版本、health/ready/OpenAPI、SQLite 完整性、桌面浏览器真实批准/撤销、限流和性能验证 | 0 | 0.17.0；schema v17 | 171 tests、全项目 89%/源码 85% branch coverage、竞品 87%、数据库 95%、20/20 eval；同款评分/裁决/内容口碑/Agent 门禁/API/后台本地候选通过；移动实机和生产 Gate 阻塞 | `docs/TEST_REPORT_0.17.0.md`、`tests/test_competitive_entity_intelligence.py`、`tests/test_competitive_monitoring.py`、`tests/test_migrations.py`、`tests/test_admin_console.py` | 0.17.0 代码、测试、竞品 schema/评分/裁决/信号/Agent 门禁或后台变化前 |
| E-20260722-004 | 2026-07-22 | branch coverage 全量 pytest、评测版本/隐私/多轮/基线/并发/迁移/发布/API/篡改/恢复专项、隔离 eval、compileall/JS、editable 版本、health/ready/OpenAPI、SQLite 完整性、桌面浏览器失败/修订/通过和性能验证 | 0 | 0.18.0；schema v18 | 184 tests、全项目 89%/源码 85% branch coverage、评测 85%、数据库 95%、发布 87%、20/20 eval；版本化客户评测/实际 Agent 隔离/回归/发布/API/后台本地候选通过；移动实机和生产 Gate 阻塞 | `docs/TEST_REPORT_0.18.0.md`、`tests/test_customer_evaluations.py`、`tests/test_migrations.py`、`tests/test_releases.py`、`tests/test_admin_console.py` | 0.18.0 代码、测试、评测 schema/runner/指标、发布契约或后台变化前 |
| E-20260722-005 | 2026-07-22 | branch coverage 全量 pytest、人工接管并发/迁移/API/worker/策略负向专项、实际 Agent 语义 Gate、compileall/JS、editable 版本、health/ready/OpenAPI、SQLite 完整性、桌面/390px 浏览器全处置和队列策略 | 0 | 0.19.0；schema v19 | 195 tests、全项目 89.45%/源码 85.63% coverage、handoff 87.94%、数据库 94.93%、20/20 实际 Agent；队列/SLA/事件/worker/API/后台本地候选通过；真实业务和生产 Gate 阻塞 | `docs/TEST_REPORT_0.19.0.md`、`tests/test_handoff_workbench.py`、`tests/test_agent.py`、`tests/test_migrations.py`、`tests/test_admin_console.py` | 0.19.0 代码、测试、handoff schema/路由/SLA/worker、高风险门或后台变化前 |
| E-20260722-006 | 2026-07-22 | branch coverage 全量 pytest、坐席租约/重启/资格/容量/分配/迁移/API 负向专项、实际 Agent Gate、compileall/JS、editable 版本、health/ready/OpenAPI、SQLite 完整性、桌面/390px 浏览器配置/在线/转人工/智能分配/历史 | 0 | 0.20.0；schema v20 | 203 tests、全项目 89.65%/源码 85.86% coverage、staffing 96%、handoff 88%、数据库 95%、20/20 实际 Agent；坐席调度/智能分配/API/后台本地候选通过；真实排班、渠道和生产 Gate 阻塞 | `docs/TEST_REPORT_0.20.0.md`、`tests/test_handoff_staffing.py`、`tests/test_handoff_workbench.py`、`tests/test_migrations.py`、`tests/test_admin_console.py` | 0.20.0 代码、测试、staffing schema/资格/排序/租约或后台变化前 |
| E-20260722-007 | 2026-07-22 | branch coverage 全量 pytest、排班/心跳/手工与自动隔离/作业租约/崩溃恢复/并发/告警/重试/API/worker/迁移负向专项、实际 Agent Gate、compileall/JS、health/ready/OpenAPI、桌面/390px 浏览器闭环 | 0 | 0.21.0；schema v21 | 215 tests、全项目 89%/源码 86% branch coverage、dispatch 86%、staffing 87%、handoff 88%、数据库 95%、20/20 实际 Agent；排班/心跳/自动派单/告警/API/后台本地候选通过；真实周期排班、渠道和生产 Gate 阻塞 | `docs/TEST_REPORT_0.21.0.md`、`tests/test_handoff_dispatch.py`、`tests/test_handoff_staffing.py`、`tests/test_migrations.py`、`tests/test_admin_console.py` | 0.21.0 代码、测试、schema/派单/心跳/排班或后台变化前 |
| E-20260722-008 | 2026-07-22 | branch coverage 全量 pytest、13 场景/模块覆盖/幂等/API 专项、默认模型关闭 CLI/HTTP 双重放、实际 Agent Gate、compileall/JSON、editable 版本、health/ready/OpenAPI、SQLite 完整性、桌面/390px 浏览器 | 0 | 0.22.0；schema v21 | 218 tests、全项目 90%/源码 86% branch coverage、simulation 95%、simulation API 100%、20/20 实际 Agent；13/13 场景、7/7 available 模块、两次幂等重放、冻结评测隔离和后台观察通过；营销/利润与生产 Gate 阻塞 | `docs/TEST_REPORT_0.22.0.md`、`docs/VIRTUAL_STORE_SIMULATION_0.22.0.md`、`tests/test_virtual_store_simulation.py`、`tests/test_agent.py`、`src/ecommerce_agent/simulation.py` | 0.22.0 代码、fixture、模块注册表、测试、API 或后台变化前 |
| E-20260722-009 | 2026-07-22 | branch coverage 全量 pytest、逐场景证据/API/兼容专项、隔离 eval、compileall/JS/JSON、editable 版本、真实 HTTP 大响应、health/ready/OpenAPI、SQLite 完整性、1280/390px 浏览器运行/筛选/明细 | 0 | 0.22.1；schema v21 | 218 tests、源码 86% branch coverage、simulation 94%、API 100%、20/20 eval；13/13 和 110,780-byte 输出、完整输入/断言/输出、后台无溢出且 console 0 error/warning；生产 Gate 阻塞 | `docs/TEST_REPORT_0.22.1.md`、`docs/VIRTUAL_STORE_EVIDENCE_0.22.1.md`、`tests/test_virtual_store_simulation.py`、`tests/test_admin_console.py`、`src/ecommerce_agent/simulation.py`、`docs/admin-console.html` | 0.22.1 证据契约、fixture、模拟器、API、测试或场景验收页面变化前 |
| E-20260722-010 | 2026-07-22 | `pytest -q`、管理员免登录/远端拒绝/客户认证专项、compileall、JS 语法、CLI 外网监听拒绝、8104 HTTP health/ready/API、桌面浏览器登录层与 console 检查 | 0 | 0.22.1；schema v21；关键文件聚合 SHA256 `A9D1B24E...794C5B0C` | 219 tests；本机无管理员请求头 overview 200，非回环测试 403，客户无凭据 chat 401，ready 200；页面登录层隐藏、退出按钮隐藏、显示“本机免登录”、无横向溢出且 console 0 error/warning | `src/ecommerce_agent/config.py`、`src/ecommerce_agent/auth.py`、`src/ecommerce_agent/api.py`、`src/ecommerce_agent/cli.py`、`docs/admin-console.html`、`tests/test_admin_console.py` | 管理认证配置、API 依赖、CLI 监听保护、后台启动逻辑或测试变化前 |
| E-20260722-011 | 2026-07-22 | `pytest -q`、schema v22/来源分类/后台 scope/派单 scope/模拟场景专项、compileall、JS、20/20 eval、8104 health/ready/API、HTTP 场景验收、Edge 页面验证 | 0 | 0.22.2；schema v22；关键文件 SHA256 `database.py=6B1F4BF3...`, `simulation.py=5BCD373D...`, `admin-console.html=27F3C311...` | 221 tests；20/20 eval；HTTP 场景 13/13、442,944 bytes；默认运营 0 会话/消息/人工任务，模拟 17 会话/34 消息/13 人工任务；智能客服来源/决策/证据可见；场景页无 console error/warning | `docs/TEST_REPORT_0.22.2.md`、`tmp/pytest-full-0.22.2-20260722-221156.out.log`、`tmp/admin-0.22.2-service-simulation.png`、`tmp/admin-0.22.2-simulation-evidence.png`、`tests/test_admin_console.py`、`tests/test_virtual_store_simulation.py` | 0.22.2 代码、schema、后台 scope、来源分类、模拟器、测试或页面变化前 |
| E-20260723-001 | 2026-07-23 | `py -3.12 -m pytest -q`、定向 API、compileall、页面 JS、8104 health/ready/HTTP 与浏览器页面实测 | 0 | 0.22.3；schema v22；SHA256 `api.py=98E6DB78...`, `customer_test_api.py=FA6F3C97...`, `schemas.py=F75EBB21...`, `config.py=4ACCA93C...`, `customer-test.html=6E277C3B...`, `test_api.py=DF73FC45...` | 223 tests；测试案例 5 个；实际 POST 返回 `local_customer_simulation`/`simulation`；默认运营会话 0；浏览器显示实际回答和来源，console error/warning 0 | `docs/TEST_REPORT_0.22.3.md`、`docs/CUSTOMER_TEST_0.22.3.md`、`tests/test_api.py`、`src/ecommerce_agent/customer_test_api.py`、`docs/customer-test.html`、`tmp/customer-test-0.22.3.png` | 本机测试接口、认证/回环边界、页面或来源隔离变化前 |
| E-20260723-002 | 2026-07-23 | `py -3.12 -m pytest -q`、后台/API 定向测试、页面 JS 与浏览器原后台实测 | 0 | 0.22.4；schema v22 | 223 tests；原“智能客服 -> 对话测试”无客户端 ID/主体/密钥输入；浏览器保修案例实际返回 12 个月保修、风险/来源可见，测试会话进入 simulation；正式 `/v1/chat` 客户认证未改变 | `docs/TEST_REPORT_0.22.4.md`、`docs/ADMIN_CUSTOMER_TEST_0.22.4.md`、`docs/admin-console.html`、`tests/test_admin_console.py`、`tests/test_api.py` | 本机测试接口、认证/回环边界、后台测试页面或来源隔离变化前 |
| E-20260723-003 | 2026-07-23 | 模型网关/决策/图/API/后台定向测试、226 项全量回归、健康检查、浏览器原后台实测 | 0 | 0.22.5；schema v22 | 23 项定向测试和 226 项全量测试；Coding Plan 以标准 Chat Completions 非流式调用 `glm-4.7`；页面保修/发货问题得到真实回答；审计包含 `deliberate:model:answer`、`generate:model`、`verify:passed` | `docs/TEST_REPORT_0.22.5.md`、`docs/glm-integration.md`、`src/ecommerce_agent/llm.py`、`src/ecommerce_agent/decision.py`、`scripts/start-glm-coding-test.ps1` | 模型端点、结构化输出边界、测试页面或凭据装载方式变化前 |
| E-20260723-004 | 2026-07-23 | 后台静态结构/API 定向测试、桌面与 390px 浏览器页面实测、控制台检查 | 0 | 0.22.6；schema v22 | 7 项定向测试通过；主内容、概览长列表、客服会话、消息、表格和测试结果均有受限尺寸与内部滚动；390px 无页面级横向溢出，console error/warning 为 0 | `docs/TEST_REPORT_0.22.6.md`、`docs/admin-console.html`、`tests/test_admin_console.py`、`tests/test_api.py` | 后台 HTML/CSS、相关测试或响应式断点变化前 |
| E-20260723-005 | 2026-07-23 | schema v23、15 个虚拟场景、营销/财务 API、后台/API 定向回归、页面 JS 解析 | 0 | 0.23.0；schema v23 | 15/15 虚拟场景通过；D14 验证投放诊断、事实检查与不可发布草稿；D15 验证管理利润、费用归集、对账任务和 Agent 工具；10 项重点回归通过 | `docs/TEST_REPORT_0.23.0.md`、`business/marketing.py`、`business/finance.py`、`tests/test_marketing_finance_api.py`、`tests/test_virtual_store_simulation.py` | 营销/利润模块、场景、API、后台页面或版本契约变化前 |
| E-20260727-006 | 2026-07-27 | 后台页面结构（适配器/灰度面板、夜间与 SOP 白名单表单控件、加载端点引用）、双适配器目录 API、夜间策略经 API 创建并回显、灰度列表空态、页面 JS 解析、浏览器渲染 | 0 | 0.30.0；schema v25 | /admin 含 adapterRows/rolloutRows/releaseNight*/releaseSopAllowlist 与三个数据端点引用；mockchat+taobao 同时出现在适配器目录；夜间策略创建后 night_mode/sop_allowlist 正确回显；5 项后台测试与 JS 解析通过 | `docs/admin-console.html`、`tests/test_admin_console.py` | 后台页面结构、加载端点或发布策略字段变化前 |
| E-20260727-005 | 2026-07-27 | 夜间字段联合校验、生效模式（含跨零点）、SOP 白名单 key/ID 双匹配与降级、mockchat 窗口内 send/窗口外 draft 端到端、v24→v25 迁移与漂移库校验保持、发布/渠道/CLI/灾备回归 | 0 | 0.29.0；schema v25 | 22:00–07:00 窗口在 23:30 与 06:59 生效、12:00 不生效；未列入 SOP 记 sop_not_allowlisted 严重违规并 handoff，列入 key 放行；窗口内任务 action=send 且 release_mode=automatic，窗口外 draft + 人工确权；84 项测试全绿 | `src/ecommerce_agent/releases.py`、`src/ecommerce_agent/database.py`、`tests/test_night_watch_release.py`、`tests/test_migrations.py` | 策略字段、时间窗语义、白名单匹配或 assignment 契约变化前 |
| E-20260727-004 | 2026-07-27 | 商品实体识别/排序、稳定证据 ID、对比差异表、店铺与租户隔离、bundle/evidence 嵌入、chat 快照携带、上下文/Agent/图回归 | 0 | 0.28.0；schema v24 | 蓝牙耳机两 SKU 命中且保温杯不误报；证据 ID 携带 catalog 行与版本且重复调用稳定；对比仅在意图出现且 ≥2 候选时输出，差异集 {续航,降噪} 正确；跨店铺/跨租户零命中；4 + 36 项测试全绿 | `src/ecommerce_agent/product_advisor.py`、`src/ecommerce_agent/context_builder.py`、`tests/test_product_advisor.py` | 匹配打分、证据 ID 格式、bundle 结构或 catalog 表变化前 |
| E-20260727-003 | 2026-07-27 | SOP 灰度分桶解析/run 固定/回滚不换版、调量与原子完成、单活动约束、管理 API 鉴权/409、治理/SOP 执行/图回归 | 0 | 0.27.0；schema v24 | 50% 灰度 in-bucket 会话解析并固定 v2、out-bucket 保持 v1；回滚后已固定运行仍 v2、新会话回 v1；complete 后定义指针推进到候选且基线退役；跨租户不可见；3 + 39 项测试全绿 | `src/ecommerce_agent/sops.py`、`src/ecommerce_agent/governance_api.py`、`tests/test_sop_rollout.py` | SOP 版本状态机、灰度表结构、分桶公式或解析逻辑变化前 |
| E-20260727-002 | 2026-07-27 | 知识灰度全生命周期（begin/调量/complete/rollback）、稳定分桶检索仲裁、chat 会话分桶一致性、评测/进化路径基线固定、v23→v24 迁移与唯一活动约束、管理 API 鉴权/409、治理/检索/Agent/CLI/灾备回归 | 0 | 0.26.0；schema v24 | 50% 灰度下 in-bucket 会话得候选、out-bucket 与无单元调用得基线；调量 100% 全量、回滚后候选保持 candidate 可再灰度；complete 原子退役基线并激活候选；跨租户不可见；18 + 43 项测试全绿 | `src/ecommerce_agent/rollouts.py`、`src/ecommerce_agent/knowledge_management.py`、`src/ecommerce_agent/rag.py`、`tests/test_knowledge_rollout.py`、`tests/test_migrations.py` | 灰度表结构、分桶公式、检索仲裁、知识生命周期或管理 API 变化前 |
| E-20260727-001 | 2026-07-27 | 多消息类型信封（归一化 kind/占位符落库/去重/回放）、非文本运行时转人工（淘宝+mockchat 双适配器）、context checkpoint 前白名单对抗敌意载荷、跨店铺会话不合并契约、落库器跨租户隔离与跨租户读取拒绝、渠道相关 61 项回归、全量 pytest、channel_sdk branch coverage | 0 | 0.25.0；schema v23 | 非文本入站不再拒收：记录事件 + 脱敏占位符 + 强制人工确权（assigned `agent-unsupported-media`），零 Agent invocation/外发；文本路径行为不变；context snapshot `current_subject` 为空且敌意 order 字段不出现在 bundle；同一外部会话跨店铺产生独立会话/事件/任务；channel_sdk 各模块分支覆盖 90–100%；全量 261 通过，14 项为随 PR #4 修复的既有 schema 期望失败 | `src/ecommerce_agent/channel_sdk/`、`src/ecommerce_agent/channel_agent.py`、`tests/test_channel_sdk_contract.py`、`tests/test_channel_sdk_runtime.py` | 信封 kind 契约、非文本处置策略、context 白名单、跨租户/店铺键或双适配器实现变化前 |
| E-20260726-001 | 2026-07-26 | 渠道适配器 SDK 契约（能力版本/验签/防重放/标准信封/上下文/去重/回放/发送/回执/错误分类/限流声明与执行）双适配器测试、跨渠道运行时/注册表/平台隔离/API 专项、渠道相关 71 项回归、全量 pytest、channel_sdk branch coverage | 0 | 0.24.0；schema v23 | 14 项契约用例 × taobao/mockchat 全通过；mockchat 采用不同验签协议仍复用同一入站落库/草稿/归属/outbox；`ChannelAgentRuntime` 经注册表处理 mockchat 任务（send/handoff/blocked/rejected）；outbox claim 平台隔离；`GET /v1/channels/adapters` 鉴权可用；channel_sdk 各模块分支覆盖 90–100%；全量 252 通过，另 14 项为 schema v23 后 migrations/backup 期望未同步的既有失败（独立任务修复，与本功能无关） | `src/ecommerce_agent/channel_sdk/`、`tests/test_channel_sdk_contract.py`、`tests/test_channel_sdk_runtime.py`、`src/ecommerce_agent/channel_agent.py`、`src/ecommerce_agent/outbox.py` | channel_sdk 契约、信封/回执/错误分类、注册表、入站落库器、outbox 平台隔离或双适配器实现变化前 |
| E-HIST-001 | 历史记录 | 项目测试与离线评测 | 历史通过 | `0.5.0` | 47 tests、20/20 offline eval | `docs/project-ledger.md`、`tests/` | 代码变化后过期，完成声明前必须重跑 |

## Gate 记录

| Gate ID | 日期 | Gate | 对象 | 结果 | 证据 ID | 豁免与确认人 |
|---|---|---|---|---|---|---|
| G-ADMIN-LOCAL-001 | 2026-07-22 | 本机管理员免登录 | 显式配置、回环限制、CLI 监听保护、客户认证隔离、后台自动进入 | 通过 | E-20260722-010 | 仅限当前本机开发实例；不构成生产认证豁免，外网部署必须恢复 `ADMIN_AUTH_REQUIRED=true` |
| G-SIMULATION-EVIDENCE-001 | 2026-07-22 | 逐场景输入输出证据 | 13 项固定输入/预期、逐项断言、完整领域/Agent/工具输出和兼容字段 | 通过 | E-20260722-009 | 仅限显式 virtual 数据；不豁免真实数据、模型、渠道、竞品源或生产 Gate |
| G-CONSOLE-016 | 2026-07-22 | 场景验收后台 | 手动运行、筛选、模块覆盖、JSON 明细、1280/390px 和移动详情定位 | 通过 | E-20260722-009 | 页面真实显示本地模拟调用，但不等于真实店铺验收 |
| G-SIMULATION-001 | 2026-07-22 | 关联虚拟店铺跨模块验收 | 领域服务导入、13 个需求、7 个 available 模块、冻结评测、幂等重放、API 和审计 | 通过 | E-20260722-008 | 仅限显式 virtual 数据和本地单机；不豁免营销/利润、真实客户/模型/渠道/竞品源、长稳和生产 Gate |
| G-CONSOLE-015 | 2026-07-22 | 虚拟店铺经营后台观察 | 6 商品、10 库存、8 订单/售后、竞品质量/告警/内容、客服会话/派单、审计、桌面和 390px | 通过 | E-20260722-008 | 复用当前后台功能观察模拟数据；不等于真实运营数据或目标设备业务验收 |
| G-DISPATCH-001 | 2026-07-22 | 值守排班与持久自动派单本地候选 | automatic/manual、UTC 班次、session/心跳、job 租约/恢复/退避/失败、告警和 health/ready | 通过 | E-20260722-007 | 仅限单机本地/虚拟链路；不豁免真实周期排班、渠道、长稳、容量、时钟故障和生产 Gate |
| G-CONSOLE-014 | 2026-07-22 | 自动派单管理后台 | 班次、心跳、作业/重试、告警/确认、并发冲突、恢复、桌面和 390px | 通过 | E-20260722-007 | 可控浏览器视口真实提交通过；不豁免真实坐席运营数据和目标移动设备 |
| G-STAFFING-001 | 2026-07-22 | 坐席调度与智能分配本地候选 | 凭据/档案、TTL 租约、队列技能、全局/队列容量、统一资格和稳定自动分配 | 通过 | E-20260722-006 | 仅限单机本地/虚拟链路；不豁免真实组织排班、在线心跳、业务 SLA、长稳和生产 Gate |
| G-CONSOLE-013 | 2026-07-22 | 坐席调度后台 | 坐席配置、暂离/上线、队列可用数、智能分配、负载、审计、桌面和 390px | 通过 | E-20260722-006 | 可控浏览器视口真实提交通过；不豁免真实坐席运营数据和目标移动设备 |
| G-HANDOFF-001 | 2026-07-22 | 人工接管队列与 SLA 本地候选 | 队列/路由、优先级、并发认领、容量、状态机、转派/升级、SLA worker 和事件历史 | 通过 | E-20260722-005 | 仅限单机本地/虚拟链路；不豁免真实渠道、业务排班/SLA、长稳、容量和生产 Gate |
| G-CONSOLE-012 | 2026-07-22 | 人工接管后台 | KPI/筛选、认领到完成、历史、队列策略、SLA 扫描、桌面和 390px | 通过 | E-20260722-005 | 可控浏览器视口真实提交通过；不豁免真实坐席运营数据和目标移动设备 |
| G-CUSTOMER-EVAL-001 | 2026-07-22 | 版本化客户 Agent 评测本地候选 | 脱敏 suite/case、完整性、实际多轮 Agent、指标/Gate、基线、幂等/恢复和发布关联 | 通过 | E-20260722-004 | 仅限本地/合成与内置样本；不豁免真实客户标注、真实模型、业务阈值、长稳和生产 Gate |
| G-CONSOLE-011 | 2026-07-22 | 客服评测工作台 | 创建、原子换版、冻结、修订、运行、基线、发布选择、失败和 Gate 详情 | 部分通过 | E-20260722-004 | 1280px 真实失败/修订/通过闭环且无全局溢出；390px/移动实机仍待复验 |
| G-COMP-ENTITY-001 | 2026-07-22 | 竞品可信实体与内容证据本地候选 | 可解释同款、人工裁决、价格/卖点/脱敏聚合口碑、approved-only 监控与 Agent 门禁 | 通过 | E-20260722-003 | 仅限本地/人工/虚拟与测试许可来源；不豁免真实来源许可、客户标注集、误报漏报、口碑代表性、长稳和生产 Gate |
| G-CONSOLE-010 | 2026-07-22 | 竞品质量工作台 | 匹配解释、状态筛选、页面内批准/撤销、监控门、价格/口碑证据和 1280px 无全局溢出 | 部分通过 | E-20260722-003 | 桌面真实交互通过；本轮视口控制未切换，390px/移动实机仍待复验 |
| G-CHANNEL-AGENT-001 | 2026-07-22 | 持久渠道 Agent 本地候选 | 事务入站、租约/恢复、调用幂等、四模式、精确事件、异步投递熔断和影子零副作用 | 通过 | E-20260722-002 | 仅限本地/mock/模拟 Qimen；不豁免真实渠道、真实模型、长稳、容量和生产 Gate |
| G-CONSOLE-009 | 2026-07-22 | 渠道 Agent 运行后台 | 队列 KPI、运行账本、证据、手工领取、桌面和 390px | 通过 | E-20260722-002 | 本地隔离服务真实查看通过；不豁免真实客服运营数据和移动设备实机 |
| G-CONTEXT-001 | 2026-07-22 | 可信上下文客服 Agent 本地候选 | 固定装配、不可变父子快照、冲突/权限门、工具证据、完整性和留存 | 通过 | E-20260722-001 | 仅限本地/mock/虚拟事实；不豁免真实渠道、业务工具、客户回放和生产 Gate |
| G-CONSOLE-008 | 2026-07-22 | 客服证据工作台 | 消息快照摘要、证据/冲突/父链/校验和、桌面和 390px | 通过 | E-20260722-001 | 本地隔离服务真实查看通过；不豁免真实客服运营数据和移动设备实机 |
| G-COMP-MON-001 | 2026-07-21 | 竞品监控 Agent 本地候选 | 策略、三类告警、幂等/并发、状态机、租户 worker 和 Agent 证据 | 通过 | E-20260721-016 | 仅限授权/人工/虚拟数据；不豁免真实平台、生产样本、长稳和业务误报漏报验收 |
| G-CONSOLE-007 | 2026-07-21 | 竞品监控后台 | 策略乐观锁、全量评估、告警队列、页面内确认/解决、桌面和 390px | 通过 | E-20260721-016 | 本地隔离服务真实提交通过；不豁免移动设备实机和真实运营数据 |
| G-SOP-EXEC-001 | 2026-07-21 | SOP 持久执行本地候选 | DSL、步骤账本、审批、重试、未知态、补偿和恢复 | 通过 | E-20260721-015 | 仅限本地/虚拟工具；不豁免真实业务工具、平台读回、客户回放和生产 Gate |
| G-CONSOLE-006 | 2026-07-21 | SOP 运行与恢复后台 | 运行列表、乐观锁、页面内处置、桌面和 390px | 通过 | E-20260721-015 | 本地隔离服务真实提交通过；不豁免真实渠道数据和移动设备实机 |
| G-RELEASE-001 | 2026-07-21 | 发布门禁本地候选 | 隔离回放、双人审批、稳定分桶、四级模式、观测和自动暂停 | 通过 | E-20260721-014 | 仅限本地/mock/虚拟渠道；不豁免客户数据、真实模型、真实渠道 shadow 和生产停止证据 |
| G-CONSOLE-005 | 2026-07-21 | 发布门禁管理后台 | 策略/回放/运行 KPI/复核员、桌面和 390px | 通过（原生弹窗点击除外） | E-20260721-014 | 浏览器控制自动取消 prompt；API 转换和最终回显通过，目标设备人工点击仍待复验 |
| G-DR-001 | 2026-07-21 | 单机加密灾备本地候选 | 运行锁、双库快照、认证加密、验证、恢复/回滚、换钥和保留 | 通过 | E-20260721-013 | 仅限本机文件系统；不豁免 24 小时长稳、故障注入、异机介质、设备密钥和业务 RPO/RTO Gate |
| G-OUTBOX-001 | 2026-07-21 | 可靠发送本地候选 | 持久加密 outbox、租约 worker、重试/死信/核对和状态一致性 | 通过 | E-20260721-012 | 仅限本地/模拟平台；不豁免真实平台、长稳、灾备或多节点 Gate |
| G-CONSOLE-004 | 2026-07-21 | 可靠发送管理后台 | worker 状态、积压 KPI、队列表、执行和人工核对 | 部分通过 | E-20260721-012 | 桌面通过；本轮移动视口工具未生效，移动实机仍待复验 |
| G-GOVERNANCE-001 | 2026-07-21 | Agent 治理本地候选 | 分层知识、SOP、质检/VOC、客服操作闭环和投递状态 | 通过 | E-20260721-011 | 仅限本地/虚拟数据；不豁免真实渠道、完整 SOP 执行、语义 VOC 或生产 Gate |
| G-CONSOLE-003 | 2026-07-21 | 治理与渠道工作台 | 知识/SOP 生命周期、质检复核、渠道接待和响应式交互 | 通过 | E-20260721-011 | 仅代表本地管理界面和 API；真实渠道无会话时为空态 |
| G-PROD-001 | 2026-07-22 | 生产放行 | 真实平台连接、生产数据和自动业务动作 | 阻塞 | E-20260722-008 | 无；虚拟店铺 13 场景及既有自动派单、坐席调度、队列/SLA、评测、竞品、渠道 Agent、可信上下文、SOP 和发布门禁本地候选已实现，但营销/利润、真实客户标注/模型、真实授权数据/渠道/工具、业务周期排班/SLA、24/72 小时长稳、容量、安全、异机灾备和业务验收未完成；虚拟通过不构成生产证据 |
| G-ACCESS-001 | 2026-07-21 | 平台接入启动 | 后台数据与客服通道 PoC | 阻塞 | E-20260721-003 | 无；专项权限、测试店铺和对接 Owner 未确认 |
| G-GOV-001 | 2026-07-21 | 项目治理配置 | 唯一事实源、schema 和兼容跳转 | 通过 | E-20260721-004 | 无；只代表治理结构通过，不代表产品或淘宝 PoC 通过 |
| G-SKILL-PUBLISH-001 | 2026-07-21 | project-to-act 发布 | GitHub 默认分支、修复分支和本地发布内容 | 通过 | E-20260721-006 | 无；只代表 skill 发布通过，不代表电商产品或淘宝真实接入通过 |
| G-TAOBAO-LOCAL-001 | 2026-07-21 | 淘宝连接器本地 PoC | OAuth、验签、幂等、人工接管、回写构造和能力门禁 | 通过 | E-20260721-008 | 只通过模拟平台和官方契约复核；不豁免真实 App/店铺/消息 Gate |
| G-TAOBAO-LIVE-001 | 2026-07-21 | 淘宝真实客服接管 | 测试店铺真实入站、人工回写、重放和回滚 | 阻塞 | E-20260721-008 | 无；等待客服机器人资格、奇门场景、平台专属凭证和测试资源 |
| G-MODULE-001 | 2026-07-21 | 业务模块首批切片 | Connector SDK、虚拟淘宝、仓储、竞品、经营工具和架构页 | 通过 | E-20260721-007 | 仅限本地虚拟/人工数据；不豁免真实平台、商品订单指标和生产 Gate |
| G-CONSOLE-001 | 2026-07-21 | 经营与客服管理 V1 | 竞品洞察、客服管理 API、后台工作台和审计 | 通过 | E-20260721-009 | 仅限本地/虚拟数据；不豁免真实渠道、知识/SOP/质检完整管理和生产 Gate |
| G-MODULE-002 | 2026-07-21 | 经营事实与受控指标候选 | 商品、订单/物流/售后、库存、竞品、指标、五个工具和 schema v8 | 通过 | E-20260721-010 | 仅限本地/虚拟数据；不豁免真实平台、真实模型、客户数据或生产 Gate |
| G-CONSOLE-002 | 2026-07-21 | 后台商品与订单扩展 | 商品库存、订单售后、指标和响应式交互 | 通过 | E-20260721-010 | 仅代表本地管理界面；客服完整操作闭环仍未验收 |

## 验收记录

- 2026-07-23：验收 0.23.0 营销与利润模块本机候选。schema v23 新增可追溯营销日指标、内容草稿、来源费用、结算单和对账任务；投放诊断和利润/对账 Agent 工具均为只读，内容禁止直接发布，差异任务仅能由人工流转。虚拟店铺扩展为 D01-D15，D14/D15 显示实际调用输入、断言和领域输出。15/15 场景与营销/财务 API 回归通过，后台/API 重点回归 10 项及页面 JS 解析通过；全量测试命令在 120 秒上限内未结束，未据此声明全量通过。结论：本机候选通过；真实广告/财务数据、竞价预算、总账税务、资金与生产 Gate 不豁免。完整证据见 `docs/TEST_REPORT_0.23.0.md`。
- 2026-07-23：验收 0.22.6 管理后台视觉优化。主内容区宽度、概览卡片与活动列表、表格、智能客服会话/消息、测试结果和移动端控件均设置稳定尺寸及内部滚动；390px 下导航保留横向滑动并隐藏原生滚动条。后台/API 定向 7 项测试、桌面与移动端浏览器实测通过，console error/warning 为 0。结论：本机界面候选通过；模型、认证、数据隔离和生产 Gate 不豁免。完整证据见 `docs/TEST_REPORT_0.22.6.md`。
- 2026-07-23：验收 0.22.4 原后台智能客服顾客直测候选。将对话测试从正式 `/v1/chat` 改为默认关闭、仅回环的 `/v1/test/customer-chat`，删除客户端 ID、主体和密钥输入；保修、发货、转人工案例和手输消息携带演示店铺上下文，实际响应展示风险、接管、会话/追踪和来源，并自动记录到 simulation。完整 223 项测试、页面 JS 及浏览器保修案例实测通过；正式客户 API 仍要求认证。结论：本机候选通过；生产 Gate 不豁免。完整证据见 `docs/TEST_REPORT_0.22.4.md`。
- 2026-07-23：验收 0.22.5 GLM Coding Plan 标准接口本机测试。显式开关启用后，`/api/coding/paas/v4` 通过 `/chat/completions` 和 `stream=false` 接入；修复 SSE 超时及 `arguments: null`/`missing_fields: null` 的模型输出兼容。原后台页面实际得到保修与发货回答，审计证明模型决策、生成和校验链路均已执行。结论：本机测试候选通过；生产 Gate 不豁免。完整证据见 `docs/TEST_REPORT_0.22.5.md`。

按时间倒序追加：日期、检查范围、命令或方法、结果、证据位置、结论。失败与跳过也必须如实记录。

- 2026-07-23：验收 0.22.3 本机顾客对话测试候选。`CUSTOMER_TEST_ENABLED` 默认关闭；启用后 `/customer-test` 和 `/v1/test/customer-chat*` 均只接受回环客户端，并使用 bootstrap client 复用实际 Agent 调用。5 个静态案例及自定义输入展示真实回答、意图、风险、来源、转人工和原始 JSON；所有测试会话固定为 `simulation/local-customer-test`，默认运营范围不统计。最终 223 项全量测试、compileall、页面 JS、health/ready、HTTP 和浏览器页面实测通过。结论：本机候选通过；生产 Gate 不豁免。完整证据见 `docs/TEST_REPORT_0.22.3.md`。

- 2026-07-22：验收 0.22.2 后台数据隔离与真实输入输出展示本地代码级候选。schema v22 增加会话来源分类；默认运营 overview 排除 simulation/evaluation，显式模拟范围展示虚拟验收会话、消息、人工任务、指标和派单；智能客服详情显示来源、证据、决策详情、上下文和轨迹；场景验收页运行 13/13 并显示真实输入、断言和 JSON 输出。最终 221 项全量测试、20/20 安全评测、compileall/JS、health ok、ready 200、schema 22、HTTP 场景 13/13 和 Edge 页面验证通过。结论：本地候选通过；生产 Gate 不豁免。完整证据见 `docs/TEST_REPORT_0.22.2.md`。

- 2026-07-22：按用户要求为本机开发实例增加可逆管理员免登录开关。`ADMIN_AUTH_REQUIRED` 默认保持 `true`；显式关闭后，API 只接受回环客户端，CLI 拒绝 `0.0.0.0` 等非回环监听，客户侧认证保持开启；管理后台读取 health 后自动隐藏登录层和退出按钮。最终 219 项全量测试、3 项管理后台专项、compileall、JS 语法、CLI 负向启动、8104 health/ready/API 和桌面浏览器通过；实际页面显示“本机免登录”，console error/warning 为 0。结论：当前 `127.0.0.1:8104` 本机联调通过；生产认证无豁免，恢复认证只需设回 `ADMIN_AUTH_REQUIRED=true` 并重启。

- 2026-07-22：验收 0.22.1 虚拟店铺逐场景输入输出证据本地代码级候选。报告契约升级为 `simulation-evidence-v1`，13 项统一提供固定输入、预期、逐项断言和完整领域/Agent/工具输出，保留 `detail` 兼容；后台新增手动运行、模块/状态筛选、模块覆盖和格式化明细。最终 218 项全量测试、生产源码 86% branch coverage、simulation 94%、API 100%、20/20 安全评测、compileall/JS/JSON、源码/包 0.22.1、真实 HTTP 13/13 与 110,780-byte 响应、health ok、ready 200、schema 21/135 paths、数据库完整性及 1280×720/390×844 浏览器通过，console error/warning 为 0。浏览器检查修复场景表横向滚动和移动详情定位/遮挡。结论：本地虚拟候选通过；生产 Gate 不豁免。完整证据见 `docs/TEST_REPORT_0.22.1.md`。

- 2026-07-22：验收 0.22.0 虚拟店铺运营模拟与跨模块本地代码级候选。显式 virtual 数据包的 6 商品、10 库存、8 订单/物流/售后、3 竞品候选/3 内容信号和 4 店铺知识全部经公开领域服务导入；D01-D13 覆盖商品、订单、库存、指标、竞品质量/监控、客服、可信订单、人工接管/派单、后台、租户隔离、Connector 和版本化客服评测，业务模块注册表 7/7 available 通过，营销/财务保持 planned。默认模型关闭的 CLI 与最终 HTTP 实例均连续运行两次 13/13，商品/库存/订单/竞品重放幂等、知识和冻结评测复用；D13 在临时 SQLite 快照运行实际 Agent 且主库 session/message/handoff 计数不变。最终 218 项全量测试、全项目 90%/生产源码 86% branch coverage、simulation 95%、API 100%、20/20 实际 Agent、compileall/JSON、源码/包 0.22.0、health ok、ready 200、schema 21/135 paths、integrity ok/外键错误 0 和桌面/390px 后台通过。测试发现并修复售后枚举、竞品匹配字段、滞销样本、保修意图词表和默认模型关闭评测断言。结论：本地虚拟候选通过；营销/利润、真实客户/模型/渠道/合法竞品源、24/72 小时长稳、容量、安全、异机灾备和生产放行仍阻塞。完整证据见 `docs/TEST_REPORT_0.22.0.md`。

- 2026-07-22：验收 0.21.0 值守排班与持久自动派单本地代码级候选。schema v21 automatic/manual、scheduled/unrestricted、显式 presence session、连续 heartbeat sequence、UTC 绝对班次/重叠拒绝/取消、任务/job 同事务、启动补种、数据库租约单赢家、过期租约恢复、等待/退避/失败、无坐席告警、恢复唤醒、乐观锁重试/确认、health/ready、管理 API 和后台真实闭环通过 215 项全量测试、全项目 89%/生产源码 86% branch coverage、dispatch 86%、staffing 87%、handoff 88%、数据库 95%、20/20 实际 Agent、compileall/JS、schema 21/133 paths 和 1280 x 720/390 x 844 浏览器。浏览器发现并修复档案保存后的心跳状态不一致和原生 prompt 不兼容；并真实验证告警版本冲突回显、刷新确认、恢复值守后 job assigned/alert resolved。结论：本地代码级候选通过；真实客户/模型/渠道、周期排班业务签收、目标设备、24/72 小时长稳、容量、安全、异机灾备和生产放行仍阻塞。完整证据见 `docs/TEST_REPORT_0.21.0.md`。

- 2026-07-22：验收 0.20.0 坐席调度与智能分配本地代码级候选。schema v20 坐席档案/队列成员、TTL 在线租约、重启不复活、凭据/档案双启用、队列技能和主队列、全局/队列容量、稳定候选排序、领取/转派资格、活动任务管理员停用保护、任务/首响/事件/审计原子提交、管理 API 和后台真实闭环通过最终 203 项全量测试、全项目 89.65%/生产源码 85.86% 覆盖率、staffing 96%、handoff 88%、数据库 95%、20/20 实际 Agent 2.5 秒、compileall/JS、editable 0.20.0、health/ready、123 条 OpenAPI、SQLite 完整性和 1280 x 720/390 x 844 浏览器。实现中发现并修复启动补种复活过期租约、迁移漂移检查顺序和旧测试任意负责人旁路。结论：本地代码级候选通过；真实客户/模型/渠道、业务组织/排班/SLA、目标设备、24/72 小时长稳、容量、安全、异机灾备和生产放行仍阻塞。完整证据见 `docs/TEST_REPORT_0.20.0.md`。
- 2026-07-22：验收 0.19.0 人工接管队列与 SLA 工作台本地代码级候选。schema v19 队列/事件和 v18 活动任务前向迁移、确定性路由/风险优先级、12 路原子认领、坐席容量、负责人状态机、转派/升级/备注、首响 L1/解决 L2 幂等扫描、租户隔离、策略负向、worker、管理 API、高风险 Agent 最终保护和后台真实闭环通过最终 195 项全量测试、全项目 89.45%/生产源码 85.63% 覆盖率、handoff 87.94%、数据库 94.93%、20/20 实际 Agent 1.119 秒/17.87 cases/s、compileall/JS、editable 0.19.0、health/ready、119 条 OpenAPI、SQLite 完整性和 1280 x 720/390 x 844 浏览器。首次测试发现并修复高风险模型回答旁路、历史 schema 夹具和移动筛选布局。结论：本地代码级候选通过；真实客户/模型/渠道、业务队列/SLA/排班、目标设备、24/72 小时长稳、容量、安全、异机灾备和生产放行仍阻塞。完整证据见 `docs/TEST_REPORT_0.19.0.md`。
- 2026-07-22：验收 0.18.0 版本化客户 Agent 评测本地代码级候选。schema v18 suite/case/run/result、脱敏校验、原子换版、冻结不可变和逐级 SHA-256、实际多轮 Agent 临时快照隔离、意图/转人工/证据/严重错误/场景/回归指标、同 key+hash 基线、8 路 run 幂等、启动中断恢复、发布乐观锁关联、管理 API 和后台失败/修订/通过闭环通过最终 184 项全量测试、全项目 89%/生产源码 85% 分支覆盖率、评测 85%、数据库 95%、发布 87%、20/20 隔离评测、compileall/JS、editable 0.18.0、health/ready、110 条 OpenAPI、SQLite 完整性、1280px 浏览器和 20 条实际 Agent 2.270 秒/8.81 cases/s。主库 session/message/handoff 保持 0。结论：本地代码级候选通过；真实客户标注/模型/渠道、业务阈值、移动实机、24/72 小时长稳、容量、安全、异机灾备和生产放行仍阻塞。完整证据见 `docs/TEST_REPORT_0.18.0.md`。
- 2026-07-22：验收 0.17.0 竞品可信实体与内容证据本地代码级候选。schema v17 确定性匹配评分、GTIN 格式、匹配/缺失/硬冲突解释、初始 pending、不可审批冲突、乐观锁批准/拒绝和不可变历史；同源幂等冲突、12 路单写入和单裁决；价格范围绑定、approved-only 告警、撤销自动解决；至少 5 样本聚合口碑、额外原始评论字段拒绝和摘要脱敏；两个 Agent 工具二次过滤；管理 API 和后台质量队列通过最终 171 项全量测试、全项目 89%/生产源码 85% 分支覆盖率、竞品 87%、数据库 95%、20/20 隔离评测、compileall/JS、editable 0.17.0、health/ready、100 条 OpenAPI、SQLite 完整性、1280px 浏览器真实批准/撤销、限流和本机性能验证。结论：本地代码级候选通过；真实授权竞品/口碑源、客户同款标注、误报漏报与口碑代表性、移动实机、24/72 小时长稳、容量、安全、异机灾备和生产放行仍阻塞。完整证据见 `docs/TEST_REPORT_0.17.0.md`。
- 2026-07-22：验收 0.16.0 持久渠道 Agent 运行时本地代码级候选。schema v16 事件/任务事务、12 线程单领取、租约恢复、退避/死信、invocation 请求哈希与稳定 ID、Agent 完成后复用、精确 source event、owner/策略二次检查、shadow/assist/collaborative/automatic、Graph 影子零 SOP/人工/发送副作用、outbox 异步失败反写和管理后台账本通过最终 161 项全量测试、全项目 89%/生产源码 85% 分支覆盖率、渠道 Agent 85%、数据库 95%、ContextBuilder 93%、竞品 91%、20/20 隔离评测、compileall/JS、editable 0.16.0、health/ready/OpenAPI、真实 HTTP Qimen shadow、SQLite 完整性、桌面/390px 浏览器和本机性能验证。测试发现并修复模型转人工越过 shadow 和 invocation 留存外键风险。结论：本地代码级候选通过；真实平台/业务工具、客户回放、24/72 小时长稳、容量、异机/设备密钥/RPO-RTO 和生产放行仍阻塞。完整证据见 `docs/TEST_REPORT_0.16.0.md`。
- 2026-07-22：验收 0.15.0 可信上下文客服 Agent 本地代码级候选。schema v15、固定顺序 ContextBuilder、decision/generation 父子快照、证据 authority/freshness/checksum、模型前身份/授权冲突降级、16 路并发幂等、重放/篡改拒绝、工具后置验证证据、消息/审计/人工任务/API/后台关联和快照留存通过最终 149 项全量测试、85% 源码分支覆盖率、ContextBuilder 93%、数据库 94%、留存 100%、20/20 隔离评测、compileall/JS、editable 0.15.0、health/ready/OpenAPI、SQLite 完整性、桌面真实证据查看、390px 响应式和本机性能验证。测试发现并修复旧 mock 授权视图、灾备 schema 夹具、漂移迁移错误契约、移动弹窗溢出和快照留存旁路。结论：本地代码级候选通过；真实平台/业务工具、客户回放、24/72 小时长稳、容量、异机/设备密钥/RPO-RTO 和生产放行仍阻塞。完整证据见 `docs/TEST_REPORT_0.15.0.md`。
- 2026-07-21：验收 0.14.0 竞品监控 Agent 本地代码级候选。schema v14 策略/告警、低价/降价/过期条件、相同证据幂等、新证据复发、自动清除、乐观锁处置、租户隔离、12 次并发重评、按租户 worker、Agent 证据、管理 API 和页面内处置通过 142 项全量测试、85% 源码分支覆盖率、竞品模块 91%、20/20 隔离评测、JS、editable 0.14.0、health/ready/OpenAPI、SQLite 完整性、桌面真实解决、390px 响应式布局和本机性能验证。首轮三项失败为灾备测试夹具写死 schema 13，更新后定向 3/3 和两次全量 142/142 通过。结论：本地代码级候选通过；真实平台/生产数据、24/72 小时长稳、容量、安全、部署灾备、业务验收和生产放行仍阻塞。完整证据见 `docs/TEST_REPORT_0.14.0.md`。
- 2026-07-21：验收 0.13.0 SOP 持久执行本地候选。schema v13 多步账本、DSL v2、上下文/顺序门、只读重试、写入未知态、人工裁决、补偿、重启恢复、Agent 集成、管理 API 和页面内处置通过 136 项全量测试、85% 源码分支覆盖率、SOP 85%、20/20 隔离评测、compileall/JS、editable 安装、health/ready/OpenAPI、SQLite 完整性、桌面真实审批、390px 响应式布局和本机性能冒烟。测试发现并修复可信上下文绕过、等待状态旧版本、结果脱敏缺口和原生 prompt 不可验收问题。结论：本地 SOP 执行候选通过；真实渠道/ERP、客户回放、真实业务写工具/补偿、24 小时长稳、异机/设备密钥/RPO-RTO、语义 VOC 和生产放行仍阻塞。完整证据见 `docs/TEST_REPORT_0.13.0.md`。
- 2026-07-21：验收 0.12.0 发布门禁本地候选。schema v12 版本化策略、完整 Agent 隔离回放、回放隐私、双人审批、稳定哈希分桶、shadow/assist/collaborative/automatic、幂等运行观测、投递异常升级和自动暂停通过 111 项全量测试、85% 源码分支覆盖率、发布模块 86%、20/20 隔离评测、compileall/JS、health/ready/OpenAPI、桌面/390px 浏览器和本机性能冒烟。浏览器控制会取消原生 prompt，因此审批/启用按钮提交未计 UI 点击通过，API 与最终状态回显通过；测试发现的密码框残留已修复并复验。结论：本地发布门禁候选通过；真实渠道/ERP、客户回放、24 小时长稳、异机/设备密钥/RPO-RTO、完整 SOP 补偿、语义 VOC 和生产放行仍阻塞。完整证据见 `docs/TEST_REPORT_0.12.0.md`。
- 2026-07-21：验收 0.11.0 加密灾备本地候选。运行目录互斥、AES-256-GCM/HKDF 双库归档、在线逐库原子与身份一致性、离线锁定维护点、归档信任边界、staging 恢复、失败自动回滚、receipt 手工回退、换钥和保留清理通过 99 项全量测试、84% 源码分支覆盖率、灾备模块 85%、20/20 隔离评测、compileall/JS、在线/离线真实运行目录演练、恢复启动和性能冒烟。在线模式不声明跨 SQLite 同一事务时刻；本版未新增浏览器交互验收。结论：本地灾备候选通过；真实渠道/ERP、客户回放、24 小时长稳与故障注入、异机介质、设备密钥、业务 RPO/RTO、完整 SOP 补偿、语义 VOC 和生产放行仍阻塞。完整证据见 `docs/TEST_REPORT_0.11.0.md`。
- 2026-07-21：验收 0.10.0 可靠发送本地候选。schema v11 持久加密 outbox、跨连接原子租约、调用前/后崩溃边界、安全重试、死信、人工核对、会话 owner 复核、草稿/出站事件一致性、worker 生命周期和管理 API 通过 83 项全量测试、84% 源码分支覆盖率、20/20 隔离评测、compileall/JS、运行服务重启、桌面浏览器、SQLite 恢复和本机性能冒烟。桌面控制台 0 错误；本轮浏览器 viewport 能力未实际切换，移动端不计实测通过。结论：可靠发送本地候选通过；真实渠道、客户回放、24 小时长稳、故障注入、加密灾备、完整 SOP 补偿和生产放行仍阻塞。完整证据见 `docs/TEST_REPORT_0.10.0.md`。
- 2026-07-21：验收 0.9.0 Agent 治理与客服操作闭环本地候选。schema v9 分层知识、SOP DSL/生命周期/会话固定版本/动作门、确定性质检/VOC、客服暂停/接管/恢复、回复草稿/diff/发送和三态投递通过全量 74 tests、84% 源码分支覆盖率、20/20 隔离离线评测和 compileall；桌面/390px 浏览器实际完成知识激活和质检复核且 0 控制台错误；SQLite 备份恢复和本机性能仅完成冒烟。结论：本地候选通过；真实渠道、脱敏客户回放、持久 outbox、完整 SOP 多步补偿、语义 VOC、24 小时长稳、加密灾备和生产放行仍阻塞。完整证据见 `docs/TEST_REPORT_0.9.0.md`。
- 2026-07-21：验收 0.8.0 经营事实与受控指标本地候选。全量 67 tests、83% branch coverage、20/20 离线安全评测、compileall、前端语法和 editable 安装通过；浏览器实际完成商品/库存与订单/售后同步、筛选和指标检查，1440×900 与 390×844 无页面级横向溢出或控制台错误。结论：本地虚拟数据候选通过；真实平台、真实模型、知识/SOP/质检、长稳/灾备/设备安全和生产放行仍阻塞。完整证据见 `docs/TEST_REPORT_0.8.0.md`。

- 2026-07-21：验收 0.7.0 经营与客服管理 V1。全量 56 tests、20/20 离线安全评测、compileall 和 managed schema 校验通过；浏览器实际完成管理员登录、竞品虚拟同步、价格洞察、客服测试对话和会话回放，默认桌面与 390×844 视口无 body 横向溢出或控制台错误。结论：本地 V1 通过；真实平台、完整知识/SOP/质检管理和生产放行仍未验收。

- 2026-07-21：按用户“不要使用 computer use 管理后台”的要求复核淘宝真实客服通道。官方资料确认客服机器人为服务市场受管能力，生产链路使用店铺 OAuth、奇门消息入站、TOP 异步回写及订阅查询；千牛登录不能生成 `request_token/tenant_id`。本地实现按官方字段与签名修正，54 tests、20/20 离线评测和 compileall 通过。结论：本地 API 契约通过；真实联调继续阻塞于平台审批与凭证。
- 2026-07-21：验收 0.6.0 全域业务模块首批切片。统一 Connector SDK 与虚拟淘宝覆盖连接、拉取、Webhook、动作幂等和读回；仓储与竞品模块覆盖持久化、来源、时间、版本、风险/价差和 Agent 工具。全量 54 tests、20/20 离线评测、compileall、managed schema 校验和桌面/窄屏架构页检查通过。结论：本地模块切片通过；真实平台和后续商品/订单/指标模块未验收。
- 2026-07-21：验收 `project-to-act` GitHub 发布。修正测试子进程的 Windows 编码隔离后，32 项单元测试与官方快速校验通过，当前项目 `--check`/`--validate` 返回 0；发布提交 `e05d84d` 已非强制快进到 `main` 和修复分支，递归检查远端 14 个 blob 与本地文件全部一致。结论：skill 已发布；不改变淘宝真实接入阻塞状态。
- 2026-07-21：验收淘宝客服接管本地 PoC。合并当前库存/竞品运营模块后全量 53 tests 通过，离线安全评测 20/20，通过 project-to-act managed schema 校验。覆盖 OAuth state、防重放、AES-GCM 凭据、TOP/奇门签名、渠道事件幂等、会话归属、人工发送门、发件箱幂等和 capability 门禁。结论：本地 PoC 通过；真实淘宝消息收发 Gate 因缺少机器人资格、应用凭证和测试店铺而阻塞。
- 2026-07-21：验收后台数据接入与客服接管调研报告。文件 50,362 字节、761 行、1 个 H1、16 个 H2、45 个 H3、18 个闭合代码围栏、63 个外部资料链接；无乱码替换字符和 TODO/TBD。交叉检查项目总览、进度、F-201 至 F-210 和旧路线替代说明一致。结论：调研文档交付和项目路线同步通过；平台专项权限、测试店铺、连接器开发和真实 PoC 仍未完成。
- 2026-07-21：用户纠正实施方向后，将原路线文档标记为历史参考，新增 F-201 至 F-210。新报告尚在调研编写中，因此不得沿用原文档的“交付通过”结论作为新路线验收结论。
- 2026-07-21：验收客服路线汇总到项目功能台账。PowerShell 精确解析 `PROJECT_FEATURES.md` 中以 `F-` 开头的表行：21 行、21 个唯一 ID、5 项已完成、13 项已规划、3 项已阻塞，非法优先级 0、非法状态 0、占位项 0；抽查的 13 个源码/测试/路线证据路径全部存在。交叉核对总览、进度与验收结论一致。结果：功能台账汇总通过；未修改业务代码，规划功能、真实渠道和真实动作仍未验收。
- 2026-07-21：验收客服产品技术路线 Markdown 文档。检查文件为 28,911 字节、677 行、1 个 H1、15 个 H2、21 个 H3、32 个闭合代码围栏；关联文档均存在，乱码和占位符搜索无命中。结论：文档交付通过，项目实施与业务评审未完成。
