"""图谱检索 API：实体查询、关系遍历、多跳推理、关键词检索、统计。

对齐验收文档交付物⑤：图谱检索服务需封装为独立 API 可被业务层调用。
懒加载 Neo4j 客户端（不阻塞服务启动），按项目模式 build_graph_router(service, require_admin)。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ..auth import AdminPrincipal
from ..service import AgentService
from .graph_retrieval import GraphRetrievalService
from .neo4j_client import Neo4jClient, Neo4jError


class MultiHopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_id: str = Field(min_length=1, max_length=128)
    rel_types: list[str] = Field(min_length=1, max_length=5)
    max_hops: int = Field(default=3, ge=1, le=5)


def build_graph_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    """构建图谱检索 API 路由（prefix=/v1/graph）。

    参数：
        service: AgentService 实例（保持与其他模块一致）
        require_admin: 管理员鉴权依赖

    返回：APIRouter，含 /v1/graph/* 端点。
    """
    router = APIRouter(prefix="/v1/graph", tags=["graph"])
    retrieval: GraphRetrievalService | None = None

    def _svc() -> GraphRetrievalService:
        nonlocal retrieval
        if retrieval is None:
            # 连接参数来自 settings（from_env 读 NEO4J_URI/USER/PASSWORD），
            # 不再硬编码本机配置（P1-5 可复现）
            retrieval = GraphRetrievalService(
                Neo4jClient(
                    service.settings.neo4j_uri,
                    service.settings.neo4j_user,
                    service.settings.neo4j_password,
                )
            )
        return retrieval

    @router.get("/entity/{label}/{key}/{value}")
    def entity_query(
        label: str,
        key: str,
        value: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """实体查询：/v1/graph/entity/SKU/sku_id/QC-AF5-WHITE"""
        try:
            result = _svc().entity_query(label, key, value)
        except (Neo4jError, ValueError) as exc:
            raise HTTPException(status_code=503 if isinstance(exc, Neo4jError) else 422,
                                detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail=f"实体 {label}:{key}={value} 不存在")
        return result

    @router.get("/relations/{entity_id}")
    def relation_traverse(
        entity_id: str,
        rel_type: str | None = Query(default=None),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        """关系遍历：/v1/graph/relations/QC-AF5-WHITE?rel_type=BELONGS_TO"""
        try:
            return _svc().relation_traverse(entity_id, rel_type)
        except (Neo4jError, ValueError) as exc:
            raise HTTPException(status_code=503 if isinstance(exc, Neo4jError) else 422,
                                detail=str(exc)) from exc

    @router.post("/multi-hop")
    def multi_hop(
        request: MultiHopRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        """多跳推理：POST /v1/graph/multi-hop（body: {start_id, rel_types, max_hops}）"""
        try:
            return _svc().multi_hop(request.start_id, request.rel_types, max_hops=request.max_hops)
        except (Neo4jError, ValueError) as exc:
            raise HTTPException(status_code=503 if isinstance(exc, Neo4jError) else 422,
                                detail=str(exc)) from exc

    @router.get("/search")
    def search(
        q: str = Query(min_length=1, max_length=200),
        limit: int = Query(default=10, ge=1, le=50),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        """关键词检索：/v1/graph/search?q=保修&limit=10"""
        try:
            return _svc().search(q, limit=limit)
        except (Neo4jError, ValueError) as exc:
            raise HTTPException(status_code=503 if isinstance(exc, Neo4jError) else 422,
                                detail=str(exc)) from exc

    @router.get("/stats")
    def stats(admin: AdminPrincipal = Depends(require_admin)) -> dict[str, Any]:
        """图谱统计：/v1/graph/stats"""
        try:
            return _svc().stats()
        except (Neo4jError, ValueError) as exc:
            raise HTTPException(status_code=503 if isinstance(exc, Neo4jError) else 422,
                                detail=str(exc)) from exc

    return router
