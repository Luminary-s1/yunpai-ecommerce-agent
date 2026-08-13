# Codex 指令：M5-R 店铺业务日历 + D-037 三元身份（schema v32）

把本文件整份当作实现任务书。产品已裁定，不要再问 A/B/C。先红测，再改生产语义。

## 0. 开工必读

按仓库 `Agents.md` 与 `CONTRIBUTING.md` 第 5、9、10、11 节执行。实现前必读：

- `docs/tasks/M5R_TRAFFIC_LAB_WORKBENCH.md` 第 6 节与「实现纪律（执行者必读）」
- `docs/M5R_TRAFFIC_LAB_PENDING_DECISIONS_20260813.md`
- `.project-to-act/PROJECT_OVERVIEW.md` 的 D-034、D-035、D-037、D-040
- 本文件

本 worktree 已有第 1–9 项未提交修复（公开实验生命周期、D19 假绿、Traffic AI interpreter、store scope、freshness、provenance、forecast fallback、SKU universe、horizon 联合校验）。**不得还原这些改动**，在它们之上继续。

## 1. 已冻结的产品裁定

### 1.1 时区 / Switchback 日历：选项 A

- 建立唯一权威 `(tenant_id, store_id)` 业务日历记录：IANA timezone、record version、effective time、审计主体。
- 窗口仍以 UTC 存储。只在 date / weekday / hour 平衡检查时转换到固化时区。
- 实验在创建或进入 `ready` 前解析该记录，并把 timezone、记录 ID/version、policy version 固化到实验和分析 evidence。
- 缺权威记录：fail closed，`store_business_timezone_required`。
- 旧实验缺固化证据：可读，分析 blocked 为 `business_timezone_evidence_missing`。
- **禁止**回退到服务器时区、import `source_timezone`、Forecast `DEMAND_V1.timezone`、写死 `Asia/Shanghai`，或让实验请求覆盖店铺日历。
- 店铺后续改时区只产生新 version；已完成/已固化实验按旧 version 重放。
- V1 **不要**做节假日、财政周或完整日历产品。
- `virtual_store_v1.json` 已有 `store.timezone=Asia/Shanghai`。`simulate-store` 必须写入权威记录，虚拟路径不得靠猜测过门。
- 非法/未知 IANA timezone 拒绝。DST 只用 `zoneinfo.ZoneInfo`，不用固定 offset。

### 1.2 D-037 source identity：选项 A

- 持久身份改为 `(tenant_id, connector_id, source_id)`。保留 connector 原生 ID。
- 同 tenant、同 native `source_id`、不同 connector 必须各自写入、独立 version、互不覆盖。
- 同 connector 的 stale / conflict / idempotent / version 语义保持 D-014。
- normal / quarantine 只在同一三元身份间互斥，禁止跨 connector 删除或覆盖。
- accepted 新写入必须有 connector；若请求只给 revision，由同一领域 helper 从不可变 `listing_revisions.connector_id` 确定，并校验 tenant/revision 一致。
- quarantine 新写入不允许缺 connector。
- 历史 quarantine 缺 connector：进入显式隔离域 `legacy_unscoped`，**禁止分析**。迁移时不得猜测归属。以后若要认领，必须是带审计的显式运维动作。
- SQLite `UNIQUE` 把多个 `NULL` 当成互不相同。隔离域 **不能** 用 `connector_id IS NULL`。使用显式 sentinel，例如 `connector_id='legacy_unscoped'`，分析查询必须排除该 sentinel。
- 不要把 `store_id` 塞进 metric 身份。不要把 `source_id` 改写成 `connector:native` 字符串。
- D-037 其余部分不变：metric 仍绑定不可变 listing revision；关系键仍带 `tenant_id`；缺失/未知/歧义/越界仍不进分析。

## 2. Schema 必须用 v32，禁止 v31

**v31 已被占用，本工作占 v32。**

事实：

- origin PR #11（`https://github.com/a1024053774/yunpai-ecommerce-agent/pull/11`，分支 `codex/workspace-conversation-history`）已登记 **schema 31** 给统筹 Agent 会话表，并新增 `_apply_v31`。
- origin PR #12 正文写明不新增 Schema；不要把它当成 v31 来源，也不要因此去抢 v31。
- 本 worktree 的 `CONTRIBUTING.md` 仍可能写着 `31+ 空闲`，`Database.SCHEMA_VERSION` 仍为 30。那是本地未同步 PR #11，不是许可。

执行：

1. 按 CONTRIBUTING 第 9 节搜全部 local/remote 分支的 `_apply_vNN`，确认 v32 仍空闲。若已被占，改用下一空闲号，并在 PR/台账写明。
2. 在 `CONTRIBUTING.md` 占号表加入 v32：M5-R / 本分支 / 店铺业务日历 + Traffic metric 三元身份 / 已占用，开发中。把 `31+ 空闲` 改成「31 已被 PR #11 统筹会话占用（即使本分支尚未合并该代码）；32 本工作占用；33+ 空闲」。
3. 新增 **唯一** `_apply_v32`。方法名不得与任何分支重名。不要写 `_apply_v31`。
4. `SCHEMA_VERSION` 取合并后较大值；`initialize()` 里 `if 32 not in applied` 块与别人的 31 块并存，禁止整段覆盖。
5. 测试只断言 `32 in migrations`、本工作的表/列/唯一键存在。禁止 `assert schema_version == 32`，禁止冻结全局用例数。
6. 同步 `_validate_schema`：新表必须加条目，先搜同名键，避免重复字典键静默覆盖。
7. 更新灾备/operations 备份策略：v30（或升级前实际版本）程序做停机备份；升级后、恢复写入前立即用新程序做并验证全量备份。v32 验证器会拒绝旧 `.ypbak`。旧归档与匹配程序保留到恢复演练通过。
8. 用户已授权本任务修改 `CONTRIBUTING.md` 占号表。不要改第 10/11 节或无关段落。

`_apply_v32` 建议顺序（同一迁移内分两段）：

1. 新建店铺业务日历表；实验/分析 evidence 用 **可空** 列固化 timezone、calendar id/version。旧行缺值，分析 fail closed，不能给无默认 `NOT NULL`。
2. 以新表重建 `traffic_metric_buckets` / `traffic_metric_quarantine`：非空或 sentinel `connector_id`、三元 UNIQUE、三元查询/互斥、对应索引。accepted 历史行经不可变 `listing_revision_id` 回填 connector，并逐行核对 tenant/revision。quarantine 从已冻结 `payload_json.connector_id` 取值；缺失则写入 `legacy_unscoped`。copy 后校验行数、payload hash、version、FK、normal/quarantine 三元互斥，再原子换表。

## 3. 实施顺序

### 包 1：店铺业务日历（先做）

先写红测，确认它们在旧实现失败，再改代码。

红测至少覆盖：

- 本地（Asia/Shanghai）平衡、UTC 看似不平衡的跨午夜正例：date/weekday/hour 全平衡，当前 UTC 算法应红、修复后应绿。
- UTC 平衡、本地实际不平衡的反例：当前零 issue，修复后必须报 imbalance。
- tenant/store 隔离；缺 timezone fail closed。
- 时区变更后旧实验按固化 version 重放，新实验用新 version。
- 非法/未知 IANA 拒绝；DST 边界不用固定 offset。
- `simulate-store` 后虚拟店存在权威日历，D19 分析不被 `business_timezone_evidence_missing` 误伤。

实现要点：

- 单一领域服务读写日历，禁止 Traffic/Forecast 各写一份。
- `_check_switchback_design` 用实验固化的 IANA timezone 解释 `window_start` / `window_end` 的 hour、date、weekday。duration 比较仍用绝对时间，不必本地化。
- 分析 evidence 写入固化 timezone 与 calendar version，保证可重放。
- 本次 **不要** 把 Forecast `DEMAND_V1.timezone` 改成读店铺日历。那是后续工作。
- D19 窗口目前是 UTC 08-10/11 的 0/2/4/6 时，换上海后仍落在同一本地日。不要为了「看起来更本地」去改 D19，除非红测证明它在新日历下假绿或假红。

### 包 2：D-037 三元身份（日历红绿后再做）

先写红测，确认旧实现失败，再迁移。

红测至少覆盖：

- 同 tenant、同 native ID、不同 connector 可各自写入且重放独立。
- 同 connector 的 stale/conflict/idempotent/version 语义保持。
- normal/quarantine 只在同一三元身份间互斥，不跨 connector 删除或覆盖。
- tenant 隔离；listing revision connector mismatch 拒绝。
- v30→v32 accepted/quarantine 迁移；缺 connector 的 legacy 行进入 `legacy_unscoped` 且分析不可见。
- 备份 manifest 兼容与重复初始化。
- 竞品表已有的 `(tenant_id, connector_id, source_id)` 不要改坏。

实现要点：

- 所有 `WHERE tenant_id=? AND source_id=?` 的 metric/quarantine 路径改成三元查询。先全仓搜 `source_id=?`。
- importer / sync / operations 透传 connector，不得丢字段。
- 派生 source_id 的稳定算法若已包含 connector，保持；若没有，不要靠改 source_id 字符串冒充三元身份。
- 更新 D-037 在 `PROJECT_OVERVIEW.md` 的风险控制表述，使其指向三元身份，并保留 revision 绑定原意。不要发明新的决策号替代 D-037。

## 4. 验证

环境：

```bash
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ALL_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 .venv/bin/python -m pytest -q <files>
```

最低验证：

1. 新红测：先失败后转绿。
2. 用户规定的 14 文件聚焦集 + `tests/test_source_provenance.py` 仍通过。
3. 迁移、灾备、Traffic ingestion/analysis/API/virtual 相关测试。
4. `python -m compileall -q src`
5. `git diff --check`
6. 若改了 `docs/admin-console.html`，做 JavaScript syntax 检查。
7. project-to-act `--validate`

不要声称全仓全量、真实数据、长稳或生产放行，除非你真的跑了。

## 5. 明确不要做

- 不要用 v31，不要写 `_apply_v31`。
- 不要选时区 B/C，不要选身份 B/C。
- 不要在迁移时手工猜 quarantine 的 connector。
- 不要扩大到 Forecast 时区切换、节假日日历、隔离处置 UI、模块登记或生产 Gate。
- 不要改 LangGraph / intent / 关键词路由。
- 不要还原第 1–9 项未提交修复。
- 不要新增全局计数全等断言。
- 未提交、未推送，除非用户另行要求。

## 6. 台账

有效工作节点更新 `.project-to-act/`：

- `PROJECT_OVERVIEW.md`：D-040 保持；D-037 在三元身份落地后更新风险控制，不写成未实现却已生效。
- `PROJECT_FEATURES.md`：F-322 从已规划改为进行中/已完成时必须带证据。
- `PROJECT_VERSIONS.md`：v32 兼容、备份策略、读侧路径。
- `PROJECT_ACCEPTANCE.md`：红绿证据；未跑全量就不要写全量。

写入后跑 validate。
