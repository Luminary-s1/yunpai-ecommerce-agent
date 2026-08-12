"""四套业务场景 Prompt 模板（含防幻觉指令）。

对齐验收文档交付物⑥：Prompt 需含防幻觉指令（仅基于图谱检索结果和 Wiki 文档回答）。

实现：
- 优先从 `knowledge_graph_output/03_dictionary/prompt_templates.json` 加载
  （机器可读唯一契约，与图谱数据同源，生产部署由 package-data 携带）
- 文件缺失/损坏时：日志警告 + fallback 内置 dict（不崩溃，保证可用性）
- 每套模板通过 {context}（检索结果）和 {question}（用户问题）渲染

导出（向后兼容）：PROMPT_TEMPLATES / render_prompt。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("knowledge_engine.prompt_templates")

# 内置默认模板（fallback；JSON 缺失时使用）
_DEFAULT_TEMPLATES: dict[str, str] = {
    "customer_service": (
        "你是电商客服助手。以下是从知识图谱/Wiki 检索到的可靠事实：\n"
        "{context}\n\n"
        "顾客问题：{question}\n\n"
        "回答要求：\n"
        "1. 仅基于上述检索事实回答，不得编造任何未出现在检索结果中的信息。\n"
        "2. 如果检索结果不包含答案，明确说'抱歉，我暂时无法确定这个问题'。\n"
        "3. 回答要简洁、友好、面向顾客。"
    ),
    "product_recommend": (
        "你是电商商品推荐助手。以下是从知识图谱检索到的商品事实：\n"
        "{context}\n\n"
        "顾客需求：{question}\n\n"
        "回答要求：\n"
        "1. 仅基于检索到的商品信息推荐，不虚构商品属性。\n"
        "2. 如果检索结果没有顾客想要的商品，说明'当前没有匹配的商品'。\n"
        "3. 推荐时给出价格、卖点等具体依据。"
    ),
    "aftersale_policy": (
        "你是电商售后政策助手。以下是从知识图谱检索到的售后政策：\n"
        "{context}\n\n"
        "顾客询问：{question}\n\n"
        "回答要求：\n"
        "1. 严格按检索到的政策条款回答，不自行扩大或缩小政策范围。\n"
        "2. 引用政策时标注来源（如'依据三包规定'）。\n"
        "3. 如果政策不明确，建议转人工处理。"
    ),
    "competitor_analysis": (
        "你是竞品分析助手。以下是从知识图谱检索到的竞品/商品信息：\n"
        "{context}\n\n"
        "分析需求：{question}\n\n"
        "回答要求：\n"
        "1. 仅基于检索到的数据做对比分析，不猜测未检索到的竞品数据。\n"
        "2. 给出价格、卖点等可量化对比，说明数据来源。\n"
        "3. 如果数据不足，明确指出缺少哪些信息。"
    ),
}

# 契约 JSON 路径：优先包内 fixtures（pip 安装可用，importlib.resources），
# 回退仓库内 knowledge_graph_output/03_dictionary/（源码运行）
_FALLBACK_JSON_PATH = (
    Path(__file__).resolve().parents[3]
    / "knowledge_graph_output"
    / "03_dictionary"
    / "prompt_templates.json"
)


def _load_from_json() -> dict[str, str] | None:
    """从契约 JSON 加载模板；失败返回 None（调用方 fallback）。"""
    raw: str | None = None
    source = ""
    # 1) 包内 fixtures（生产安装）
    try:
        import importlib.resources

        raw = importlib.resources.files("ecommerce_agent.fixtures").joinpath(
            "prompt_templates.json"
        ).read_text(encoding="utf-8")
        source = "包内 fixtures"
    except (ImportError, FileNotFoundError, OSError):
        raw = None
    # 2) 回退仓库内路径（源码运行）
    if raw is None:
        try:
            if _FALLBACK_JSON_PATH.is_file():
                raw = _FALLBACK_JSON_PATH.read_text(encoding="utf-8")
                source = str(_FALLBACK_JSON_PATH)
        except OSError:
            raw = None
    if raw is None:
        logger.warning("prompt_templates.json 不可用，使用内置默认模板")
        return None
    try:
        data = json.loads(raw)
        templates = data.get("templates")
        if not isinstance(templates, dict) or not templates:
            logger.warning("prompt_templates.json 缺少 templates 字段，使用内置默认模板")
            return None
        required = {"customer_service", "product_recommend", "aftersale_policy", "competitor_analysis"}
        missing = required - set(templates)
        if missing:
            logger.warning("prompt_templates.json 缺场景 %s，使用内置默认模板", sorted(missing))
            return None
        logger.info("prompt_templates 已从 %s 加载（%d 场景）", source, len(templates))
        return {k: str(v) for k, v in templates.items()}
    except (ValueError, TypeError) as exc:
        logger.warning("prompt_templates.json 解析失败（%s），使用内置默认模板", exc)
        return None


PROMPT_TEMPLATES: dict[str, str] = _load_from_json() or _DEFAULT_TEMPLATES


def render_prompt(scene: str, context: str, question: str) -> str:
    """渲染指定场景的 Prompt。

    参数：
        scene: 场景 key（customer_service/product_recommend/aftersale_policy/competitor_analysis）
        context: 从图谱/Wiki 检索到的上下文
        question: 用户问题

    返回：完整 Prompt 字符串。
    未知场景抛 ValueError。
    """
    if scene not in PROMPT_TEMPLATES:
        raise ValueError(f"未知场景: {scene}，可选: {list(PROMPT_TEMPLATES)}")
    return PROMPT_TEMPLATES[scene].format(context=context, question=question)
