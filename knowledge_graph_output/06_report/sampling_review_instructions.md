# 关系抽检人工标注指南（sampling_plan.csv）

> 对应验收要求"人工抽检"（validation_report 注明）。`sampling_plan.csv` 的 `expected` 列
> **留空待人工标注**，本指南说明标注规则与回填流程。

## 一、标注规则

对每条抽样关系 `source --[rel_type]--> target`，核对：

| 判定 | 条件 |
|---|---|
| **TRUE** | 头实体（source）、尾实体（target）、关系类型（rel_type）、方向四者与知识库数据一致，且能从原始来源（evidence 列的 source_url/source）佐证 |
| **FALSE** | 任一不符：如头尾实体不存在、关系类型错误、方向相反、或与原始来源矛盾 |

`evidence` 列已给出端点实体的来源引用（source_url / source / 名称）。若某条 evidence 为空，请到 `knowledge_graph_output/01_raw/` 或 `02_clean/` 对应实体文件人工核验后再判定。

## 二、回填步骤

1. 打开 `knowledge_graph_output/06_report/sampling_plan.csv`（UTF-8，可用 Excel/WPS 编辑，注意保持 UTF-8 编码）。
2. 逐条填写：
   - `expected`：`TRUE` 或 `FALSE`
   - `annotation`：判定说明（FALSE 必填原因；TRUE 可写"与证据一致"）
   - `verifier`：标注人姓名
   - `verified_at`：标注日期（YYYY-MM-DD）
3. 保存后，运行以下命令计算真实准确率：

```bash
python -c "
import csv
rows = list(csv.DictReader(open('knowledge_graph_output/06_report/sampling_plan.csv', encoding='utf-8')))
labeled = [r for r in rows if r['expected'].strip()]
unlabeled = [r for r in rows if not r['expected'].strip()]
total = len(labeled)
passed = sum(1 for r in labeled if r['expected'].strip().upper() == 'TRUE')
print(f'已标注 {total}/60 条，未标注 {len(unlabeled)} 条')
print(f'准确率: {passed}/{total} = {passed/total*100:.1f}%' if total else '请先完成标注')
# 按关系类型统计
from collections import Counter
by_rel = Counter(r['rel_type'] for r in labeled if r['expected'].strip().upper()=='TRUE')
print('通过的关系分布:', dict(by_rel))
"
```

## 三、验收口径

- 验收报告不得声称"自动 100%"，而应报"**60 条人工抽检，准确率 X%（X 为实测）**"，附标注人/日期。
- 标注完成的 `sampling_plan.csv` 与准确率结果，作为验收证据链的一部分提交。

## 四、已知边界

- 60 条样本由固定种子 `random.Random(20260803)` 分层随机抽取，可复现。
- 部分关系（如 FAQ→Script）端点实体无 source_url，evidence 为空属正常，按数据文件核验。
- 若发现 FALSE 关系，请在 `annotation` 写明原因，并到 `02_clean/*.json` 或 `05_scripts/10_extend_rels.py` 修正源头。
