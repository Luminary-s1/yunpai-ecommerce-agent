from __future__ import annotations

import binascii
import hashlib
import struct
import zlib
from datetime import UTC, datetime, timedelta

import pytest

from ecommerce_agent.connectors import PullRequest, VirtualTaobaoConnector
from ecommerce_agent.database import Database
from ecommerce_agent.traffic_lab import (
    CURRENT_FEATURE_SCHEMA_VERSION,
    CreativeAssetCreate,
    ListingRevisionCreate,
    SemanticFeatureOutput,
    SemanticFeatureRequest,
    SemanticFeatureUnavailable,
    TitleFeatureContext,
    TrafficFeatureEngine,
    TrafficLabService,
    get_feature_schema,
)


BASE_TIME = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(kind)
    checksum = binascii.crc32(payload, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def _rgb_png(rows: list[list[tuple[int, int, int]]]) -> bytes:
    height = len(rows)
    width = len(rows[0])
    assert height > 0 and width > 0
    assert all(len(row) == width for row in rows)
    raw = b"".join(
        b"\x00" + bytes(channel for pixel in row for channel in pixel) for row in rows
    )
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(
                b"IHDR",
                struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
            ),
            _png_chunk(b"IDAT", zlib.compress(raw)),
            _png_chunk(b"IEND", b""),
        )
    )


def _grayscale_png(rows: list[bytes]) -> bytes:
    height = len(rows)
    width = len(rows[0])
    assert height > 0 and width > 0
    assert all(len(row) == width for row in rows)
    raw = b"".join(b"\x00" + row for row in rows)
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(
                b"IHDR",
                struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0),
            ),
            _png_chunk(b"IDAT", zlib.compress(raw)),
            _png_chunk(b"IEND", b""),
        )
    )


def _register_revision(
    service: TrafficLabService,
    *,
    tenant_id: str = "tenant-a",
    image_bytes: bytes | None = None,
    width: int = 8,
    height: int = 8,
    title: str = "云湃 静音 静音 家用 限时 2代 空气循环扇",
    feature_schema_version: str = CURRENT_FEATURE_SCHEMA_VERSION,
) -> tuple[dict[str, object], dict[str, object]]:
    digest = hashlib.sha256(image_bytes).hexdigest() if image_bytes is not None else "a" * 64
    asset = service.register_asset(
        tenant_id,
        CreativeAssetCreate(
            sha256=digest,
            mime_type="image/png",
            width=width,
            height=height,
            storage_ref=f"objects/traffic-lab/{digest}.png",
            source_ref="fixture://wp3/asset",
            feature_schema_version=feature_schema_version,
        ),
    )
    revision = service.create_revision(
        tenant_id,
        ListingRevisionCreate(
            connector_id="virtual_taobao",
            store_id="store-001",
            item_id="item-001",
            sku_id="sku-001",
            revision_no=1,
            title=title,
            main_image_asset_id=str(asset["asset_id"]),
            sale_price="109.00",
            attributes={"stock_status": "in_stock"},
            active_from=BASE_TIME,
            active_to=BASE_TIME + timedelta(hours=4),
            source_updated_at=BASE_TIME,
        ),
    )
    return asset, revision


class _FixedSemanticExtractor:
    def __init__(self) -> None:
        self.requests: list[SemanticFeatureRequest] = []

    def extract(self, request: SemanticFeatureRequest) -> SemanticFeatureOutput:
        self.requests.append(request)
        labels = (
            {"title_structure": "brand_benefit_scenario"}
            if request.target_type == "title"
            else {"background_style": "white", "visual_style": "minimal"}
        )
        return SemanticFeatureOutput(
            extractor_version="semantic-fixture-v1",
            model_provider="fixture",
            model_name="fixed-table",
            prompt_version="traffic-semantic-prompt-v1",
            labels=labels,
        )


class _UnavailableSemanticExtractor:
    def extract(self, request: SemanticFeatureRequest) -> SemanticFeatureOutput:
        raise SemanticFeatureUnavailable("model_disabled")


class _BrokenSemanticExtractor:
    def extract(self, request: SemanticFeatureRequest) -> SemanticFeatureOutput:
        raise RuntimeError("fixture transport failure")


def test_feature_schema_is_the_single_versioned_source_for_lists_and_virtual_assets() -> None:
    schema = get_feature_schema(CURRENT_FEATURE_SCHEMA_VERSION)

    assert {
        "title_length",
        "benefit_keyword_count",
        "promotion_keyword_count",
        "duplicate_term_ratio",
        "first_10_chars_information_density",
    } <= set(schema.title.feature_names)
    assert {
        "width",
        "height",
        "aspect_ratio",
        "file_size_bytes",
        "brightness",
        "contrast",
        "sharpness",
        "edge_density",
        "text_area_ratio",
        "subject_area_ratio",
        "whitespace_ratio",
    } <= set(schema.image.feature_names)
    assert "静音" in schema.title.wordlists["benefit"]
    assert "限时" in schema.title.wordlists["promotion"]

    connector = VirtualTaobaoConnector()
    batch = connector.pull(PullRequest(resource="listing_revision", limit=10))
    assert {
        record.payload["asset"]["feature_schema_version"] for record in batch.records
    } == {CURRENT_FEATURE_SCHEMA_VERSION}

    with pytest.raises(ValueError, match="unsupported_feature_schema_version"):
        get_feature_schema("unknown-feature-schema")
    with pytest.raises(ValueError, match="unsupported_feature_schema_version"):
        CreativeAssetCreate(
            sha256="a" * 64,
            mime_type="image/png",
            width=8,
            height=8,
            storage_ref="objects/traffic-lab/unknown.png",
            feature_schema_version="unknown-feature-schema",
        )


def test_title_features_are_revision_bound_reproducible_statistics(tmp_path) -> None:
    db = Database(tmp_path / "wp3-title.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    _asset, revision = _register_revision(service)
    engine = TrafficFeatureEngine(service)
    context = TitleFeatureContext(brand="云湃", category_keywords=("循环扇", "空气循环扇"))

    first = engine.extract_title(
        "tenant-a", str(revision["id"]), context=context
    )
    second = engine.extract_title(
        "tenant-a", str(revision["id"]), context=context
    )

    assert first == second
    assert first["target"] == {"type": "title", "id": revision["id"]}
    assert first["feature_schema_version"] == CURRENT_FEATURE_SCHEMA_VERSION
    assert first["status"] == "degraded"
    assert first["semantic"]["reason_code"] == "semantic_extractor_unavailable"
    assert first["deterministic"]["authority"] == "statistical_feature"
    features = first["deterministic"]["features"]
    assert features["brand_present"] is True
    assert features["brand_position"] == 0
    assert features["category_keyword_present"] is True
    assert features["category_keyword_position"] > 0
    assert features["numeric_token_count"] == 1
    assert features["benefit_keyword_count"] == 2
    assert features["scenario_keyword_count"] == 1
    assert features["promotion_keyword_count"] == 1
    assert features["duplicate_term_ratio"] > 0
    assert 0 < features["first_10_chars_information_density"] <= 1
    stored = service.get_revision("tenant-a", str(revision["id"]))
    assert stored["payload_hash"] == revision["payload_hash"]
    assert stored["title"] == revision["title"]


def test_v2_title_statistics_detect_unspaced_cjk_repetition(tmp_path) -> None:
    db = Database(tmp_path / "wp3-title-cjk.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    _asset, revision = _register_revision(
        service,
        title="静音静音循环扇",
    )

    engine = TrafficFeatureEngine(service)
    legacy = engine.extract_title(
        "tenant-a",
        str(revision["id"]),
        feature_schema_version="image-v1",
    )
    result = engine.extract_title(
        "tenant-a",
        str(revision["id"]),
        feature_schema_version="image-v2",
    )
    features = result["deterministic"]["features"]

    assert legacy["deterministic"]["features"]["duplicate_term_ratio"] == 0.0
    assert features["duplicate_term_ratio"] == pytest.approx(1 / 6, abs=1e-6)
    assert features["first_10_chars_information_density"] == pytest.approx(
        5 / 7, abs=1e-6
    )


def test_keyword_statistics_do_not_bypass_or_control_optional_semantic_extraction(
    tmp_path,
) -> None:
    db = Database(tmp_path / "wp3-semantic.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    _asset, revision = _register_revision(service)
    semantic = _FixedSemanticExtractor()
    engine = TrafficFeatureEngine(service, semantic_extractor=semantic)

    complete = engine.extract_title(
        "tenant-a",
        str(revision["id"]),
        context=TitleFeatureContext(
            brand="云湃", category_keywords=("空气循环扇",)
        ),
    )

    assert complete["deterministic"]["features"]["promotion_keyword_count"] == 1
    assert len(semantic.requests) == 1
    assert semantic.requests[0].target_id == revision["id"]
    assert complete["status"] == "complete"
    assert complete["semantic"] == {
        "authority": "advisory_signal",
        "status": "available",
        "reason_code": None,
        "extractor_version": "semantic-fixture-v1",
        "model_provider": "fixture",
        "model_name": "fixed-table",
        "prompt_version": "traffic-semantic-prompt-v1",
        "labels": {"title_structure": "brand_benefit_scenario"},
    }

    degraded = TrafficFeatureEngine(
        service, semantic_extractor=_UnavailableSemanticExtractor()
    ).extract_title("tenant-a", str(revision["id"]))
    baseline = TrafficFeatureEngine(service).extract_title(
        "tenant-a", str(revision["id"])
    )
    assert degraded["deterministic"] == baseline["deterministic"]
    assert degraded["status"] == "degraded"
    assert degraded["semantic"]["reason_code"] == "model_disabled"

    failed = TrafficFeatureEngine(
        service, semantic_extractor=_BrokenSemanticExtractor()
    ).extract_title("tenant-a", str(revision["id"]))
    assert failed["deterministic"] == baseline["deterministic"]
    assert failed["semantic"]["reason_code"] == "semantic_extractor_failed"


def test_png_features_are_asset_bound_reproducible_and_dependency_free(tmp_path) -> None:
    image_bytes = _rgb_png([[(255, 255, 255)] * 8 for _ in range(8)])
    db = Database(tmp_path / "wp3-image.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    asset, _revision = _register_revision(service, image_bytes=image_bytes)
    engine = TrafficFeatureEngine(service)

    first = engine.extract_image("tenant-a", str(asset["asset_id"]), image_bytes)
    second = engine.extract_image("tenant-a", str(asset["asset_id"]), image_bytes)

    assert first == second
    assert first["target"] == {"type": "image", "id": asset["asset_id"]}
    features = first["deterministic"]["features"]
    assert features["width"] == 8
    assert features["height"] == 8
    assert features["aspect_ratio"] == 1.0
    assert features["file_size_bytes"] == len(image_bytes)
    assert features["brightness"] == 1.0
    assert features["contrast"] == 0.0
    assert features["sharpness"] == 0.0
    assert features["edge_density"] == 0.0
    assert features["text_area_ratio"] == 0.0
    assert features["subject_area_ratio"] == 0.0
    assert features["whitespace_ratio"] == 1.0

    with pytest.raises(ValueError, match="image_sha256_mismatch"):
        engine.extract_image(
            "tenant-a", str(asset["asset_id"]), image_bytes + b"tampered"
        )


def test_png_pixel_statistics_detect_contrast_edges_subject_and_whitespace(tmp_path) -> None:
    rows = [
        [
            (0, 0, 0)
            if 4 <= x < 12 and 4 <= y < 12 and (x + y) % 2 == 0
            else (255, 255, 255)
            for x in range(16)
        ]
        for y in range(16)
    ]
    image_bytes = _rgb_png(rows)
    db = Database(tmp_path / "wp3-image-statistics.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    asset, _revision = _register_revision(
        service,
        image_bytes=image_bytes,
        width=16,
        height=16,
    )

    result = TrafficFeatureEngine(service).extract_image(
        "tenant-a", str(asset["asset_id"]), image_bytes
    )
    features = result["deterministic"]["features"]

    assert 0 < features["brightness"] < 1
    assert features["contrast"] > 0
    assert features["sharpness"] > 0
    assert features["edge_density"] > 0
    assert features["text_area_ratio"] > 0
    assert features["subject_area_ratio"] == 0.125
    assert features["whitespace_ratio"] == 0.875


def test_v2_large_image_statistics_do_not_alias_fixed_stride_patterns(tmp_path) -> None:
    width = 1024
    height = 1024
    rows = [
        bytes(255 if (x + y) % 2 else 0 for x in range(width))
        for y in range(height)
    ]
    image_bytes = _grayscale_png(rows)
    db = Database(tmp_path / "wp3-image-alias.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    asset, _revision = _register_revision(
        service,
        image_bytes=image_bytes,
        width=width,
        height=height,
    )

    engine = TrafficFeatureEngine(service)
    legacy = engine.extract_image(
        "tenant-a",
        str(asset["asset_id"]),
        image_bytes,
        feature_schema_version="image-v1",
    )
    result = engine.extract_image(
        "tenant-a", str(asset["asset_id"]), image_bytes
    )
    features = result["deterministic"]["features"]

    assert legacy["deterministic"]["features"]["brightness"] == 0.0
    assert legacy["deterministic"]["features"]["edge_density"] == 0.0
    assert features["brightness"] == pytest.approx(0.5, abs=1e-6)
    assert features["contrast"] == pytest.approx(0.5, abs=1e-6)
    assert features["sharpness"] == pytest.approx(1.0, abs=1e-6)
    assert features["edge_density"] == pytest.approx(1.0, abs=1e-6)
    assert features["whitespace_ratio"] == pytest.approx(0.5, abs=1e-6)


def test_v2_edge_density_counts_adjacent_checkerboard_edges(tmp_path) -> None:
    rows = [
        bytes(255 if (x + y) % 2 else 0 for x in range(16))
        for y in range(16)
    ]
    image_bytes = _grayscale_png(rows)
    db = Database(tmp_path / "wp3-image-edges.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    asset, _revision = _register_revision(
        service,
        image_bytes=image_bytes,
        width=16,
        height=16,
    )

    result = TrafficFeatureEngine(service).extract_image(
        "tenant-a", str(asset["asset_id"]), image_bytes
    )

    assert result["deterministic"]["features"]["edge_density"] == 1.0


def test_image_schema_upgrade_reextracts_same_asset_without_mutating_it(tmp_path) -> None:
    image_bytes = _rgb_png([[(255, 255, 255)] * 8 for _ in range(8)])
    db = Database(tmp_path / "wp3-image-upgrade.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    asset, _revision = _register_revision(
        service,
        image_bytes=image_bytes,
        feature_schema_version="image-v1",
    )
    engine = TrafficFeatureEngine(service)

    original = engine.extract_image(
        "tenant-a",
        str(asset["asset_id"]),
        image_bytes,
        feature_schema_version="image-v1",
    )
    upgraded = engine.extract_image(
        "tenant-a",
        str(asset["asset_id"]),
        image_bytes,
        feature_schema_version="image-v2",
    )
    original_again = engine.extract_image(
        "tenant-a",
        str(asset["asset_id"]),
        image_bytes,
    )

    assert original_again == original
    assert original["feature_schema_version"] == "image-v1"
    assert original["deterministic"]["extractor_version"] == "deterministic-png-v1"
    assert upgraded["feature_schema_version"] == "image-v2"
    assert upgraded["deterministic"]["extractor_version"] == "deterministic-png-v2"
    assert original["deterministic"]["input_sha256"] == upgraded["deterministic"][
        "input_sha256"
    ]
    assert original["deterministic"]["output_sha256"] != upgraded["deterministic"][
        "output_sha256"
    ]
    assert service.get_asset("tenant-a", str(asset["asset_id"]))[
        "feature_schema_version"
    ] == "image-v1"


def test_image_feature_contract_rejects_dimension_and_tenant_mismatches(tmp_path) -> None:
    image_bytes = _rgb_png([[(20, 20, 20)] * 4 for _ in range(4)])
    db = Database(tmp_path / "wp3-image-guard.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    asset, _revision = _register_revision(
        service,
        image_bytes=image_bytes,
        width=5,
        height=4,
    )
    engine = TrafficFeatureEngine(service)

    with pytest.raises(ValueError, match="image_dimensions_mismatch"):
        engine.extract_image("tenant-a", str(asset["asset_id"]), image_bytes)
    with pytest.raises(ValueError, match="creative_asset_not_found"):
        engine.extract_image("tenant-b", str(asset["asset_id"]), image_bytes)
