# 云湃电商一体机 Agent 0.11.0 测试报告

## 1. 验收结论

- 测试对象：`yunpai-ecommerce-agent 0.11.0`，SQLite schema v11。
- 本地灾备候选：**通过**。本版将 0.10.0 的可靠发送候选扩展为具备运行目录互斥、加密备份、完整性验证、恢复、自动回滚、手工回退、换钥和保留清理的单机灾备候选。
- 生产放行：**不通过（NO-GO）**。真实淘宝/ERP 联调、脱敏客户会话回放、24 小时长稳与断电/磁盘写满/数据库锁故障演练、设备密钥托管和异机恢复、完整 SOP 补偿、语义 VOC、灰度门禁仍缺少生产证据。
- 本报告不把虚拟淘宝、框架性能或本机 SQLite 恢复结果表述为真实平台与整机生产能力。

## 2. 当前技术实现路径

系统采用“单机模块化单体 + 明确边界 + 后续可拆分”的路径：

```text
渠道/ERP/报表
    -> Connector 标准事件与版本契约
    -> 业务事实库（商品、库存、订单、竞品、指标）
    -> Agent 上下文组装（身份、会话、证据、SOP）
    -> LangGraph 有界 ReAct 与结构化决策
    -> 权限/风险/证据/人工接管安全门
    -> 持久加密 outbox + 租约 worker
    -> 平台回执、人工核对、审计、质检与 VOC

运行数据
    -> SQLite online backup / 离线锁定快照
    -> AES-256-GCM 加密归档 + 内部清单
    -> verify / restore / rollback / rekey / prune
```

关键原则：模型只负责理解、检索和提出结构化决策；代码负责身份、租户边界、参数类型、权限、幂等、风险、状态机、发送成功判定和恢复。

## 3. 模块设计与实现

| 模块 | 设计职责 | 当前实现 | 主要验证 |
|---|---|---|---|
| 配置与启动 | 环境配置、路径解析、密钥引用、生命周期 | `config.py` 统一配置；`cli.py` 提供服务、评测和灾备命令；Uvicorn 使用应用工厂，导入 API 不再隐式占用数据库 | CLI 子进程、版本一致性、工厂启动、运行服务健康检查 |
| 身份与会话 | 客户端/管理员认证、租户和会话绑定、可信订单上下文 | `auth.py`、`service.py` 在服务端约束 tenant/session/subject；进入图前清洗 checkpoint 上下文 | 认证、跨租户、会话归属、伪造上下文测试 |
| Agent 编排 | 有界推理、动态工具、人工接管和安全输出 | `graph.py` 使用 LangGraph checkpointer；`decision.py` 定义结构化决策；最多 4 步；`policy.py`、`tools.py` 执行代码级安全门 | ReAct、工具参数、越权、证据不足、接管与回归测试 |
| 模型网关 | 标准模型 API、超时和禁用策略 | `llm.py` 统一调用；`MODEL_ENABLED=false` 时完全离线；拒绝把 Coding Plan 当客服运行端点 | 离线评测、模型禁用、错误映射测试 |
| 知识与检索 | 平台/行业/店铺/商品分层事实和可追溯证据 | `rag.py` 混合检索；`knowledge_management.py` 不可变版本、审批、激活、退役和回滚；`knowledge_seed.py` 提供受控种子 | 租户/店铺/SKU 隔离、版本生命周期、证据 ID、检索过滤 |
| SOP 与治理 | 业务流程边界、会话固定版本、动作门和补偿元数据 | `sops.py` 实现 DSL、候选/批准/激活/退役、会话版本固定；`governance_api.py` 暴露治理接口 | 生命周期、非法转换、固定版本、动作门、API 错误契约 |
| 质检与 VOC | 确定性规则质检、人工复核、问题聚合 | `quality.py` 生成可复核结果和 VOC 汇总；当前不是语义聚类模型 | 规则命中、复核状态、租户隔离和汇总测试 |
| 客服接待 | 渠道入站、会话 owner、暂停/接管/恢复、草稿与人工发送 | `taobao.py` 实现 OAuth/验签/防重放/事件幂等、owner 状态机、草稿 diff 和投递三态 | 本地协议、竞态、幂等、错误语义、人工接管测试 |
| Connector SDK | 平台能力声明、标准事件、读写契约和回放 | `connectors/base.py` 定义接口；`registry.py` 注册；`virtual_taobao.py` 提供显式 `virtual=true` 的确定性本地实现 | 能力门、拉取、Webhook、动作幂等、回读和版本冲突 |
| 商品与库存 | 商品/SKU/库存事实及来源版本 | `business/catalog.py`、`inventory.py` 维护结构化事实；旧版本拒绝、同版本同载荷幂等、冲突显式失败 | 同步、筛选、库存风险、来源版本和 Agent 工具测试 |
| 订单与售后 | 订单、物流、售后事实和可信店铺绑定 | `business/orders.py` 维护订单链路；Agent 查询必须匹配服务端可信 `order_id + shop_id` | 跨店阻断、同步版本、物流/售后和后台 API 测试 |
| 竞品分析 | 竞品快照、价格差、趋势、风险和建议 | `business/competitive.py` 保存可追溯快照，按 SKU/平台生成确定性洞察；不声称具备自动爬取未授权平台能力 | 重复同步、版本冲突、价差/趋势/风险、API 与后台交互 |
| 指标与经营工具 | 受控指标计算和低风险建议 | `business/metrics.py`、`business/service.py` 组合商品、订单、库存和竞品事实；工具有超时、权限和不确定态 | 指标口径、工具超时、权限、失败分类和回放测试 |
| 业务模块注册 | 模块边界、状态和 Agent 工具暴露 | `business/registry.py` 声明模块职责、依赖和工具，领域模块不直接访问第三方 HTTP | 注册表完整性和模块契约测试 |
| 可靠发送 | 进程崩溃后不丢任务、不盲目重复回复 | `outbox.py` 先持久化 AES-GCM 密文，再由租约 worker 发送；区分调用前失败、调用后未知、拒绝、死信和人工核对 | 8 实例抢租约、崩溃边界、退避、死信、核对、乐观锁 |
| 持久化 | 业务状态、迁移、完整性与 checkpoint | `database.py` 使用 SQLite WAL、外键、busy timeout、schema v11 物理校验；LangGraph 使用独立 checkpoint 库 | v1/v7/v9 到 v11、伪造版本、完整性和并发测试 |
| API 与后台 | 对话、经营、治理、渠道和运维聚合 | `api.py` 组合路由；`admin_api.py`、`operations_api.py`、`governance_api.py` 分域；`admin.py` 提供静态管理台 | API 鉴权/隔离、错误契约、后台桌面历史验收、JS 语法 |
| 运维与留存 | health/readiness、数据留存、清理 | `maintenance.py` 执行留存；health/readiness 检查 schema、空间、worker、checkpoint、模块和配置 | 生命周期、就绪状态、清理和运行时 HTTP 检查 |
| 灾备 | 加密快照、验证、恢复、回滚、换钥、保留 | `disaster_recovery.py` 实现下述完整灾备链路；`service.py` 持有运行目录锁 | 归档信任边界、故障注入、在线/离线恢复和 CLI 端到端 |

## 4. 灾备归档设计

### 4.1 归档格式与加密

- 外层固定 magic：`YPAIBAK1`，格式版本 1。
- 外层只包含安全元数据：算法、KDF、`key_id`、归档 ID、时间、salt 和 nonce；业务清单与数据库均在密文内。
- 32 字节主密钥通过 HKDF-SHA256 派生归档密钥；流式 AES-256-GCM 加密，外层头作为 AAD，认证 tag 位于文件尾。
- 输出先写 `.partial`，刷新并同步磁盘后原子重命名；目标已存在时拒绝覆盖。
- 密钥仅从环境变量读取，CLI 参数和归档中不保存明文密钥。

### 4.2 快照一致性语义

归档包含 `agent.sqlite3`、`checkpoints.sqlite3` 和加密清单。每个数据库使用 SQLite online backup API 取得自身原子快照，并转换为 `journal_mode=DELETE`，避免恢复文件依赖外部 WAL/SHM。

- 在线模式 `online_identity_consistent`：服务可继续运行；保证两个库各自原子，并验证 checkpoint thread 都属于业务库 session。它**不承诺两个独立 SQLite 文件处于同一个跨库事务时刻**。
- 离线模式 `offline_runtime_locked`：要求服务停止并取得 `.yunpai-runtime.lock`，适合精确维护点备份。
- 在线校验若发现孤立 checkpoint，最多重新快照 3 次；仍不一致则失败且不留下目标归档。

### 4.3 验证、恢复和回退

- `backup-verify` 先验证 GCM tag，再验证 ZIP 精确成员集合、路径安全、清单结构、成员大小/SHA256、SQLite `integrity_check`、schema v11 物理结构和跨库 session/checkpoint 关系。
- `backup-restore` 在 staging 目录解密和复验；运行目录必须未被服务占用，SQLite `-wal/-shm/-journal` sidecar 存在时拒绝恢复。
- `--force` 时，旧数据库先移动到独立 rollback 目录，再提交新数据库。提交失败会自动恢复旧数据库。
- 成功恢复生成 receipt；`backup-rollback` 按 receipt 恢复旧数据库，并把当前恢复版本保存在 forward 目录。回退失败时仍保留当前集和旧 rollback 集。
- `backup-rekey` 完整解密验证后用新 key/key-id 重封装；`backup-prune` 默认 dry-run，至少保留一份，只处理有效命名归档。

## 5. 自动化回归与覆盖率

执行命令：

```powershell
py -3.12 -m coverage erase
py -3.12 -m coverage run --branch -m pytest -q
py -3.12 -m coverage report --include="src/*" -m
```

结果：

```text
99 passed in 138.99s
TOTAL source branch-aware coverage: 84%
```

| 关键模块 | 分支感知覆盖率 |
|---|---:|
| `database.py` | 95% |
| `service.py` | 93% |
| `disaster_recovery.py` | 85% |
| `outbox.py` | 85% |
| `taobao.py` | 83% |
| `graph.py` | 83% |
| 全部 `src/` | 84% |

灾备模块为 706 条语句，覆盖率 85%。未覆盖分支主要是其他操作系统的底层锁实现、极少见的文件系统损坏组合和部分日志性分支，不影响本轮已声明的 Windows 单机候选范围。

## 6. 灾备测试矩阵

| 场景 | 预期 | 结果 |
|---|---|---|
| 服务运行时在线备份 | 每库原子快照、跨库身份一致、服务不中断 | 通过 |
| `--require-stopped` 遇到运行服务 | 无法取得运行目录锁，明确失败 | 通过 |
| 停止服务后离线备份 | 清单模式为 `offline_runtime_locked` | 通过 |
| 第二个服务使用同一数据目录 | 启动失败，避免两个进程共享 SQLite 目录 | 通过 |
| 密文搜索数据库标记 | 归档中不出现数据库明文标记 | 通过 |
| 正确密钥验证 | tag、成员、清单、哈希、SQLite 和跨库关系全部通过 | 通过 |
| 错误密钥或密文篡改 | 认证失败，无明文输出 | 通过 |
| 截断、错误 magic/版本/header | 在信任边界外拒绝 | 通过 |
| 认证后恶意 ZIP 路径/多余成员 | 拒绝路径穿越和非精确成员集合 | 通过 |
| 无效清单、哈希、大小或 schema | 拒绝恢复 | 通过 |
| checkpoint 存在孤立 thread | 有界重试，失败时不产生归档 | 通过 |
| 新目录恢复 | 两库恢复、receipt 生成、服务可启动 | 通过 |
| 目标已有数据库未指定 `--force` | 拒绝覆盖 | 通过 |
| 目标存在 WAL/SHM/journal | 拒绝恢复，要求先安全停库 | 通过 |
| 强制恢复中注入提交失败 | 自动恢复原数据库 | 通过 |
| 强制恢复后手工 rollback | 原数据库恢复，当前版本保留到 forward 目录 | 通过 |
| rollback 中注入失败 | 当前版本和旧 rollback 集均保留 | 通过 |
| 无 rollback 集执行 rollback | 明确失败 | 通过 |
| rekey | 新密钥验证通过，旧密钥不再可用 | 通过 |
| prune dry-run/apply | 默认不删除；apply 按保留数删除；无效文件忽略 | 通过 |
| CLI 错误 | JSON stderr、非零退出、无 traceback 和密钥泄漏 | 通过 |

主要证据：`tests/test_disaster_recovery.py`、`tests/test_cli.py`。

## 7. 真实运行目录演练

### 7.1 在线演练

在服务持有运行目录锁且继续响应时执行在线备份、验证和隔离目录恢复：

```text
backup_ms=1116.85
verify_ms=883.73
restore_ms=942.31
archive_bytes=877180
schema=11
sessions=100
checkpoint_threads=100
```

同时验证向活动数据目录恢复会被运行目录锁拒绝。

### 7.2 离线维护点演练

停止服务后执行 `backup --require-stopped` 并验证：

```text
backup_ms=1037.46
archive_bytes=1246870
mode=offline_runtime_locked
schema=11
sessions=150
checkpoint_threads=150
```

完成后重启服务，health、ready 和 API 版本均正常。

这些数字是本机功能演练，不是承诺的 RPO/RTO。正式 RPO/RTO 需要业务方定义目标，并在目标一体机、异机存储和真实数据规模上重新测量。

## 8. 运行时、评测和性能

| 检查 | 结果 |
|---|---|
| 隔离 `DATA_DIR` 离线评测 | 20/20，通过；failure 数组均为空 |
| `py -3.12 -m compileall -q src tests` | 通过 |
| 后台两个 HTML 内嵌 JavaScript 语法 | 通过 |
| editable 安装版本 | package/module 均为 `0.11.0` |
| 运行服务 | `http://127.0.0.1:8092`，health ok，schema 11，worker running，ready |
| OpenAPI | `0.11.0` |
| `pip check` | 项目依赖正常；宿主环境外部 `cadpy` 缺少 `cadquery-ocp`，与本项目依赖树无关 |

本地 framework/mock 性能结果：

| 负载 | 成功 | p50 | p95 | max |
|---|---:|---:|---:|---:|
| `/health` 顺序 100 次 | 100/100 | 24.93 ms | 27.63 ms | 33.54 ms |
| outbox summary 顺序 50 次 | 50/50 | 77.77 ms | 89.95 ms | 136.67 ms |
| `/v1/chat` 顺序 30 次 | 30/30 | 125.20 ms | 140.49 ms | 142.31 ms |
| `/v1/chat` 8 worker / 20 次 | 20/20 | 347.20 ms | 436.70 ms | 442.00 ms |

一次重复性能脚本超过管理员接口配置的 120 次/分钟限额并收到 HTTP 429；等待窗口恢复并控制请求数后，上表复测全部通过。这证明限流器在生效，但不替代生产容量测试。

0.11.0 未重新执行浏览器 UI 交互；本版前端变化只有版本标签，已完成 HTML/JavaScript 静态语法检查。0.10.0 的桌面浏览器功能证据仍有效，但本报告不声称新增一轮浏览器或移动端实测。

## 9. 测试发现并修复的问题

| 问题 | 风险 | 修复 |
|---|---|---|
| WAL 模式数据库直接恢复会产生 sidecar 依赖 | 归档只带主文件时可能不完整 | 快照归档前规范化为 `journal_mode=DELETE` |
| SQLite connection 上下文只提交事务、不保证关闭 | Windows 恢复/清理时文件被占用 | 所有灾备验证连接使用显式 `closing()` |
| 离线验证可能创建临时 sidecar | 静态验证改变被验证目录 | 使用 `immutable=1` 只读连接 |
| API 模块级全局 `app` 在 import 时打开默认数据库 | CLI 灾备命令导入 API 即抢占运行锁 | 移除模块级实例，改为 Uvicorn `factory=True` 应用工厂 |

## 10. 生产放行剩余 Gate

以下任一项未完成，都不得把 0.11.0 标记为生产可用：

1. 淘宝客服机器人资质、正式 App/奇门/TOP 凭证、测试店铺真实入站、人工接管、回写、回执、重放、限流和核对。
2. 真实 ERP/OMS 商品、订单、物流、售后数据字典、增量同步、日对账、失败补偿和权限审计。
3. 首个客户脱敏会话、知识、SOP、质检和经营口径回放；真实模型准确率与严重事实错误为 0 的门禁。
4. 至少 24 小时 soak，以及进程强杀、断电、磁盘写满、数据库锁、积压、平台超时/限流、密钥轮换与 checkpoint 一致性演练。
5. 备份写入异机/离线介质、密钥 escrow/轮换、目标一体机和异机恢复；由业务确定 RPO/RTO 并签收演练结果。
6. 完整 SOP 多步执行和补偿、语义 VOC 聚类、影子/仅提示/人机协同/白名单自动化灰度与自动停止门禁。
7. 真实移动浏览器、目标一体机屏幕、可访问性、磁盘容量和设备安全基线。

## 11. 下一轮验证顺序

1. 在隔离副本执行 24 小时 soak 和可重复故障注入，产出时间线、资源曲线、错误分类和恢复证据。
2. 将加密归档写入实际异机介质，完成设备密钥托管、换钥和整机恢复演练，确定业务 RPO/RTO。
3. 取得淘宝测试店与 ERP 沙箱权限，执行真实渠道/数据联调和幂等回放。
4. 导入首个客户脱敏集，补齐完整 SOP 补偿、语义 VOC 和模型质量 Gate。
5. 依次进入影子、仅提示、人机协同和白名单自动化，任何严重错误触发停止放量与回滚。
