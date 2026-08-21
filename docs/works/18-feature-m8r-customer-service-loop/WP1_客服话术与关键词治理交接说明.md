# M8-R WP1 客服话术与关键词治理交接说明

## 基本信息

- 模块：M8-R 销售与售后客服闭环
- 工作包：WP1 客服话术/关键词导入与批准治理
- 开发负责人：谢良璇
- 当前状态：WP1 开发侧功能与谢良璇人工 HTTP 黑盒验收完成；已纳入完整 M8-R PR #25，等待缪海南 WP5 独立验收和项目负责人审阅
- 基线提交：`48013b1d3b29a810288c32f73df028c69070064c`
- 开发分支：`feature/m8r-customer-service-loop`
- 证据 ID：`E-20260817-008`、`E-20260818-001`、`E-20260819-001`、`E-20260819-002`、`E-20260819-003`

## 本轮交付

WP1 复用 M7-R 导入清单和现有知识发布状态机，没有新增数据库表、schema 版本或第三方依赖。

- `CustomerServiceContentService` 接收受控导入清单和规范化行，生成话术或关键词候选。
- 话术和关键词继续使用现有 `candidate/draft -> evaluated -> active/approved -> retired` 生命周期。
- 导入清单保存原始文件引用和版本事实；知识来源保存导入 ID、物理行号和原始行摘要。
- 追溯接口可从知识项回查原始文件引用、原始行摘要、规范化问法、答复、批准人和有效期。
- 管理员接口提供导入、上下文预览和追溯；审核、批准、退役继续复用既有知识治理接口。

## 交付与界面边界

- WP1 不新增独立前端。当前工作包的验收对象是导入适配、治理接口、上下文预览和来源追溯；
  现有通用知识后台可继续处理生命周期，专属客服影子界面归 WP4。
- M8-R 按 D-047 在同一分支完成 WP1～WP4，开发侧全链自测结束后提交一个完整里程碑 PR；
  2026-08-19 的“一个 WP 一个 PR”临时口径已被取代。
- 完整 PR 建立并固定待验 head 后，通知任务书指定的缪海南从干净状态执行 WP5；通过后再合入。

## 接口位置

| 用途 | 方法与路径 |
|---|---|
| 导入话术/关键词候选 | `POST /v1/admin/customer-service/content/import` |
| 预览指定店铺/SKU 的批准上下文 | `POST /v1/admin/customer-service/content/context` |
| 查看单条内容来源追溯 | `GET /v1/admin/customer-service/content/{item_id}/trace` |
| 评估候选 | `POST /v1/admin/knowledge/{item_id}/evaluate` |
| 批准并激活 | `POST /v1/admin/knowledge/{item_id}/approve` |
| 退役 | `POST /v1/admin/knowledge/{item_id}/retire` |

## 字段与范围语义

- `content_type=script`：必须有 `scenario/question/answer/store_id`，可选 `sku_id`、关键词、风险级别和有效期。
- `content_type=keyword`：必须有 `scenario/keyword/store_id`，只形成非权威上下文 signal。
- 行内 `store_id` 必须与导入清单店铺一致；冲突行拒绝，不生成候选。
- 有 SKU 时使用商品层范围；无 SKU 时使用店铺层范围。
- 同一标准问法同时存在商品层和店铺层批准话术时，指定 SKU 的查询优先选择商品层，
  店铺层只作兜底；未指定 SKU 的查询不会读取商品层话术。
- 未批准、已退役、过期、跨店和不匹配 SKU 的内容不会进入 WP1 上下文查询结果。
- 新来源对同一业务知识键生成新候选版本；新候选未批准时，旧批准版本继续生效。

## D-034 边界

- 只有人工批准、处于有效期、范围匹配且规范化问题完全相等的话术才标记为快速路径候选。
- 相似问法即使检索分数高，也不会获得快速路径资格。
- 关键词记录从通用 RAG 答复候选中排除，只返回 `authority=advisory_only` 的 signal。
- 否定、假设/售前和复合请求可以命中 signal，但 signal 中没有 `route` 或 `mode`，不能决定语义路由。

## 不可信文件边界

- 外部公式、链接和文字指令只作为字符串数据处理，不调用、不访问、不执行。
- 上游标记为隐藏的必填字段会使该行隔离，不生成知识候选。
- 导入复用 M7-R `ReportFieldPolicy` 与敏感值清理；中文批准列名映射到同一权威字段契约。
- 非白名单辅助列不会进入知识答复字段；手机号等敏感必填值被移除后，该行拒绝且不生成候选。
- 原始行只保存摘要和受控文件引用，返回脱敏字段、敏感值和非白名单字段移除计数。

## 人工验收工具

- `启动_WP1_人工验收环境.ps1`：在 F 盘创建全新隔离数据目录，以本机回环地址启动服务；
  模型关闭、管理员免登录仅限 loopback，不连接真实店铺。
- `WP1_人工验收助手.ps1`：通过真实 HTTP API 逐步执行导入、候选不可见、审核批准、
  未来生效拒绝、范围过滤、精确匹配、关键词 signal、过期排除、惰性公式、来源追溯和退役。
- `WP1_人工验收指南.md`：给谢良璇的双窗口操作步骤、观察重点、证据位置和停止方式。
- 两份 PowerShell 脚本使用带 BOM 的 UTF-8，已通过 Windows PowerShell 5.1 语法和实际运行验证；
  HTTP 响应从原始字节按 UTF-8 解码，避免 5.1 把中文 JSON 错读为乱码。

## 开发侧验证

- 2026-08-19 红灯固定为 `5 failed, 6 passed`：缺少脱敏汇总、敏感必填值拒绝、中文别名、
  显式场景快速路径和未来生效批准门。
- 修复后 WP1 聚焦 `11 passed`；mutation 恢复正式树后再次 `11 passed`。
- M7-R 只读导入、RAG、知识治理/错误契约/灰度、运行时知识桥、知识引擎和 Wiki API
  关联回归分别 `23 + 12 + 17 + 24 + 15 = 91 passed`；与 WP1 合计 102 个明确通过。
- 四项临时 mutation 均在 F 盘副本被拒绝：关闭敏感值检测、允许无场景快速直答、允许
  未来内容提前激活、颠倒 SKU 与店铺话术优先级；正式工作树恢复后聚焦测试通过。
- `python -m compileall -q src tests`、`git diff --check`、修改文件行宽检查和
  `project-to-act --validate` 均通过。
- 2026-08-19 提交前按六组重新运行 `11 + 23 + 12 + 17 + 24 + 15 = 102 passed`；
  PowerShell 5.1 两份脚本语法、worktree/cached whitespace 和 managed ledger 再次通过。
- 完整套件收集 `961` 个测试；单进程运行在 30 分钟上限被终止，仅有连续通过点，未取得
  完整退出码，因此不登记为全量通过。
- Windows PowerShell 5.1 下开发侧真实 HTTP 演练完成 8 步，自动契约检查全部通过；结果
  `F:\CodexProjects\yunpai-ecommerce-agent-m8r-runtime\wp1-manual-evidence\谢良璇_WP1人工验收结果_20260819-183030.json`
  明确记录 `confirmation_mode=developer_dry_run`、`final_status=developer_dry_run_only`。

## 谢良璇人工黑盒验收

- 2026-08-19 19:37:17 +08:00，谢良璇在 F 盘隔离数据环境使用真实本机 HTTP 接口完成
  8 步验收，并在每一步阅读实际返回后亲自输入 Y/N；未使用 `-AutoConfirm`。
- 验收覆盖服务就绪、8 行受控导入的 6 接受/1 隔离/1 拒绝、批准前不可见、审核和未来
  生效 409、SKU/店铺优先级与跨店/缺场景/相似问法边界、关键词 `advisory_only`、过期内容
  排除、惰性公式与来源追溯、SKU 退役后的店铺兜底及两层退役后的空上下文。
- 结果记录 `confirmation_mode=human`、`automatic_contract_checks=passed`、8 项 observation
  全部 `confirmed=true`、`human_observations_passed=true`、`final_status=human_accepted`。
- 结果文件：
  `F:\CodexProjects\yunpai-ecommerce-agent-m8r-runtime\wp1-manual-evidence\谢良璇_WP1人工验收结果_20260819-185053.json`
  （SHA-256 `b0e7776d073810a2ecdd56d33e3d25157c8ea9fee110a3b8a6661fd7603a7d91`）。
- 过程记录：
  `F:\CodexProjects\yunpai-ecommerce-agent-m8r-runtime\wp1-manual-evidence\谢良璇_WP1人工验收过程_20260819-185053.txt`
  （SHA-256 `3f9d59b791e810852f9cec00cea05abbc05c67dff0cb9c4d016f2220072e9e12`）。
- 该结果证明 WP1 开发侧黑盒功能验收完成，不替代远端推送/PR 审阅与合入、仓库完整全量、WP2～WP4、
  缪海南 WP5、真实渠道、长稳或生产放行。

## 未完成与后续

- 当前 `main` 的 Windows 依赖清单未声明 IANA 时区数据；本机 M8-R 独立环境为运行
  `Asia/Shanghai` 测试单独安装了 `tzdata==2026.3`，尚未修改项目依赖清单。
- WP1～WP4 已组成完整 M8-R PR #25；审核前自查修复见提交 `261a964`，最终待验 head
  以 PR 页面为准。
- 尚未执行真实模型、真实店铺文件、长稳、容量或生产放行测试。
- 缪海南尚未执行 WP5 独立验收，开发者自测不得替代独立签署；WP5 通过前不得合入。
