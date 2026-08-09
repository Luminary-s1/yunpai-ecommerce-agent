"""Read-only statistical extractors; outputs carry no routing or experiment authority."""

from __future__ import annotations

import binascii
import hashlib
import hmac
import json
import math
import re
import struct
import unicodedata
import zlib
from dataclasses import dataclass
from statistics import median
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..business.source_versioning import payload_digest
from ..traffic_feature_schema import (
    CURRENT_FEATURE_SCHEMA_VERSION,
    IMAGE_EXTRACTOR_V1,
    IMAGE_EXTRACTOR_V2,
    TITLE_EXTRACTOR_V1,
    TITLE_EXTRACTOR_V2,
    TrafficFeatureSchema,
    get_feature_schema,
)
from .service import TrafficLabError, TrafficLabService


FeatureTargetType = Literal["title", "image"]
FeatureScalar = bool | int | float | str
_NUMERIC_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*")
_TITLE_TOKEN_RE = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class TitleFeatureContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    brand: str | None = Field(default=None, max_length=256)
    category_keywords: tuple[str, ...] = ()

    @field_validator("brand")
    @classmethod
    def normalize_brand(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("category_keywords")
    @classmethod
    def normalize_category_keywords(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 100:
            raise ValueError("too_many_category_keywords")
        normalized = tuple(item.strip() for item in value if item.strip())
        if any(len(item) > 256 for item in normalized):
            raise ValueError("category_keyword_too_long")
        return tuple(dict.fromkeys(normalized))


class SemanticFeatureOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    extractor_version: str = Field(min_length=1, max_length=128)
    model_provider: str = Field(min_length=1, max_length=128)
    model_name: str = Field(min_length=1, max_length=256)
    prompt_version: str = Field(min_length=1, max_length=128)
    labels: dict[str, Any]


@dataclass(frozen=True)
class SemanticFeatureRequest:
    target_type: FeatureTargetType
    target_id: str
    feature_schema_version: str
    deterministic_features: Mapping[str, FeatureScalar]
    title: str | None = None
    image_bytes: bytes | None = None
    mime_type: str | None = None


class SemanticFeatureExtractor(Protocol):
    def extract(self, request: SemanticFeatureRequest) -> SemanticFeatureOutput: ...


class SemanticFeatureUnavailable(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _ImageStatistics:
    pixel_count: int
    luminance_sum: float
    luminance_square_sum: float
    whitespace_count: int
    sharpness: float
    edge_density: float


@dataclass(frozen=True)
class _DecodedImage:
    width: int
    height: int
    sample_width: int
    sample_height: int
    pixels: tuple[tuple[int, int, int], ...]
    statistics: _ImageStatistics | None = None


class TrafficFeatureEngine:
    def __init__(
        self,
        traffic_lab: TrafficLabService,
        *,
        semantic_extractor: SemanticFeatureExtractor | None = None,
    ) -> None:
        self.traffic_lab = traffic_lab
        self.semantic_extractor = semantic_extractor

    def extract_title(
        self,
        tenant_id: str,
        revision_id: str,
        *,
        context: TitleFeatureContext | None = None,
        feature_schema_version: str = CURRENT_FEATURE_SCHEMA_VERSION,
    ) -> dict[str, Any]:
        revision = self.traffic_lab.get_revision(tenant_id, revision_id)
        schema = get_feature_schema(feature_schema_version)
        resolved_context = context or TitleFeatureContext()
        title = _normalize_text(str(revision["title"]))
        features = _extract_title_features(title, resolved_context, schema)
        _require_feature_keys(features, schema.title.feature_names)
        input_sha256 = payload_digest(
            {
                "revision_id": revision["id"],
                "revision_payload_hash": revision["payload_hash"],
                "context": resolved_context.model_dump(mode="json"),
            }
        )
        deterministic = _deterministic_view(
            feature_schema_version=schema.version,
            extractor_version=schema.title.extractor_version,
            input_sha256=input_sha256,
            features=features,
        )
        semantic = self._extract_semantic(
            SemanticFeatureRequest(
                target_type="title",
                target_id=revision_id,
                feature_schema_version=schema.version,
                deterministic_features=MappingProxyType(dict(features)),
                title=title,
            ),
            allowed_labels=schema.title.semantic_label_names,
        )
        return _feature_result(
            target_type="title",
            target_id=revision_id,
            feature_schema_version=schema.version,
            deterministic=deterministic,
            semantic=semantic,
        )

    def extract_image(
        self,
        tenant_id: str,
        asset_id: str,
        image_bytes: bytes,
        *,
        feature_schema_version: str | None = None,
    ) -> dict[str, Any]:
        asset = self.traffic_lab.get_asset(tenant_id, asset_id)
        asset_schema = get_feature_schema(str(asset["feature_schema_version"]))
        schema = (
            get_feature_schema(feature_schema_version)
            if feature_schema_version is not None
            else asset_schema
        )
        digest = hashlib.sha256(image_bytes).hexdigest()
        if not hmac.compare_digest(digest, str(asset["sha256"])):
            raise TrafficLabError("image_sha256_mismatch")
        if str(asset["mime_type"]).lower() != "image/png":
            raise TrafficLabError("image_mime_type_not_supported")
        decoded = _decode_png(image_bytes, schema)
        if (decoded.width, decoded.height) != (int(asset["width"]), int(asset["height"])):
            raise TrafficLabError("image_dimensions_mismatch")
        features = _extract_image_features(decoded, len(image_bytes), schema)
        _require_feature_keys(features, schema.image.feature_names)
        deterministic = _deterministic_view(
            feature_schema_version=schema.version,
            extractor_version=schema.image.extractor_version,
            input_sha256=digest,
            features=features,
        )
        semantic = self._extract_semantic(
            SemanticFeatureRequest(
                target_type="image",
                target_id=asset_id,
                feature_schema_version=schema.version,
                deterministic_features=MappingProxyType(dict(features)),
                image_bytes=image_bytes,
                mime_type=str(asset["mime_type"]),
            ),
            allowed_labels=schema.image.semantic_label_names,
        )
        return _feature_result(
            target_type="image",
            target_id=asset_id,
            feature_schema_version=schema.version,
            deterministic=deterministic,
            semantic=semantic,
        )

    def _extract_semantic(
        self,
        request: SemanticFeatureRequest,
        *,
        allowed_labels: tuple[str, ...],
    ) -> dict[str, Any]:
        if self.semantic_extractor is None:
            return _semantic_unavailable("semantic_extractor_unavailable")
        try:
            raw_output = self.semantic_extractor.extract(request)
        except SemanticFeatureUnavailable as exc:
            return _semantic_unavailable(exc.code)
        except Exception:
            return _semantic_unavailable("semantic_extractor_failed")
        try:
            output = SemanticFeatureOutput.model_validate(raw_output)
            unknown = set(output.labels) - set(allowed_labels)
            if unknown:
                raise ValueError("semantic_feature_labels_not_supported")
            json.dumps(output.labels, allow_nan=False, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return _semantic_unavailable("semantic_output_invalid")
        except Exception:
            return _semantic_unavailable("semantic_extractor_failed")
        return {
            "authority": "advisory_signal",
            "status": "available",
            "reason_code": None,
            "extractor_version": output.extractor_version,
            "model_provider": output.model_provider,
            "model_name": output.model_name,
            "prompt_version": output.prompt_version,
            "labels": dict(output.labels),
        }


def _feature_result(
    *,
    target_type: FeatureTargetType,
    target_id: str,
    feature_schema_version: str,
    deterministic: dict[str, Any],
    semantic: dict[str, Any],
) -> dict[str, Any]:
    return {
        "target": {"type": target_type, "id": target_id},
        "feature_schema_version": feature_schema_version,
        "status": "complete" if semantic["status"] == "available" else "degraded",
        "deterministic": deterministic,
        "semantic": semantic,
    }


def _deterministic_view(
    *,
    feature_schema_version: str,
    extractor_version: str,
    input_sha256: str,
    features: dict[str, FeatureScalar],
) -> dict[str, Any]:
    output_sha256 = payload_digest(
        {
            "feature_schema_version": feature_schema_version,
            "extractor_version": extractor_version,
            "features": features,
        }
    )
    return {
        "authority": "statistical_feature",
        "extractor_version": extractor_version,
        "input_sha256": input_sha256,
        "output_sha256": output_sha256,
        "features": features,
    }


def _semantic_unavailable(reason_code: str) -> dict[str, Any]:
    return {
        "authority": "advisory_signal",
        "status": "unavailable",
        "reason_code": reason_code,
        "extractor_version": None,
        "model_provider": None,
        "model_name": None,
        "prompt_version": None,
        "labels": {},
    }


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def _extract_title_features(
    title: str,
    context: TitleFeatureContext,
    schema: TrafficFeatureSchema,
) -> dict[str, FeatureScalar]:
    folded_title = title.casefold()
    brand = _normalize_text(context.brand).casefold() if context.brand else None
    categories = tuple(
        _normalize_text(keyword).casefold() for keyword in context.category_keywords
    )
    brand_position = folded_title.find(brand) if brand else -1
    category_positions = [
        folded_title.find(keyword)
        for keyword in categories
        if keyword and folded_title.find(keyword) >= 0
    ]
    category_position = min(category_positions, default=-1)
    first_ten = title[:10]
    if schema.title.extractor_version == TITLE_EXTRACTOR_V1:
        tokens = [token.casefold() for token in _TITLE_TOKEN_RE.findall(title)]
        duplicate_ratio = (
            (len(tokens) - len(set(tokens))) / len(tokens) if tokens else 0.0
        )
        information_density = (
            sum(character.isalnum() for character in first_ten) / len(first_ten)
            if first_ten
            else 0.0
        )
    elif schema.title.extractor_version == TITLE_EXTRACTOR_V2:
        duplicate_ratio = _character_ngram_duplicate_ratio(
            title,
            size=int(schema.title.parameters["duplicate_ngram_size"]),
        )
        informative = [
            character.casefold() for character in first_ten if character.isalnum()
        ]
        information_density = (
            len(set(informative)) / len(first_ten) if first_ten else 0.0
        )
    else:
        raise AssertionError("title_extractor_version_not_implemented")
    return {
        "title_length": len(title),
        "brand_present": brand_position >= 0,
        "category_keyword_present": category_position >= 0,
        "brand_position": brand_position,
        "category_keyword_position": category_position,
        "numeric_token_count": len(_NUMERIC_TOKEN_RE.findall(title)),
        "benefit_keyword_count": _keyword_count(
            folded_title, schema.title.wordlists["benefit"]
        ),
        "scenario_keyword_count": _keyword_count(
            folded_title, schema.title.wordlists["scenario"]
        ),
        "promotion_keyword_count": _keyword_count(
            folded_title, schema.title.wordlists["promotion"]
        ),
        "duplicate_term_ratio": round(duplicate_ratio, 6),
        "first_10_chars_information_density": round(information_density, 6),
    }


def _keyword_count(title: str, terms: tuple[str, ...]) -> int:
    return sum(title.count(_normalize_text(term).casefold()) for term in terms)


def _character_ngram_duplicate_ratio(title: str, *, size: int) -> float:
    compact = "".join(character.casefold() for character in title if character.isalnum())
    if len(compact) < size:
        return 0.0
    units = [compact[index : index + size] for index in range(len(compact) - size + 1)]
    return (len(units) - len(set(units))) / len(units)


def _require_feature_keys(
    features: Mapping[str, FeatureScalar], expected: tuple[str, ...]
) -> None:
    if set(features) != set(expected):
        raise AssertionError("feature_schema_and_extractor_keys_differ")


def _decode_png(image_bytes: bytes, schema: TrafficFeatureSchema) -> _DecodedImage:
    parameters = schema.image.parameters
    if len(image_bytes) > int(parameters["max_image_bytes"]):
        raise TrafficLabError("image_file_too_large")
    if not image_bytes.startswith(_PNG_SIGNATURE):
        raise TrafficLabError("image_png_invalid")

    offset = len(_PNG_SIGNATURE)
    ihdr: bytes | None = None
    palette: bytes | None = None
    transparency: bytes | None = None
    compressed_parts: list[bytes] = []
    saw_iend = False
    chunk_index = 0
    while offset < len(image_bytes):
        if offset + 12 > len(image_bytes):
            raise TrafficLabError("image_png_invalid")
        length = struct.unpack(">I", image_bytes[offset : offset + 4])[0]
        kind = image_bytes[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(image_bytes):
            raise TrafficLabError("image_png_invalid")
        payload = image_bytes[offset + 8 : offset + 8 + length]
        supplied_crc = struct.unpack(">I", image_bytes[offset + 8 + length : chunk_end])[0]
        expected_crc = binascii.crc32(kind)
        expected_crc = binascii.crc32(payload, expected_crc) & 0xFFFFFFFF
        if supplied_crc != expected_crc:
            raise TrafficLabError("image_png_crc_invalid")
        if chunk_index == 0 and kind != b"IHDR":
            raise TrafficLabError("image_png_invalid")
        if kind == b"IHDR":
            if ihdr is not None or len(payload) != 13:
                raise TrafficLabError("image_png_invalid")
            ihdr = payload
        elif kind == b"PLTE":
            palette = payload
        elif kind == b"tRNS":
            transparency = payload
        elif kind == b"IDAT":
            compressed_parts.append(payload)
        elif kind == b"IEND":
            if payload:
                raise TrafficLabError("image_png_invalid")
            saw_iend = True
            offset = chunk_end
            break
        elif kind and not kind[0] & 0x20:
            raise TrafficLabError("image_png_critical_chunk_not_supported")
        offset = chunk_end
        chunk_index += 1
    if not saw_iend or offset != len(image_bytes) or ihdr is None or not compressed_parts:
        raise TrafficLabError("image_png_invalid")

    width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    if width <= 0 or height <= 0:
        raise TrafficLabError("image_png_invalid")
    if width * height > int(parameters["max_image_pixels"]):
        raise TrafficLabError("image_pixel_limit_exceeded")
    if bit_depth != 8 or compression != 0 or filtering != 0 or interlace != 0:
        raise TrafficLabError("image_png_format_not_supported")
    channels_by_color_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    channels = channels_by_color_type.get(color_type)
    if channels is None:
        raise TrafficLabError("image_png_format_not_supported")
    if color_type == 3 and (
        palette is None or not palette or len(palette) % 3 != 0 or len(palette) > 768
    ):
        raise TrafficLabError("image_png_invalid")

    row_bytes = width * channels
    expected_size = height * (row_bytes + 1)
    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(b"".join(compressed_parts), expected_size + 1)
        if decompressor.unconsumed_tail or len(raw) > expected_size:
            raise TrafficLabError("image_png_invalid")
        if len(raw) < expected_size:
            raw += decompressor.flush(expected_size + 1 - len(raw))
    except zlib.error as exc:
        raise TrafficLabError("image_png_invalid") from exc
    if (
        len(raw) != expected_size
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise TrafficLabError("image_png_invalid")

    sample_stride = _sample_stride(
        width, height, int(parameters["max_sample_pixels"])
    )
    sample_width = (width + sample_stride - 1) // sample_stride
    sample_height = (height + sample_stride - 1) // sample_stride
    pixels: list[tuple[int, int, int]] = []
    previous = bytearray(row_bytes)
    raw_offset = 0
    full_statistics = schema.image.extractor_version == IMAGE_EXTRACTOR_V2
    if schema.image.extractor_version not in {IMAGE_EXTRACTOR_V1, IMAGE_EXTRACTOR_V2}:
        raise AssertionError("image_extractor_version_not_implemented")
    luminance_sum = 0.0
    luminance_square_sum = 0.0
    whitespace_count = 0
    edge_count = 0
    edge_comparison_count = 0
    laplacian_total = 0.0
    laplacian_count = 0
    previous_luminances: list[float] | None = None
    above_luminances: list[float] | None = None
    sample_red_sums = [0] * sample_width
    sample_green_sums = [0] * sample_width
    sample_blue_sums = [0] * sample_width
    sample_counts = [0] * sample_width
    for y in range(height):
        filter_type = raw[raw_offset]
        filtered = raw[raw_offset + 1 : raw_offset + 1 + row_bytes]
        raw_offset += row_bytes + 1
        reconstructed = _unfilter_png_row(
            filter_type, filtered, previous, channels
        )
        if full_statistics:
            current_luminances: list[float] = []
            previous_luminance: float | None = None
            for x in range(width):
                pixel = _png_rgb_pixel(
                    reconstructed,
                    x,
                    color_type=color_type,
                    palette=palette,
                    transparency=transparency,
                )
                luminance = _luminance(pixel)
                current_luminances.append(luminance)
                luminance_sum += luminance
                luminance_square_sum += luminance * luminance
                whitespace_count += _is_whitespace(
                    pixel,
                    min_luma=float(parameters["whitespace_min_luma"]),
                    max_chroma=int(parameters["whitespace_max_chroma"]),
                )
                if previous_luminance is not None:
                    edge_count += abs(luminance - previous_luminance) >= float(
                        parameters["edge_threshold"]
                    )
                    edge_comparison_count += 1
                if previous_luminances is not None:
                    edge_count += abs(luminance - previous_luminances[x]) >= float(
                        parameters["edge_threshold"]
                    )
                    edge_comparison_count += 1
                previous_luminance = luminance
                sample_x = x // sample_stride
                sample_red_sums[sample_x] += pixel[0]
                sample_green_sums[sample_x] += pixel[1]
                sample_blue_sums[sample_x] += pixel[2]
                sample_counts[sample_x] += 1
            if above_luminances is not None and previous_luminances is not None:
                for x in range(1, width - 1):
                    center = previous_luminances[x]
                    laplacian_total += abs(
                        4 * center
                        - previous_luminances[x - 1]
                        - previous_luminances[x + 1]
                        - above_luminances[x]
                        - current_luminances[x]
                    )
                    laplacian_count += 1
            if (y + 1) % sample_stride == 0 or y + 1 == height:
                pixels.extend(
                    (
                        (sample_red_sums[index] + sample_counts[index] // 2)
                        // sample_counts[index],
                        (sample_green_sums[index] + sample_counts[index] // 2)
                        // sample_counts[index],
                        (sample_blue_sums[index] + sample_counts[index] // 2)
                        // sample_counts[index],
                    )
                    for index in range(sample_width)
                )
                sample_red_sums = [0] * sample_width
                sample_green_sums = [0] * sample_width
                sample_blue_sums = [0] * sample_width
                sample_counts = [0] * sample_width
            above_luminances = previous_luminances
            previous_luminances = current_luminances
        elif y % sample_stride == 0:
            pixels.extend(
                _png_rgb_pixel(
                    reconstructed,
                    x,
                    color_type=color_type,
                    palette=palette,
                    transparency=transparency,
                )
                for x in range(0, width, sample_stride)
            )
        previous = reconstructed
    if len(pixels) != sample_width * sample_height:
        raise TrafficLabError("image_png_invalid")
    statistics = None
    if full_statistics:
        statistics = _ImageStatistics(
            pixel_count=width * height,
            luminance_sum=luminance_sum,
            luminance_square_sum=luminance_square_sum,
            whitespace_count=whitespace_count,
            sharpness=(
                laplacian_total / (laplacian_count * 4 * 255)
                if laplacian_count
                else 0.0
            ),
            edge_density=(
                edge_count / edge_comparison_count if edge_comparison_count else 0.0
            ),
        )
    return _DecodedImage(
        width=width,
        height=height,
        sample_width=sample_width,
        sample_height=sample_height,
        pixels=tuple(pixels),
        statistics=statistics,
    )


def _sample_stride(width: int, height: int, max_sample_pixels: int) -> int:
    stride = max(1, math.isqrt(max(1, (width * height) // max_sample_pixels)))
    while (
        (width + stride - 1) // stride
    ) * ((height + stride - 1) // stride) > max_sample_pixels:
        stride += 1
    return stride


def _unfilter_png_row(
    filter_type: int,
    filtered: bytes,
    previous: bytearray,
    bytes_per_pixel: int,
) -> bytearray:
    if filter_type not in {0, 1, 2, 3, 4}:
        raise TrafficLabError("image_png_filter_not_supported")
    reconstructed = bytearray(len(filtered))
    for index, value in enumerate(filtered):
        left = reconstructed[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
        above = previous[index]
        upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
        if filter_type == 0:
            predictor = 0
        elif filter_type == 1:
            predictor = left
        elif filter_type == 2:
            predictor = above
        elif filter_type == 3:
            predictor = (left + above) // 2
        else:
            predictor = _paeth_predictor(left, above, upper_left)
        reconstructed[index] = (value + predictor) & 0xFF
    return reconstructed


def _paeth_predictor(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    distance_left = abs(estimate - left)
    distance_above = abs(estimate - above)
    distance_upper_left = abs(estimate - upper_left)
    if distance_left <= distance_above and distance_left <= distance_upper_left:
        return left
    if distance_above <= distance_upper_left:
        return above
    return upper_left


def _png_rgb_pixel(
    row: bytearray,
    x: int,
    *,
    color_type: int,
    palette: bytes | None,
    transparency: bytes | None,
) -> tuple[int, int, int]:
    if color_type == 0:
        value = row[x]
        alpha = 0 if transparency == struct.pack(">H", value) else 255
        return _composite_white(value, value, value, alpha)
    if color_type == 2:
        offset = x * 3
        red, green, blue = row[offset : offset + 3]
        transparent_rgb = (
            struct.unpack(">HHH", transparency)
            if transparency is not None and len(transparency) == 6
            else None
        )
        alpha = 0 if transparent_rgb == (red, green, blue) else 255
        return _composite_white(red, green, blue, alpha)
    if color_type == 3:
        if palette is None:
            raise TrafficLabError("image_png_invalid")
        palette_index = row[x]
        offset = palette_index * 3
        if offset + 3 > len(palette):
            raise TrafficLabError("image_png_invalid")
        red, green, blue = palette[offset : offset + 3]
        alpha = (
            transparency[palette_index]
            if transparency is not None and palette_index < len(transparency)
            else 255
        )
        return _composite_white(red, green, blue, alpha)
    if color_type == 4:
        value, alpha = row[x * 2 : x * 2 + 2]
        return _composite_white(value, value, value, alpha)
    offset = x * 4
    red, green, blue, alpha = row[offset : offset + 4]
    return _composite_white(red, green, blue, alpha)


def _composite_white(
    red: int, green: int, blue: int, alpha: int
) -> tuple[int, int, int]:
    if alpha == 255:
        return red, green, blue
    inverse = 255 - alpha
    return (
        (red * alpha + 255 * inverse + 127) // 255,
        (green * alpha + 255 * inverse + 127) // 255,
        (blue * alpha + 255 * inverse + 127) // 255,
    )


def _extract_image_features(
    decoded: _DecodedImage,
    file_size: int,
    schema: TrafficFeatureSchema,
) -> dict[str, FeatureScalar]:
    parameters = schema.image.parameters
    luminances = tuple(_luminance(pixel) for pixel in decoded.pixels)
    if decoded.statistics is None:
        mean_luminance = sum(luminances) / len(luminances)
        contrast = math.sqrt(
            sum((value - mean_luminance) ** 2 for value in luminances)
            / len(luminances)
        )
        sharpness, edge_density = _sharpness_and_edges(
            luminances,
            decoded.sample_width,
            decoded.sample_height,
            edge_threshold=float(parameters["edge_threshold"]),
        )
        whitespace_ratio = sum(
            _is_whitespace(
                pixel,
                min_luma=float(parameters["whitespace_min_luma"]),
                max_chroma=int(parameters["whitespace_max_chroma"]),
            )
            for pixel in decoded.pixels
        ) / len(decoded.pixels)
    else:
        statistics = decoded.statistics
        mean_luminance = statistics.luminance_sum / statistics.pixel_count
        variance = max(
            0.0,
            statistics.luminance_square_sum / statistics.pixel_count
            - mean_luminance * mean_luminance,
        )
        contrast = math.sqrt(variance)
        sharpness = statistics.sharpness
        edge_density = statistics.edge_density
        whitespace_ratio = statistics.whitespace_count / statistics.pixel_count
    subject_area_ratio = _subject_area_ratio(
        decoded,
        threshold=int(parameters["background_distance_threshold"]),
    )
    text_area_ratio = _text_area_ratio(
        luminances,
        decoded.sample_width,
        decoded.sample_height,
        block_size=int(parameters["text_block_size"]),
        edge_threshold=float(parameters["edge_threshold"]),
        min_contrast=float(parameters["text_min_contrast"]),
        min_edge_density=float(parameters["text_min_edge_density"]),
    )
    return {
        "width": decoded.width,
        "height": decoded.height,
        "aspect_ratio": round(decoded.width / decoded.height, 6),
        "file_size_bytes": file_size,
        "brightness": round(mean_luminance / 255, 6),
        "contrast": round(contrast / 255, 6),
        "sharpness": round(sharpness, 6),
        "edge_density": round(edge_density, 6),
        "text_area_ratio": round(text_area_ratio, 6),
        "subject_area_ratio": round(subject_area_ratio, 6),
        "whitespace_ratio": round(whitespace_ratio, 6),
    }


def _luminance(pixel: tuple[int, int, int]) -> float:
    red, green, blue = pixel
    return (77 * red + 150 * green + 29 * blue) / 256


def _sharpness_and_edges(
    luminances: tuple[float, ...],
    width: int,
    height: int,
    *,
    edge_threshold: float,
) -> tuple[float, float]:
    if width < 3 or height < 3:
        return 0.0, 0.0
    laplacian_total = 0.0
    edge_count = 0
    sample_count = 0
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            index = y * width + x
            center = luminances[index]
            left = luminances[index - 1]
            right = luminances[index + 1]
            above = luminances[index - width]
            below = luminances[index + width]
            gradient = (abs(right - left) + abs(below - above)) / 2
            edge_count += gradient >= edge_threshold
            laplacian_total += abs(4 * center - left - right - above - below)
            sample_count += 1
    return (
        laplacian_total / (sample_count * 4 * 255),
        edge_count / sample_count,
    )


def _is_whitespace(
    pixel: tuple[int, int, int], *, min_luma: float, max_chroma: int
) -> bool:
    return _luminance(pixel) >= min_luma and max(pixel) - min(pixel) <= max_chroma


def _subject_area_ratio(decoded: _DecodedImage, *, threshold: int) -> float:
    width = decoded.sample_width
    height = decoded.sample_height
    border: list[tuple[int, int, int]] = []
    for x in range(width):
        border.append(decoded.pixels[x])
        if height > 1:
            border.append(decoded.pixels[(height - 1) * width + x])
    for y in range(1, height - 1):
        border.append(decoded.pixels[y * width])
        if width > 1:
            border.append(decoded.pixels[y * width + width - 1])
    background = tuple(int(median(channel)) for channel in zip(*border))
    foreground = sum(
        max(abs(pixel[channel] - background[channel]) for channel in range(3))
        >= threshold
        for pixel in decoded.pixels
    )
    return foreground / len(decoded.pixels)


def _text_area_ratio(
    luminances: tuple[float, ...],
    width: int,
    height: int,
    *,
    block_size: int,
    edge_threshold: float,
    min_contrast: float,
    min_edge_density: float,
) -> float:
    if width < 2 or height < 2:
        return 0.0
    text_like_pixels = 0
    for block_y in range(0, height, block_size):
        for block_x in range(0, width, block_size):
            block_width = min(block_size, width - block_x)
            block_height = min(block_size, height - block_y)
            values = [
                luminances[(block_y + y) * width + block_x + x]
                for y in range(block_height)
                for x in range(block_width)
            ]
            if not values or max(values) - min(values) < min_contrast:
                continue
            edge_count = 0
            comparison_count = 0
            for y in range(block_height):
                for x in range(block_width):
                    index = (block_y + y) * width + block_x + x
                    if x + 1 < block_width:
                        edge_count += (
                            abs(luminances[index] - luminances[index + 1])
                            >= edge_threshold
                        )
                        comparison_count += 1
                    if y + 1 < block_height:
                        edge_count += (
                            abs(luminances[index] - luminances[index + width])
                            >= edge_threshold
                        )
                        comparison_count += 1
            if comparison_count and edge_count / comparison_count >= min_edge_density:
                text_like_pixels += block_width * block_height
    return text_like_pixels / (width * height)
