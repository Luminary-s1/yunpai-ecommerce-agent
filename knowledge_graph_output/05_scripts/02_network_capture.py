"""02_network_capture.py — 将已抓取的 S4–S10 原始 HTML 转为结构化 Markdown 事实摘录。

输入：01_raw/network/_s{N}_raw.html（已由 curl 抓取）
输出：01_raw/network/S{N}.md，统一头部 + 正文摘录（仅事实性规则/法条）
"""
from __future__ import annotations

import re
import html as html_lib
from datetime import datetime
from pathlib import Path

NET_DIR = Path(__file__).resolve().parent.parent / "01_raw" / "network"

# 源配置：html文件名 -> (md名, 标题, 主题, 进图归属, 正文起始关键词, 正文结束关键词)
SOURCES: dict[str, tuple[str, str, str, str, list[str], list[str]]] = {
    "_s4_raw.html": (
        "S4.md",
        "淘宝价格保护规则",
        "价格保护（7日/活动限制）",
        "Rule + Policy(price_protection)",
        ["价保", "价格保护", "保价"],
        ["相关标签", "去打赏", "回帖", "loading"],
    ),
    "_s5_raw.html": (
        "S5.md",
        "电商平台发货时效与延迟发货规则",
        "发货时效与延迟赔付",
        "Rule",
        ["发货", "延迟发货", "违背承诺"],
        ["footer", "copyright", "版权所有"],
    ),
    "_s6_raw.html": (
        "S6.md",
        "强制性产品认证（3C）目录与适用范围",
        "3C认证（数码品类合规）",
        "Rule",
        ["3C", "强制性产品认证", "CCC", "目录"],
        ["copyright", "备案", "版权所有", "footer"],
    ),
    "_s7_raw.html": (
        "S7.md",
        "小家电整机保修与核心部件售后政策",
        "小家电售后细则（保修/滤网更换）",
        "Policy + FAQ",
        ["保修", "服务承诺", "售后"],
        ["copyright", "备案", "版权所有", "footer"],
    ),
    "_s8_raw.html": (
        "S8.md",
        "数码类商品退换货细则",
        "数码品类规则（电池保修/激活后退货）",
        "Rule + FAQ",
        ["退换货", "数码", "手机"],
        ["copyright", "备案", "版权所有", "footer"],
    ),
    "_s9_raw.html": (
        "S9.md",
        "服饰鞋帽类商品退换货细则",
        "服饰品类规则（吊牌/尺码/内衣特殊/面料）",
        "Rule + FAQ",
        ["退换货", "服饰", "鞋帽"],
        ["copyright", "备案", "版权所有", "footer"],
    ),
    "_s10_raw.html": (
        "S10.md",
        "电商客服标准话术范本",
        "客服话术范本（物流异常/发票/拒收/换货/退款进度）",
        "FAQ + Script",
        ["客服", "话术", "回覆", "物流"],
        ["copyright", "footer", "版权所有", "填寫", "登入"],
    ),
}


def html_to_text(raw: str) -> str:
    raw = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<(br|/p|/div|/li|/h[1-6])[^>]*>", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    txt = html_lib.unescape(raw)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n", txt)
    return txt.strip()


def extract_core(text: str, start_kws: list[str], end_kws: list[str]) -> str:
    core = text
    for kw in start_kws:
        i = text.find(kw)
        if i >= 0:
            core = text[i:]
            break
    for kw in end_kws:
        j = core.find(kw)
        if j >= 0:
            core = core[:j]
            break
    return core.strip()


def main() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    for html_name, (md_name, title, theme, target, start_kws, end_kws) in SOURCES.items():
        src = NET_DIR / html_name
        if not src.exists():
            print(f"跳过（缺失）：{html_name}")
            continue
        text = html_to_text(src.read_text(encoding="utf-8", errors="replace"))
        core = extract_core(text, start_kws, end_kws)
        if len(core) < 80:
            # 正文提取失败，保底：取全文前 3000 字符并标注
            print(f"警告：{md_name} 正文过短（{len(core)}），已降级为全文摘录")
            core = text[:3000]
        md_path = NET_DIR / md_name
        md_path.write_text(
            f"# {title}\n\n"
            f"- 主题：{theme}\n"
            f"- 进图归属：{target}\n"
            f"- 抓取日期：{today}\n"
            f"- 采集方式：降级链第2级（curl 抓公开页）\n"
            f"\n---\n\n## 正文摘录（事实性引用）\n\n{core}\n",
            encoding="utf-8",
        )
        print(f"✓ {md_name} 生成，正文 {len(core)} 字符")


if __name__ == "__main__":
    main()
