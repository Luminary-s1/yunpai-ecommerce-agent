from __future__ import annotations

import json
import math
import re
from typing import Any

from .database import Database
from .text_utils import search_terms

_COMPARISON_HINTS = re.compile(r"对比|区别|哪个好|哪款|差别|比较|不同")


def recognize_products(
    db: Database,
    *,
    tenant_id: str,
    store_id: str,
    question: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Match the question against active catalog items of the tenant's store.

    Returns ranked candidates with stable evidence ids so every product claim
    downstream can cite the exact catalog version it came from.
    """
    question_terms = set(search_terms(question))
    if not question_terms:
        return []
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, item_id, sku_id, title, sale_price, currency,
                   attributes_json, version, source_updated_at
            FROM catalog_items
            WHERE tenant_id=? AND store_id=? AND status='active'
            """,
            (tenant_id, store_id),
        ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        try:
            attributes = json.loads(str(row["attributes_json"] or "{}"))
        except ValueError:
            attributes = {}
        haystack = " ".join(
            [str(row["title"]), str(row["sku_id"])]
            + [f"{key} {value}" for key, value in attributes.items()]
        )
        item_terms = set(search_terms(haystack))
        overlap = question_terms & item_terms
        if not overlap:
            continue
        score = len(overlap) / max(1.0, math.sqrt(len(item_terms)))
        candidates.append(
            {
                "evidence_id": f"catalog:{row['id']}:v{row['version']}",
                "catalog_row_id": str(row["id"]),
                "item_id": str(row["item_id"]),
                "sku_id": str(row["sku_id"]),
                "title": str(row["title"]),
                "sale_price": str(row["sale_price"]),
                "currency": str(row["currency"]),
                "attributes": attributes,
                "version": int(row["version"]),
                "source_updated_at": str(row["source_updated_at"]),
                "score": round(score, 4),
                "matched_terms": sorted(overlap),
            }
        )
    candidates.sort(key=lambda item: (item["score"], item["version"]), reverse=True)
    return candidates[: max(1, limit)]


def compare_products(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Attribute-by-attribute comparison across matched SKUs."""
    if len(candidates) < 2:
        return None
    keys: list[str] = []
    for candidate in candidates:
        for key in candidate["attributes"]:
            if key not in keys:
                keys.append(key)
    attribute_rows = []
    differences = []
    for key in keys:
        values = {
            candidate["sku_id"]: candidate["attributes"].get(key)
            for candidate in candidates
        }
        attribute_rows.append({"attribute": key, "values": values})
        if len({json.dumps(value, ensure_ascii=False) for value in values.values()}) > 1:
            differences.append(key)
    return {
        "sku_ids": [candidate["sku_id"] for candidate in candidates],
        "evidence_ids": [candidate["evidence_id"] for candidate in candidates],
        "price": {
            candidate["sku_id"]: f"{candidate['sale_price']} {candidate['currency']}"
            for candidate in candidates
        },
        "attributes": attribute_rows,
        "differences": differences,
    }


def wants_comparison(question: str) -> bool:
    return bool(_COMPARISON_HINTS.search(question))


def advise(
    db: Database,
    *,
    tenant_id: str,
    store_id: str | None,
    question: str,
) -> dict[str, Any]:
    """The product advisor section a context bundle embeds."""
    if not store_id:
        return {"candidates": [], "comparison": None}
    candidates = recognize_products(
        db, tenant_id=tenant_id, store_id=store_id, question=question
    )
    comparison = (
        compare_products(candidates)
        if len(candidates) >= 2 and wants_comparison(question)
        else None
    )
    return {"candidates": candidates, "comparison": comparison}
