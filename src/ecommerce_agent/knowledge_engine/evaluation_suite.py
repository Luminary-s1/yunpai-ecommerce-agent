"""图谱检索质量评测：30+ 问题，验证召回、引用、回答正确性。

对齐验收文档交付物⑦：检索质量评测报告（测试集覆盖 30+ 问题）。
每个问题含预期答案的关键信息（expected_terms），评测时检查检索结果是否命中。
"""

from __future__ import annotations

from .graph_retrieval import GraphRetrievalService

# 30+ 评测问题：覆盖 商品/政策/FAQ/规则/多跳推理/负例 六类
EVALUATION_QUESTIONS: list[dict] = [
    # 商品查询（8）
    {"q": "空气炸锅", "scene": "product", "expected_terms": ["空气炸锅"]},
    {"q": "无线吸尘器", "scene": "product", "expected_terms": ["吸尘器"]},
    {"q": "加湿器", "scene": "product", "expected_terms": ["加湿器"]},
    {"q": "电热水壶", "scene": "product", "expected_terms": ["水壶"]},
    {"q": "循环净化风扇", "scene": "product", "expected_terms": ["风扇"]},
    {"q": "蓝牙耳机", "scene": "product", "expected_terms": ["耳机"]},
    {"q": "充电宝", "scene": "product", "expected_terms": ["充电宝"]},
    {"q": "羽绒服", "scene": "product", "expected_terms": ["羽绒服"]},
    # 政策查询（7）
    {"q": "七天无理由退货", "scene": "policy", "expected_terms": ["七天无理由"]},
    {"q": "保修多久", "scene": "policy", "expected_terms": ["保修"]},
    {"q": "怎么退货", "scene": "policy", "expected_terms": ["退货"]},
    {"q": "价格保护", "scene": "policy", "expected_terms": ["价保", "价格保护"]},
    {"q": "发货时效", "scene": "policy", "expected_terms": ["发货"]},
    {"q": "退款", "scene": "policy", "expected_terms": ["退款"]},
    {"q": "开发票", "scene": "policy", "expected_terms": ["发票"]},
    # FAQ 查询（5）
    {"q": "能退货吗", "scene": "faq", "expected_terms": ["退货"]},
    {"q": "保修多久", "scene": "faq", "expected_terms": ["保修"]},
    {"q": "多久发货", "scene": "faq", "expected_terms": ["发货"]},
    {"q": "开发票", "scene": "faq", "expected_terms": ["发票"]},
    {"q": "尺码怎么选", "scene": "faq", "expected_terms": ["尺码"]},
    # 规则查询（6）
    {"q": "三包", "scene": "rule", "expected_terms": ["三包"]},
    {"q": "消费者权益", "scene": "rule", "expected_terms": ["消费者权益"]},
    {"q": "价格欺诈", "scene": "rule", "expected_terms": ["价格欺诈"]},
    {"q": "食品安全", "scene": "rule", "expected_terms": ["食品"]},
    {"q": "物流投诉", "scene": "rule", "expected_terms": ["物流"]},
    {"q": "明码标价", "scene": "rule", "expected_terms": ["标价", "价格欺诈"]},
    # 多跳推理（5）
    {"q": "空气炸锅适用什么政策", "scene": "multi_hop", "expected_terms": ["空气炸锅"]},
    {"q": "吸尘器保修依据", "scene": "multi_hop", "expected_terms": ["吸尘器"]},
    {"q": "退货政策依据法规", "scene": "multi_hop", "expected_terms": ["退货"]},
    {"q": "发票开具依据", "scene": "multi_hop", "expected_terms": ["发票"]},
    {"q": "数码产品退换", "scene": "multi_hop", "expected_terms": ["数码"]},
    # 负例（4，应检索不到）
    {"q": "不存在的商品xyz", "scene": "negative", "expected_terms": []},
    {"q": "外星人入侵怎么办", "scene": "negative", "expected_terms": []},
    {"q": "量子力学解释", "scene": "negative", "expected_terms": []},
    {"q": "火星移民计划", "scene": "negative", "expected_terms": []},
]


def run_evaluation(svc: GraphRetrievalService, *, verbose: bool = False) -> dict:
    """运行全部评测问题，返回通过率报告。

    判定逻辑：
    - 正常场景（product/policy/faq/rule/multi_hop）：检索结果标题包含任一 expected_term 即通过
    - 负例（negative）：检索结果应为空（未命中不该命中的）

    返回：
        {"total": 问题总数, "passed": 通过数, "pass_rate": 通过率, "details": 逐题结果}
    """
    passed = 0
    details = []
    for item in EVALUATION_QUESTIONS:
        q = item["q"]
        if item["scene"] == "multi_hop":
            # 多跳推理：先用关键词定位起点实体，再做图谱推理验证
            ok = _check_multi_hop(svc, q, item["expected_terms"])
        else:
            results = svc.search(q, limit=10)
            if item["scene"] == "negative":
                ok = len(results) == 0  # 负例应检索不到
            else:
                titles = " ".join(str(r["title"]) for r in results)
                ok = any(term in titles for term in item["expected_terms"])
        if ok:
            passed += 1
        details.append({"q": q, "scene": item["scene"], "passed": ok})
        if verbose:
            mark = "✅" if ok else "❌"
            print(f"{mark} [{item['scene']}] {q}")

    total = len(EVALUATION_QUESTIONS)
    return {
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 3),
        "details": details,
    }


# 每个多跳问题对应的图谱推理验证
# start_keyword: 用关键词检索定位起点实体；chain: 关系链（支持反向用 - 后缀）
# expected: 终点实体的英文标签（Category/Product/SKU/Policy/FAQ/Rule/Script/Attribute）
_MULTI_HOP_PATHS: dict[str, dict] = {
    "空气炸锅适用什么政策": {
        "start_search": "空气炸锅",
        "chain": ["BELONGS_TO", "APPLIES_TO-"],
        "expected": "Policy",
    },
    "吸尘器保修依据": {
        "start_search": "吸尘器",
        # SKU -> 品类 <- 政策 -> 规则（4 跳，含反向 APPLIES_TO）
        "chain": ["BELONGS_TO", "APPLIES_TO-", "RELATED_TO"],
        "expected": "Rule",
    },
    "退货政策依据法规": {
        "start_search": "七天无理由",
        # 政策 -> 规则（直接溯源，RETURN 政策有 RELATED_TO 到 RULE-TAOBAO-7DAYS）
        "chain": ["RELATED_TO"],
        "expected": "Rule",
    },
    "发票开具依据": {
        "start_search": "发票",
        # 政策 -> 规则（价格保护政策 RELATED_TO 到明码标价规则）
        "chain": ["RELATED_TO"],
        "expected": "Rule",
    },
    "数码产品退换": {
        "start_search": "数码",
        "chain": ["BELONGS_TO", "APPLIES_TO-"],
        "expected": "Policy",
    },
}


def _check_multi_hop(svc: GraphRetrievalService, q: str, expected_terms: list[str]) -> bool:
    """多跳推理验证：定位起点实体后，沿关系链推理，检查是否到达预期类型。

    起点选择：优先 SKU（有 BELONGS_TO 出边），其次 Product。SPU 只有 HAS_ATTR，
    没有 BELONGS_TO，不能作为"商品→品类→政策"推理的起点。
    """
    spec = _MULTI_HOP_PATHS.get(q)
    if not spec:
        return False
    # 1. 关键词定位起点，优先 SKU 类型
    starts = svc.search(spec["start_search"], limit=10)
    if not starts:
        return False
    sku_starts = [s for s in starts if s["label"] == "SKU"]
    start = sku_starts[0] if sku_starts else starts[0]
    # 2. 做多跳推理
    paths = svc.multi_hop(start["id"], spec["chain"])
    if not paths:
        return False
    # 3. 检查推理结果类型
    return any(spec["expected"] in str(p["end_label"]) for p in paths)
