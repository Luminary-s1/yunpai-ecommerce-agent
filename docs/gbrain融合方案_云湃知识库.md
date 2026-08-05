# gbrain 融合方案：云湃 Wiki + 知识图谱双引擎（v4 · B1b 落地版）

> 版本 v4.0 · 2026-08-05 · 承接人：胡磊
> **范围：只做 wiki + 知识图谱双引擎（任务6交付物之上）。**
> 融合对象：`garrytan/gbrain` 的**梦循环（Dream Cycle）+ 架构设计思想**。
> 不涉及 M6 竞品分析、M4/M5 等其他模块。
> **B1b 决策**：scope 不新增列，映射到运行时已有 `layer`（零 schema 改动）。

---

## 〇、方向确认（一句话）

**在任务6已交付的双引擎（`02_clean/` 结构化数据 + `04_import/` 图谱导入文件 + `dictionary_schema.json` 契约）之上，融合 gbrain 的两样东西：① 梦循环（让知识库自动维护）② 架构设计思想（三层边界 + 编译真相 + 时间线）。落地采用 B1b：不新增 scope 列，直接映射到运行时已有的 `layer` 字段。**

---

## 一、任务6已交付的双引擎（融合的起点）

任务6已交付（验收 2/2 通过）：

| 引擎 | 已交付产物 | 位置 |
|---|---|---|
| **知识图谱** | 六类实体 211 节点 + 五类关系 213 边 | `02_clean/*.json` + `04_import/*.csv` + Cypher |
| **Wiki（人读文档）** | 5 份可读 Markdown（商品/政策/话术/FAQ/规则） | `02_clean/*.md` |
| **契约** | `dictionary_schema.json`（唯一对齐口径） | `03_dictionary/` |

**融合只在这之上"加层"，不改已有数据。**

---

## 二、融合 gbrain 的"架构设计思想"（3 个）

### 思想 1：三层知识边界（scope 分层）

**gbrain（Amazon-GBrain）做法**：通用知识 / 单店私有 / 长期记忆 三层，记忆层默认隔离，不参与普通检索。

**落地到双引擎**：
- `dictionary_schema.json` 每个实体定义加 `scope` 枚举：`general` / `seller` / `memory`
- `02_clean/` 每条记录标 scope：
  - `general`：行业规则（三包/消保/3C/价保）、通用话术
  - `seller`：单店商品、SKU、售后政策、FAQ、话术
  - `memory`：长期记忆（默认隔离）
- **查询时按 scope 路由**：seller 查不到回退 general，memory 默认隔离

**价值**：双引擎各自"该通用的通用、该私有的私有"，不混层。

### 思想 2：编译真相 + 时间线

**gbrain 做法**：每个知识页面分"当前结论（可重写）"+"演化历史（只追加）"。

**落地到双引擎**：
- `dictionary_schema.json` 加 `compiled_truth`（当前最佳结论）+ `timeline`（证据轨迹数组，只追加）
- Wiki 5 份文档每页：上半段结论、下半段历史
- **图谱侧**：实体加 `updated_at` 已有，补 `timeline` 记录变更链

**价值**：知识"结论可更新、历史永留存"，可溯源、可回滚。

### 思想 3：薄代码、厚知识（gbrain 架构哲学）

**gbrain 做法**：智能体逻辑写在 markdown 技能里，代码只管执行器（thin harness, fat skills）。

**落地到双引擎**：
- 图谱的六类实体 / 五类关系，保持"契约驱动"（`dictionary_schema.json` 是唯一口径）
- Wiki 页面分类，严格对齐契约，不另起一套
- 未来加知识类型，只改契约 + 文档，不改引擎代码

**价值**：双引擎"改内容不动代码"，扩展知识不扩展复杂度。

---

## 三、融合 gbrain 的"梦循环"（1 个核心机制）

### 梦循环：让双引擎自动维护

**gbrain 做法**：24/7 后台作业，自动摄取、丰富、合并、修复引用、合并记忆。

**落地到双引擎（轻量版，3 个作业）**：

```
┌────────────── 双引擎梦循环 ──────────────┐
│                                          │
│  作业① 增量摄取   自动扫新增客服问答→进库   │
│  作业② 一致性校验 扫图谱孤立节点/悬空引用   │
│  作业③ 合并记忆   相似知识聚类→归纳→不删原文 │
│                                          │
│  运行频率：作业①② 每天一次，作业③ 每周一次  │
└──────────────────────────────────────────┘
```

**作业③ 合并记忆规则（对齐 gbrain `consolidate.ts`）**：
- 同一实体事实 ≥ 3 条才合并
- 最老事实 ≥ 24 小时（等稳定）
- 相似度 ≥ 0.85 归簇（余弦相似度）
- 取置信度最高那条作"合并结论"
- **永不删除原文**，只标"已并入"（可溯源、可回滚）

**价值**：双引擎从"静态数据"变成"会自我维护的活系统"，知识不膨胀、不重复、不腐烂。

---

## 四、落地路线（阶段划分）

| 阶段 | 做什么 | 产出 |
|---|---|---|
| **A. 契约扩展** | `dictionary_schema.json` 加 `scope` + `compiled_truth` + `timeline` 字段定义 | 更新后的契约 |
| **B. 数据标注** | `02_clean/` 5 份文档记录补 scope + 双字段标注 | 标注后的知识文档 |
| **C. 梦循环作业** | 增量摄取 / 一致性校验 / 合并记忆 三个 worker | 可跑的作业脚本 |
| **D. 验证** | 校验标注完整性 + 作业运行测试 + 不破坏已有数据断言 | 验证报告 |

**关键顺序**：先契约（A）→ 再标数据（B）→ 再自动化（C）→ 最后验证（D）。A/B 只加字段不删改，C 是可选项，D 是每个阶段都跑的门禁。

---

## 五、不融合的（明确边界）

| 不做的 | 原因 |
|---|---|
| **不改其他模块**（M6/M4/M5） | 不在双引擎范围，是其他负责人（缪海南/闫睿涵）的活 |
| **不引入向量库** | 云湃架构定位"不依赖独立向量服务"，双引擎检索用图谱+关键词即可 |
| **不换引擎**（PGLite/Postgres） | 双引擎数据已落地，与引擎选择无关 |
| **不做 Wiki 系统搭建 / RAG** | 那是任务3/任务5 的范围，本方案只融合思想到数据层 |

---

## 六、需要审查的决策点

| # | 决策 | 建议 | 原因 |
|---|---|---|---|
| 1 | scope 三层是否现在标 | **建议做** | 双引擎自身分层，成本低，是基础 |
| 2 | compiled_truth + timeline 是否显式化 | **建议做** | 可溯源/可回滚是云湃核心价值 |
| 3 | 梦循环三作业是否现在实现 | **建议阶段C后置** | 先跑通 A/B 数据层，自动化再说 |

---

## 七、参考

- gbrain 上游：https://github.com/garrytan/gbrain
- Amazon-GBrain：https://github.com/WOHUPA/Amazon-GBrain
- 任务6交付报告：`knowledge_graph_output/任务6交付报告_知识库数据采集与清洗加工.md`
- 教学档案：D:\claude-checkpoints\teach_wiki_knowledge_graph.md

---

---

# ✅ 落地实施状态（2026-08-05 · B1b 版）

> 方案已落地为**可运行、低耦合、可复用、已接入运行时**的知识库融合模块，测试通过。

## 一、实施内容（代码位置）

新增独立包 `src/ecommerce_agent/knowledge_engine/`（纯标准库，低耦合）：

| 文件 | 职责 | 对应 gbrain 思想 |
|---|---|---|
| `models.py` | 数据模型：`KnowledgeScope`(三层) + `KnowledgeItem`(compiled_truth/timeline) | 三层边界 + 编译真相/时间线 |
| `loader.py` | 读任务6 `02_clean/` JSON → 统一 KnowledgeItem，自动标 scope | 数据接入 |
| `dream_cycle.py` | 梦循环三作业：增量摄取 / 一致性校验 / 合并记忆 | Dream Cycle |
| `runtime_bridge.py` | **B1b 导入桥**：scope→layer 映射，导入运行时 knowledge 表 | 资产层→运行时 |
| `__init__.py` | 统一对外接口 | 薄接口 |

新增测试：
- `tests/test_knowledge_engine.py`（14 通过 + 1 集成跳过）：模型/加载/梦循环
- `tests/test_knowledge_runtime_bridge.py`（7 通过）：B1b 导入 + RAG 隔离 + 反证

## 二、B1b 核心：scope → layer 映射（零 schema 改动）

```
scope（资产层，gbrain 三层边界）  →  layer（运行时已有字段）
general（跨租户通用）            →  platform（通用话术）/ industry（规则）
seller（单店私有）              →  store（store_id 取自 scope_key）
memory（长期记忆）              →  evolution（默认隔离）
```

**为什么选 B1b**：
- 不新增 `scope` 列，零 schema 迁移、零占号
- 复用运行时已有 `layer` + `store_id` + `sku_id` 的隔离逻辑（不重复造轮子）
- RAG 检索（`rag.py`）已按 layer/store 过滤，导入即生效

**导入范围**：仅 Q&A 类（FAQ/Script/Policy/Rule）进运行时 RAG 表；实体类（Category/Product/SKU/Attribute）留在图谱资产层供 Wiki 人读 + 将来 Neo4j 导入。

## 三、真实数据验证（任务6产物）

```
资产层加载: 203 条（seller=191, general=12）
导入运行时: 130 条（Q&A 类）+ 73 条实体留图谱
layer 分布: store=115, platform=12, product=3
store_id:   qinchuan=118, None=12（general 全局）
```

## 四、测试与反证（验收证据）

**21 passed, 1 skipped**，其中关键验证：

1. **端到端**：导入后 RAG 能检索到 `kg:FAQ-TEST-1`
2. **隔离**：A 店知识 `kg:FAQ-STORE-A` 只能在 A 店检索到，B 店检索不到
3. **通用可见**：general 规则对所有店铺可见
4. **反证**：把 A 店知识的 `store_id` 置 NULL 后，B 店**必须能检索到**（证明隔离测试真实有效）

## 五、快速使用

```bash
# 加载资产层 → 导入运行时 → RAG 检索
.venv/Scripts/python.exe -c "
from ecommerce_agent.knowledge_engine import load_clean_dir, import_to_runtime
from tests.conftest import make_settings
from ecommerce_agent.service import AgentService
import tempfile, pathlib
items = load_clean_dir('knowledge_graph_output/02_clean')
svc = AgentService(make_settings(pathlib.Path(tempfile.mkdtemp())))
print(import_to_runtime(items, svc.knowledge, default_store_id='qinchuan'))
svc.close()
"
```

## 六、后续可扩展（不阻塞当前落地）

- **合并记忆接入时间戳**：真实数据合并需等知识"年龄≥24h"
- **一致性校验的自动修复**：目前只报告悬空引用，后续可加自动重定向
- **实体类进 Neo4j**：Category/Product/SKU/Attribute 走 `04_import/` 导入图谱
- **Wiki 页面渲染**：`KnowledgeItem` 是统一模型，可直接渲染词条页

---

# ✅ 知识图谱接入效果（2026-08-05）

> 任务6知识图谱数据已成功导入 Neo4j，多跳推理/溯源/引用链路全部验证通过。

## 一、环境与导入

- **Neo4j**：`D:\neo4j-community-2026.04.0`（已清库重建，全新实例）
- **账号**：neo4j / yunpai123（本地开发用，可自行改）
- **导入**：任务6 `04_import/` 的 3 个 Cypher（约束→节点→关系），CSV 拷入 `import/kg/`
- **数据规模**：211 节点 + 213 关系（与任务6交付一致）

## 二、图谱效果验证

### 1. 数据统计（导入完整）

```
节点 211: FAQ(60) Script(52) Attribute(51) SKU(12) Category(10) Policy(9) Rule(9) Product(8)
关系 213: REFERS_TO(64) RELATED_TO(52) HAS_ATTR(51) APPLIES_TO(34) BELONGS_TO(12)
```

### 2. 多跳推理（商品→品类→政策）

```
问: 空气炸锅 AF5 适用哪些政策?
答: SKU(QC-AF5-WHITE) →BELONGS_TO→ 空气炸锅品类 ←APPLIES_TO← 七天无理由退货 / 价保 / 发货时效
```

### 3. 政策溯源（FAQ→政策→法规）

```
问: 退货运费谁出?
答: FAQ "退货运费谁出" →REFERS_TO→ 七天无理由退货政策
```

### 4. 标准话术引用（FAQ→Script）

```
问: 你好 / 尺码怎么选?
答: FAQ →REFERS_TO→ Script(greeting/product)
```

### 5. 运营统计（SKU按品类）

```
服饰3 空气炸锅2 数码音频2 无线吸尘器1 加湿器1 电热水壶1 循环风扇1
```

## 三、关键结论

1. **多跳推理打通**：SKU→品类→政策、FAQ→政策、FAQ→话术 全部可查
2. **可溯源**：每个政策/话术都能从 FAQ 逆推到源头
3. **方向正确**：APPLIES_TO 是"政策→品类"，BELONGS_TO 是"商品→品类"（教学强调的方向）
4. **知识图谱双引擎的"图谱"侧已可用**，可支撑客服问答、运营分析、可溯源审查

## 四、快速验证命令

```bash
# 连接
D:\neo4j-community-2026.04.0\bin\cypher-shell.bat -a bolt://localhost:7687 -u neo4j -p yunpai123

# 多跳推理
MATCH (s:SKU {sku_id:'QC-AF5-WHITE'})-[:BELONGS_TO]->(c:Category)<-[:APPLIES_TO]-(p:Policy)
RETURN s.title, c.category_name, p.policy_name
```

---

# ✅ 验收指标测试结果（2026-08-05）

> 对照任务6《电商知识库数据采集与清洗加工计划》的验收标准，逐项实测。

## 〇、运维清单：梦循环自动任务

**计划任务 `YunpaiDreamCycle` 已注册**（每天 03:00 自动跑梦循环：自检 + 自动修复）：

```bash
# 查看任务状态
schtasks /query /tn "YunpaiDreamCycle" /fo LIST

# 手动跑一次梦循环（含自动修复）
.venv/Scripts/python.exe -m ecommerce_agent.knowledge_engine.scheduler --once

# 删除计划任务（不再需要自动跑时）
schtasks /delete /tn "YunpaiDreamCycle" /f
```

**注意**：以上命令在 Git Bash 里需加 `MSYS_NO_PATHCONV=1` 前缀避免路径转换。

**梦循环每次运行自动做 4 件事**：
1. **增量摄取**：去重识别新知识
2. **一致性校验**：扫悬空引用 + 孤立节点
3. **自动修复**：标记失效（不删数据，可溯源）
4. **合并记忆**：相似知识聚类（满 24h 后生效）


## 一、核心指标（实测通过）

| 指标 | 达标线 | 实测结果 | 判定 |
|---|---|---|---|
| **核心实体覆盖率** | ≥36/39（90%） | **39/39 = 100%** | ✅ 达标 |
| **关系准确率（总体）** | ≥51/60（85%） | **60/60 = 100%** | ✅ 达标 |
| **关系准确率（每类）** | 每类 ≥80% | 五类全 100% | ✅ 达标 |
| **数据校验** | 错误 0 项 | **PASS，0 错误** | ✅ 通过 |

## 二、图谱验证查询（Q1-Q7）

| 查询 | 验证内容 | 结果 |
|---|---|---|
| Q1 | 空气炸锅→SKU 链路 | ✅ 2 个 SKU 返回 |
| Q2 | 在售 SKU 按品类统计 | ✅ 7 品类分布 |
| Q3 | 政策适用 air_fryer | ✅ 3 条政策 |
| Q5 | 政策→法规溯源 | ✅ 10 条溯源边（本次补全） |
| Q7 | 五类关系覆盖 | ✅ 全类型存在 |

## 三、本次扩充

- **规则 Rule**：9 → **17** 条（新增发票/价签/退款/物流/食品等 8 条公开法规）
- **溯源关系**：新增 10 条 Policy→Rule（补全 Q5）
- **图谱规模**：219 节点 + 223 关系

## 四、验收动作（你自己可复测）

```bash
# 覆盖率（Neo4j 需运行）
# 见上面"一、核心指标"的验证逻辑，用 truth_table.csv 比对图谱

# 准确率
# 用 sampling_plan.csv 的 60 条期望关系，比对图谱是否存在

# 数据校验
cat knowledge_graph_output/06_report/validation_report.md  # PASS 0错误

# 可视化
# 打开 knowledge_graph_output/knowledge_graph.html（219节点+223关系）
```

---

*方案 + 落地 + 验收指标测试全部完成。验收就绪。*

