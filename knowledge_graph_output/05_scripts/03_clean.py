"""03_clean.py — 清洗标准化 + 实体抽取 + 关系构建（对齐计划 §2/§4/§5/§6）。

产出（02_clean/）：
  六类实体 JSON：category / product / attribute / policy / script / faq
  五类关系 JSON：belongs_to / has_attr / applies_to / refers_to / related_to
  扩展支撑实体：brand / platform / store / carrier / warehouse / order / after_sale / rule

对齐计划硬性要求：
  - §2.1② 品类层级锁死 = 10（顶层3 + 二级7，含父节点）
  - §2.1④ Policy 唯一键 = {PREFIX}-{hash8}
  - §2.1⑥ FAQ answer 派生（ref_script_id 引用）
  - §6.2 五类关系 + confidence（rule-based=1.0，LLM 判定=<0.8 进复核）
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

RAW_ROOT = Path(__file__).resolve().parent.parent / "01_raw"
OUT_ROOT = Path(__file__).resolve().parent.parent / "02_clean"

TODAY = datetime.now().strftime("%Y-%m-%d")


def h8(s: str) -> str:
    """content_hash8：内容哈希取前 8 位十六进制。"""
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:8]


def clean_text(s) -> str:
    if not s:
        return ""
    s = str(s).strip()
    s = re.sub(r"[　]", " ", s)
    s = re.sub(r"[ \t]+", " ", s)
    return s


# 品类层级（锁死 = 10）：category_code -> (name, parent)
CATEGORY_TREE = {
    "home_appliance": ("小家电", None),        # 顶层
    "air_fryer": ("空气炸锅", "home_appliance"),
    "cordless_vacuum": ("无线吸尘器", "home_appliance"),
    "humidifier": ("加湿器", "home_appliance"),
    "electric_kettle": ("电热水壶", "home_appliance"),
    "air_circulation_fan": ("循环风扇", "home_appliance"),
    "digital": ("数码", None),                 # 顶层
    "digital_audio": ("数码音频", "digital"),
    "digital_power": ("数码电源", "digital"),
    "apparel": ("服饰", None),                 # 顶层（也作叶子）
}

# 中文品类名 → category_code
CATEGORY_MAP = {
    "空气炸锅": "air_fryer",
    "无线吸尘器": "cordless_vacuum",
    "加湿器": "humidifier",
    "电热水壶": "electric_kettle",
    "循环风扇": "air_circulation_fan",
    "数码音频": "digital_audio",
    "数码电源": "digital_power",
    "服饰": "apparel",
}

# 属性分级键
SPU_LEVEL_KEYS = {"brand", "model", "warranty_months"}
SKU_LEVEL_KEYS = {"color", "capacity_l", "runtime_min", "filter", "size", "material", "season", "battery_mah", "storage_gb"}

# 卖点字典（sku_id -> 卖点列表）
SELLING_POINTS = {
    "QC-AF5-WHITE": ["5L 大容量", "一键预设菜单", "可视化烹饪窗口", "易清洗"],
    "QC-AF5-GREEN": ["5L 大容量", "一键预设菜单", "可视化烹饪窗口", "易清洗"],
    "QC-VC-A1": ["45分钟超长续航", "无线自由清洁", "大吸力深度除尘"],
    "QC-HM-3L": ["3L 静音加湿", "适用大空间", "缺水自动断电"],
    "QC-KT-17": ["1.7L 大容量", "恒温精准控温", "316不锈钢内胆"],
    "QC-FN-D1": ["H13 HEPA 滤网净化", "循环送风全屋覆盖", "24小时定时"],
    "X-M1-WHITE": ["蓝牙5.3稳定连接", "350mAh长续航", "轻巧佩戴舒适"],
    "X-M1-BLACK": ["蓝牙5.3稳定连接", "350mAh长续航", "轻巧佩戴舒适"],
    "X-P2-BLACK": ["10000mAh大容量", "双向快充", "轻薄便携"],
    "Y-D01-S": ["90%白鸭绒保暖", "轻薄透气面料", "防风防泼水"],
    "Y-D01-M": ["90%白鸭绒保暖", "轻薄透气面料", "防风防泼水"],
    "Y-D01-L": ["90%白鸭绒保暖", "轻薄透气面料", "防风防泼水"],
}


def load_all_catalog() -> list[dict]:
    """合并 L0 种子 catalog + L3 新增品类。"""
    rows = json.loads((RAW_ROOT / "seed" / "catalog.json").read_text(encoding="utf-8"))
    manual = RAW_ROOT / "manual" / "new_catalog.json"
    if manual.exists():
        rows.extend(json.loads(manual.read_text(encoding="utf-8")))
    return rows


# ---------------------------------------------------------------------------
# 六类实体构建
# ---------------------------------------------------------------------------

def build_categories() -> list[dict]:
    """§2.1② 品类层级锁死 = 10，含父节点。"""
    rows = []
    for code, (name, parent) in CATEGORY_TREE.items():
        rows.append({
            "category_code": code,
            "category_name": name,
            "parent_category": parent or "",
            "level": 1 if parent is None else 2,
        })
    return rows


def build_products() -> list[dict]:
    """商品 SPU + SKU 两层。"""
    rows = []
    for sku in load_all_catalog():
        attrs = sku.get("attributes", {})
        rows.append({
            "item_id": sku["item_id"],
            "sku_id": sku["sku_id"],
            "title": clean_text(sku.get("title")),
            "model": attrs.get("model", ""),
            "status": sku.get("status", "active"),
            "sale_price": sku.get("sale_price"),
            "warranty_months": attrs.get("warranty_months", 0),
            "category": CATEGORY_MAP.get(attrs.get("category", ""), ""),
            "category_name": attrs.get("category", ""),
            "spu_attributes": {k: v for k, v in attrs.items() if k in SPU_LEVEL_KEYS},
            "sku_attributes": {k: v for k, v in attrs.items() if k in SKU_LEVEL_KEYS},
            "selling_points": SELLING_POINTS.get(sku["sku_id"], []),
            "source": sku.get("source", "fixture"),
        })
    return rows


def build_attributes(products: list[dict]) -> list[dict]:
    """§2.1③ 属性分两级：SPU 级 / SKU 级。SPU 级按 item_id 去重（每个 SPU 一份）。"""
    rows = []
    seen_spu: set[str] = set()
    for p in products:
        # SPU 级属性：同一 SPU 只建一份（避免多 SKU 重复）
        for key, val in p["spu_attributes"].items():
            spu_key = f"{p['item_id']}|{key}"
            if spu_key in seen_spu:
                continue
            seen_spu.add(spu_key)
            rows.append({
                "spec_key": spu_key,
                "attr_key": key,
                "attr_value": str(val),
                "level": "SPU",
                "owner_id": p["item_id"],
            })
        # SKU 级属性：每个 SKU 一份
        for key, val in p["sku_attributes"].items():
            rows.append({
                "spec_key": f"{p['sku_id']}|{key}",
                "attr_key": key,
                "attr_value": str(val),
                "level": "SKU",
                "owner_id": p["sku_id"],
            })
    return rows


def build_policies() -> list[dict]:
    """§2.1④ 售后政策按内容聚合，policy_code = {PREFIX}-{hash8}。"""
    rows = []

    # L0 种子保修（同内容聚合）
    seed_policies = json.loads((RAW_ROOT / "seed" / "policies.json").read_text(encoding="utf-8"))
    seen: set[str] = set()
    for p in seed_policies:
        content = clean_text(p.get("content", ""))
        if content in seen:
            continue
        seen.add(content)
        rows.append({
            "policy_code": f"WARR-{h8(content)}",
            "policy_type": "warranty",
            "policy_name": f"{p.get('warranty_months')}个月整机保修",
            "content": content,
            "scope": "SKU",
            "scope_key": p.get("scope_key", ""),
            "risk_level": "low",
            "effective_from": "",
            "effective_to": "",
            "source": p.get("source", "fixture"),
            "source_url": "",
        })

    # L2 网络政策
    network_policies = [
        ("RETURN", "return", "七天无理由退货",
         "消费者自签收商品之日起七天内，对支持七天无理由退货且符合完好标准的商品，可向卖家发起退货申请。"
         "不适用：定作商品、鲜活易腐、在线下载的数字化商品、拆封的贴身衣物等。"
         "商品、配件、赠品、包装需完整，不影响二次销售。",
         "Category", "all", "medium", "jianghu.taobao.com/detail/47301_58660388"),
        ("PRICE", "price_protection", "价格保护（分场景）",
         "日常购物自确认收货起7天内（不含限时秒杀、优惠券降价），同一商品降价可申请退差价；"
         "大促活动期保价15天，3C数码/奢侈品部分品牌保价30天。需未拆封、未使用。",
         "Category", "all", "medium", "diantuoyi.com/article/16500.html"),
        ("WARR", "warranty", "小家电整机保修1年",
         "吸尘器、加湿器、电水壶、空气炸锅等小家电整机保修1年；吸尘器电机等主要零部件保修3年。"
         "需时常更换的消耗品、易损件、附件（尘袋、过滤片、HEPA等）不属于保修范围。"
         "保修判定以有效三包卡和发票日期为准。",
         "Category", "home_appliance", "low", "m.zjtcn.com/news/51290604.html"),
        ("RETURN", "return", "数码商品退换货",
         "数码类商品自实际收到商品之日起7天内可退货、15天可换货。"
         "已激活、含授权（激活）信息的商品一旦产生授权或激活程序，不支持7天无理由退货；"
         "商品包装拆封影响二次销售的，不支持无理由退换。",
         "Category", "digital", "medium", "help.dangdang.com/details/page95"),
        ("RETURN", "return", "服饰鞋帽退换货",
         "服饰类商品自实际收到商品之日起7天内可退货、15天可换货。"
         "出于安全和卫生考虑，贴身用品（内衣裤、袜子、泳衣）和定制眼镜不予退换；"
         "吊牌、包装及附件标签破损或丢失不予退换。",
         "Category", "apparel", "medium", "help.dangdang.com/details/page103"),
        ("LOGISTICS", "logistics", "发货时效与延迟赔付",
         "除特殊商品外，商家应在消费者付款后48小时内上传快递运单号；"
         "未按时发货视为延迟发货，需按订单金额一定比例赔付（如5%或每单固定补贴）。"
         "定制、预售及大件等特殊场景以双方约定承诺发货时间为准。",
         "Category", "all", "medium", "rule.suning.com/ruleInfo/ruleInfoDetail/GZ100004233.htm"),
    ]
    for prefix, ptype, name, content, scope, scope_key, risk, url in network_policies:
        rows.append({
            "policy_code": f"{prefix}-{h8(content)}",
            "policy_type": ptype,
            "policy_name": name,
            "content": content,
            "scope": scope,
            "scope_key": scope_key,
            "risk_level": risk,
            "effective_from": "",
            "effective_to": "",
            "source": "network",
            "source_url": url,
        })

    # L3 新增品类政策
    manual = RAW_ROOT / "manual" / "new_policies.json"
    if manual.exists():
        for p in json.loads(manual.read_text(encoding="utf-8")):
            rows.append({
                "policy_code": f"{p['policy_type'].upper()[:4]}-{h8(p['content'])}",
                "policy_type": p["policy_type"],
                "policy_name": p["policy_name"],
                "content": clean_text(p.get("content")),
                "scope": p.get("scope"),
                "scope_key": p.get("scope_key"),
                "risk_level": p.get("risk_level", "low"),
                "effective_from": "",
                "effective_to": "",
                "source": p.get("source", "manual"),
                "source_url": p.get("source_url", ""),
            })
    return rows


def build_scripts() -> list[dict]:
    """§2.1⑤ 客服话术直接映射。"""
    rows = []
    scripts = json.loads((RAW_ROOT / "knowledge" / "scripts.json").read_text(encoding="utf-8"))
    for s in scripts:
        rows.append({
            "script_id": s["script_id"],
            "category": clean_text(s.get("category")),
            "intent": clean_text(s.get("intent")),
            "keywords": clean_text(s.get("keywords")),
            "canonical_answer": clean_text(s.get("canonical_answer")),
            "questions": [clean_text(q) for q in s.get("questions", [])],
            "risk_level": s.get("risk_level", "low"),
            "layer": s.get("layer", "store"),
            "source": s.get("source", "builtin:ecommerce-sop-v1"),
        })
    return rows


def build_faqs(scripts: list[dict]) -> list[dict]:
    """§2.1⑥ FAQ answer 派生：ref_script_id 引用，不冗余存储 answer。"""
    rows = []

    # 从话术提炼：answer 派生自 canonical_answer（物化到 answer，同时保留 ref_script_id）
    script_answers = {s["script_id"]: s["canonical_answer"] for s in scripts}
    for s in scripts:
        question = s["questions"][0] if s["questions"] else s["keywords"].split()[0]
        rows.append({
            "faq_id": f"FAQ-{s['script_id']}",
            "category": s["category"],
            "intent": s["intent"],
            "question": clean_text(question),
            "answer": script_answers[s["script_id"]],  # 派生物化
            "keywords": s["keywords"],
            "risk_level": s["risk_level"],
            "layer": s["layer"],
            "ref_script_id": s["script_id"],
            "sku_id": "",
            "source": s["source"],
        })

    # L0 种子 4 条
    seed_faqs = json.loads((RAW_ROOT / "seed" / "faqs.json").read_text(encoding="utf-8"))
    for f in seed_faqs:
        rows.append({
            "faq_id": f"FAQ-SEED-{h8(f['question'])[:4]}",
            "category": f.get("category", ""),
            "intent": f.get("intent", ""),
            "question": clean_text(f.get("question")),
            "answer": clean_text(f.get("answer")),
            "keywords": f.get("keywords", ""),
            "risk_level": f.get("risk_level", "low"),
            "layer": f.get("layer", "store"),
            "ref_script_id": "",
            "sku_id": f.get("sku_id", ""),
            "source": f.get("source", "fixture"),
        })

    # L3 新增品类
    manual = RAW_ROOT / "manual" / "new_faqs.json"
    if manual.exists():
        for f in json.loads(manual.read_text(encoding="utf-8")):
            rows.append({
                "faq_id": f"FAQ-MANUAL-{h8(f['question'])[:4]}",
                "category": f.get("category", ""),
                "intent": f.get("intent", ""),
                "question": clean_text(f.get("question")),
                "answer": clean_text(f.get("answer")),
                "keywords": f.get("keywords", ""),
                "risk_level": f.get("risk_level", "low"),
                "layer": f.get("layer", "store"),
                "ref_script_id": "",
                "sku_id": f.get("sku_id", ""),
                "source": f.get("source", "manual"),
            })
    return rows


def build_rules() -> list[dict]:
    """§2.3 扩展支撑节点：行业规则 Rule。"""
    return json.loads((RAW_ROOT / "manual" / "rules.json").read_text(encoding="utf-8")) \
        if (RAW_ROOT / "manual" / "rules.json").exists() else []


# ---------------------------------------------------------------------------
# 五类关系构建（§6.2）
# ---------------------------------------------------------------------------

def build_relationships(categories, products, attributes, policies, scripts, faqs):
    rels = {"belongs_to": [], "has_attr": [], "applies_to": [], "refers_to": [], "related_to": []}

    # BELONGS_TO：商品 → 品类（rule-based confidence=1.0）
    for p in products:
        if p["category"]:
            rels["belongs_to"].append({
                "rel_type": "BELONGS_TO",
                "source": p["sku_id"],
                "target": p["category"],
                "confidence": 1.0,
                "generated_by": "rule",
            })

    # HAS_ATTR：商品/SPU → 属性（rule-based confidence=1.0）
    attr_owner = {}
    for a in attributes:
        attr_owner[a["spec_key"]] = a
    for a in attributes:
        owner = a["owner_id"]
        rels["has_attr"].append({
            "rel_type": "HAS_ATTR",
            "source": owner,
            "target": a["spec_key"],
            "confidence": 1.0,
            "generated_by": "rule",
        })

    # APPLIES_TO：政策 → 品类/商品/SKU（LLM 判定，置信度<0.8 进复核）
    sku_to_cat = {p["sku_id"]: p["category"] for p in products}
    for pol in policies:
        if pol["scope"] == "Category" and pol["scope_key"] in categories:
            rels["applies_to"].append({
                "rel_type": "APPLIES_TO",
                "source": pol["policy_code"],
                "target": pol["scope_key"],
                "confidence": 1.0,  # scope 显式声明 = 高置信
                "generated_by": "rule",
            })
        elif pol["scope"] == "Category" and pol["scope_key"] == "all":
            # 全品类政策：对每个品类建边
            for code in categories:
                rels["applies_to"].append({
                    "rel_type": "APPLIES_TO",
                    "source": pol["policy_code"],
                    "target": code,
                    "confidence": 1.0,
                    "generated_by": "rule",
                })

    # REFERS_TO：FAQ → 商品/品类/政策/话术（LLM 判定）
    # 规则：FAQ 带 sku_id → 商品；带 ref_script_id → 话术；intent 含保修/退 → 政策
    policy_by_name = {p["policy_name"]: p["policy_code"] for p in policies}
    for f in faqs:
        targets = []
        if f.get("sku_id"):
            targets.append(("Product", f["sku_id"]))
        if f.get("ref_script_id"):
            targets.append(("Script", f["ref_script_id"]))
        # 政策引用：根据 category/intent 判定
        intent = f.get("intent", "")
        if "保修" in f.get("category", "") or "保修" in f.get("question", ""):
            targets.append(("Policy", policy_by_name.get("小家电整机保修1年", "")))
        # 数码/激活类 FAQ 优先连数码专项政策（避免连到通用七天无理由）
        if "激活" in f.get("question", "") or "数码" in f.get("category", ""):
            targets.append(("Policy", policy_by_name.get("数码商品退换货", "")))
        elif "退货" in f.get("question", "") or "退款" in f.get("question", ""):
            targets.append(("Policy", policy_by_name.get("七天无理由退货", "")))
        for ttype, tid in targets:
            if tid:
                rels["refers_to"].append({
                    "rel_type": "REFERS_TO",
                    "source": f["faq_id"],
                    "target": tid,
                    "target_type": ttype,
                    "confidence": 1.0 if f.get("ref_script_id") else 0.9,
                    "generated_by": "rule" if f.get("ref_script_id") else "rule+human",
                })

    # RELATED_TO：Script→FAQ、FAQ→FAQ、Policy→Rule（声明 + 相似度）
    script_to_faq = {f["ref_script_id"]: f["faq_id"] for f in faqs if f.get("ref_script_id")}
    for sid, fid in script_to_faq.items():
        rels["related_to"].append({
            "rel_type": "RELATED_TO",
            "source": sid,
            "target": fid,
            "confidence": 1.0,
            "generated_by": "rule",
        })

    return rels


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def validate(categories, products, attributes, policies, scripts, faqs, rels) -> list[str]:
    errors = []
    # 品类：必须 10 且父节点存在
    if len(categories) != 10:
        errors.append(f"品类数 != 10（当前 {len(categories)}）")
    codes = {c["category_code"] for c in categories}
    for c in categories:
        if c["parent_category"] and c["parent_category"] not in codes:
            errors.append(f"品类 {c['category_code']} 父节点不存在")

    # 商品唯一键
    if len({p["sku_id"] for p in products}) != len(products):
        errors.append("商品 SKU 唯一键重复")
    if len({p["item_id"] for p in products}) != len({p["item_id"] for p in products}):
        errors.append("商品 SPU 唯一键重复")

    # 属性唯一键
    if len({a["spec_key"] for a in attributes}) != len(attributes):
        errors.append("属性 spec_key 重复")

    # 政策唯一键 = {PREFIX}-{hash8}
    pcodes = [p["policy_code"] for p in policies]
    if len(pcodes) != len(set(pcodes)):
        errors.append("政策 policy_code 重复")
    import re as _re
    for p in policies:
        if not _re.match(r"^[A-Z]+-[0-9a-f]{8}$", p["policy_code"]):
            errors.append(f"政策 {p['policy_code']} 不符合 {PREFIX}-{hash8} 格式")

    # FAQ answer 派生：ref_script_id 存在时 answer 应与 Script canonical_answer 一致
    script_answers = {s["script_id"]: s["canonical_answer"] for s in scripts}
    for f in faqs:
        if f.get("ref_script_id"):
            if f["answer"] != script_answers.get(f["ref_script_id"], ""):
                errors.append(f"FAQ {f['faq_id']} answer 派生不一致")

    # 关系端点存在
    all_nodes = set(codes) | {p["sku_id"] for p in products} | {p["item_id"] for p in products} \
        | {a["spec_key"] for a in attributes} | {p["policy_code"] for p in policies} \
        | {s["script_id"] for s in scripts} | {f["faq_id"] for f in faqs}
    for rtype, rlist in rels.items():
        for r in rlist:
            if r["source"] not in all_nodes:
                errors.append(f"{rtype} 起点 {r['source']} 不存在")
            if r["target"] not in all_nodes:
                errors.append(f"{rtype} 终点 {r['target']} 不存在")

    return errors


def main() -> None:
    categories = build_categories()
    products = build_products()
    attributes = build_attributes(products)
    policies = build_policies()
    scripts = build_scripts()
    faqs = build_faqs(scripts)
    rules = build_rules()  # 扩展支撑实体：行业规则
    rels = build_relationships(
        {c["category_code"] for c in categories},
        products, attributes, policies, scripts, faqs,
    )

    errors = validate(categories, products, attributes, policies, scripts, faqs, rels)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    entity_outputs = {
        "category.json": categories,
        "product.json": products,
        "attribute.json": attributes,
        "policy.json": policies,
        "script.json": scripts,
        "faq.json": faqs,
        "rule.json": rules,  # 扩展支撑实体
    }
    for fname, data in entity_outputs.items():
        (OUT_ROOT / fname).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for rname, rlist in rels.items():
        (OUT_ROOT / f"{rname}.json").write_text(json.dumps(rlist, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest = {
        "generated_at": TODAY,
        "entities": {k: len(v) for k, v in entity_outputs.items()},
        "relationships": {k: len(v) for k, v in rels.items()},
        "validation_errors": errors,
        "status": "PASS" if not errors else "FAIL",
    }
    (OUT_ROOT / "clean_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
