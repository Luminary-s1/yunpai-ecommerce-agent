from __future__ import annotations

import hashlib
import math
import re
import struct
from collections.abc import Iterable, Mapping


_CJK_OR_WORD = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z]+|\d+(?:\.\d+)?")
_SPACE = re.compile(r"\s+")

# \u7535\u5546\u5ba2\u670d\u9886\u57df\u540c\u4e49\u8bcd\u8868\uff08P2-1 \u4fee\u590d\uff1a\u8bcd\u9762 n-gram \u65e0\u6cd5\u5339\u914d\u540c\u4e49\u8868\u8fbe\uff0c\u5982"\u4fdd\u4fee\u2194\u8d28\u4fdd"\uff09
# \u7528\u6cd5\uff1a\u68c0\u7d22/\u54c8\u5e0c\u524d\u5bf9\u6bcf\u4e2a term \u5c55\u5f00\u540c\u4e49\u8bcd\uff08\u9996\u9879\u4e3a\u89c4\u8303\u5f62\uff0c\u5176\u4f59\u4e3a\u7b49\u4ef7\u8868\u8fbe\uff09\u3002
_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("\u4fdd\u4fee", "\u8d28\u4fdd", "\u4fdd\u56fa"),
    ("\u9000\u6b3e", "\u9000\u94b1", "\u9000\u8d27\u6b3e"),
    ("\u9000\u8d27", "\u9000\u56de", "\u9000\u56de\u53bb"),
    ("\u53d1\u8d27", "\u5bc4\u51fa", "\u5bc4\u9001", "\u7269\u6d41\u53d1\u51fa"),
    ("\u6536\u8d27", "\u7b7e\u6536", "\u6536\u5230\u8d27"),
    ("\u5ba2\u670d", "\u4eba\u5de5", "\u4eba\u5de5\u5ba2\u670d", "\u5728\u7ebf\u5ba2\u670d"),
    ("\u4f18\u60e0\u5238", "\u4f18\u60e0\u5377", "\u4ee3\u91d1\u5238", "\u4f18\u60e0"),
    ("\u4fc3\u9500", "\u6253\u6298", "\u6298\u6263", "\u4f18\u60e0\u6d3b\u52a8"),
    ("\u652f\u4ed8", "\u4ed8\u6b3e", "\u4e0b\u5355\u4ed8\u6b3e"),
    ("\u8ba2\u5355", "\u5355\u5b50", "\u8d2d\u4e70\u8bb0\u5f55"),
    ("\u8fd0\u8d39", "\u90ae\u8d39", "\u5feb\u9012\u8d39"),
    ("\u53d1\u7968", "\u5f00\u7968"),
    ("\u4ef7\u683c", "\u4ef7\u94b1", "\u552e\u4ef7"),
    ("\u5e93\u5b58", "\u6709\u8d27", "\u73b0\u8d27", "\u5b58\u8d27"),
    ("\u5c3a\u7801", "\u7801\u6570", "\u5927\u5c0f\u7801", "\u5c3a\u5bf8"),
    ("\u989c\u8272", "\u8272\u53f7", "\u6b3e\u5f0f\u8272"),
)

# \u53cc\u5411\u7b49\u4ef7\u7d22\u5f15\uff1a\u4efb\u4e00\u8868\u8fbe \u2192 \u540c\u7ec4\u5168\u90e8\u8868\u8fbe\uff08\u9996\u9879\u89c4\u8303\u5f62\u4f18\u5148\uff09
_SYNONYM_MAP: dict[str, tuple[str, ...]] = {}
for _group in _SYNONYM_GROUPS:
    for _term in _group:
        _SYNONYM_MAP[_term] = _group


def expand_synonyms(term: str) -> tuple[str, ...]:
    """\u8fd4\u56de term \u7684\u7b49\u4ef7\u8868\u8fbe\uff08\u542b\u81ea\u8eab\uff09\u3002\u547d\u4e2d\u540c\u4e49\u8bcd\u7ec4\u65f6\u8fd4\u56de\u6574\u7ec4\u3002"""
    return _SYNONYM_MAP.get(term, (term,))

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
    # P2-1 \u4fee\u590d\uff1a\u5148\u505a\u540c\u4e49\u8bcd\u5f52\u4e00\u5316\uff08"\u4fdd\u4fee"\u2192"\u4fdd\u4fee \u8d28\u4fdd \u4fdd\u56fa"\uff09\uff0c
    # \u4f7f"\u4fdd\u4fee\u591a\u4e45"\u80fd\u547d\u4e2d"\u8d28\u4fdd\u4e00\u5e74"\u7c7b\u6587\u6863\u7684\u8bcd\u9762 n-gram\u3002
    text = expand_synonyms_text(text)
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


def expand_synonyms_text(text: str) -> str:
    """\u628a\u6587\u672c\u4e2d\u7684\u540c\u4e49\u8bcd\u8868\u8fbe\u66ff\u6362\u4e3a\u89c4\u8303\u5f62\uff08\u4fdd\u7559\u539f\u6587\uff0c\u8ffd\u52a0\u7b49\u4ef7\u8bcd\uff09\u3002

    \u4ec5\u5c55\u5f00\u5b8c\u6574\u547d\u4e2d\u8bcd\uff08\u4e0d\u505a\u5b50\u4e32\u76f2\u6269\uff0c\u907f\u514d"\u53d1\u8d27"\u8bef\u6269\u51fa"\u53d1\u8d27\u91cf"\uff09\uff0c
    \u6bcf\u4e2a\u547d\u4e2d\u8bcd\u8ffd\u52a0\u540c\u7ec4\u5176\u4f59\u8868\u8fbe\uff0c\u7528\u7a7a\u683c\u5206\u9694\uff0c\u4f9b\u8bcd\u9762\u5339\u914d\u5171\u4eab\u3002
    """
    for group in _SYNONYM_GROUPS:
        canonical, *rest = group
        if canonical in text and not any(alt in text for alt in rest):
            text = f"{text} {' '.join(rest)}"
    return text


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


def contains_forbidden_token(value: object, forbidden: frozenset[str]) -> bool:
    """递归扫描任意结构化值是否命中禁止词条。

    覆盖：
    - Mapping：对键名与每个值递归（dict 键名可能是 forbidden 词）
    - list/tuple：对每个元素递归
    - str：子串匹配（自然语言越权，如「平台权重提升 20%」）
    确定性：纯遍历，无 I/O、无随机。
    """
    if isinstance(value, Mapping):
        for key in value:
            if isinstance(key, str) and key in forbidden:
                return True
        return any(contains_forbidden_token(item, forbidden) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_forbidden_token(item, forbidden) for item in value)
    if isinstance(value, str):
        # B1（盲点 #1 修复）：诚实缺失声明豁免——「缺竞品/无竞品数据/竞品数据缺失/
        # 暂无对标」等是任务书要求的诚实说明（WP3 L364"缺竞品不假装对标"），必须能
        # 表达；只有"假装有对标"式的虚假/越权表述才拒。
        # 豁免判定：禁词（竞品/对标/行业）前后 3 字符窗口内出现缺失修饰词
        # （缺/无/缺失/暂无/未提供/没有/缺少）→ 视为诚实缺失声明，不拒。
        for token in forbidden:
            if token not in ("竞品", "对标", "行业"):
                if token in value:
                    return True
            else:
                if _is_honest_missing_declaration(value, token):
                    continue  # 诚实缺失声明，豁免
                if token in value:
                    return True
        return False
    return False


# B1：诚实缺失声明修饰词
_HONEST_MISSING_PREFIXES: tuple[str, ...] = (
    "缺", "无", "缺失", "暂无", "未提供", "没有", "缺少", "不做", "不",
)


def _is_honest_missing_declaration(text: str, token: str) -> bool:
    """判断禁词出现处是否构成诚实缺失声明（前后 4 字符窗口内有缺失修饰词）。"""
    start = 0
    while True:
        pos = text.find(token, start)
        if pos == -1:
            return False
        window = text[max(0, pos - 4): pos + len(token) + 4]
        if any(m in window for m in _HONEST_MISSING_PREFIXES):
            return True
        start = pos + len(token)
