# 云湃电商一体机 Agent 0.22.2 测试报告

日期：2026-07-22  
运行实例：`http://127.0.0.1:8104/admin`  
数据库：SQLite schema v22  
结论：本地代码级候选通过，生产放行仍为 NO-GO。

## 本版目标

0.22.2 主要补齐后台数据边界和可复核展示：默认运营看板不再被虚拟场景和评测会话污染；模拟、评测、渠道和 API 会话有明确来源标记；智能客服详情展示决策模式、工具、上下文与轨迹；人工任务、派单 job 和 SLA 扫描支持同一 scope 过滤。

## 关键变更

- schema v22 为 `sessions` 增加 `source_type/source_reference`，历史 `virtual-*`、`evaluation:*`、`taobao:*` 会话按来源回填。
- `/v1/admin/overview`、会话列表/详情、人工任务、派单 job/告警和后台页面默认使用 `operational` 范围；可切换 `simulation/evaluation/all`。
- 请求指标统计保留无 session 的旧指标兼容性，但默认运营范围排除 simulation/evaluation。
- 管理后台新增数据范围下拉、来源标签、Mock 模型状态、决策详情和虚拟场景输入/输出展示。

## 验证结果

| 项目 | 结果 |
|---|---|
| 全量回归 | `221 passed in 378.58s`，退出码 0 |
| 关键专项 | `tests/test_virtual_store_simulation.py`、管理员免登录专项共 3 passed |
| 静态检查 | `python -m compileall -q src` 退出码 0 |
| 前端脚本 | `validated 1 inline script(s)` |
| 安全评测 | `20/20 passed`，三类失败数组为空 |
| 运行 health/ready | health `ok`，ready `ready`，schema v22 |
| HTTP 场景验收 | `13/13` passed，`simulation-evidence-v1`，响应约 442,944 bytes |
| 数据隔离 | 默认运营视图：0 会话、0 消息、0 人工任务、0 指标请求；模拟视图：17 会话、34 消息、13 人工任务、17 指标请求 |
| 页面验证 | Edge 1280x720：智能客服页和场景验收页无横向溢出；场景页 13 通过、D07/D09 可见；无 console error/warning 和 4xx/5xx 页面响应 |

## 运行态配置

- `ADMIN_AUTH_REQUIRED=false`：仅本机回环免登录。
- `ADMIN_API_KEY=test-admin-key-123456`、`BOOTSTRAP_ADMIN_ID=admin-test`：用于本机坐席初始化，不要求用户登录。
- `AUTH_REQUIRED=true` 且客户凭据已配置：客户 API 认证仍开启。
- `MODEL_ENABLED=false`、`MODEL_MOCK_MODE=true`：页面明确显示 Mock 模拟模型。

## 证据位置

- 全量测试日志：`tmp/pytest-full-0.22.2-20260722-221156.out.log`
- 页面截图：`tmp/admin-0.22.2-service-simulation.png`、`tmp/admin-0.22.2-simulation-evidence.png`
- 关键源码 SHA-256：见 `.project-to-act/PROJECT_ACCEPTANCE.md` 的 E-20260722-011

## 未通过生产 Gate

本版仍不代表生产可上线。真实淘宝/ERP 权限、真实渠道消息收发、客户脱敏多轮标注集、真实模型基线、合法竞品/口碑来源、营销/利润模块、24/72 小时长稳、容量、安全、异机灾备和业务 RPO/RTO 尚未验收。
