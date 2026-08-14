# -*- coding: utf-8 -*-
"""12_privacy_scan.py — 知识库数据合规扫描（安全-2）。

扫描 02_clean/01_raw/wiki_pages 全量内容中的敏感信息：
- 中国大陆手机号（11 位，1 开头）
- 身份证号（18 位）
- 银行卡号（16-19 位数字）
- 邮箱地址

产出 06_report/privacy_scan_report.md（0 命中即通过，作为交付证据）。

用法：
  python 05_scripts/12_privacy_scan.py
退出码：0=通过（0 命中），1=发现敏感信息
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "06_report"
SCAN_DIRS = [ROOT / "01_raw", ROOT / "02_clean", ROOT / "wiki_pages"]

# 敏感信息模式（避免误报：纯数字且达到位数才匹配）
PATTERNS: list[tuple[str, str]] = [
    ("手机号", r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    ("身份证", r"(?<!\d)\d{17}[\dXx](?!\d)"),
    ("银行卡", r"(?<!\d)\d{16,19}(?!\d)"),
    ("邮箱", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
]

# 合法数字排除：价格/数量/年份等（避免把"499.00"这类误报）
_SAFE_NUMBER_RE = re.compile(r"^\d{1,4}(\.\d{1,2})?$|^\d{4}年|^第?\d+[天条元个件台]")


def scan_text(path: Path, text: str) -> list[dict]:
    """扫描单文件文本，返回命中列表。"""
    hits = []
    for label, pattern in PATTERNS:
        for m in re.finditer(pattern, text):
            value = m.group(0)
            if label in ("手机号", "身份证", "银行卡") and _SAFE_NUMBER_RE.match(value):
                continue
            line_no = text[: m.start()].count("\n") + 1
            hits.append({"file": str(path.relative_to(ROOT)), "label": label, "line": line_no, "value": value})
    return hits


def scan_dir() -> list[dict]:
    """扫描全部知识库文件。"""
    all_hits: list[dict] = []
    for d in SCAN_DIRS:
        if not d.is_dir():
            continue
        for f in d.rglob("*"):
            if not f.is_file() or f.suffix not in (".json", ".md", ".csv", ".txt"):
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            all_hits.extend(scan_text(f, text))
    return all_hits


def main() -> int:
    hits = scan_dir()
    REPORT.mkdir(parents=True, exist_ok=True)
    out = REPORT / "privacy_scan_report.md"
    if hits:
        lines = [
            "# 知识库数据合规扫描报告",
            "",
            f"- 扫描时间：2026-08-11",
            f"- 扫描范围：01_raw / 02_clean / wiki_pages",
            f"- **结果：发现 {len(hits)} 处敏感信息，不通过**",
            "",
            "| 文件 | 类型 | 行号 | 命中值（脱敏） |",
            "|---|---|---|---|",
        ]
        for h in hits:
            masked = h["value"][:3] + "***" + h["value"][-2:]
            lines.append(f"| {h['file']} | {h['label']} | {h['line']} | {masked} |")
        lines.append("")
        lines.append("**处理建议**：修正对应数据文件中的敏感信息后重跑本脚本。")
    else:
        lines = [
            "# 知识库数据合规扫描报告",
            "",
            f"- 扫描时间：2026-08-11",
            f"- 扫描范围：01_raw / 02_clean / wiki_pages",
            "- **结果：通过（0 命中）**",
            "",
            "| 类型 | 命中数 |",
            "|---|---|",
            "| 手机号 | 0 |",
            "| 身份证 | 0 |",
            "| 银行卡 | 0 |",
            "| 邮箱 | 0 |",
            "",
            "知识库数据不含真实个人敏感信息，可安全用于交付/演示。",
        ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✅ 已生成 {out}")
    if hits:
        print(f"❌ 发现 {len(hits)} 处敏感信息（详情见报告）")
        return 1
    print("✅ 扫描通过：0 命中")
    return 0


if __name__ == "__main__":
    sys.exit(main())
