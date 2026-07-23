from __future__ import annotations

import hashlib
import math
import re
import struct
from collections.abc import Iterable


_CJK_OR_WORD = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z]+|\d+(?:\.\d+)?")
_SPACE = re.compile(r"\s+")

_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)"), r"\1****\2"),
    (re.compile(r"(?<!\d)(\d{6})\d{8}([\dXx]{4})(?!\d)"), r"\1********\2"),
    (re.compile(r"(?<!\d)(\d{4})\d{8,11}(\d{4})(?!\d)"), r"\1********\2"),
    (
        re.compile(r"((?:密码|验证码|口令)\s*[:：]?\s*)[^\s，。；,;]{3,}", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
)


def normalize_text(text: str) -> str:
    return _SPACE.sub(" ", text.strip()).replace("\u0000", "")


def redact_sensitive(text: str) -> tuple[str, bool]:
    redacted = text
    for pattern, replacement in _SENSITIVE_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted, redacted != text


def search_terms(text: str) -> list[str]:
    terms: list[str] = []
    for part in _CJK_OR_WORD.findall(normalize_text(text).lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            if len(part) <= 8:
                terms.append(part)
            terms.extend(part[index : index + 2] for index in range(max(0, len(part) - 1)))
            if len(part) == 1:
                terms.append(part)
        else:
            terms.append(part)
    return list(dict.fromkeys(term for term in terms if term))


def search_text(*parts: str) -> str:
    return " ".join(search_terms(" ".join(parts)))


def hash_embedding(text: str, dimensions: int = 256) -> list[float]:
    vector = [0.0] * dimensions
    for term in search_terms(text):
        digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
        raw = int.from_bytes(digest, "little")
        index = raw % dimensions
        sign = 1.0 if raw & 1 else -1.0
        vector[index] += sign * (1.0 + min(len(term), 6) / 10.0)
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude:
        return [value / magnitude for value in vector]
    return vector


def vector_to_blob(vector: Iterable[float]) -> bytes:
    values = list(vector)
    return struct.pack(f"<{len(values)}f", *values)


def blob_to_vector(blob: bytes) -> tuple[float, ...]:
    count = len(blob) // 4
    return struct.unpack(f"<{count}f", blob)


def cosine_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=False))


def checksum(*parts: str) -> str:
    payload = "\u241f".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def extract_numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?%?", text))
