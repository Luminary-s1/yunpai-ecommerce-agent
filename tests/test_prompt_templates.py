"""Prompt 模板测试：四套场景 + 防幻觉指令 + 上下文/问题注入。"""

from __future__ import annotations

import pytest

from ecommerce_agent.knowledge_engine.prompt_templates import PROMPT_TEMPLATES, render_prompt


def test_four_templates_exist() -> None:
    """四套场景模板齐全。"""
    assert set(PROMPT_TEMPLATES.keys()) == {
        "customer_service",
        "product_recommend",
        "aftersale_policy",
        "competitor_analysis",
    }


def test_render_prompt_injects_context_and_question() -> None:
    """渲染：上下文和问题都被注入。"""
    prompt = render_prompt("customer_service", "七天无理由退货规则", "能退吗")
    assert "七天无理由退货规则" in prompt  # 上下文注入
    assert "能退吗" in prompt  # 问题注入


def test_all_templates_have_antihallucination() -> None:
    """每套模板都有防幻觉指令（仅基于检索结果/不编造/严格按检索回答）。"""
    for name, template in PROMPT_TEMPLATES.items():
        assert (
            ("仅基于" in template)
            or ("不编造" in template)
            or ("严格按" in template)
        ), f"{name} 缺防幻觉"


def test_render_unknown_scene_raises() -> None:
    """未知场景抛 ValueError。"""
    with pytest.raises(ValueError):
        render_prompt("no_such_scene", "ctx", "q")


def test_prompt_templates_are_strings() -> None:
    """模板都是可格式化字符串。"""
    for name, template in PROMPT_TEMPLATES.items():
        assert isinstance(template, str)
        assert "{context}" in template
        assert "{question}" in template
