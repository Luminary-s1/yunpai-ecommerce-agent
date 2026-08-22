# 飞书汇报文案（给闫睿涵）

闫老师您好，M9-R 第 5 轮修复已完成，向您汇报如下：

---

## 一、修复范围（对应您第 4 轮复验的 7 个阻断项）

| # | 阻断项 | 修复内容 |
|---|---|---|
| 1 | item 身份隔离击穿 | 已确认修复（冲突键补 item_id、NULL 行不广播），本轮另修复了 R1 遗留的 SQL 注释 bug（详见第六节） |
| 2 | 证据语义不诚实 ×3 | **net_sales 不再用 GMV 冒充**：多行订单退款无法归 SKU 时显式 MISSING + 独立 reason；**商品映射取最新事件**，revoked 使映射失效（不再回落旧 confirmed）；**来源同源确定**：CTE 去分区全局取最新整行 |
| 3 | D-034 默认路径 | diagnose() 新增结构化 `degradation_reasons`，门禁 blocked 不再被 `model_unavailable` 吞并，reason 保持稳定码 |
| 4 | WP4 页面缺下钻 | HTML/JS 已补齐（revision 输入、insights 面板、诊断面板、审核操作），浏览器测试验证通过 |
| 5 | Eval 假覆盖 | 选品/上新/清仓场景改为**信号注入 → 非降级真实方向**（SELECTION/NEW_LAUNCH/CLEARANCE），证明"发现真实方向"而非只证降级 |
| 6 | 跨平台测试失败 | 已确认修复（纯 Python 扫描替代 grep subprocess） |
| 7 | 文档不可复现 | Base 修正为 `454b35c`、collect 计数修正、EOF 空白清理、浏览器 skip 条件注明 |

## 二、验收证据

- **全量回归：1273 passed**（21 分 02 秒，串行单进程）
- **浏览器测试：4 passed**（工作台渲染、生成建议落库、桌面/窄屏无溢出、console 无错误）
- **定向测试 + mutation 红绿**：R2-1 net_sales、R2-2 映射两个关键修复均做了注入旧 bug 验证（变红 → 还原 → 变绿）
- **编译门禁**：compileall EXIT=0、collect 1273、git diff --check 干净

## 三、PR 状态

- PR #19：OPEN / MERGEABLE
- Head：`0302c1a`（fix(m9r): 第5轮修复 — 负责人 WP5 复验 7 阻断项 + 全量回归全绿）
- Base：`454b35c`

## 四、额外说明（R1 遗留 bug）

全量回归抓出 R1 修复时的一个遗留 bug：`inventory.py` / `orders.py` 的 SQL 里用了 `#` 作注释，但 SQLite 不识别（导致 `unrecognized token: "#"`，78 个测试失败）。已改为 `--`，全部转绿。这是第 5 轮全量回归的价值——只跑定向测试不会暴露它。

---

请您抽空复验，如需补充证据随时告知，感谢您的时间！
