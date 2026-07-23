# 云湃电商一体机 0.14.0 技术实现说明

## 1. 本版目标

0.14.0 把竞品分析从一次性查询提升为可持续运行的 Agent 业务模块。核心闭环为：授权观察进入事实库，监控策略定义业务阈值，调度器原子重评，持久告警进入人工队列，Agent 和后台读取同一份证据。

本版不抓取未授权数据、不把虚拟或估算数据伪装为真实事实，也不自动修改商品价格。

## 2. 组件与职责

| 组件 | 实现位置 | 职责 |
|---|---|---|
| 竞品领域服务 | `src/ecommerce_agent/business/competitive.py` | 观察写入、策略版本、条件评估、告警状态机、趋势和 Agent 证据 |
| 数据迁移 | `src/ecommerce_agent/database.py` | schema v14、物理结构校验、索引和约束 |
| 管理 API | `src/ecommerce_agent/operations_api.py` | 策略、评估、告警查询和处置，管理员认证与审计 |
| 调度生命周期 | `src/ecommerce_agent/service.py` | 按租户周期重评、健康状态、就绪门禁和有界停止 |
| Agent 工具 | `src/ecommerce_agent/business/service.py` | `get_competitor_price_analysis` 返回观察、策略和未解决告警证据 |
| 管理后台 | `docs/admin-console.html` | 策略表单、告警队列、处置对话框和响应式展示 |

## 3. 数据模型

schema v14 新增：

- `competitive_monitors`：以 `tenant_id + store_id + subject_sku` 唯一定位策略，保存低价阈值、降价阈值、数据新鲜度、是否纳入估算、启停状态和 `record_version`。
- `competitive_alerts`：以 `tenant_id + monitor_id + competitor_sku + alert_code` 唯一定位告警条件，保存证据观察、阈值、级别、发生次数、确认/解决信息和 `record_version`。

告警只允许三类条件：

- `competitor_undercut`：最新竞品价低于本店价且达到策略阈值。
- `competitor_price_drop`：同一竞品相邻两期价格降幅达到策略阈值。
- `data_stale`：没有合格观察，或最新观察超过新鲜度阈值。

所有表带租户字段；查询、更新和唯一索引均包含租户范围。数据库初始化完成后会检查表和关键列，伪造迁移标记不能掩盖物理结构缺失。

## 4. 写入与评估语义

观察写入沿用不可变来源版本契约。同一来源键和时间的同载荷重放返回 `idempotent`，不同载荷返回 `observation_version_conflict`。观察写入成功后自动评估匹配策略。

策略通过 PUT 语义创建或更新：

- 首次创建要求 `expected_record_version=0`。
- 更新要求提交当前 `record_version`，成功后版本加一。
- 版本不匹配返回 409，不允许旧页面覆盖新策略。
- 创建或更新完成后立即重评，避免策略和告警短暂不一致。

每次评估在 `BEGIN IMMEDIATE` 事务中完成。相同证据重复评估不会增加 `occurrence_count`；新观察再次满足条件会增加发生次数。条件清除会自动解决告警；已人工解决的告警在相同证据上保持解决，新证据再次触发时重开。

## 5. 告警状态机

```text
new condition -> open
open -> acknowledged
open -> resolved
acknowledged -> resolved
open/acknowledged + condition cleared -> resolved(system)
resolved + same evidence -> resolved
resolved + new matching evidence -> open
```

人工迁移必须提交处置说明和期望版本。确认人、解决人、时间、说明和每次审计事件都持久保存。

## 6. 调度与故障隔离

生产环境默认启用 `COMPETITIVE_MONITOR_WORKER_ENABLED=true`，默认周期 60 秒。worker 每轮先枚举有策略的租户，再逐租户执行；单个租户失败会记录错误和审计，不阻断其他租户。

`/health` 暴露启用状态、线程状态、周期、累计评估数、最近运行时间和最近错误。启用 worker 后，`/ready` 将线程存活作为就绪条件。服务关闭时通过停止事件和最多 5 秒 join 有界退出。

该 worker 是单机进程内调度器，配合数据目录单实例锁使用；本版不实现分布式选主。

## 7. API

```text
POST /v1/competitive/observations
GET  /v1/competitive/observations
PUT  /v1/competitive/monitors
GET  /v1/competitive/monitors
POST /v1/competitive/monitors/evaluate-all
POST /v1/competitive/monitors/{monitor_id}/evaluate
GET  /v1/competitive/alerts
POST /v1/competitive/alerts/{alert_id}/transition
GET  /v1/competitive/overview
GET  /v1/competitive/analysis
```

所有管理接口要求 `X-Admin-Id` 和 `X-Admin-Key`。不存在的租户内资源返回 404，乐观锁和非法状态迁移返回 409，Pydantic 类型或范围错误返回 422。

## 8. Agent 与后台

`get_competitor_price_analysis` 是只读 L0 工具。输出同时包含价格位置、历史趋势、来源、估算标识、策略版本和未解决告警，因此模型只能基于服务端已验证证据组织答案，不能自行生成阈值或声称已改价。

后台竞品页支持：

- 创建和带版本更新监控策略；
- 手动执行全量评估；
- 查看持久告警、证据 ID、发生次数和锁版本；
- 在页面内对话框确认或解决告警；
- 查看价格位置、趋势、建议和数据质量。

## 9. 生产边界

0.14.0 是本地代码级生产候选，不是最终生产放行。真实平台授权、生产数据映射、长稳运行、部署环境备份恢复演练、安全复核和业务验收仍必须完成。完整证据与未通过门禁见 `docs/TEST_REPORT_0.14.0.md`。
