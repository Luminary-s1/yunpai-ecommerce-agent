# 云湃电商一体机 Agent 0.10.0 测试报告

## 1. 结论

- 测试对象：`yunpai-ecommerce-agent 0.10.0`，SQLite schema v11。
- 本地候选结论：**通过**。本版把渠道发送从同步函数调用升级为持久、加密、可租约竞争、可重试、可死信和可人工核对的出站链路；服务重启后 worker、健康检查、就绪检查、管理 API 和后台页面均正常。
- 生产放行结论：**不通过（NO-GO）**。真实淘宝机器人资格与凭证、真实店铺收发、脱敏客户回放、24 小时长稳、断电/磁盘写满演练、加密备份产品命令和设备密钥管理尚未取得证据。
- 本报告验证的是本地可靠发送候选和模拟平台故障语义，不把 mock 网关、虚拟连接器或本机性能数据当作真实平台生产证据。

## 2. 环境与范围

| 项目 | 值 |
|---|---|
| 日期 | 2026-07-21 |
| 操作系统 | Windows / PowerShell |
| Python | 3.12 |
| Web | FastAPI + Uvicorn |
| Agent | LangGraph + SQLite checkpointer |
| 业务存储 | SQLite WAL、foreign keys、20 秒 busy timeout、schema v11 |
| 发件箱 | SQLite 持久队列、AES-GCM 密文、原子租约、指数退避、死信、人工核对 |
| 模型 | `MODEL_ENABLED=false`，评测和性能测试使用确定性本地模式 |
| 外部平台 | 淘宝 OAuth/TOP/奇门本地协议和模拟响应；无真实店铺调用 |
| 运行服务 | `http://127.0.0.1:8092`，本地测试凭证，outbox worker 启用 |

本轮重点覆盖：

- schema v1/v7/v9 到 v11 的迁移，以及伪造迁移标记时的物理结构校验。
- 出站消息持久化、敏感载荷密文、幂等键、来源事件与出站事件分离。
- 多数据库连接并发争抢、租约过期、发送前崩溃和发送后结果未知两条恢复路径。
- 安全重试、指数退避、最大尝试次数、死信和带乐观锁的人工核对。
- 发送前实时检查会话归属，人工接管后取消旧的自动发送。
- 异步回复草稿、出站事件与发件箱最终状态一致性。
- worker 生命周期、健康/就绪状态、租户隔离管理 API 和后台操作。

本轮不覆盖：真实淘宝/ERP、真实模型质量、真实顾客数据、24 小时长稳、断电、磁盘写满、平台真实限流、硬件密钥和整机灾备。

## 3. 自动化回归

执行命令：

```powershell
py -3.12 -m coverage erase
py -3.12 -m coverage run --branch -m pytest -q
py -3.12 -m coverage report --include="src/*" -m
```

结果：

```text
83 passed in 97.18s
source branch coverage: 84%
```

关键模块覆盖率：

| 模块 | 分支感知覆盖率 |
|---|---:|
| `database.py` | 95% |
| `service.py` | 95% |
| `outbox.py` | 85% |
| `taobao.py` | 83% |
| `graph.py` | 83% |
| `sops.py` | 79% |
| 全部 `src/` | 84% |

`api.py` 的 60% 覆盖率主要来自未执行的真实 OAuth/奇门入口和部分路由错误分支。发件箱状态机、数据库事务和淘宝发送边界由服务测试及 API 契约测试共同覆盖。

其他自动化检查：

| 检查 | 结果 |
|---|---|
| `py -3.12 -m compileall -q src tests` | 通过 |
| 后台内嵌 JavaScript `new Function` 解析 | 通过，1 个脚本块 |
| editable 安装版本 | `package_version=0.10.0`、`module_version=0.10.0` |
| 隔离 `DATA_DIR` 离线评测 | 20/20 通过；precheck/retrieval/safety failure 均为 0 |
| OpenAPI 版本与发件箱路由 | 0.10.0；4 条 outbox 路径 |
| 宿主环境 `pip check` | 项目依赖正常；宿主机外部 `cadpy` 缺少 `cadquery-ocp`，与本项目依赖树无关 |

## 4. 持久发件箱测试矩阵

| 场景 | 期望 | 结果 |
|---|---|---|
| 8 个独立数据库实例争抢同一记录 | 只有一个 owner 获得租约 | 通过 |
| 入队后重启/延迟处理 | 记录保留，worker 可继续领取 | 通过 |
| 调用平台前进程崩溃 | 租约过期后安全回队，不消耗一次真实尝试 | 通过 |
| 调用平台后、记录结果前崩溃 | 标记 `uncertain`，禁止自动盲重试 | 通过 |
| 建连失败 | 视为尚未交付，按退避重试 | 通过 |
| 读取超时/响应不可解析 | 结果不确定，进入人工核对 | 通过 |
| 平台明确业务错误 | 标记 `rejected` | 通过 |
| 超过最大尝试次数 | 标记 `dead_letter` | 通过 |
| 人工确认已送达 | `confirmed`，不可再次发送 | 通过 |
| 人工确认未送达 | 回到可重试队列 | 通过 |
| 死信直接回队 | 拒绝；必须先明确确认未送达 | 通过 |
| 旧 `record_version` 核对 | 冲突，不覆盖新状态 | 通过 |
| 重复幂等键 | 返回同一出站记录，不重复入队 | 通过 |
| 发送载荷落库 | 仅保存 AES-GCM 密文，不出现回复明文 | 通过 |
| 核对备注落库 | 入库前脱敏 | 通过 |
| API 响应 | 不暴露 `payload_ciphertext` | 通过 |

测试证据：`tests/test_outbox.py`、`tests/test_outbox_api.py`、`tests/test_migrations.py`。

## 5. 状态一致性与会话安全

| 场景 | 验收行为 | 结果 |
|---|---|---|
| 异步草稿发送 | 入队时保持 `sending`，平台确认后转 `sent` | 通过 |
| 异步发送失败 | 草稿转失败态并保存安全错误分类 | 通过 |
| 出站事件时间线 | 入队生成 `queued`，确认生成/更新 `sent`，失败为 `failed` | 通过 |
| 来源事件和出站事件 | schema v11 用 `source_event_id` 显式关联，不复用出站 `event_id` | 通过 |
| 人工接管竞态 | worker 调用平台前再次读取会话 owner，非 bot 状态取消发送 | 通过 |
| 凭证变化 | worker 发送时读取当前连接与凭证，不使用入队时的陈旧凭证 | 通过 |
| 自动回复权限 | `allow_bot` 和实时 owner 同时满足才允许自动发送 | 通过 |

这里验证的是单机 SQLite 的原子状态更新。多节点部署仍需迁移 PostgreSQL 或引入专用队列；当前版本不声明支持共享文件上的多节点生产运行。

## 6. 迁移与结构校验

| 场景 | 结果 |
|---|---|
| legacy v1 -> v11 | 通过 |
| 带历史竞品数据 v7 -> v11 | 通过 |
| v9 `sending` 记录升级 | 转为 `failed/uncertain`，不静默重发 |
| v9 无密文 `queued` 记录升级 | 转死信，等待人工核对 |
| 迁移表声称最新但物理表/列缺失 | 初始化失败，阻止带病启动 |
| `PRAGMA user_version` | 11 |

schema v10 增加密文载荷、租约、尝试、退避、死信和核对字段；schema v11 将来源事件和出站事件拆分为独立标识。

## 7. Worker、API 与运行态

运行服务重启到新代码后检查：

```text
health=ok
schema=11
worker_enabled=true
worker_running=true
due=0
requires_reconciliation=0
dead_letters=0
ready=ready
```

`/ready` 的 schema、客户端认证、管理员认证、知识种子、磁盘空间、checkpoint、模型配置、业务模块、虚拟连接器和 outbox worker 十项检查全部为 `true`。

管理 API 验证：

| API | 验证内容 | 结果 |
|---|---|---|
| `GET /v1/integrations/taobao/outbox/summary` | 待发、待核对、死信、最老积压 | 通过 |
| `GET /v1/integrations/taobao/outbox` | 管理员鉴权、租户隔离、状态过滤、密文隐藏 | 通过 |
| `POST /v1/integrations/taobao/outbox/run` | 显式领取并处理到期记录 | 通过 |
| `POST /v1/integrations/taobao/outbox/{id}/reconcile` | 三种核对结果、版本冲突、备注脱敏 | 通过 |

## 8. 后台界面验收

在 Codex in-app browser 中对 `http://127.0.0.1:8092/admin` 实际执行：

- 管理员登录，确认版本 0.10.0、数据库 schema v11。
- 打开“渠道接待”，确认 worker 运行、待发送/待核对/死信/最老积压 KPI 和发件箱表格。
- 点击“执行待发队列”，空队列返回“已处理 0 条队列记录”。
- 页面控制台 error/warning 为 0；默认 1280 x 720 视口无页面或文本溢出。

移动端说明：响应式 CSS 与后台静态测试继续覆盖导航、KPI、表格滚动和单列规则；本轮浏览器插件的临时 viewport capability 调用没有改变实际 1280 x 720 视口，因此**没有把 390 x 844 记为本轮浏览器实测通过**。生产放行前仍需在真实移动浏览器或可工作的设备模拟环境复验。

## 9. 性能冒烟

本地 framework/mock 模式，顺序请求除特别标注外均为单连接发起：

| 负载 | 成功 | p50 | p95 | max |
|---|---:|---:|---:|---:|
| `/health` 顺序 100 次 | 100/100 | 23.63 ms | 26.12 ms | 33.18 ms |
| outbox summary 顺序 100 次 | 100/100 | 79.31 ms | 94.00 ms | 102.71 ms |
| `/v1/chat` 顺序 30 次 | 30/30 | 129.21 ms | 150.54 ms | 157.78 ms |
| `/v1/chat` 8 worker / 20 次 | 20/20 | 344.05 ms | 437.20 ms | 439.20 ms |

这些结果仅用于发现明显回归，不代表真实模型、真实平台、真实会话规模或一体机硬件容量。outbox summary 走管理员鉴权和 SQLite 查询，当前本机 p95 为 94 ms，适合后台轮询，但后续长稳应继续观察锁等待和 WAL 增长。

## 10. 备份恢复冒烟

对运行中 `data/agent.sqlite3` 使用 SQLite online backup API，恢复到隔离数据库并重新执行 schema 初始化：

```text
backup_bytes=1179648
schema=11
integrity_check=ok
knowledge=156
sop_definitions=4
sessions=50
channel_outbox=0
channel_events=0
```

首轮临时目录自动清理时 Windows 对恢复库仍有短暂文件占用，导致清理步骤退出失败；保留临时目录复跑后数据库复制、初始化、完整性和计数检查全部退出 0。该现象不影响恢复数据结论，但说明正式恢复命令必须显式关闭连接、checkpoint/WAL 文件并设计可重试清理。

本测试仍不等价于产品化灾备：没有覆盖备份加密、签名清单、保留轮换、checkpoint 库、断电恢复、跨设备恢复和一键回滚。

## 11. 仍未通过的生产 Gate

以下任一项未完成，都不得把 0.10.0 标记为生产可用：

1. 淘宝客服机器人类目审批、AppKey/AppSecret、奇门 customerId、request_token/tenant_id 和测试店铺真实收发。
2. 真实 ERP/OMS 商品、订单、物流、售后数据字典、增量同步、限流和日对账。
3. 首个客户脱敏会话、知识、SOP、质检基线及业务验收口径。
4. 真实模型准确率、SOP 路径命中率、严重事实错误为 0、提示注入和越权回放。
5. 24 小时以上 soak、强杀恢复、断电、磁盘写满、数据库锁、平台限流和大规模积压清空。
6. 加密备份、密钥轮换、保留策略、正式恢复命令、checkpoint 一致性和整机恢复演练。
7. 首个低风险真实写动作的业务批准、dry-run/读回、补偿和资损评审。
8. 完整 SOP 多步执行/补偿、语义 VOC 聚类和灰度发布门禁。
9. 真实移动浏览器、目标一体机屏幕和无障碍复验。

## 12. 后续测试顺序

1. 产品化加密备份/恢复命令，并覆盖主库、checkpoint、WAL、校验清单和版本回滚。
2. 建立可重复的强杀、租约过期、数据库锁、磁盘不足和 24 小时 soak 测试。
3. 用真实淘宝沙箱/测试店铺完成入站、人工接管竞态、异步回写、回执、限流、重复消息和人工核对。
4. 用首个客户脱敏集执行知识准确率、SOP 正常/边界/失败路径、质检一致性和 VOC 验收。
5. 先进入影子和仅提示模式，所有 Gate 有证据后再逐级开放人机协同和白名单自动回复。
