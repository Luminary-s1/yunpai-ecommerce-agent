# 运行与运维

## 启动

1. 创建并激活 Python 3.11+ 虚拟环境，并用 `python --version` 核对解释器。
2. 执行 `python -m pip install -e .[dev]`；Windows 再用 `Get-Command yunpai-agent` 确认命令来自当前虚拟环境。多 Python 环境未激活虚拟环境时，改用 `py -3.12 -m ecommerce_agent.cli <command>`。
3. 配置 `ADMIN_API_KEY`、上游客户端密钥、`SUBJECT_HASH_KEY`；首次初始化保持 `MODEL_ENABLED=false`。
4. 执行 `yunpai-agent init`，确认知识条数不少于 100。
5. 执行 `yunpai-agent eval`，必须全部通过。
6. 使用标准 API 密钥配置 GLM，执行 `yunpai-agent model-probe`。
7. 探测通过后再设置 `MODEL_ENABLED=true` 并执行 `yunpai-agent serve --host 0.0.0.0 --port 8080`。

当前程序读取系统环境变量，不主动加载 `.env`，避免意外读取错误目录中的密钥文件。

## 上线门禁

- `/health` 显示的 schema 必须等于运行代码的 `Database.SCHEMA_VERSION`；客户端和管理员认证已配置、知识库不少于 100 条、渠道 Agent/outbox/竞品监控/SLA/自动派单 worker 正常且无未处置异常积压；评测中断恢复数量必须核对。
- `/ready` 必须返回 200，并通过 checkpoint、磁盘余量等全部检查。
- `yunpai-agent eval` 必须零失败；目标发布还必须使用冻结的客户脱敏标注集运行版本化 Agent 评测并通过 Gate。
- `ADMIN_API_KEY` 必须为长随机值，管理接口不得暴露到公网。
- 平台适配器必须携带 `X-Client-Id`、`X-Client-Key`、`X-Subject-Id`。
- 订单上下文能力由 `api_clients.can_supply_order_context` 决定，正文中的 `authorized` 无效。
- 已注册写工具必须通过可信上下文、人工注入规则、幂等字段和后置验证；条件不足时必须追问或产生 `handoff_id`。
- 未完成的人工任务必须阻止会话留存清理。
- 进化候选必须带可追溯 `evidence_source`，租户 A 批准的知识不得被租户 B 检索。
- `RELEASE_GATE_REQUIRED` 必须保持 `true`；开启渠道自动回复前，目标店铺必须存在已通过回放、审批并启用的发布策略。
- 先进行隔离回放和 `shadow`，再依次推进 `assist`、`collaborative` 和 `automatic`；每次扩量都创建新策略版本，不原地修改已评测策略。
- `CHANNEL_AGENT_WORKER_ENABLED` 必须保持 `true`。`GET /v1/integrations/taobao/agent-jobs/summary` 中出现 `dead_letter`，或最老待处理时间持续增长时必须告警。

## 发布门禁操作

1. 由发布创建人新建店铺策略，先选择 `shadow` 或 `assist`，设置保守流量、意图白名单、证据要求和错误预算。
2. 优先在管理后台“客服评测”创建客户标注 suite，原子导入并检查场景后冻结；测试集不得包含未脱敏个人信息。历史兼容场景仍可执行 `yunpai-agent release-replay RELEASE_ID CASES_JSON`。
3. 选择目标 release 运行实际 Agent 评测。用例数、通过率、意图、转人工召回、证据覆盖、严重错误和相对基线回归必须全部通过；suite 版本、数据集 SHA-256 和 runner version 作为本次审批证据。
4. `collaborative` 和 `automatic` 必须由不同于创建人的活动管理员审批；管理员通过 `POST /v1/admin/operators` 创建，密钥只在创建时由线下安全通道交付。
5. 审批后启用策略。使用 `GET /v1/admin/releases/{id}/runtime` 观察样本、失败率和严重错误；发布流量按会话稳定分桶，同一会话不会随机跳组。
6. 任何严重违规或发送故障超过预算时系统自动转 `paused`。人工处置原因、平台核对结果和恢复证据完整后，应创建新版本重新回放，不直接恢复旧策略。

发布回放只在临时数据库快照中执行完整 Agent 链，源库不会新增会话、消息或人工任务。数据库持久记录不包含回放原始问句和回答；回放文件本身仍由运维负责加密、访问控制和按期销毁。

## 客户评测操作

1. 只导入客户确认脱敏的标注数据；`customer_labeled` suite 必须声明 `deidentified=true`，来源引用不得包含手机号、证件、银行卡、订单个人标识或口令。
2. 草稿编辑使用 `expected_record_version` 整体替换 cases。冻结前复核 case key、scenario、逐回合 expectation、最少用例、必需场景和阈值。
3. 冻结后记录 suite key/version 和 dataset hash。不得直接修改数据库；任何修订都从当前 suite 创建新版本。
4. 首次运行不选 baseline；后续版本选择同 suite key 的已完成 run。后台会排除 case hash 已变化的定义，不把改题算作回归。
5. 关联 release 时同时提交当前 release record version。运行期间策略变化会拒绝应用，必须读取新版本后重跑。
6. `error` run 不能复用为通过证据。服务启动若恢复到 `interrupted_by_restart`，应排查中断原因并使用新 run key 重跑。
7. 结果只用于门禁和复核；失败用例的 answer excerpt 已脱敏但仍按客户数据等级保护。正式导出、保留和销毁遵循客户数据协议。

## 故障与恢复

- 模型不可用：返回安全兜底并转人工，不使用未经验证的本地拼接答案。
- GLM Coding Plan 仅可作为显式本机测试：必须设置 `MODEL_ALLOW_CODING_PLAN=true`，并通过非流式标准 Chat Completions 调用；正式环境使用标准 API 配置。
- 知识缺失：不猜测，转人工。
- SQLite：使用 WAL 和 busy timeout；适用于单机轻量部署，不适合多副本并发写。
- 授权失效、平台限流和消息去重由上游平台适配器负责；工具执行器通过 `trace_id`、幂等字段和后置状态与上游日志关联。
- 禁止直接复制运行中的 SQLite 主文件。必须使用下述 `backup` 命令取得包含 WAL 已提交内容的 online backup 快照。

## 自动派单处置

- 生产保持 `HANDOFF_DISPATCH_WORKER_ENABLED=true`。`/ready` 的 `handoff_dispatch_worker` 必须为 true，`/health` 的 worker `last_error` 必须为空。
- pending/leased 是短暂运行状态；waiting 持续增长时按凭据、档案、automatic、心跳、班次、队列成员和容量顺序排查。
- 无可用坐席不会丢弃任务，而是形成持久告警并按退避重试。恢复值守后等待作业会被唤醒；确认 assigned 和 alert resolved 后再关闭事件。
- failed 表示技术错误达到预算。修复根因后使用后台带版本重试；409 表示 worker 或另一管理员已更新对象，必须刷新后重新判断。
- 不得直接修改 SQLite、手填负责人或调大重试次数来掩盖排班/容量问题。完整流程见 `docs/handoff-dispatch-runbook.md`。

## SOP 运行处置

- `GET /v1/admin/sop-runs` 用于查看运行状态和当前步骤；`GET /v1/admin/sop-runs/{run_id}` 返回完整逐步账本。管理员只能读取本租户数据。
- `waiting_input` 表示缺少服务端可信上下文。不能把模型生成的工具参数或顾客正文手工提升为授权事实；应由已授权连接器重新提供上下文后继续会话。
- `waiting_approval` 表示 `evaluate`、`propose` 或声明 `requires_approval` 的动作正在等待人工。使用 `POST /v1/admin/sop-runs/{run_id}/steps/{step_id}/resolve` 提交 `approve` 和当前 `record_version`，审批依据必须写入 `note`。
- `uncertain` 表示写工具可能已经改变外部系统。禁止选择 `retry`；先从业务系统读回实际状态，再以 `confirm_succeeded` 或 `confirm_failed` 核对。版本冲突返回 409，必须刷新后重新判断。
- 只有状态为 `succeeded` 且 DSL 声明 `compensate_with` 的动作可以调用 `/compensate`。补偿工具必须是已注册写工具，具备幂等字段和后置验证；请求参数不进入普通日志，账本只保存输入哈希和脱敏结果摘要。
- `compensation_uncertain` 同样禁止盲目重试。平台读回确认补偿已生效后使用 `confirm_succeeded`，确认未生效则使用 `confirm_failed` 并转人工处置。
- 服务启动会扫描中断步骤：读取步骤在剩余尝试预算内回到 `pending`；动作转 `uncertain`；补偿转 `compensation_uncertain`。每次恢复写入 `sop.step_recovered` 审计事件。

## 加密备份与恢复

### 首次配置

`BACKUP_DIR` 在生产中必须位于独立磁盘、受控网络存储或可离线复制的介质，不能只放在 `DATA_DIR` 同一故障域。`BACKUP_ENCRYPTION_KEY` 必须解码为正好 32 字节，密钥不得进入命令行、仓库、日志或与归档放在同一设备。

```powershell
$bytes = New-Object byte[] 32
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
$rng.Dispose()
$env:BACKUP_ENCRYPTION_KEY = [Convert]::ToBase64String($bytes)
$env:BACKUP_KEY_ID = "appliance-2026-q3"
$env:BACKUP_DIR = "D:\yunpai-backups"
```

必须把密钥复制到企业密钥托管或离线恢复介质后再清除临时变量 `$bytes`。归档外层只记录非秘密的 `key_id`；没有对应旧密钥时无法恢复旧备份。

### 日常备份

```powershell
yunpai-agent backup
yunpai-agent backup --require-stopped
yunpai-agent backup-verify D:\yunpai-backups\yunpai-20260721T....ypbak
yunpai-agent backup-prune --backup-dir D:\yunpai-backups --keep 14
yunpai-agent backup-prune --backup-dir D:\yunpai-backups --keep 14 --apply
```

- `backup` 可以在服务运行时执行。程序先快照 checkpoint，再快照业务库，并验证每个 checkpoint thread 都存在对应业务会话；无法取得一致集合时最多重试三次后失败。
- 在线模式保证两个数据库各自事务一致，并保证 checkpoint thread 属于已知会话；它不宣称两个独立 SQLite 处于同一事务时点。升级前、正式恢复前和要求精确静止点的维护窗口必须先停服务，并使用 `backup --require-stopped` 获取运行锁后再快照。
- 每个 SQLite 快照执行 `integrity_check`、schema/物理列检查、关键表计数和 SHA-256；清单、业务库和 checkpoint 库作为整体使用 AES-256-GCM 认证加密。
- 输出先写隐藏临时文件并 `fsync`，成功后再原子改名；目标文件已存在时拒绝覆盖。
- `backup-verify` 必须作为备份任务的后置步骤。错钥、密文或头部篡改、ZIP 成员异常、哈希不符、SQLite 损坏、schema 不兼容或跨库关系不一致均返回非零。
- `backup-prune` 默认仅预览，只处理具有合法云湃备份头的 `yunpai-*.ypbak`；必须复核候选列表后显式 `--apply`。

每次 schema 升级都必须在维护窗口内先用旧版本程序创建并验证停机备份；迁移完成后、恢复业务写入前，立即用新版本程序重新执行一次完整 `backup --require-stopped` 和 `backup-verify`。验证器按 `Database.SCHEMA_VERSION` 精确匹配，因此升级后的程序会拒绝升级前生成的 `.ypbak`；旧归档及其匹配程序必须保留到新 schema 归档完成验证和隔离恢复演练后，期间不得被保留策略提前清理。

### 恢复演练与正式恢复

1. 先使用当前运行实例执行最后一次在线备份并验证。
2. 停止 Agent 服务，确认 `agent.sqlite3-wal/-shm` 和 `checkpoints.sqlite3-wal/-shm` 不存在。存在侧文件时不得手工删除，应先查明仍占用数据库的进程并完成干净关闭。
3. 优先恢复到新目录并启动隔离实例验证：

```powershell
yunpai-agent backup-verify D:\yunpai-backups\yunpai-....ypbak
yunpai-agent backup-restore D:\yunpai-backups\yunpai-....ypbak `
  --target-data-dir D:\yunpai-restore-test
```

4. 覆盖正式目录时必须显式 `--force`。命令会先把原双库移动到独立 rollback 目录，完整安装和复验新双库后才写恢复回执：

```powershell
yunpai-agent backup-restore D:\yunpai-backups\yunpai-....ypbak `
  --target-data-dir D:\yunpai-data --force
```

5. 使用恢复目录执行 `yunpai-agent init`、`yunpai-agent eval`，再启动服务并检查 `/health`、`/ready`、outbox 待核对/死信、会话抽样和审计。
6. 验收失败时再次停止服务，根据恢复命令输出的 receipt 回滚。回滚会恢复旧双库，并把当前恢复版本保留为 forward 目录：

```powershell
yunpai-agent backup-rollback D:\yunpai-data\restore-receipt-....json `
  --target-data-dir D:\yunpai-data
```

服务、恢复和回滚共同使用 `.yunpai-runtime.lock`。同一数据目录已有服务或维护操作时，第二实例、恢复或回滚必须失败，不能绕过锁文件强行替换。

### 密钥轮换

新备份应立即改用新 `BACKUP_KEY_ID/BACKUP_ENCRYPTION_KEY`。需要迁移仍在保留期内的旧归档时，当前 `BACKUP_*` 指向旧钥，`BACKUP_NEW_*` 指向新钥：

```powershell
$env:BACKUP_NEW_KEY_ID = "appliance-2026-q4"
$env:BACKUP_NEW_ENCRYPTION_KEY = "new-32-byte-key-in-base64"
yunpai-agent backup-rekey D:\yunpai-backups\old.ypbak `
  --output D:\yunpai-backups\old-rekeyed.ypbak
yunpai-agent backup-verify D:\yunpai-backups\old-rekeyed.ypbak
```

换钥保留原 `archive_id` 和快照清单，生成新的随机 salt/nonce 和认证密文；新归档验证通过且异地副本完成后，才按双人复核流程清理旧归档和旧钥。

## 发送队列处置

- 生产保持 `OUTBOX_WORKER_ENABLED=true` 和 `OUTBOX_SYNC_DISPATCH=false`；`OUTBOX_LEASE_SECONDS` 不得低于单次平台 HTTP 超时，本版本环境变量最低为 30 秒。
- `/ready` 的 `outbox_worker` 必须为 `true`。`GET /v1/integrations/taobao/outbox/summary` 中 `requires_reconciliation` 或 `dead_letters` 大于零时必须告警并进入人工队列。
- `retry_scheduled` 仅用于连接尚未建立等能够证明请求未送出的错误；达到尝试预算后进入 `dead_letter`，不会继续自动重试。
- `uncertain` 表示外呼已开始但未取得确定回执。禁止直接重复发送；先在平台侧按会话、时间和脱敏正文核对，再通过管理后台或 `POST /v1/integrations/taobao/outbox/{id}/reconcile` 记录证据。
- 平台确认已送达时选择 `confirmed`；确认未投递时选择 `not_delivered`，系统才会以原幂等记录重新排队；平台明确拒绝时选择 `rejected`。
- 每次核对必须提交当前 `record_version` 和至少 8 个字符的处置说明。版本冲突必须刷新后重做，不能覆盖另一位管理员的结果。
- 进程退出会等待当前平台调用结束。若被强杀，重启时外呼前过期租约可恢复排队，外呼后过期租约只能进入 `uncertain`。

## 渠道 Agent 队列处置

- 生产保持 `CHANNEL_AGENT_WORKER_ENABLED=true`，轮询、租约、批量、最大尝试和退避分别由 `CHANNEL_AGENT_POLL_SECONDS`、`CHANNEL_AGENT_LEASE_SECONDS`、`CHANNEL_AGENT_BATCH_SIZE`、`CHANNEL_AGENT_MAX_ATTEMPTS`、`CHANNEL_AGENT_RETRY_BASE_SECONDS/MAX_SECONDS` 控制。
- `CHANNEL_AGENT_LEASE_SECONDS` 必须长于一次 Agent 最坏执行时间。`running` 租约过期会在下一轮恢复为 `retry`；达到最大尝试数进入 `dead_letter`，不会无限重试。
- 使用后台“渠道接待”或 `GET /v1/integrations/taobao/agent-jobs` 查看 `stage`。`agent_completed` 表示回复已持久化但下游动作未完成，重试会复用原 invocation，不应手工补写消息。
- `blocked` 的 `automation_disabled/control/safety_gate` 是策略或所有权阻断，不按运行异常处理。应核对目标店铺策略、稳定分桶和会话 owner 后创建新事件或新策略，不改历史账本。
- `dead_letter` 必须结合审计、Agent invocation 和 context snapshot 分析。当前管理 API 只会领取到期 `queued/retry`，不会直接重放死信，防止管理员绕过证据和发布门禁。
- `delivery_rejected/delivery_uncertain/delivery_dead_letter` 来自 outbox 异步回执。先按发送队列流程核对平台实际状态；该回执已经反写发布观测并可能暂停策略，禁止单独重启 Agent 任务重复回答。
- shadow 必须满足零线上副作用：允许产生 invocation、脱敏消息、上下文快照和发布观测，不允许产生草稿、人工任务、SOP run 或 outbox。上线前用客户脱敏样本抽查这一约束。

## 数据保留

入口会在写入 checkpoint 前脱敏。执行 `yunpai-agent retention` 可预览到期数据，确认后使用 `yunpai-agent retention --apply`；管理 API 为 `POST /v1/maintenance/retention`。默认消息保留 30 天、审计 365 天。

应用层清理不能替代磁盘和备份加密。生产部署仍需启用设备全盘加密、限制数据库目录权限，并为备份设置同等级保护。

## 虚拟店铺验收

只在隔离、本地或明确的演示租户运行：

```powershell
yunpai-agent simulate-store
```

HTTP 方式先用管理员身份读取 `GET /v1/simulations/virtual-store`，再向 `POST /v1/simulations/virtual-store/run` 显式提交 `confirm_virtual=true`。完整运行应返回 13/13 场景通过、7 个 `available` 模块覆盖通过和 `production_claim=false`；立即重放时商品 6、库存 10、订单 8 以及竞品证据应返回 `idempotent`，知识应复用。`--skip-customer-service` 会跳过客服、派单和客服评测场景，只能用于经营数据诊断，不能作为验收通过证据。

模拟数据不得导入生产租户，也不得替代真实平台权限、客户脱敏标注、合法竞品来源、长稳、容量、安全或灾备 Gate。执行完成后的 `simulation.virtual_store.completed` 审计可用于定位 run ID 和汇总，不保存密钥或原始顾客对话。
