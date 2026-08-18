# 项目版本

> 只在版本号、发布状态、升级路径或兼容性发生变化时读取和更新。

## 当前版本

- 版本号：`0.30.0`。权威来源为 `pyproject.toml` 的 `[project].version` 与
  `src/ecommerce_agent/__init__.py::__version__`；两处必须一致。
- 发布状态：`main` 已包含 0.30.0 之后的客服、M5-R、M6-R、F-322、知识库和 M7-R WP1 增量，但这些
  提交没有同步提升运行时包版本，因此不得把历史内部候选标签 `0.31.0`～`0.33.0` 写成
  当前运行时版本。生产放行继续阻塞。
- 兼容性说明（0.30.0 运行时 + main 未升包增量）：schema v28 additive 新增 Traffic Lab 六类核心表、一张 metric 隔离表、索引、复合租户外键和 revision 不可变触发器；v27 可前向迁移。WP2 不改 schema 或依赖；虚拟 Connector capability 1.2 additive 增加 `listing_revision` / `traffic_metrics`，通用 sync 响应 additive 增加幂等、隔离计数和回执。WP3 沿用 v28、无新依赖/HTTP API，additive 导出 `TrafficFeatureEngine` 与版本化特征契约；`image-v1` 保留读侧与旧算法，`image-v2` 为当前版本，同一 asset 可显式选择版本重算且不更新资产。WP4 沿用 v28；Python 包不再公开任意统计载荷 `TrafficAnalysisRunCreate`，调用方改用只接收实验 ID 的 `TrafficAnalysisEngine`；当前新分析显式要求 `traffic-analysis-v2`，历史 v1 run 保持可读；黑盒 runner 报告 additive 增加 `ground_truth_boundary`，保留原 `analysis_imported_ground_truth` 字段但改由运行轨迹审计派生。WP5 沿用 v28、无新依赖或迁移，additive 增加管理员限定的 `/v1/traffic-lab/*` 工作流、`traffic_lab` available 模块与模型可见的只读 `get_listing_traffic_insights`；既有 API 响应契约、LangGraph 拓扑和语义路由不变，控制台只在管理员显式点击后运行分析，未加入自动发布、改标题/换图或投放动作。M6-R WP1–WP2 以 schema v29 固化 demand fact 与 forecast engine；WP3 以 schema v30 additive 增加 planning policy/plan、quantity/quality/risk evidence 和不可变边界，v29 可前向迁移且不重建既有表；WP4 沿用 v30，无依赖或迁移变化，additive 增加 `/v1/forecasting/*`、两个只读工具、D20 与显式运行后台，既有 API/路由/拓扑不变；WP5 仍沿用 v30，新增纯 Python Eval fixture/runner/report 与 D-039 oracle 边界，不改变依赖、持久 schema、API 或生产路由。F-322 未单独升版，使用 schema **v32**；**v31 已被 origin PR #11 占用**，合并时须保留两段迁移。v32 新增版本化 `(tenant,store)` IANA 业务日历和 nullable experiment 固化证据，并将 Traffic accepted/quarantine 重建为 `(tenant,connector,source_id)`；v30（或合并后的实际前序版本）可前向迁移，accepted 从不可变 revision 回填 connector，quarantine 仅从冻结 payload 读取，缺失写 `legacy_unscoped`。历史实验可读但缺日历证据时分析 blocked。灾备 manifest 继续精确匹配当前 schema：升级前以旧程序完成停机备份，升级后恢复写入前以 v32 程序生成并验证新全量备份；旧归档与匹配程序保留到隔离恢复演练通过。
- 占号状态：PR #10 已合入 main `1906365`，schema **v33** 在 `main`（knowledge_key 唯一索引 + retrieval_logs）。F-322 **v32** 已在 main。PR #11 的 `_apply_v31`（workspace 会话表）仍占用中、未合入。M7-R WP1 的功能提交 `0b54a24` 已把 **v34** 合入 main；合 #11 时必须 31+32+33+34 四块并存，扫描 `MERGE-GATE PR-11`。下一空闲号 **35**。
- 最后更新：2026-08-18

## M7-R WP2 报表适配与规范化写入（未单独升应用版）

- 状态：`generic-cn-v1` 开发自测候选位于 `a5d02ed`，尚未合入 main，仍须随 WP1～WP4 接受缪海南的正式 WP5 独立复验。
- schema / 依赖：沿用 schema v34，无新迁移、第三方依赖、HTTP API、Agent 路由、LangGraph 语义或平台写动作；WP3 若确需持久映射 schema，必须先在 main 预留下一空闲版本。
- 兼容性：additive 导出 `REPORT_ADAPTERS`、`ReportImportRequest`、`ReportImportJob` 与 `ReadonlyReportIngestionService`。`generic-cn-v1` 接受严格 UTF-8 CSV 和受控 XLSX，旧 WP1 契约保持可用；同版本不同内容与旧来源在领域写入前拒绝。新的真实平台字段契约必须使用独立 mapping version，不得静默改写已冻结输入语义。
- 样本边界：仓库及会话附件没有经授权的淘宝/天猫真实受控导出；当前中文别名是通用候选，不代表真实平台列已完整确认。原始文件上传仍由受控存储边界负责，导入服务只接收内容和已批准引用。
- 验证：E-20260818-001；初始缺实现收集退出 2，订单子事实覆盖、Excel 日期序列和 Demo 显式回执三项回归均先红后绿。WP1+WP2 聚焦 `77 passed`，最终全量 `1008 passed, 24 warnings`，无 failed/skipped/xfailed；24 条为既有 FastAPI 重复 Operation ID warning。

## M7-R WP1 只读经营数据统一契约（未单独升应用版）

- 状态：WP1 开发自测候选已合入 main，仍等待缪海南在干净状态执行 WP5 独立复验；当前运行时包版本保持 `0.30.0`，不得据此声明 M7-R 或生产放行通过。
- schema：v34 additive 新增 `readonly_import_manifests`、`readonly_import_row_issues`、`readonly_field_evidence` 三张表及证据不可变触发器；支持 v33→v34 前向迁移，不重建既有表。v34 已合入 main，下一空闲 schema 为 v35。
- 兼容性：新增统一的 `actual/manual/demo` 来源、`actual/manual/demo/missing` 证据、字段名/字段值双层隐私过滤、受控 storage reference、manifest/逐行隔离和 D-014 版本契约；manifest 输入以解析器观察的 `parsed_rows` 为单一行数入口，accepted / quarantined / rejected 由逐行问题派生。无新依赖、HTTP API、Agent 路由、LangGraph 语义或生产动作。平台专属字段白名单、真实解析与数据域导入归 WP2。
- 灾备：v34 会使精确匹配旧 schema 的灾备 manifest 失效；升级前用旧程序备份，升级后立即生成 v34 全量备份，并保留旧程序和旧归档，直到隔离恢复验证完成。
- 验证：初始 E-20260817-003，独立反馈收口 E-20260817-004，既有 M4/知识库测试欠账收口 E-20260817-005，main 集成见 E-20260817-007。WP1 最终聚焦 `19 passed`，迁移/Traffic Lab/灾备/CLI 组合 `61 passed`；三项 red-first 契约覆盖 allowlist 值携带 PII、证件/邮箱/邮编字段名和调用方伪造质量计数。随后原七项欠账定点 `7 passed`、skip/xfail 相关集合 `59 passed`、关联 `108 passed`；隔离 main 集成树全量 `950 passed, 24 warnings`，无 failed/skipped/xfailed。E-005～007 未改变依赖、租户权限、D-034 语义权威或 WP1 冻结范围。

## M6-R WP5 Forecast Eval（未单独升版）

- 状态：覆盖 WP1–WP5 的同一 Grok 4.6 xhigh 会话完成两轮对抗验收与最终整链合入审阅，明确批准 `03d3b85` 从 `4065b12` 快进；该精确代码 tip 已合入并推送 `main`，已合入工作包分支完成清理。
- 兼容性：沿用 schema v30 与现有依赖；只新增 `evals/forecasting/` synthetic fixture、可复跑 CLI 和测试。生产 API、Agent 目录、LangGraph/intent/prompt、迁移和自动动作均未改变。
- Eval：十类序列全部使用 rolling-origin；对 test window 做未来扰动不变性检查，数值评分 WAPE/Bias/P80/P95 覆盖与 baseline fallback；库存场景调用公开 planning service。oracle 只在生产调用后进入 scorer，并以实际字段 overlap/unexpected 调用 Gate 审计；存在回测误差时零宽 P80/P95 明确失败。
- 对抗修复：计划证据 JSON 解析与结构类型失败统一为 `inventory_plan_evidence_invalid`，两个读 API 映射 409；forecast policy 同 `active_from/created_at` 由 `rowid DESC` 确定最新版本。成功响应、schema、依赖、Agent 目录、路由、拓扑和自动动作均不变。
- 验证：E-20260813-001/002/003/004；最终合入审阅的 Grok 独立全量为 `730 passed, 1 xfailed`（255.95 秒），新增 sqlite 拒绝覆盖 probe 与同戳 rowid mutation；合入后的 Codex 独立全量为 `730 passed, 1 xfailed`（300.74 秒），Eval/静态/台账均通过。开发者与 Grok 证据分开，累计 mutation 均失败后还原。服务器 v30、真实数据、灾备实操、长稳和生产 Gate 仍未豁免。

## M6-R WP4 API / Agent / Admin（未单独升版）

- 状态：WP4 验收代码和后续治理提交均在 `67222d7` 祖先链中，并随 WP5 候选 `03d3b85` 经最终整链批准后快进合入并推送 `main`；原 WP4 工作分支已清理。
- 兼容性：沿用 schema v30 和既有依赖；九个管理员 API 均为 additive，GET 只读固化证据，需求重建、policy 配置和 forecast run 需显式 POST/PUT。`forecasting` 登记 available 并由注册表 D20 场景覆盖；LangGraph、intent、prompt、既有 API 响应、采购/付款/库存事实写路径不变。
- Agent / Admin：`get_demand_forecast` / `get_inventory_plan` 经动态目录暴露，只读最新固化 run/plan 并带 run、data hash、库存快照、policy 和质量证据；后台展示历史需求、区间、选定分位库存线、缺货日、建议量、质量和 backtest，只有显式按钮会运行。
- 验证：E-20260812-007/008 与整链合入证据 E-20260813-004；开发者全量 `722 passed, 1 xfailed`（348.84 秒），WP4 独立验收全量 `722 passed, 1 xfailed`（283.82 秒），整链独立与合入后全量均为 `730 passed, 1 xfailed`。服务器 v30、真实数据、长稳和生产 Gate 不豁免。

## M6-R WP3 Inventory Planning（未单独升版）

- 状态：schema v30 planning policy/plan 与确定性库存计划经对抗修复和独立复验后已合入 `main`；当前未提交修复把 `required_forecast_days=max(lead+review, maximum_stock_days)` 与产品 7/14/30 horizons 做配置/run 联合校验，并阻止 superseded plan 作为 current，见 E-20260813-013。无 schema 升版。
- 兼容性：v30 仍只新增 `inventory_planning_policies` / `inventory_plans`、索引、复合 tenant/forecast/policy 外键与不可变触发器，v29 可前向迁移且既有表不重建。quantity/quality/risk evidence 在合入 main 前的 v30 定义内补齐，不另占 v31；正式支持路径只有 v29→修正版 v30，旧分支期临时 v30 数据库/备份需从 v29 备份重建。无新依赖、HTTP API、Agent tool、关键词路由或 `forecasting` available 登记。
- 计算与证据：从 `ForecastRunService.get_run` 和 `InventoryService.list_balances` 的公开投影读取；store+SKU 需求只计算一次；仓级 plan 强制 qty null/withheld。`available=max(0,on_hand-reserved)` 并单列 shortfall；risk 按选定分位缺货日相对 lead/review 分层；forecast degraded/anomaly、inbound day-0、快照混时/陈旧进入 `plan_quality`；service level 仅 0.50/0.80/0.95。
- 安全边界：结果固定为 `advisory_only`，不会创建采购、付款或库存写入。升级前必须用 v29 程序生成并验证停机备份，升级后恢复业务写入前生成并验证 v30 全量备份；精确 schema 校验会拒绝 v29 `.ypbak`。
- 验证：E-002/003 为旧 tip 史；E-004 对抗纠偏；E-005 独立复验：WP3 `15`、聚焦 `69`、全量 `705 passed, 1 xfailed`（230.55 秒），三项 P1 mutation 与八项对抗探针通过；合入后验证见 E-20260812-006。

## M6-R WP2 Forecast Engine（未单独升应用版）

- 状态：`forecast-v1` / `forecast-engine-v1`、七种纯 Python 候选、数值需求类型、rolling-origin backtest、baseline fallback、失败候选隔离及 30 日 P50/P80/P95 已通过两份独立验收并合入 main；当前未提交修复以 `forecast-engine-v2` / `forecast-final-selection-v1` 增加 final-only failure 安全重选与失败证据，见 E-20260813-011。历史 v1 run 保持可读，本机候选未发布。
- 兼容性：沿用 schema v29 已有 `forecast_policies/runs/backtests/points/anomalies`，未新增迁移、依赖、HTTP API、Agent tool、关键词路由、模块 available 登记、库存计划或自动采购/库存写入。`OperationsService.forecast_runs` additive 接入，既有 `forecasting` Demand Fact 服务保持原契约。
- 策略与读侧：所有候选共享同一批时间 origins；零需求窗口 WAPE/Bias 返回不可比并使用 RMSE；challenger 只有达到固定相对改进阈值才可替换 baseline。模型、阈值、interval levels 与 policy version 同行固化，同版本内容漂移明确拒绝；逐窗失败原因与候选资格可读回。
- 验证：开发者候选 E-20260811-006；首份独立验收 E-20260811-007；第二份独立验收与缺日/缺货 `None` 序列门禁补强 E-20260811-008；合入 main 证据 E-20260812-001。合入后聚焦 `39 passed`、全量 `690 passed, 1 xfailed`（253.33 秒）。

## M6-R WP1 Demand Fact 数据层（未单独升版）

- 状态：`demand-v1`、schema v29 daily facts、全量/增量重建、水位、回补和质量标记已合入；当前未提交修复新增无 schema 的 `demand-sku-universe-v1`，store-wide 只从公开 window orders 与 tenant/store inventory balances 取 SKU 并集并记录来源/count/digest，见 E-20260813-012。未新增自动采购或库存写入。
- 兼容性：schema v29 在 v28 基线上仅新增 `demand_daily_facts`、forecast policy/run/backtest/point/anomaly 表、索引与 immutable fact triggers；v28 可前向迁移，既有表不重建。需求事实从 `OrderService.demand_source_orders` 与 `InventoryService.list_balances` 的公开投影读取，未绕过领域服务读取或修改订单/库存表。
- 口径与证据：`demand-v1` 固定 Asia/Shanghai、已支付且未取消订单行、14 日固定回补窗口；重放同一水位不产生新版本，订单取消/更正产生可追溯的新 fact version。无订单真零、来源覆盖缺失和缺货 true/false/unknown 均用独立结构化值或质量标记表达。
- 灾备：升级前以 v28 程序创建并验证停机备份；升级后、恢复业务写入前立即创建并验证 v29 全量备份。v29 验证器按精确 schema 拒绝 v28 `.ypbak`；旧归档及匹配程序保留到新的隔离恢复演练完成。
- 验证：迁移、需求事实、订单/库存关联、M5 Traffic Lab 和既有迁移聚焦回归 `40 passed`；实际反证：改用 UTC 归日、移除取消排除、移除幂等短路分别使跨日、回补、重放断言失败，恢复后复验通过。

## M5-R WP5 Agent / Admin / Eval（未单独升版）

- 状态：完整管理员 HTTP 工作流、持久化洞察只读 Agent tool、显式触发的实验控制台、D19 虚拟店铺场景和六类机制 Eval 通过本机代码级候选；M5-R 整体仍在开发
- 兼容性：沿用 schema v28、无迁移或第三方依赖；所有既有 API 响应契约不变，新增端点均要求管理员租户上下文。同步 `origin/main` 时其数据库基线仍为 v27；本分支保留 v28 migration 与读取兼容，迁移专项及全量回归通过
- 权责边界：动态工具目录供模型选择，不向 `graph.py` 或 `intent.py` 写入 traffic_lab 关键词/正则分支；工具只读取 `traffic_analysis_runs` 已固化的结构化证据，显式标记不重算统计、不主张平台权重。页面的查询仅 GET，分析 POST 只能由管理员操作触发；不会自动发布商品、改标题/图片或投放
- 验证：8 个机制场景覆盖无影响、CTR/CVR、库存、标题/图片、interaction、时间噪声；每项以数值和结构化字段判定，oracle 与分析调用轨迹隔离。D19 由公开服务写入显式 virtual 数据，并为 available 模块提供通过场景；全量 `668 passed, 1 xfailed`，证据 E-20260810-007
- 证据：E-20260810-007；`traffic_lab_api.py`；`business/service.py`；`business/registry.py`；`docs/admin-console.html`；`evals/traffic_lab/wp5_mechanism_v1.json`；`tests/test_traffic_lab_wp5.py`

## M5-R WP4 实验与黑盒分析引擎（未单独升版）

- 状态：A/A、switchback uplift、置信区间、lag、数据质量 Gate、带运行轨迹 ground-truth 审计的独立黑盒 Eval 与版本化 AI 解释边界通过本机代码级候选；M5-R 整体仍在开发
- 兼容性：沿用 schema v28，无新表、迁移、依赖、HTTP API 或模块 available 登记；当前 policy/code 为 `traffic-analysis-v2` / `traffic-analysis-code-v2`，v1 analysis run 仍可读取，但旧 policy 的新分析在写入前明确拒绝，避免静默套用变更后的 Gate
- 权责边界：v2 固化生成 data window、sample size、effect、95% interval、lag、quality Gate、evidence/counter-evidence 和完整输入值/哈希；确定性 run 先落库，AI 随后只能更新 explanation-only 字段并受硬超时，越权、异常或超时均不改变统计字段。黑盒 runner 在分析完成后才读取 oracle 评分，报告记录分析场景与引擎调用的真实字段集合；oracle 字段重叠或额外调用字段会直接令评测失败
- 验证：E-20260809-007 保留分析引擎红绿；本次旧报告因无结构化边界证据红灯，对抗 fixture 注入 oracle 字段后整份报告按预期失败。修复后聚焦 16 项、Traffic 相关 46 项、独立黑盒 4/4、工作区全量 `658 passed, 1 xfailed`，证据 E-20260810-006
- 证据：E-20260810-006（当前黑盒边界）；E-20260809-007（当前分析引擎）；E-20260809-005（v1 历史，已被审查反例取代）；`src/ecommerce_agent/traffic_lab/analysis.py`；`scripts/run_traffic_analysis_eval.py`；`tests/test_traffic_lab_analysis.py`；`tests/test_traffic_lab_blackbox_eval.py`

## M5-R WP3 标题 / 图片特征引擎（未单独升版）

- 状态：标题/图片确定性统计、单点 feature schema、可选语义 signal 与显式降级通过本机代码级候选；M5-R 整体仍在开发
- 兼容性：沿用 schema v28，无新表、迁移、依赖、HTTP API 或模块 available 登记；Python 包 additive 导出 `TrafficFeatureEngine`、`TitleFeatureContext`、语义 extractor 契约和当前 schema 查询；`CreativeAssetCreate` 对未知 feature schema 明确拒绝，`image-v1` 及旧 extractor 保持有效，`image-v2` 为当前版本
- 特征契约：v1/v2 共用单点标题/图片特征清单、三类统计词表和阈值；v2 使用 `deterministic-title-v2` / `deterministic-png-v2`，无空格中文重复按归一化字符 bigram 统计，前 10 字密度按唯一信息字符占比计算，图片基础统计使用全分辨率累计与相邻边缘。同一 asset 可显式选择 v1/v2，输入 SHA 相同而版本化输出 SHA 分离，默认仍读取资产登记版本且不更新资产
- 决策边界：词表命中仅增加 `benefit/scenario/promotion_keyword_count`，不进入对话/分析路由、模型旁路、实验有效性或机制结论；AI 标签固定为 `advisory_signal`，不可用/失败/坏输出只把 extraction 标为降级，确定性块逐字段不变
- 验证：保留 E-20260809-004 初始接口/关键词反证；本次四项旧实现红灯均按预期失败，修复后聚焦 10 项、WP3 关联 24 项、迁移/灾备/CLI 扩展关联 63 项、工作区全量 `655 passed, 1 xfailed`；PNG 格式矩阵、随机图独立数值核对、compileall/whitespace 通过，证据 E-20260809-006
- 证据：E-20260809-006（当前）；E-20260809-004（v1 历史）；`src/ecommerce_agent/traffic_feature_schema.py`；`src/ecommerce_agent/traffic_lab/features.py`；`tests/test_traffic_lab_features.py`

## M5-R WP2 数据接入与虚拟推流器（未单独升版）

- 状态：Connector resources、CSV/JSON importer、稳定变更回执、私有隐藏策略和可重放 fixture 本机代码级候选通过；M5-R 整体仍在开发
- 兼容性：沿用 schema v28，无新表、依赖或专用 HTTP API；`VirtualTaobaoConnector.capability_version` 由 1.1 升至 1.2，并 additive 增加两个只读 pull resource；既有 resource 和 action 行为不变
- 导入契约：小时/日级输入按显式 `source_timezone` 解释并规范为 UTC，时窗必须对齐粒度；来源 ID 缺失时由 connector/listing/时窗/粒度/流量来源稳定派生；revision 自动解析要求唯一覆盖整个 bucket，归属失败隔离，结构错误或显式身份冲突逐行拒绝
- 虚拟边界：私有 fixture 生成器保留基础曝光、素材信号、近期 CTR/CVR 反馈、库存惩罚和固定随机种子；公开 Connector、数据库与 Traffic Lab 包只接收观测和回执，不输出 ground truth 或预期方向；公开 revision 以独立缺货时窗提供库存控制变量，使库存惩罚可由观测 Eval 复核
- 验证：同步最新 `main` 后，聚焦 8 项、Traffic Lab/Connector/迁移/灾备/CLI 关联 52 项、全量 `632 passed, 1 xfailed`；重叠 revision 守卫反证和缺货时窗红绿均成立，证据 E-20260809-003
- 证据：E-20260809-003；`src/ecommerce_agent/traffic_lab/ingestion.py`；`src/ecommerce_agent/connectors/_virtual_traffic.py`；`tests/test_traffic_lab_ingestion.py`

## M5-R WP1 数据契约（未单独升版）

- 状态：Listing / Creative 数据模型与领域 service 本机代码级候选通过；M5-R 整体仍在开发
- 兼容性：`Database.SCHEMA_VERSION` 由 27 升至 28；只新增 `creative_assets`、`listing_revisions`、`traffic_metric_buckets`、`traffic_metric_quarantine`、`traffic_experiments`、`traffic_experiment_windows`、`traffic_analysis_runs` 及相关索引/触发器，不重建旧表；既有 API、模块注册表和依赖不变
- 契约：所有时间输入要求带时区并规范为 UTC；`washout_window` 的整数单位冻结为分钟；`storage_ref` 只接受 `objects/` 项目对象键或无查询参数的 `s3://`、`oss://`、`cos://` URI；跨表引用使用 `tenant_id + id`；revision 不可更新/删除；metric 与隔离记录共享 `data_as_of + payload_hash` 来源版本语义，并按 `tenant_id + source_id` 保持互斥
- 灾备：v28 迁移前用旧程序生成并验证停机备份；迁移后、恢复业务写入前立即生成并验证新的 v28 全量备份。v28 程序按精确 schema 拒绝 v27 `.ypbak`，旧归档及匹配程序保留到新归档完成隔离恢复演练
- 验证：初始 E-20260809-001 通过；隔离补丁的最新证据见 E-20260809-002
- 证据：E-20260809-001、E-20260809-002；`src/ecommerce_agent/traffic_lab/`；`tests/test_traffic_lab.py`；`docs/operations.md`

## M4 验收补丁（未单独升版）

### D25（2026-08-08）

- 状态：FIX-13/14 代码侧交外测候选；冻结 WP4 mock/live 门禁通过，M4 最终签署等待负责人 gate 裁定、外部密封集和新截图
- 兼容性：沿用 schema v27；无新增迁移、依赖、请求/响应字段；LangGraph 20 节点 / 35 边与 `ChatResponse` 不变；新增三个均有默认值的 deliberate 专用环境变量
- 行为修正：DeepSeek deliberate 显式 `thinking disabled`，独立使用 15 秒/300-token 预算且不重试；最终生成保留 provider 默认 thinking；决策上下文去重并最多携带 3 条知识；售后关键条款原样输出，普通咨询与长期追责/实际办理 handoff 边界收敛；compact JSON mock 按解析后的 `task_type` 分流
- 验证：全量 `618 passed / 1 xfailed`；final mock `0.940 / severe 3 / passed`、final live `0.920 / severe 2 / passed`，after-sales `9/12`、complaint `8/8`、product `15/15`；thinking disabled 后 K3 total `9780.5ms` / TTFT `9068.4ms`，工具调用 0
- 遗留：泄漏投诉平衡集 recall `65%`，分类 gate 保持 failed；FIX-14 待负责人选择 gate 位置，FIX-15 密封集与浏览器新截图待外部验收；四场景延迟不能外推生产容量
- 证据：E-20260808-004；代码 revision `0fae3ba`、`92da05f`，文档澄清 `ccd9290`；`docs/works/13-feature-m4-customer-service/FIX14_GATE_DECISION_20260808.md`

### D24（2026-08-08）

- 状态：FIX-11/12 修复候选；WP4 mock 门禁复跑通过，M4 本机独立验收仍暂不签署
- 兼容性：沿用 schema v27；无新增迁移、依赖、请求/响应字段；LangGraph 20 节点 / 35 边与 `ChatResponse` 不变
- 行为修正：非复核规则命中恢复 `rule / 0.95` 零模型短路；`退货/保修` 责任追问才触发窄口径模型仲裁；唯一目录候选且知识已装配时进入一次有界规划，模型生成 grounded answer，禁止工具循环
- 验证：聚焦 `182 passed / 1 xfailed`，全量 `610 passed / 1 xfailed`；FIX-12 后 mock `0.940 / severe 3 / passed`、live `deepseek-v4-flash` `0.820 / severe 3 / passed`；泄漏回归 `31/40=77.5%`、投诉平衡回归 recall `75%`（均非泛化证据）；四场景延迟 `p50=16297.7ms / p95=33594.4ms`
- 遗留：FIX-14 分类 gate 位置待负责人裁定，FIX-15 密封留出集待验收人提供，浏览器截图未更新；当前延迟仍只能作泄漏场景 P1 证据
- 证据：E-20260808-003；`evals/performance/runs/20260808-m4-latency-post-fix12.json`；`evals/customer_service/runs/20260808-m4-customer-eval-post-fix12-{mock,live}.json`

- 状态：D23 修复候选；WP4 客服门禁通过，但 M4 本机独立验收暂不签署，生产放行继续阻塞
- 兼容性：沿用 schema v27；没有新增迁移、依赖或请求/响应字段；既有非流式 `POST /v1/chat` 契约与 LangGraph 20 节点 / 35 边不变
- 行为修正：分类与关键词只作为 advisory signal；投诉 handoff / SLA 由规划模型确认；普通商品回答取消目录/高分短路，仅保留标准化问法完全相等的人工批准知识复用；流式与非流式共用生成计划
- 验证：聚焦 `199 passed / 1 xfailed`，全量 `603 passed / 1 xfailed`，compileall 与 whitespace 通过；冻结 50 例 mock/live gate 均 passed（mock `0.940 / severe 3`；live `0.900 / severe 1`）；但当前 40 条泄漏意图回归总体 `29/40=72.5%`，投诉平衡集 recall `45%`，均不足以重新签署
- 证据：E-20260807-002；`docs/works/13-feature-m4-customer-service/README.md` D23

## 历史候选标签说明

`0.31.0`、`0.32.0`、`0.33.0` 曾用于合并过程中的内部候选编号，但没有同步修改
`pyproject.toml` 和包 `__version__`，因此不是当前运行时包版本。其功能与证据继续保留在下方
历史表及对应功能台账中；后续发布不得只改本文，必须在同一变更中同步两个运行时权威点。

## 下一版本计划

- 目标版本：待发布负责人确认。不得沿用已经发布过的 `0.26.0`，也不得把 M7-R～M10-R
  里程碑编号直接当成包版本。
- 计划内容：先把 main 上 0.30.0 之后的已合入能力、兼容性和生产阻塞整理成一次明确发布
  范围；M7-R～M10-R 仍是产品开发里程碑，只有实际实现并达到发布 Gate 的部分才进入版本。
- 发布条件：在同一提交中更新 `pyproject.toml`、`src/ecommerce_agent/__init__.py` 和本文；
  完成升级/回退说明、全量测试、灾备兼容和适用生产 Gate 后，才能声明新运行时版本。

## 版本历史

| 版本 | 状态 | 主要变更 | 验证证据 |
|---|---|---|---|
| `0.33.0` | 历史内部候选标签；未升包版本 | 工作台：适配器能力面板、知识/SOP 灰度状态面板、夜间值守与 SOP 白名单策略创建/展示 | E-20260727-006：后台 5 项测试、页面单脚本 JS 解析、浏览器渲染检查 |
| `0.32.0` | 历史内部候选标签；未升包版本 | schema v25 夜间值守时间窗/夜间模式与 SOP 白名单；assignment 生效模式；mockchat 窗口内自动、窗口外草稿端到端 | E-20260727-005：6 项专项 + v24→v25 迁移 + 84 项发布/渠道/迁移/灾备回归 |
| `0.31.0` | 历史内部候选标签；未升包版本 | SSE 流式客服接口；两段式生成保持拓扑零改动；断连重试复用既有幂等键 | E-20260731-002：流式与服务层专项、编排/网关/接口回归 |
| `0.30.0` | 当前运行时包版本；生产放行阻塞 | Token 预算截断替代条数截断；会话 CRUD 四端点与游标分页；空闲超时独立配置；后续 main 增量尚未升包 | E-20260731-001；`pyproject.toml` 与包 `__version__` |
| `0.29.0` | 历史本机候选 | 运营辅助与文案生成模块；schema v25；D16 虚拟店铺场景 | E-20260730-001：全量 313 通过，含门禁双反证 |
| `0.28.0` | 历史本机候选 | `product_advisor` 商品实体识别/推荐/对比；稳定版本化证据 ID 进入 bundle 与 evidence；店铺/租户隔离 | E-20260727-004：4 项专项、36 项上下文/Agent/图/渠道回归 |
| `0.27.0` | 历史本机候选 | SOP 渠道灰度：已批准候选按会话分桶解析并被 run 固定；原子完成与一步回滚；管理 API | E-20260727-003：3 项灰度专项、39 项治理/SOP/图回归 |
| `0.26.0` | 历史本机候选 | schema v24 通用 `staged_rollouts`；知识灰度 begin/调量/complete/rollback 生命周期；检索按会话稳定分桶仲裁 baseline/candidate；无分桶单元路径固定基线；管理 API | E-20260727-002：18 项灰度/迁移测试、43 项治理/检索/Agent/灾备回归、chat 会话分桶一致性 |
| `0.25.0` | 历史本机候选 | 信封归一化 `message_kind` 与多消息类型：非文本入站记录 + 脱敏占位符 + 运行时强制转人工；context checkpoint 前白名单对抗敌意载荷验证；跨店铺/跨租户不可合并契约与落库器测试 | E-20260727-001：2 项契约用例 × 2 适配器、非文本转人工双适配器运行时、context snapshot 白名单、落库器租户隔离、61 项渠道回归、channel_sdk 分支覆盖 90–100%、全量 261 通过 |
| `0.24.0` | 历史本机候选 | `channel_sdk` 通用渠道适配器 SDK：标准信封/发送/回执/错误分类/能力声明契约、共享入站落库/草稿/归属、适配器注册表；淘宝包装为标准适配器，新增协议不同的虚拟 mockchat 第二渠道；渠道 Agent 运行时按 platform 路由，outbox claim 平台隔离；`GET /v1/channels/adapters` | E-20260726-001：14 项契约用例 × 2 适配器、10 项跨渠道运行时/注册表/API 测试、渠道相关 71 项回归、channel_sdk 分支覆盖 90–100%、全量 252 通过（14 项既有 schema 期望失败另行修复） |
| `0.23.0` | 历史本机候选 | schema v23 版本化营销日指标、内容草稿有限事实检查、费用与结算单；提供投放诊断、管理利润估算、差异任务人工流转、两个 Agent 只读工具、控制台工作台和 D14/D15 真实输入输出 | E-20260723-005：15/15 虚拟场景、营销/财务 API 回归、后台/API 定向 10 项测试、页面 JS 解析 |
| `0.22.6` | 历史本机候选 | 约束后台内容区、概览内容流、表格、会话、消息和测试结果的尺寸与内部滚动；390px 导航保持滑动且隐藏原生滚动条 | E-20260723-004：后台/API 定向 7 项测试、桌面/390px 浏览器与 console 0 error/warning |
| `0.22.5` | 历史本机候选 | GLM Coding Plan 通过标准 Chat Completions 非流式调用接入原后台顾客直测；`AgentDecision` 兼容空容器 null，其他无效结构仍拒绝 | E-20260723-003：23 项定向测试、226 项全量测试、健康检查、原后台保修/发货真实模型回复与审计轨迹 |
| `0.22.4` | 历史本地候选 | 原后台智能客服对话测试改走仅回环、默认关闭的本机测试 API；移除客户端 ID/主体/密钥输入，预置店铺上下文，显示实际回答、风险、接管、会话/追踪和来源；会话固定归入 simulation，正式 `/v1/chat` 鉴权未改变 | E-20260723-002：223 tests、JS、后台浏览器保修案例真实发送、无客户端密钥控件 |
| `0.22.3` | 历史本地候选 | 默认关闭、仅回环的独立顾客测试页面/API；五个静态案例与自定义顾客问题复用实际客服链路；实际回答、来源、风险、转人工和原始 JSON 可见；会话固定归入 simulation，默认运营视图不受影响 | E-20260723-001：223 tests、compileall/JS、health/ready、HTTP 5 案例接口和真实对话、浏览器无 console error/warning |
| `0.22.2` | 历史本地候选 | schema v22 会话来源分类，后台默认运营数据隔离，智能客服/人工任务/派单范围切换，来源标签、Mock 状态和决策详情，场景验收保持真实输入输出 | E-20260722-011：221 tests、20/20 安全评测、HTTP 场景 13/13、默认运营 0 会话/消息/人工任务、模拟 17 会话/34 消息/13 人工任务、Edge 页面通过 |
| `0.22.1` | 历史版本 | `simulation-evidence-v1` 逐场景输入/预期/断言/完整输出，兼容旧 detail；后台手动运行、筛选、模块覆盖和响应式证据明细 | E-20260722-009：218 tests、源码 86% branch coverage、20/20 安全评测、真实 HTTP 13/13、1280/390px 浏览器和运行完整性 |
| `0.22.0` | 历史版本 | 显式 virtual 的关联店铺数据包、13 个跨模块需求、7 个 available 模块覆盖审计、冻结客服标注集实际 Agent 隔离回放、CLI/API、幂等重放和后台观察 | E-20260722-008：218 tests、全项目 90%/源码 86% branch coverage、simulation 95%、20/20 实际 Agent、CLI/HTTP 双重放、桌面/390px 浏览器和运行完整性 |
| `0.21.0` | 历史版本 | schema v21 automatic/manual、scheduled/unrestricted、presence session/连续心跳、UTC 绝对班次、持久 job/数据库租约/恢复/退避/失败、派单告警、管理 API 和后台闭环 | E-20260722-007：215 tests、全项目 89%/源码 86% branch coverage、dispatch 86%、staffing 87%、20/20 实际 Agent、桌面/390px 浏览器和运行完整性 |
| `0.20.0` | 历史版本 | schema v20 坐席档案、在线 TTL 租约、队列成员/技能/主队列、全局/队列容量、统一资格检查、确定性智能分配、管理 API 和响应式调度工作台 | E-20260722-006：203 tests、全项目 89.65%/源码 85.86% coverage、坐席调度 96%、20/20 实际 Agent、桌面/390px 浏览器和运行完整性 |
| `0.19.0` | 历史版本 | schema v19 租户接管队列、确定性路由/优先级、原子认领/容量、负责人状态机、转派/升级/备注、L1/L2 SLA worker、不可变事件历史、高风险最终保护、管理 API 和响应式工作台 | E-20260722-005：195 tests、全项目 89.45%/源码 85.63% coverage、人工接管 87.94%、20/20 实际 Agent、桌面/390px 浏览器和运行完整性 |
| `0.18.0` | 历史版本 | schema v18 版本化脱敏 suite/case、完整性哈希、实际多轮 Agent 隔离 run、指标/Gate、基线回归、发布关联、恢复、管理 API 和评测工作台 | E-20260722-004：184 tests、全项目 89%/源码 85% branch coverage、评测 85%、数据库 95%、发布 87%、20/20 eval、桌面失败/修订/通过和 8.81 cases/s |
| `0.17.0` | 历史版本 | schema v17 可解释同款匹配、版本化人工裁决、脱敏聚合内容/口碑、approved-only 监控与 Agent 门禁、管理 API 和质量队列 | E-20260722-003：171 tests、全项目 89%/源码 85% branch coverage、竞品 87%、数据库 95%、20/20 eval、桌面批准/撤销和性能验证 |
| `0.16.0` | 历史版本 | schema v16 事务入站任务、Agent invocation 幂等、租约/退避/死信、四模式动作、影子零副作用、精确事件草稿/发送、异步投递熔断、管理 API 和后台账本 | E-20260722-002：161 tests、全项目 89%/源码 85% branch coverage、渠道 Agent 85%、数据库 95%、20/20 eval、HTTP Qimen、桌面/390px 和性能验证 |
| `0.15.0` | 历史版本 | schema v15 不可变 decision/generation 上下文快照、证据权威级别/校验和、冲突降级、消息/审计/人工任务/API/后台关联和留存 | E-20260722-001：149 tests、85% source coverage、ContextBuilder 93%、20/20 eval、桌面/390px 和性能验证 |
| `0.14.0` | 历史版本 | schema v14 竞品策略/持久告警、原子幂等重评、确认/解决/复发状态机、按租户 worker、Agent 证据、管理 API 和后台闭环 | E-20260721-016：142 tests、85% source coverage、竞品 91%、20/20 eval、桌面/390px 和性能验证 |
| `0.13.0` | 历史版本 | schema v13 SOP 步骤账本、DSL v2、审批/读取重试/未知态裁决/补偿/恢复、Agent 工具门、管理 API 和页面内处置对话框 | E-20260721-015：136 tests、85% coverage、SOP 85%、20/20 eval、桌面真实审批/390px 和性能冒烟 |
| `0.12.0` | 历史版本 | schema v12 版本化发布策略、完整 Agent 隔离回放、双人审批、稳定分桶、四级灰度、运行观测、投递故障自动暂停、管理 API/后台和复核员管理 | E-20260721-014：111 tests、85% coverage、发布模块 86%、20/20 eval、桌面/390px 浏览器和性能冒烟 |
| `0.11.0` | 历史版本 | 运行目录锁、AES-256-GCM 双库备份、在线/离线一致性模式、严格验证、staging 恢复、自动回滚/手工回退、换钥、保留清理和 ASGI 应用工厂 | E-20260721-013：99 tests、84% coverage、灾备 85%、20/20 eval、在线/离线恢复和性能冒烟 |
| `0.10.0` | 历史版本 | schema v11 持久加密 outbox、租约 worker、崩溃边界、重试/死信/核对、出站事件、健康就绪、管理 API 和后台队列 | E-20260721-012：83 tests、84% coverage、20/20 eval、运行态/桌面浏览器/备份/性能冒烟 |
| `0.9.0` | 历史版本 | schema v9 分层知识、SOP DSL/版本固定/动作门、质检/VOC、客服草稿/diff/发送、投递状态和治理后台 | E-20260721-011：74 tests、84% coverage、20/20 eval、浏览器/备份/性能冒烟 |
| `0.8.0` | 历史版本 | schema v8 商品/订单/物流/售后事实、统一来源版本、六项受控指标、五个经营工具、执行超时/重试/不确定态、后台商品/订单视图 | E-20260721-010：67 tests、83% coverage、20/20 eval、浏览器验收 |
| `0.7.0` | 历史版本 | 竞品总览/趋势/风险/建议、客服会话与审计聚合 API、经营与客服管理后台、桌面/移动端工作台 | E-20260721-009：56 tests、20/20 offline eval、浏览器交互检查 |
| `0.6.1` | 历史版本 | 淘宝官方机器人 API 契约校准：HTTPS 网关、HMAC-MD5、用户字段映射、订阅读回、准入申请材料和 capability 声明 | E-20260721-008：54 tests、20/20 offline eval、compileall |
| `0.6.0` | 历史版本 | 统一 Connector SDK、淘宝虚拟接口、业务模块注册表、仓储与竞品模块、schema v6、经营 API 和两个 L0 Agent 工具 | 54 tests、20/20 offline eval、桌面/窄屏架构页检查 |
| `0.5.0` | 历史版本 | LLM 结构化决策、动态工具目录、有界 ReAct；淘宝 OAuth/TOP/奇门、人工接管和能力门禁本地 PoC | 53 tests、20/20 offline eval |
| `0.4.0` | 历史版本 | GLM 标准 API、SSE、schema v4、租户知识、证据门与学习回归 | 38 tests、29/29 eval |
| `0.3.0` | 历史版本 | checkpoint 前清洗、留存保护、管理员身份、限流和 readiness | 29 tests、29/29 eval |
| `0.2.0` | 历史版本 | 身份会话、人工任务、迁移、脱敏、留存和指标 | 24 tests、29/29 eval、API smoke pass |
| `0.1.0` | 历史版本 | LangGraph、RAG、自进化和 API/CLI | 13 tests、29/29 eval |
