# 云湃电商一体机 Agent 0.22.1 测试报告

## 1. 结论

0.22.1 输入输出证据版通过本地代码级候选验证。13 个虚拟店铺场景现在统一返回真实调用输入、固定预期、逐项断言和实际结构化输出；管理后台可手动运行、筛选并逐项查看证据。

本结论不等于生产放行。报告数据均为显式虚拟数据；真实渠道、真实模型、客户数据、合法竞品来源、营销/利润、长稳、容量、安全和异机灾备仍未验收。

## 2. 变更范围

- 报告契约升级为 `simulation-evidence-v1`，保留 0.22.0 `detail` 兼容字段。
- D01-D13 输出完整领域记录、Agent/工具响应、连接器回执和隔离评测报告，不再只有计数与布尔摘要。
- `GET /v1/simulations/virtual-store` 提前返回 13 项固定输入和预期。
- `/admin` 新增“场景验收”工作台、模块/状态筛选、模块覆盖和格式化 JSON 明细。
- 页面只在管理员明确点击后运行场景；浏览场景定义不会创建虚拟会话或任务。
- 版本升级到 0.22.1；数据库 schema 保持 v21。

## 3. 自动化验证

```powershell
py -3.12 -m coverage run --branch -m pytest -q -p no:cacheprovider
py -3.12 -m coverage report --include='src/*' -m
```

- 全量：`218 passed in 546.01s (0:09:06)`；失败 0，跳过 0。
- 生产源码：10,223 statements，1,117 miss，2,578 branches，540 partial，86%。
- `simulation.py`：94%；`simulation_api.py`：100%。
- 最终页面与模拟定向回归：`4 passed in 19.10s`。
- 内联 JavaScript 语法、`compileall`、fixture JSON 和 editable 安装均通过。
- 源码版本与包元数据均为 0.22.1。
- 默认模型关闭的隔离安全评测：20/20 通过，三类失败数组均为空。

## 4. 真实 HTTP 证据

运行实例：`http://127.0.0.1:8104`，隔离目录 `tmp/runtime-0.22.1`。

- 场景定义契约为 `simulation-evidence-v1`，共 13 项。
- 真实运行 `simulation-2aca3e16681b4f84920cc8b4e58fa468` 为 13/13 通过。
- HTTP JSON 响应 110,780 bytes；D01 含 6 条完整商品，D02 含 8 条完整订单/物流/售后。
- D07 含完整 Agent 响应、3 条来源和上下文快照；默认模型关闭时以 `model_unavailable` 安全转人工。
- D13 返回完整评测门禁并通过，主运行库会话、消息和人工任务前后计数一致。
- OpenAPI 版本 0.22.1、135 条路径；`/health=ok`、`/ready=200`、派单 worker 无错误。
- SQLite `integrity_check=ok`、外键错误 0、schema v21。

## 5. 浏览器验收

在应用内浏览器登录后台并实际点击“场景验收”完成运行：

- 运行前显示 13 项输入与预期，不伪造实际输出。
- 运行后显示 13 通过、0 失败、0 跳过，7 个 available 模块通过，营销/财务明确为规划中。
- D01 可见完整商品 JSON；D07 可见问题、店铺/SKU 上下文、三项断言、完整回复、来源、转人工原因和证据 ID。
- 模块筛选选择“智能客服”后只显示 D07。
- 1280×720 下场景表 `scrollWidth=clientWidth`，页面无横向溢出。
- 390×844 下 `bodyScrollWidth=documentScrollWidth=375`，详情宽 351；点击场景后标题位于 sticky 导航下方，不被遮挡。
- 浏览器 console error/warning 为 0。

浏览器检查中发现并修复了场景表继承全局最小宽度导致横向滚动、移动端点击后未定位详情、详情标题被 sticky 导航遮挡三项问题。

## 6. 生产边界

当前仍为“0.22.1 本地虚拟候选通过，生产 NO-GO”。`virtual=true` 和 `production_claim=false` 保持不变，尚未实现的营销/利润模块不会因页面展示而被标记通过；其余未通过 Gate 与 0.22.0 测试报告一致。
