"""Product/SKU advisor (F-106): entity recognition, comparison, evidence ids.

Questions are matched against the tenant/store catalog; candidates enter the
context bundle with stable versioned evidence ids, and comparison questions
get an attribute-by-attribute diff across the matched SKUs.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ecommerce_agent.business.catalog import CatalogItemUpsert, CatalogService
from ecommerce_agent.product_advisor import advise, compare_products, recognize_products
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


def _seed_catalog(service: AgentService) -> None:
    catalog = CatalogService(service.db)
    source_time = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    for sku, title, price, attributes in (
        (
            "sku-earbuds-a",
            "云湃蓝牙耳机 A1 半入耳",
            "199.00",
            {"续航": "6 小时", "降噪": "无", "颜色": "白色"},
        ),
        (
            "sku-earbuds-b",
            "云湃蓝牙耳机 B2 主动降噪",
            "399.00",
            {"续航": "8 小时", "降噪": "主动降噪", "颜色": "白色"},
        ),
        (
            "sku-bottle-1",
            "云湃保温杯 500ml",
            "89.00",
            {"容量": "500ml"},
        ),
    ):
        catalog.upsert(
            "tenant-test",
            CatalogItemUpsert(
                connector_id="virtual_taobao",
                store_id="shop-advisor-1",
                item_id=f"item-{sku}",
                sku_id=sku,
                title=title,
                status="active",
                sale_price=price,
                currency="CNY",
                attributes=attributes,
                source_updated_at=source_time,
            ),
        )


def test_recognizes_products_with_stable_versioned_evidence_ids(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        _seed_catalog(service)
        candidates = recognize_products(
            service.db,
            tenant_id="tenant-test",
            store_id="shop-advisor-1",
            question="蓝牙耳机的续航怎么样",
        )
        skus = [candidate["sku_id"] for candidate in candidates]
        assert set(skus) == {"sku-earbuds-a", "sku-earbuds-b"}
        for candidate in candidates:
            assert candidate["evidence_id"].startswith("catalog:")
            assert candidate["evidence_id"].endswith(f":v{candidate['version']}")
            assert candidate["matched_terms"]
        again = recognize_products(
            service.db,
            tenant_id="tenant-test",
            store_id="shop-advisor-1",
            question="蓝牙耳机的续航怎么样",
        )
        assert [item["evidence_id"] for item in again] == [
            item["evidence_id"] for item in candidates
        ]
        other_store = recognize_products(
            service.db,
            tenant_id="tenant-test",
            store_id="shop-other",
            question="蓝牙耳机的续航怎么样",
        )
        assert other_store == []
        other_tenant = recognize_products(
            service.db,
            tenant_id="tenant-other",
            store_id="shop-advisor-1",
            question="蓝牙耳机的续航怎么样",
        )
        assert other_tenant == []
    finally:
        service.close()


def test_comparison_diffs_attributes_only_when_asked(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        _seed_catalog(service)
        plain = advise(
            service.db,
            tenant_id="tenant-test",
            store_id="shop-advisor-1",
            question="蓝牙耳机续航多久",
        )
        assert plain["comparison"] is None
        assert len(plain["candidates"]) == 2

        compared = advise(
            service.db,
            tenant_id="tenant-test",
            store_id="shop-advisor-1",
            question="两款蓝牙耳机对比一下哪个好",
        )
        comparison = compared["comparison"]
        assert comparison is not None
        assert set(comparison["sku_ids"]) == {"sku-earbuds-a", "sku-earbuds-b"}
        assert set(comparison["differences"]) == {"续航", "降噪"}
        assert "颜色" not in comparison["differences"]
        assert comparison["price"]["sku-earbuds-b"] == "399.00 CNY"
        assert len(comparison["evidence_ids"]) == 2

        assert compare_products(plain["candidates"][:1]) is None
        no_store = advise(
            service.db,
            tenant_id="tenant-test",
            store_id=None,
            question="蓝牙耳机对比",
        )
        assert no_store == {"candidates": [], "comparison": None}
    finally:
        service.close()


def test_context_bundle_embeds_advisor_and_catalog_evidence(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        _seed_catalog(service)
        snapshot = service.contexts.build(
            tenant_id="tenant-test",
            session_id=service.db.resolve_session(
                tenant_id="tenant-test",
                client_id="client-test",
                external_session_id="advisor-session-1",
                subject_hash="subject-advisor",
            ),
            trace_id="trace-advisor-1",
            stage="decision",
            sequence=0,
            question="两款蓝牙耳机对比哪个降噪好",
            trusted_context={"shop_id": "shop-advisor-1", "platform": "taobao"},
            documents=[],
            sops=[],
            tool_catalog=[],
            history=[],
        )
        advisor = snapshot.bundle["product_advisor"]
        assert len(advisor["candidates"]) == 2
        assert advisor["comparison"] is not None
        catalog_evidence = [
            item for item in snapshot.evidence if item["type"] == "catalog_item"
        ]
        assert len(catalog_evidence) == 2
        for item in catalog_evidence:
            assert item["source_id"].startswith("catalog:")
            assert item["authority"] == "versioned_catalog_fact"
        assert snapshot.checksum
    finally:
        service.close()


def test_chat_context_snapshot_carries_product_candidates(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        _seed_catalog(service)
        principal = principal_for(service, "buyer-advisor-1")
        service.chat(
            principal,
            "advisor-chat-session",
            "云湃蓝牙耳机续航多久",
            {"shop_id": "shop-advisor-1"},
        )
        import json as jsonlib

        with service.db.connect() as conn:
            rows = conn.execute(
                "SELECT bundle_json FROM context_snapshots"
            ).fetchall()
        assert rows
        bundles = [jsonlib.loads(row["bundle_json"]) for row in rows]
        assert any(
            bundle.get("product_advisor", {}).get("candidates") for bundle in bundles
        )
        skus = {
            candidate["sku_id"]
            for bundle in bundles
            for candidate in bundle.get("product_advisor", {}).get("candidates", [])
        }
        assert "sku-earbuds-a" in skus or "sku-earbuds-b" in skus
    finally:
        service.close()
