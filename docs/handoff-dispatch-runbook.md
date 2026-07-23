# 人工接管自动派单运维手册

适用版本：`0.21.0` / schema v21

## 1. 配置

```text
HANDOFF_DISPATCH_WORKER_ENABLED=true
HANDOFF_DISPATCH_POLL_SECONDS=2
HANDOFF_DISPATCH_LEASE_SECONDS=30
HANDOFF_DISPATCH_BATCH_SIZE=20
HANDOFF_DISPATCH_MAX_ATTEMPTS=5
HANDOFF_DISPATCH_RETRY_BASE_SECONDS=5
HANDOFF_DISPATCH_RETRY_MAX_SECONDS=300
```

`LEASE_SECONDS` 必须大于单次本地分配事务的最坏执行时间。单机默认值足够，但发生持续数据库锁等待时应先修复锁争用，不应只扩大租约。

## 2. 上线前检查

1. `/health` 中 `database.schema_version` 为 22。
2. `/health` 中 `handoff_dispatch.worker.enabled/running` 均为 true，`last_error` 为空。
3. `/ready` 返回 200，`checks.handoff_dispatch_worker=true`。
4. 每名自动坐席的管理员凭据和档案均为 active。
5. 坐席 `dispatch_mode=automatic`，队列成员、技能、主队列和容量配置正确。
6. scheduled 坐席已存在当前有效班次；所有时间均按带时区值提交并以 UTC 保存。
7. 坐席从本人后台启动值守，心跳租约持续更新。

## 3. 日常观察

后台“智能客服”查看：

- 自动派单作业：pending/waiting 是否持续增长，最老待处理时间是否变长；
- 派单告警：open/acknowledged 数量、原因和发生次数；
- 坐席调度：有效在线、班次内外、全局负载和队列技能；
- 人工接管队列：未认领数、SLA 即将到期和违约数。

作业短暂处于 pending/leased 是正常现象。`waiting` 持续超过业务阈值或出现 `failed` 必须处置。

## 4. 无可用坐席

按顺序核对：

1. 管理员凭据是否 active；
2. 坐席档案是否 active；
3. 是否为 automatic；
4. 心跳租约是否有效且 presence 为 available；
5. scheduled 坐席是否位于当前有效班次；
6. 是否属于任务目标队列；
7. 全局和队列容量是否已满。

修复配置或让坐席重新开始值守后，服务会唤醒 waiting job。确认任务 assigned 且告警 resolved 后再关闭事件。禁止直接修改 SQLite 或手填负责人绕过资格门。

## 5. failed 作业

1. 查看 `last_error`、attempt count、任务状态和审计。
2. 先消除数据库、配置或代码异常。
3. 在后台点击“重试”，填写至少 2 个字符的处置说明。
4. 若返回版本冲突，刷新后重新读取最新状态，不覆盖其他管理员或 worker 的更新。
5. 重试后确认状态从 pending/leased 到 assigned，或再次形成可解释告警。

无坐席属于 `waiting`，不是技术失败，不应通过提高最大尝试数掩盖排班或容量问题。

## 6. Worker 中断

- 进程正常退出会停止线程；服务重启后扫描并补齐 proposed 任务的 job。
- `leased` 作业在租约到期后可由新 worker 领取。
- 任务若已由人工分配，worker 会对账为 assigned；任务已取消/离开 proposed 则 job 转 cancelled。
- `/ready` 在 worker 启用但线程未运行时返回失败。不要在未说明风险的情况下关闭 readiness 检查。

## 7. 数据库与恢复

- 禁止复制运行中的 SQLite 主文件；使用 `yunpai-agent backup`。
- 升级到 schema v21 前执行 `backup` 和 `backup-verify`。
- 恢复后先运行 `yunpai-agent init`、`yunpai-agent eval`，再检查 pending/waiting/leased job、未解决告警和坐席租约。
- 从备份恢复的短租约可能已过期，这是安全行为；坐席必须重新开始值守。

## 8. 暂停自动派单

紧急暂停优先将相关坐席改为 `dispatch_mode=manual` 或档案 inactive，并保留任务和 job 账本。全局关闭 worker 需要修改 `HANDOFF_DISPATCH_WORKER_ENABLED=false` 并重启，只用于维护窗口；此时新任务仍会持久化 job，恢复 worker 后继续处理。

暂停自动派单不等于关闭人工队列。客服主管仍需处理未认领任务和 SLA，不能让任务只停留在数据库中。
