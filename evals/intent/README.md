# 意图分类基准

`tests/` 回答「有没有回归」，这里回答「现在有多好、差在哪」。
两者的关键区别：**测试的样例是照着实现写的，基准的语料是照着现实写的。**

## 跑

```bash
.venv/bin/python evals/intent/run.py --mode rule    # 只测规则层（model=None）
.venv/bin/python evals/intent/run.py --mode mock    # 走 mock 网关
.venv/bin/python evals/intent/run.py --mode live    # 打真实模型
```

`live` 需要 `MODEL_ENABLED=true` 和有效的 `MODEL_API_KEY`，否则 `run.py` 会直接
退出而不是给你一份全 `default` 的假成绩单。

平台返回 1305 过载时，可用 `--request-interval <秒>` 拉开真实模型尝试；默认值为
0，不改变原命令。间隔只用于评测器，不会改变生产 `classify()` 的超时或降级行为，
并会写入结果 JSON 便于审计运行条件。

留档对比：

```bash
.venv/bin/python evals/intent/run.py --mode live --out evals/intent/runs/20260804-live.json
```

## 三个数字怎么读

| 指标 | 含义 |
|---|---|
| 端到端准确率 | `default` 兜底也算一次预测。反映用户实际体感 |
| 覆盖率 | 非 `default` 的比例。**它掉下去通常是故障，不是模型变笨** |
| 判定准确率 | 只统计真正给了答案的那些。反映分类能力本身 |

`default` 在这里被当作**弃权**而不是「预测为 chitchat」。混在一起统计，兜底会
把 chitchat 类的分数刷得很好看，掩盖掉「模型根本没被调用」这种故障。

## 标签体系

标签比总分有用——总分告诉你好不好，标签告诉你往哪修。

| 标签 | 含义 | 谁该负责 |
|---|---|---|
| `plain` | 直白表达，含关键词 | 规则层 |
| `keyword_free` | 语义清楚但不含任何关键词 | 模型层 |
| `negation` | 含否定（「不需要退款了」） | 风险门控后交给模型层 |
| `cross_domain` | 关键词出现在别的语境（相机的「曝光」） | 风险门控后交给模型层 |
| `typo` | 错别字 | 模型层 |
| `mixed` | 多意图混合 | 优先级策略 |
| `meta` | 关于 AI 本身的问题 | 待定义 |
| `ambiguous` | 标注本身有争议 | **人**——这些需要团队定调，不是模型的锅 |
| `escalation` | 有升级/维权信号 | 应触发人工接管 |
| `degenerate` | 空串 / 纯符号 / 纯 emoji | 输入校验 |

## 标注口径（有争议时按这里裁）

**诉求优先于语气。** 一条消息既表达了具体业务诉求、又带着不满情绪时，按诉求
归类，不按情绪归类。「我这东西坏了，质量也太差了吧」(`as-007`) 判 `after_sales`
而非 `complaint`——用户要的是把东西修好，不是要一个道歉。

`complaint` 保留给**诉求本身就是投诉**的消息：要求处理服务问题、索赔、维权、
威胁曝光或举报。情绪强度不是判据。

**售前咨询归商品咨询。** 询问退换货政策、保修条款、发货时效本身**不等于**已经
产生售后诉求。「支持七天无理由吗」(`pi-014`，原 `as-013`) 判 `product_inquiry`
——用户在决定买不买，不在处理已发生的问题。`after_sales` 要求存在一笔已成立的
交易和一个待处理的问题。

这两条口径是不同的轴，不要混用：`as-007` 裁的是「诉求 vs 语气」，`pi-014` 裁的
是「售前 vs 售后」。

## 弃权会伪装成答对

`expected` 为 `chitchat` 的样例上，`method="default"` 的兜底结果与正确答案完全
一致，逐条计分无法区分「答对了」和「没答」。所以：

- 任何一类的准确率提升，先查这一类里 `method` 的分布再下结论
- `chitchat` 类的召回率单独看没有意义，必须和覆盖率一起读
- 两次运行给出**完全相同**的分类别准确率时要警惕：这通常说明该指标由标签分布
  决定，与被测行为无关

2026-08-04 的两级链修复就踩过这个坑：`negation` 从 0% 涨到 100%，实际是那条样例
改为弃权、而它的 `expected` 恰好是 `chitchat`。

## 加语料的规矩

1. **来源要真**。`source` 字段区分 `probe-YYYYMMDD`（手工探针）、`handcraft`
   （凭经验编的）、`plan-d11`（照需求文档抄的）。第三类最不可信——用规格造的
   样例验规格，是循环论证。
2. **不要为了让分数好看而删语料**。分数低是信息，删掉就没了。
3. **`ambiguous` 标签是给人看的**。这类样例的 expected 只是当前约定，改标注要
   连带改需求文档，不要偷偷改。
4. 加完跑一遍 `--mode rule`，如果新语料让规则层分数掉了，**那是对的**——说明你
   找到了规则层的真实边界。

## 历次 live 基线

| 日期 | 文件 | 端到端 | 覆盖率 | model 命中 | 备注 |
|---|---|---|---|---|---|
| 2026-08-04 | `runs/20260804-live-baseline.json` | 53.8% | 40.4% | 1/52 | 首次真实运行，暴露响应形状缺陷 |
| 2026-08-04 | `runs/20260804-live-after-fix.json` | 80.8% | 92.3% | 28/52 | 加入信封归一化与降级原因 |

第一份的 `覆盖率 40.4%` 就是那次故障的指纹：模型每次都答对了，答案在解析层被
整条丢弃。**端到端准确率只掉了不到 2 个百分点**（兜底成 chitchat 恰好蒙对一部分），
单看它根本发现不了——这就是覆盖率必须单列的理由。

## 两级链风险仲裁

规则先按 `_RULE_PRIORITY` 取候选；只有命中词被局部否定，或 `曝光` / `推荐` /
`物流` 出现在已知多义上下文时才交给模型复核。普通规则命中仍直接返回，风险提示与
换措辞 few-shot 也只附加在复核路径。

| rule 指标 | 修改前 | 修改后 |
|---|---:|---:|
| 端到端 | 51.9%（27/52） | 57.7%（30/52） |
| 规则判定准确率 | 75.0%（15/20） | 100.0%（15/15） |
| `negation` | 0%（0/1） | 100%（1/1） |
| `cross_domain` | 0%（0/4） | 50%（2/4） |
| `plain` | 100%（22/22） | 100%（22/22） |

证据文件为 `runs/20260804-rule-before-two-stage-fix.json` 与
`runs/20260804-rule-after-two-stage-fix.json`。两份文件的 52 组 `id + expected`
完全一致；分数变化不是通过修改标注获得。

真实模型可用的逐条探针已确认五条风险消息都能由模型纠正；但当日全量运行受
provider code 1305 模型池过载影响。当前
`runs/20260804-live-after-two-stage-fix.json` 含 25 条
`model_call_failed:ModelUnavailableError`，端到端为 34/52，不作为新 live 基线。
需在模型池恢复后重跑，至少达到旧基线的 42/52，才能加入上方“历次 live 基线”。

同日恢复复测见 `runs/20260804-live-retest-after-recovery.json`：端到端 41/52、
覆盖率 36/52，仍含 12 条 `ModelUnavailableError`，因此同样不列为新基线。
按 method 重读后，`negation` 1/1 与 `cross_domain` 4/4 都是实际模型作答且正确；
`plain` 则是覆盖 19/22、已作答 19/19，不能写成无条件的 22/22。基准内五条正确
不改变留出表达仅 2/6 被仲裁的结论。

## 平台不稳时怎么比较两次运行

端到端准确率里混着服务商的可用性：provider 每返回一次 1305，这个数就掉一点，
与分类能力无关。**用它当验收门槛，等于让对方的运维状况决定你们的结论。**

正确做法是只比两次运行**都作答**（`method != "default"`）的交集：

```bash
.venv/bin/python - <<'PY'
import json
a = json.load(open("evals/intent/runs/<旧>.json"))["records"]
b = json.load(open("evals/intent/runs/<新>.json"))["records"]
A = {r["id"]: r for r in a}
B = {r["id"]: r for r in b}
both = [i for i in A if i in B
        and A[i]["method"] != "default" and B[i]["method"] != "default"]
for label, M in (("旧", A), ("新", B)):
    hit = sum(M[i]["correct"] for i in both)
    print(f"{label} {hit}/{len(both)} = {hit / len(both) * 100:.1f}%")
for i in sorted(both):
    if A[i]["correct"] != B[i]["correct"]:
        verb = "修好" if B[i]["correct"] else "弄坏"
        print(f"  {verb} {i} {A[i]['method']}/{A[i]['predicted']}"
              f" -> {B[i]['method']}/{B[i]['predicted']}")
PY
```

逐条列出翻盘方向比总分更重要：**净增 5 分可能是「修好 5 条」，也可能是
「修好 15 条、弄坏 10 条」**，后者通常意味着改动引入了新的失败模式。

局限：该方法假设「哪些条被打掉」与样例难度无关。缺测比例小时大致成立，缺测
过半时不要用。

2026-08-04 两级链仲裁就是这样验收的：交集 36 条，75.0% → 91.7%，6 条翻盘全部
为修好、零回退，其中 5 条是 `rule → model` 的风险仲裁路径。端到端只有 41/52，
按原定的 42/52 门槛会被误判为未通过。

## 已知缺口（截至 2026-08-04）

- `after_sales` 召回 61.5%，明显低于其他三类，模型倾向把带情绪的售后判成
  `complaint`。标注口径已按「诉求优先」裁定（见上），但该口径**尚未写进
  `_MODEL_SYSTEM_PROMPT`**，模型无从知晓。
- `typo` 样本仅 1 条，统计上说明不了任何问题。真实错别字语料需要继续收集。
- mock 模式的分数**不能代表真实模型**：`_mock_generate` 是另一张手写关键词表，
  它衡量的是那张表的覆盖度。下结论一律以 `--mode live` 为准。
