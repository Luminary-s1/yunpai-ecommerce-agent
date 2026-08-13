# M5-R Traffic Lab 产品裁定与 schema v32 契约

日期：2026-08-13
审计基线：`dbf2027`
状态：产品已裁定；schema v32 已在 F-322 分支实施并通过全量验证。metric revision-only 审查修复以 E-20260813-019 取代 E-20260813-018 的对应写入结论。不得使用 schema v31。

## 0. 占号与裁定摘要

- 产品裁定（2026-08-13，本会话确认）：
  1. Switchback 使用版本化店铺业务日历（选项 A），缺配置 fail closed。
  2. D-037 持久身份改为 `(tenant_id, connector_id, source_id)`（选项 A）；缺 connector 的历史隔离行进入 `legacy_unscoped`，禁止分析。
- Schema 占号：本工作使用 **v32**，不使用 v31。
  - origin PR #11 `feat: persist unified agent workspace conversations`（`codex/workspace-conversation-history`）已把 CONTRIBUTING 中的 **31** 登记为统筹 Agent `workspace_conversations` / `workspace_messages`，并将 `Database.SCHEMA_VERSION` 设为 31、新增 `_apply_v31`。
  - origin PR #12 `feat(workspace): support composite read queries` 正文写明不新增 Schema；它依赖 #9，不是 v31 占号来源。
  - 开工时 `main` / 本 worktree 为 `SCHEMA_VERSION = 30`，CONTRIBUTING 本地副本仍写 `31+ 空闲`；按第 9 节扫描全部远端 refs 后确认 v32 空闲并完成占号。
  - 当前实现只有 `_apply_v32`，没有回写 `_apply_v31`；合并 PR #11 时必须同时保留 31/32 两个 migration block。
- 实现顺序：先店铺业务日历与 switchback 红测，再三元身份迁移。同一 PR 可共用 `_apply_v32`；若拆 PR，日历占 v32，身份包开工前再搜下一空闲号。

## 1. Switchback 使用哪一个业务日历

### 已确认事实（裁定与实施前基线）

- `TrafficAnalysisEngine._check_switchback_design` 直接从已经规范为 UTC 的 `window_start` 计算 hour、date 和 weekday。
- Traffic import 的 `source_timezone` 只用于解释无时区输入；指标入库后仅保留 UTC 时间，experiment 与 analysis evidence 都不保存该来源时区。
- 数据库没有 tenant/store profile 或店铺业务时区服务。
- virtual fixture 虽含 `store.timezone=Asia/Shanghai`，该字段没有进入领域写入或分析证据。
- `DEMAND_V1.timezone=Asia/Shanghai` 是 Forecast 全局需求政策，不是 tenant/store 的权威映射，不能借用为 Traffic 的店铺事实。

### 双向反例

| 构造 | 当前 UTC Gate | Asia/Shanghai 业务日历 |
|---|---|---|
| 本地平衡：control/treatment 都覆盖本地 8 月 10/11 日、周一/周二、0/23 时各一次 | 报 `switchback_date_distribution_imbalanced`、`switchback_weekday_distribution_imbalanced` | date、weekday、hour 全部平衡 |
| UTC 平衡：control/treatment 都覆盖 UTC 8 月 10/11 日、周一/周二、0/23 时各一次 | 零 issue | control 为本地 8 月 10/12 日，treatment 为 8 月 11 日两次；weekday 同样失衡 |

因此，保持 UTC 与改为上海时区会让两组实验得到相反的质量门结论；这不是无语义的实现修正。

### 最小设计选项

#### A. 版本化店铺业务日历（推荐）

- 建立唯一权威的 `(tenant_id, store_id)` business-calendar 领域记录，至少保存 IANA timezone、record version、effective time 与审计主体。
- experiment 在创建或进入 ready 前解析该记录，并把 timezone、记录 ID/version 与 policy version 固化到实验/分析 evidence；窗口仍以 UTC 存储，只在日历平衡检查时转换。
- 缺少权威记录时 fail closed 为 `store_business_timezone_required`；旧实验缺固化证据时可读，但分析明确 blocked 为 `business_timezone_evidence_missing`，不按服务器时区或 Forecast 全局设置猜测。
- 店铺时区后续变更不重解释已完成实验；新实验使用新版本。

优点是单一事实可供后续经营模块复用，证据可重放；代价是需要 additive schema/API 与 legacy read path。

#### B. 每个实验显式提交 business timezone

- 在 experiment create 请求中强制 IANA timezone，并与 policy version 一起固化。
- 不建立店铺 profile，分析只读取实验快照。

改动较小，但同一店铺不同实验可被填成不同日历，重复事实且更难治理。

#### C. 明确规定 UTC 为 Traffic 实验日历

- 将 UTC 写入版本化分析 policy/evidence，并保留当前算法。

兼容成本最低，但不满足“按店铺本地日期/星期平衡”的业务目标；除非产品明确选择 UTC，否则不建议。

### 裁定后必须先写的红测

- 本地平衡、UTC 看似不平衡的跨午夜正例。
- UTC 平衡、本地实际不平衡的反例。
- tenant/store 隔离与缺 timezone fail-closed。
- timezone 变更后旧实验仍按固化版本重放，新实验使用新版本。
- 非法/未知 IANA timezone 拒绝，DST 边界不使用固定 offset 猜测。

## 2. D-037 metric source identity 是否包含 Connector

### 已确认事实（裁定与实施前基线）

- Connector SDK 的 `PullRecord.source_id` 只是字符串；`ConnectorCapabilities` 没有 identity scope，registry 只保证 `connector_id` 自身唯一。
- 两个不同 connector 的 `PullBatch` 可同时合法携带 `source_id=native-42`，说明 SDK 不保证租户内全局唯一。
- Operations sync 原样透传 connector ID 与 source ID。
- `traffic_metric_buckets` 和 `traffic_metric_quarantine` 均以 `UNIQUE(tenant_id, source_id)` 决胜；normal 表甚至不单列 connector ID。
- 临时 v30 DB 探针显示：同 tenant/source、同 data_as_of、不同 connector 会报 `source_version_conflict`；较新的第二 connector 记录会复用原 bucket ID、把 version 升至 2，并改绑第二个 listing revision。
- Competitive 表已使用 `UNIQUE(tenant_id, connector_id, source_id)`，说明项目内原生 ID 按 connector 隔离已有先例。

VirtualTaobao fixture 当前 ID 碰巧不重复，只是 fixture 事实，不能替代 Connector 契约。

### 方案 A：三元持久身份（推荐）

把 accepted/quarantine 的逻辑身份改为 `(tenant_id, connector_id, source_id)`，保留 connector 原生 ID。

最小 schema/migration 提案：

1. 在 main 上预留下一 schema 版本并更新 CONTRIBUTING reservation、D-037 和备份兼容说明。
2. SQLite 以新表重建 accepted/quarantine，新增非空 connector ID、三元 UNIQUE、三元查询/互斥迁移及对应索引。
3. accepted 历史行通过不可变 `listing_revision_id` 联表取得 connector ID；逐行核对 tenant/revision 一致性。
4. quarantine 历史行从已冻结 `payload_json.connector_id` 取得；缺失 connector 的 legacy 行不得猜测，可选择迁移前人工归属，或进入显式 `legacy_unscoped` 隔离域并禁止参与分析。该选择也需在迁移批准时确认。
5. copy 后校验行数、payload hash、version、foreign key、normal/quarantine 三元互斥，再原子换表并重建索引。
6. 新写入要求显式 connector；若 accepted 请求只给 revision，可由同一领域 helper 从 revision 确定 connector，quarantine 不允许无 connector。
7. v30 备份只由 v30 程序恢复；升级前后各生成并验证停机备份，保留旧程序与归档直到恢复演练通过。

优点是契约清楚、保留原生 ID、与 competitive 先例一致；代价是 schema migration 和 legacy quarantine 归属决策。

### 方案 B：Connector 契约强制 tenant-global canonical ID

- 在 capabilities 增加显式 `source_id_scope`，或由统一 helper 将 `(connector_id, native_source_id)` 编码为持久 canonical source ID；原生 ID另存 evidence。
- registry/sync 拒绝不声明或不满足该能力的 connector，不能仅靠字符串前缀约定。
- 历史记录需要 canonical ID backfill/alias，accepted connector 可从 revision 得到，quarantine 缺 connector 仍需 legacy 处置。

该方案可以不改唯一键形状，但会改变已有 source ID、引用和 replay 语义；实际上仍需要数据迁移与兼容别名。

### 方案 C：正式要求外部 Connector 自行保证 tenant-global ID

- 只更新 Connector contract/capability，无法保证者不得注册或同步 Traffic metrics。

改动最小，但限制第三方接入且无法从任意字符串自动验证保证；只适合所有目标 connector 都能给出并履行该承诺时选择。

### 裁定后必须先写的红测

- 同 tenant、同 native ID、不同 connector 可各自写入且重放独立。
- 同 connector 的 stale/conflict/idempotent/version 语义保持。
- normal/quarantine 只在同一三元身份间互斥，不跨 connector 删除或覆盖。
- tenant 隔离与 listing revision connector mismatch 拒绝。
- v30 accepted/quarantine migration、缺 connector legacy 行处置、备份 manifest 兼容与重复初始化。

## 已确认的产品决定

1. Switchback：**A**。建立版本化店铺业务日历；实验固化其版本；缺配置与旧实验缺固化证据一律 fail closed。否决 B（每实验显式时区）和 C（正式 UTC 日历）。V1 只存 IANA timezone + version + effective time + 审计主体，不做节假日/财政周。实验请求不得再提供可覆盖店铺日历的 timezone。虚拟 fixture 的 `store.timezone` 必须经 `simulate-store` 写入权威记录。
2. D-037：**A**。持久身份改为 `(tenant_id, connector_id, source_id)`。缺 connector 的历史 quarantine 进入显式隔离域 `legacy_unscoped` 并禁止分析；迁移时不得猜测归属。否决 B（canonical 字符串命名空间）和 C（只接受自称 tenant-global 的 Connector）。D-037 的 revision 绑定、租户外键、未知不进分析、`data_as_of + payload_hash` 版本语义保持不变。

完整执行说明见 `docs/tasks/M5R_TRAFFIC_LAB_V32_CODEX_HANDOFF.md`。

## 实施结果

- D-040：`store-business-calendar-v1` 以 `(tenant_id, store_id)` 保存 IANA timezone、record version、effective time 与审计主体；实验固化 calendar ID/version/timezone/policy。缺配置拒绝创建，legacy 缺证据分析 blocked；本地 hour/date/weekday 使用 `ZoneInfo`，D19 使用 simulate-store 写入的 fixture 店铺日历。
- D-037：v32 将 accepted/quarantine 重建为 `(tenant_id, connector_id, source_id)`；accepted 从不可变 revision 回填/校验 connector，quarantine 只读冻结 payload，缺失进入 `legacy_unscoped`。新 quarantine 缺 connector 拒绝，分析显式排除 sentinel，同三元 normal/quarantine 冲突使迁移失败。
- Metric 写入审查修复：accepted 在 D-014 hash 前由不可变 revision 补全 connector/store/item/sku；revision-only 出窗隔离复用相同身份解析。omit/explicit 同戳重放和 quarantine→accepted 提升幂等；v30 已保留的旧 revision-only hash 通过明确兼容候选重放，不改写历史记录。
- 证据：首轮旧实现日历 `8 failed`、身份 `5 failed, 1 passed`；审查新增两条反例先精确 `2 failed`，修复后定点 `3 passed`、Traffic/v32/provenance `71 passed`、迁移/灾备/身份 `41 passed`、规定 14 文件加 provenance `135 passed`（E-20260813-019）。未运行仓库全量、真实数据或长稳，不构成生产放行。
