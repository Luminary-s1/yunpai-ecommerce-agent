# 项目账本：M3 知识库与 RAG 检索链路（PROJECT_KNOWLEDGE）

> 本文件是 M3 知识库工作包（PR #10）的专用账本，从属于 `.project-to-act`（唯一项目事实源）。
> 记录检索评分公式演进、评测基线、embedding 决策、降级契约与遗留问题处置。
> 编号沿用项目口径：D-KB-xxx 决策 / E-2026MMDD-xxx 证据。

## 1. 检索评分公式演进

| 版本 | 公式 | 说明 |
|---|---|---|
| 当前（7a43407 起） | `0.55·semantic + 0.45·lexical + intent_bonus(0.12)` | rag.py `_score`；semantic=hash 余弦，lexical=词面交集归一化 |
| main 线变体 | `0.60/0.40 + IDF 有界 boost` | origin/main 存在 IDF 加权版；合入时需决策对齐（D-KB-001） |

- 检索阈值：`rag_min_score`（默认 0.12；wiki_api 检索路径 0.08）
- 同义词扩展：15 组（保修↔质保等），`expand_synonyms_text` 仅完整命中词展开（不子串盲扩）

## 2. 评测基线

- **图谱检索评测**：35 题（含负例），`pass_rate >= 0.9`（pytest 硬门禁）
- **RAG 改写查询评测**：recall@3 >= 0.75、MRR >= 0.65（`test_rag_evaluation.py`）
- **门禁**：`RAG_EVAL_THRESHOLD=0.9` 环境变量；`--eval` 低于阈值 exit 1
- **M3 模块回归**：`scripts/m3_regression.sh`（20 文件固定流程）；基线 187 passed @ 7a43407
- 无 Neo4j 环境时评测降级 `status=skipped`（不误报回归）

## 3. Embedding 决策（D-KB-002）

- **已接受技术债**：`hash_embedding`（零外部依赖，非语义向量），改写查询 recall@3=88.1%
- **不重议**：用户决策 A——不引入本地 embedding（如 BGE-small-zh）
- **升级触发条件**：口语化 query 占比上升致转人工率升高时，重估方案 B，用 `test_rag_evaluation.py` 做 A/B

## 4. R4 降级契约（D-KB-003）

- 检索故障：`route=handoff` + `route_reason=knowledge_unavailable` + `requires_human=True` + `model_fallback=True`
- 可观测：`db.audit("knowledge.retrieval_failure", ...)`（只记 error_type/stage，不外泄内部详情）+ request_metrics
- SSE 流式：`handoff` 事件 + `reason=knowledge_unavailable`（可区分码）
- 负责人已认可（复验 12a4b2b / f8bec9d 轮），不重议

## 5. 本轮修复决策与证据（2026-08-13）

| 决策 | 内容 | 证据 |
|---|---|---|
| D-KB-010 | 意图路由集成链对齐 origin/main（precheck 分类 + knowledge_intent + prompt_variant 全链路） | 6c7cf34~1a6062d；test_intent_routing_integration 18 全绿 |
| D-KB-011 | knowledge_key active 唯一约束（v31，保留多版本语义）+ 去重迁移 | e3c93e6；test_migrations 17 passed |
| D-KB-012 | 热更新 FTS 同步 + _write_lock + record_version + 租户条件 + update_failed | c78eae1/476575b；test_knowledge_runtime_bridge 17 passed |
| D-KB-013 | 评测门禁硬失效（--eval exit 1） | 59e2059 |
| D-KB-014 | 投诉语境降级（legal_boundary 投诉句式 escalate 而非 block） | 7a43407；test_security_observability 22 passed |
| D-KB-015 | Wiki status 透传 + 搜索 id 对齐 knowledge_key | f7afe2d/2653b21 |
| D-KB-016 | create/revise 竞态：预检与版本分配移入 _write_lock（方案 a） | cd6de5f |
| D-KB-017 | import 预查按租户过滤（skipped_foreign 防跨租户改写） | acccd76 |
| D-KB-018 | 死代码清理（coerce_scope / KnowledgeItem.revise） | d72fb00/8588035 |
| D-KB-019 | loader 缺失文件打 warning | 33ed593 |
| D-KB-020 | 终审闭环：门禁 sys.exit + Wiki 预检移除 + COALESCE 索引 + 热更新 active 过滤 + IntegrityError 捕获 + layer/store_id 沿用 | 8403a62~457793f；三代理终审发现 11 项全部处置（7 修复 + 4 记账） |

## 6. 遗留问题处置表

| 问题 | 处置 | 状态 |
|---|---|---|
| P2-1 Wiki status 参数恒空 | Task 1：statuses 透传 | ✅ f7afe2d |
| P2-2 Wiki 搜索 id 不可链接 | Task 2：knowledge_key 对齐 | ✅ 2653b21 |
| P3-1 热更新无锁/无版本/跨租户 | Task 3：四合一修复 | ✅ 476575b |
| P3-2 import 预查无租户过滤 | Task 4：foreign 分组 | ✅ acccd76 |
| P3-3 create/revise 竞态 | Task 5：方案 a 移锁 | ✅ cd6de5f |
| P3-4 loader 静默 | Task 6：warning | ✅ 33ed593 |
| P3-5 死导出 coerce_scope | Task 7：删除 | ✅ d72fb00 |
| P3-6 死方法 KnowledgeItem.revise | Task 8：删除 | ✅ 8588035 |
| P3-7 观测 _all_records 不合并内存 | **已核实关闭**：observability.py L117 现役且被 report() L158 消费，旧报告依据版本不适用 | ✅ 无需修复 |
| P2-3 RAG 线上 feedback 回归 | 等真实负反馈数据（点踩/转人工/修正答案）再标注入评测集 | ⏸ 待数据 |
| P2-4 知识库账本 | 本文件 | ✅ 本文档 |

### 6.1 终审遗留处置（2026-08-13 三代理终审）

| 问题 | 处置 | 状态 |
|---|---|---|
| 终审 P1-门禁退出码恒 0 | scheduler `__main__` 补 `sys.exit(main())` | ✅ 8403a62 |
| 终审 P1-Wiki 二次编辑预检误拦 | 移除 create 预检（create 全链进 _write_lock 已覆盖竞态） | ✅ fd39257 |
| 终审 P1-v31 索引 NULL 租户失效 | COALESCE 表达式索引 + retire/approve/rollout 含全局行 | ✅ fd39257 |
| 终审 P2-热更新命中 retired 行 | import 预查只收 active 行 | ✅ d6d40ba |
| 终审 P2-INSERT 未捕获 IntegrityError | try/except sqlite3.IntegrityError 计 update_failed | ✅ d6d40ba |
| 终审 P3-7 Wiki 编辑 layer 硬编码 store | put_item 沿用原词条 layer/store_id 防 scope 漂移 | ✅ ad1a741 |
| 终审低项-兜底 dict 缺键 | service.py 三处兜底 dict 补 update_failed/skipped_foreign | ✅ 457793f |
| 终审 P3-5 Wiki 反复编辑 version=1 | Wiki PUT 走 create 新行（同 knowledge_key 多版本），版本号不复用原行 MAX+1，与 revise 口径不一致。**建议**：Wiki PUT 改走 revise（expected_record_version 取自当前 active 行），生命周期一步到位 | ⏸ 待下一迭代 |
| 终审 P3-6 热更新不刷新 checksum/version | 热更新刷新 search_text/embedding/updated_at/record_version，但不重算 checksum、不递增 version。检索一致性已闭环；checksum 仅用于资产层校验，影响面低 | ⏸ 待下一迭代 |
| 终审 P3-8 general 资产以 bootstrap 租户导入 | **已修复**（多租户升级为 P2-1，见 6.2） | ✅ 3f7cb20 |
| 终审 P3-9 memory dedup 缺租户 | **已修复**（多租户升级，见 6.2） | ✅ 3f7cb20 |

### 6.2 多租户隔离专项修复（2026-08-13 用户确认多租户为当前目标场景）

> 专项审查按多租户基线重审，发现 2 P1 + 1 P2 + 6 P3。用户拍板：**租户影子编辑**（租户编辑全局词条→私有新版本，其他租户仍见全局版）；**无店铺 seller 资产全局可见**。

| 项 | 处置 | commit |
|---|---|---|
| P1-1 租户 approve/rollback/complete_rollout 可退休全局行（偷走全局知识） | 三处 retire 条件去 `OR tenant_id IS NULL`，改精确 `tenant_id=?` | ✅ 3f7cb20 |
| P1-2 租户热更新可越权改写全局行 | import_to_runtime 按有效租户分组 + `allow_global_update` 旗标（仅 appliance 启动导入传 True）+ 热更新租户条件收紧 | ✅ 3f7cb20 |
| P2-1+⑤ general/无店铺 seller 资产挂 bootstrap（全局不可见） | GENERAL 或 scope_key=all 强制 tenant_id=NULL；无店铺 seller store_id=None | ✅ 3f7cb20 |
| P3-4 load_from_runtime None 无租户过滤（泄露所有租户） | None → 只读全局行；传租户 → 本租户+全局 | ✅ 3f7cb20 |
| P3-5 forget 含 NULL 分支（租户可删全局记忆） | 精确租户匹配 | ✅ 3f7cb20 |
| P3-9 dedup 无租户条件（跨租户记忆被吞） | dedup SQL 加租户条件 | ✅ 3f7cb20 |
| ① 影子排序无 tiebreak（影子编辑不生效） | RetrievedDocument 加 tenant_id，排序 score 后插本租户优先 | ✅ 01f0ec5 |
| ③ Wiki 服务写死 bootstrap 租户视角 | WikiService 四方法 + 路由传 admin.tenant_id | ✅ 9be55ad/a9e7f71 |
| ④ put_item 影子编辑语义 | 恒定 admin.tenant_id + store/product 层兜底 store_id、platform/industry 恒 None | ✅ 9be55ad |
| ⑥ 存量库资产挂 bootstrap（升级不生效） | 启动导入前幂等重租户化（全局层行→NULL、冲突行 retired、店铺行不动） | ✅ 93a6615 |
| 低项 next_version 混全局行 / retire_document 不递增版本 | 精确租户 / record_version+1 | ✅ 1ae8d53 |
| V1（复审发现）Wiki 详情/stats 路由漏传租户（跨租户读泄露） | 两条路由补 admin.tenant_id | ✅ a9e7f71 |

### 6.3 多租户复审遗留（2026-08-13 对抗性复审）

| 项 | 决策 | 状态 |
|---|---|---|
| V2 店铺资产归属未决（kg-id first-wins，非 bootstrap 租户拿不到店铺级 seller 知识） | **记账**：需产品层决策——按租户分片 02_clean 或 scope_key→tenant 映射；当前部署以 bootstrap 租户店铺为主，不影响主线 | ⏸ 待产品决策 |
| P3-1 全局生命周期死路径（_require 传 None 永不匹配；create(None) 留孤儿 candidate） | **记账**：全局行改版仅走 appliance 热更新/retrofit；注释已澄清 | ⏸ 待全局管理员体系 |
| P3-2 memory.recall(None) 无过滤 | **记账**：API 契约地雷（当前调用方都传租户，无实际泄露） | ⏸ 待下一迭代 |
| P3-3 evolution get_document(None) 读无过滤 | **记账**：写路径被 retire_document 精确租户兜住，仅读泄露；API 层均传租户 | ⏸ 待下一迭代 |
| P3-4 load_from_runtime 合并视图版本平局无 tiebreak | **记账**：展示与检索可能不一致（非安全洞） | ⏸ 待下一迭代 |
| P3-5 product 层 Wiki 编辑必 400（put_item 不传 sku_id） | **记账**：预存在问题 | ⏸ 待下一迭代 |

## 7. 合入条件核对（负责人 08-12 10:06 终验五条）

1. ✅ 04_import ≡ 02_clean（8be3b97 + test_single_source 锁死）
2. ✅ 文档 yunpai123 清除（8be3b97）
3. ⏳ 全量测试结果贴 PR（M3 模块测试基线 187 passed；交叉验证后贴最终结果）
4. ✅ mergeable=clean（9ae462f 已 merge main）
5. ✅ 评测 pytest 阈值 0.9 + RAG_EVAL_THRESHOLD 门禁
