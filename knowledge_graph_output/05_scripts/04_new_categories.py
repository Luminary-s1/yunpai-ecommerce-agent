"""04_new_categories.py — 新增品类（数码/服饰）数据构造。

补充任务书"覆盖至少 3 个商品品类"的硬指标：
  - 数码（星脉）：蓝牙耳机 X-M1（白/黑）+ 充电宝 X-P2
  - 服饰（云织）：羽绒服 Y-D01（S/M/L）

按 catalog 字段契约构造，落盘 01_raw/manual/ 作为 L3 人工整理源。
"""
from __future__ import annotations

import json
from pathlib import Path

RAW_MANUAL = Path(__file__).resolve().parent.parent / "01_raw" / "manual"

# 新品类构造：与 virtual_store_v1.json 的 catalog 字段契约一致
NEW_CATALOG = [
    # 数码 - 蓝牙耳机 X-M1
    {
        "item_id": "X-SPU-M1",
        "sku_id": "X-M1-WHITE",
        "title": "星脉蓝牙耳机 X-M1 云白色",
        "status": "active",
        "sale_price": "299.00",
        "attributes": {
            "brand": "星脉",
            "category": "数码音频",
            "model": "X-M1",
            "color": "云白",
            "battery_mah": 350,
            "warranty_months": 12,
        },
        "source": "manual",
    },
    {
        "item_id": "X-SPU-M1",
        "sku_id": "X-M1-BLACK",
        "title": "星脉蓝牙耳机 X-M1 曜石黑",
        "status": "active",
        "sale_price": "299.00",
        "attributes": {
            "brand": "星脉",
            "category": "数码音频",
            "model": "X-M1",
            "color": "曜石黑",
            "battery_mah": 350,
            "warranty_months": 12,
        },
        "source": "manual",
    },
    # 数码 - 充电宝 X-P2
    {
        "item_id": "X-SPU-P2",
        "sku_id": "X-P2-BLACK",
        "title": "星脉充电宝 X-P2 10000mAh 黑色",
        "status": "active",
        "sale_price": "129.00",
        "attributes": {
            "brand": "星脉",
            "category": "数码电源",
            "model": "X-P2",
            "color": "黑色",
            "storage_gb": 0,
            "battery_mah": 10000,
            "warranty_months": 12,
        },
        "source": "manual",
    },
    # 服饰 - 羽绒服 Y-D01
    {
        "item_id": "Y-SPU-D01",
        "sku_id": "Y-D01-S",
        "title": "云织羽绒服 Y-D01 黑色 S 码",
        "status": "active",
        "sale_price": "599.00",
        "attributes": {
            "brand": "云织",
            "category": "服饰",
            "model": "Y-D01",
            "color": "黑色",
            "size": "S",
            "material": "90%白鸭绒",
            "season": "冬季",
            "warranty_months": 0,  # 服饰不保修，走三包退换
        },
        "source": "manual",
    },
    {
        "item_id": "Y-SPU-D01",
        "sku_id": "Y-D01-M",
        "title": "云织羽绒服 Y-D01 黑色 M 码",
        "status": "active",
        "sale_price": "599.00",
        "attributes": {
            "brand": "云织",
            "category": "服饰",
            "model": "Y-D01",
            "color": "黑色",
            "size": "M",
            "material": "90%白鸭绒",
            "season": "冬季",
            "warranty_months": 0,
        },
        "source": "manual",
    },
    {
        "item_id": "Y-SPU-D01",
        "sku_id": "Y-D01-L",
        "title": "云织羽绒服 Y-D01 黑色 L 码",
        "status": "active",
        "sale_price": "599.00",
        "attributes": {
            "brand": "云织",
            "category": "服饰",
            "model": "Y-D01",
            "color": "黑色",
            "size": "L",
            "material": "90%白鸭绒",
            "season": "冬季",
            "warranty_months": 0,
        },
        "source": "manual",
    },
]

# 新增品类的政策（对齐清洗脚本的 policy_type 契约）
# 注意：RETURN-APPAREL-7（服饰退换）已由网络源 S9 覆盖，不再重复定义
NEW_POLICIES = [
    {
        "policy_code": "WARR-DIGITAL-1Y",
        "policy_type": "warranty",
        "policy_name": "数码产品整机保修1年",
        "content": (
            "蓝牙耳机、充电宝等数码产品整机保修1年；内置电池属产品组成部分，保修期内非人为损坏可免费维修。"
            "消耗品（耳塞、充电线等）不属于保修范围。"
        ),
        "scope": "Category",
        "scope_key": "digital",
        "risk_level": "low",
        "source": "manual",
        "source_url": "",
    },
]

# 新增品类的 FAQ（对齐 Q6 验证：每品类 ≥2 条）
NEW_FAQS = [
    {
        "category": "商品保修",
        "intent": "product",
        "question": "星脉蓝牙耳机保修多久？",
        "answer": "星脉 X-M1 蓝牙耳机整机保修 12 个月，内置电池属产品组成部分，保修期内非人为损坏可免费维修。",
        "keywords": "星脉 耳机 保修",
        "risk_level": "low",
        "layer": "product",
        "sku_id": "X-M1-WHITE",
        "source": "manual",
    },
    {
        "category": "售后规则",
        "intent": "refund",
        "question": "数码产品激活后还能退货吗？",
        "answer": "数码类商品已激活、含授权信息的产品，激活后仅支持 7 天内无理由退货；激活前可支持退货。",
        "keywords": "数码 激活 退货",
        "risk_level": "medium",
        "layer": "store",
        "sku_id": "",
        "source": "manual",
    },
    {
        "category": "售后规则",
        "intent": "refund",
        "question": "羽绒服吊牌剪了还能退吗？",
        "answer": "服饰类退换需保持吊牌、包装完整；吊牌已剪或丢失，可能影响二次销售，不支持无理由退换。",
        "keywords": "羽绒服 吊牌 退换",
        "risk_level": "medium",
        "layer": "store",
        "sku_id": "Y-D01-M",
        "source": "manual",
    },
    {
        "category": "商品",
        "intent": "product",
        "question": "云织羽绒服充绒量是多少？",
        "answer": "云织 Y-D01 羽绒服填充 90% 白鸭绒，具体克重请以商品详情页标注为准。",
        "keywords": "云织 羽绒服 充绒",
        "risk_level": "low",
        "layer": "product",
        "sku_id": "Y-D01-M",
        "source": "manual",
    },
]


def main() -> None:
    RAW_MANUAL.mkdir(parents=True, exist_ok=True)
    (RAW_MANUAL / "new_catalog.json").write_text(
        json.dumps(NEW_CATALOG, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (RAW_MANUAL / "new_policies.json").write_text(
        json.dumps(NEW_POLICIES, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (RAW_MANUAL / "new_faqs.json").write_text(
        json.dumps(NEW_FAQS, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(
        {
            "new_catalog": len(NEW_CATALOG),
            "new_policies": len(NEW_POLICIES),
            "new_faqs": len(NEW_FAQS),
            "categories": sorted({c["attributes"]["category"] for c in NEW_CATALOG}),
        },
        ensure_ascii=False, indent=2,
    ))


if __name__ == "__main__":
    main()
