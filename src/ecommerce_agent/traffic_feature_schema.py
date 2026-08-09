"""Single source for versioned Traffic Lab statistical feature definitions."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


TITLE_EXTRACTOR_V1 = "deterministic-title-v1"
TITLE_EXTRACTOR_V2 = "deterministic-title-v2"
IMAGE_EXTRACTOR_V1 = "deterministic-png-v1"
IMAGE_EXTRACTOR_V2 = "deterministic-png-v2"


@dataclass(frozen=True)
class TitleFeatureSchema:
    extractor_version: str
    feature_names: tuple[str, ...]
    wordlists: Mapping[str, tuple[str, ...]]
    semantic_label_names: tuple[str, ...]
    parameters: Mapping[str, int | float]


@dataclass(frozen=True)
class ImageFeatureSchema:
    extractor_version: str
    feature_names: tuple[str, ...]
    semantic_label_names: tuple[str, ...]
    parameters: Mapping[str, int | float]


@dataclass(frozen=True)
class TrafficFeatureSchema:
    version: str
    title: TitleFeatureSchema
    image: ImageFeatureSchema


_FEATURE_SCHEMA_V1 = TrafficFeatureSchema(
    version="image-v1",
    title=TitleFeatureSchema(
        extractor_version=TITLE_EXTRACTOR_V1,
        feature_names=(
            "title_length",
            "brand_present",
            "category_keyword_present",
            "brand_position",
            "category_keyword_position",
            "numeric_token_count",
            "benefit_keyword_count",
            "scenario_keyword_count",
            "promotion_keyword_count",
            "duplicate_term_ratio",
            "first_10_chars_information_density",
        ),
        wordlists=MappingProxyType(
            {
                "benefit": (
                    "大容量",
                    "便携",
                    "防水",
                    "高清",
                    "静音",
                    "节能",
                    "耐用",
                    "强劲",
                    "轻薄",
                    "快速",
                    "远距送风",
                ),
                "scenario": (
                    "办公",
                    "车载",
                    "户外",
                    "家用",
                    "客厅",
                    "厨房",
                    "旅行",
                    "通勤",
                    "卧室",
                    "宿舍",
                ),
                "promotion": (
                    "包邮",
                    "买一送一",
                    "满减",
                    "秒杀",
                    "特价",
                    "限时",
                    "优惠",
                    "折扣",
                    "赠品",
                    "直降",
                ),
            }
        ),
        semantic_label_names=(
            "benefit_summary",
            "scenario_summary",
            "title_structure",
        ),
        parameters=MappingProxyType({}),
    ),
    image=ImageFeatureSchema(
        extractor_version=IMAGE_EXTRACTOR_V1,
        feature_names=(
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
        ),
        semantic_label_names=(
            "background_style",
            "benefit_prominence",
            "crowdedness",
            "person_presence",
            "scene",
            "visual_style",
        ),
        parameters=MappingProxyType(
            {
                "background_distance_threshold": 32,
                "edge_threshold": 48,
                "max_image_bytes": 64 * 1024 * 1024,
                "max_image_pixels": 16_000_000,
                "max_sample_pixels": 262_144,
                "text_block_size": 8,
                "text_min_contrast": 64,
                "text_min_edge_density": 0.12,
                "whitespace_max_chroma": 10,
                "whitespace_min_luma": 245,
            }
        ),
    ),
)

_FEATURE_SCHEMA_V2 = TrafficFeatureSchema(
    version="image-v2",
    title=TitleFeatureSchema(
        extractor_version=TITLE_EXTRACTOR_V2,
        feature_names=_FEATURE_SCHEMA_V1.title.feature_names,
        wordlists=_FEATURE_SCHEMA_V1.title.wordlists,
        semantic_label_names=_FEATURE_SCHEMA_V1.title.semantic_label_names,
        parameters=MappingProxyType({"duplicate_ngram_size": 2}),
    ),
    image=ImageFeatureSchema(
        extractor_version=IMAGE_EXTRACTOR_V2,
        feature_names=_FEATURE_SCHEMA_V1.image.feature_names,
        semantic_label_names=_FEATURE_SCHEMA_V1.image.semantic_label_names,
        parameters=_FEATURE_SCHEMA_V1.image.parameters,
    ),
)

_FEATURE_SCHEMAS: Mapping[str, TrafficFeatureSchema] = MappingProxyType(
    {
        _FEATURE_SCHEMA_V1.version: _FEATURE_SCHEMA_V1,
        _FEATURE_SCHEMA_V2.version: _FEATURE_SCHEMA_V2,
    }
)

CURRENT_FEATURE_SCHEMA_VERSION = _FEATURE_SCHEMA_V2.version


def get_feature_schema(version: str) -> TrafficFeatureSchema:
    try:
        return _FEATURE_SCHEMAS[version]
    except KeyError as exc:
        raise ValueError("unsupported_feature_schema_version") from exc
