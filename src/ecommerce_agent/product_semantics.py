"""M9-R 模型语义产物的可重放来源元数据。"""
from __future__ import annotations

from typing import Any


def semantic_provenance(
    gateway: Any,
    *,
    decision_source: str,
    prompt_version: str,
) -> dict[str, str]:
    """从模型网关读取非敏感配置，形成稳定的语义来源记录。"""
    settings = getattr(gateway, "settings", None)
    return {
        "decision_source": decision_source,
        "model_provider": str(getattr(settings, "model_provider", "unknown")),
        "model_name": str(getattr(settings, "model_name", "unknown")),
        "prompt_version": prompt_version,
    }


__all__ = ["semantic_provenance"]
