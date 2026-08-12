"""Wiki 浏览 API：合并运行时知识表 + 资产层，供控制台「知识库」模块消费。

对齐任务3 Wiki 搭建（M3 交付报告 L185："词条页已渲染，待选型建站/接前端"）。

设计（低耦合、复用优先，零新增依赖）：
- 列表/详情/stats 走运行时 knowledge 表 + 资产层 02_clean/（页面本体不依赖 Neo4j）
- 搜索复用 /v1/graph/search（Neo4j），不另起一套
- 合并规则（字段级，运行时为准）：
  - Q&A 类（FAQ/Script/Policy/Rule）：compiled_truth/answer 取运行时 active 行（编辑即时可见）；
    attributes 取资产层同名 id（strip('kg-') 归一化匹配）保留，管理员新建无资产对应的用运行时列合成；
    演化历史 = 运行时版本行历史 + 资产层 timeline（旧）拼接
  - 实体类（Category/Product/SKU/Attribute）：只读资产层（不编辑）
- id 归一化：运行时 id `kg-X` → 资产层 `X`，统一匹配键
- 状态徽章：列表显示 status/review_status，默认只看 active，可按类型/状态筛选
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth import AdminPrincipal
from ..knowledge_management import KnowledgeCreateRequest
from ..service import AgentService
from .loader import load_clean_dir
from .models import KnowledgeItem
from .runtime_bridge import load_from_runtime


class WikiEditRequest(BaseModel):
    """Wiki 编辑请求（仅 Q&A 类词条；answer 必填）。"""

    model_config = {"extra": "forbid"}

    answer: str = Field(min_length=2, max_length=2000)
    question: str | None = Field(default=None, min_length=2, max_length=500)
    keywords: str | None = Field(default=None, max_length=500)
    category: str | None = Field(default=None, max_length=80)
    intent: str | None = Field(
        default=None, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    risk_level: str = Field(default="low", pattern=r"^(low|medium|high)$")

# 资产层 02_clean/ 目录（相对项目根：src/ecommerce_agent/knowledge_engine/ → 根）
_CLEAN_DIR = Path(__file__).resolve().parents[3] / "knowledge_graph_output" / "02_clean"

# Wiki 展示的类型清单（顺序即导航顺序）
WIKI_KINDS: list[str] = [
    "rule", "policy", "script", "faq",
    "product", "category", "sku", "attribute",
]

# Q&A 类（可从运行时编辑）；实体类只读资产层
RUNTIME_KINDS: set[str] = {"rule", "policy", "script", "faq"}
ENTITY_KINDS: set[str] = {"product", "category", "sku", "attribute"}


def _to_view(item: KnowledgeItem, *, runtime: bool = False) -> dict[str, Any]:
    """KnowledgeItem → 前端视图（含状态/属性/时间线/来源）。

    含 category 字段（前端分类导航用；运行时行来自 attributes.category，
    实体类从资产层 attributes 读，缺失则按 kind 兜底）。
    """
    attrs = dict(item.attributes)
    category = attrs.get("category") or attrs.get("policy_type") or ""
    if not category:
        # 兜底：按 kind 映射到语义分类（前端导航分组）
        _KIND_CATEGORY = {
            "rule": "行业规则", "policy": "售后政策",
            "script": "客服话术", "faq": "常见问答",
            "product": "商品", "category": "品类",
            "sku": "SKU", "attribute": "属性",
        }
        category = _KIND_CATEGORY.get(item.kind.value, "")
    return {
        "id": item.id,
        "kind": item.kind.value,
        "scope": item.scope.value,
        "scope_key": item.scope_key,
        "compiled_truth": item.compiled_truth,
        "category": category,
        "attributes": attrs,
        "timeline": [e.to_dict() for e in item.timeline],
        "source": "runtime" if runtime else "asset",
    }


def load_merged_items(
    *,
    knowledge_base: Any = None,
    tenant_id: str | None = None,
    clean_dir: Path | None = None,
    statuses: tuple[str, ...] = ("active",),
) -> list[dict[str, Any]]:
    """加载合并后的 Wiki 词条列表（运行时 Q&A 为准 + 资产层实体类）。

    参数：
        knowledge_base: 运行时 KnowledgeBase（service.knowledge）；None 时跳过运行时侧
        tenant_id:      租户过滤（传 service.settings.bootstrap_tenant_id）
        clean_dir:      资产层 02_clean/ 路径（默认项目内路径）
        statuses:       只读哪些状态的运行时行（默认 active）

    返回：
        合并后的词条视图列表。资产层目录缺失时降级（只返回运行时侧，不崩溃）。
    """
    by_id: dict[str, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []

    # 1) 运行时 Q&A 类（编辑即时可见）
    if knowledge_base is not None:
        try:
            runtime_items = load_from_runtime(
                knowledge_base, tenant_id=tenant_id, statuses=statuses
            )
        except Exception:
            runtime_items = []
        for item in runtime_items:
            view = _to_view(item, runtime=True)
            by_id[item.id] = view
            items.append(view)

    # 2) 资产层（实体类只读 + Q&A 类作属性/时间线补充）
    try:
        asset_items = load_clean_dir(clean_dir or _CLEAN_DIR)
    except (FileNotFoundError, OSError):
        asset_items = []
    for item in asset_items:
        if item.kind.value in RUNTIME_KINDS:
            existing = by_id.get(item.id)
            if existing is not None:
                # 运行时为准：资产层只补充缺失属性 + 拼接旧时间线，不覆盖结论
                merged_attrs = dict(existing["attributes"])
                for k, v in item.attributes.items():
                    merged_attrs.setdefault(k, v)
                existing["attributes"] = merged_attrs
                existing["timeline"] = existing["timeline"] + [
                    e.to_dict() for e in item.timeline
                ]
                existing["asset_attrs"] = dict(item.attributes)
                continue
            # 资产层有、运行时无对应（未导入）：也展示，来源标 asset
            items.append(_to_view(item, runtime=False))
        else:
            # 实体类只读资产层
            items.append(_to_view(item, runtime=False))
    return items


class WikiService:
    """Wiki 浏览服务：合并运行时 + 资产层，供 build_wiki_router 使用。

    对齐 graph_retrieval.GraphRetrievalService 的"懒加载 + 闭包"模式。
    """

    def __init__(self, service: AgentService) -> None:
        self.service = service

    def list_items(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        items = load_merged_items(
            knowledge_base=self.service.knowledge,
            tenant_id=self.service.settings.bootstrap_tenant_id,
        )
        if kind:
            items = [i for i in items if i["kind"] == kind]
        if status:
            items = [i for i in items if i["attributes"].get("status") == status]
        return items[offset : offset + limit]

    def search(self, q: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """双通道搜索：运行时 knowledge 表为主 + 资产层实体类补充。

        - 主通道：service.knowledge.retrieve（SQLite FTS + 本地 embedding 打分）
        - 补充通道：资产层 02_clean 实体类（Category/Product/SKU/Attribute）
          按 question/title 等字段做简单文本匹配（不依赖 Neo4j）
        - 去重：id 归一化（运行时 kg-X ↔ 资产层 X），运行时命中优先
        """
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        # R6 修复：Wiki 检索路径也接入观测器（此前 knowledge.retrieve 不被观测）
        from .observability import get_observer
        _obs = get_observer()
        _start = time.monotonic()
        try:
            hits = self.service.knowledge.retrieve(
                q,
                top_k=limit,
                min_score=0.08,
                tenant_id=self.service.settings.bootstrap_tenant_id,
            )
            _obs.record_search(
                tenant_id=self.service.settings.bootstrap_tenant_id,
                store_id="",
                query=q,
                hits=len(hits),
                latency_ms=(time.monotonic() - _start) * 1000,
                source="wiki_api",
            )
        except Exception:
            _obs.record_search(
                tenant_id=self.service.settings.bootstrap_tenant_id,
                store_id="",
                query=q,
                hits=0,
                failed=True,
                latency_ms=(time.monotonic() - _start) * 1000,
                event_type="failure",
                source="wiki_api",
            )
            hits = []
        for h in hits:
            doc_id = str(h.get("id", "")).removeprefix("kg-")
            if not doc_id or doc_id in seen:
                continue
            seen.add(doc_id)
            results.append(
                {
                    "id": doc_id,
                    "kind": "faq",  # 运行时行无 kind 字段，由 category 反推
                    "compiled_truth": h.get("answer") or h.get("question", ""),
                    "category": h.get("category", ""),
                    "score": round(float(h.get("score", 0)), 4),
                    "source": "runtime",
                }
            )
        # 补充通道：资产层实体类（只在主通道没覆盖时补）
        if len(results) < limit:
            try:
                asset_items = load_clean_dir(_CLEAN_DIR)
            except (FileNotFoundError, OSError):
                asset_items = []
            for item in asset_items:
                if item.kind.value not in ENTITY_KINDS:
                    continue
                if item.id in seen:
                    continue
                haystack = " ".join(
                    str(v) for v in item.attributes.values() if v
                ) + " " + item.compiled_truth
                if q in haystack:
                    seen.add(item.id)
                    results.append(
                        {
                            "id": item.id,
                            "kind": item.kind.value,
                            "compiled_truth": item.compiled_truth,
                            "category": item.attributes.get("category", ""),
                            "score": None,
                            "source": "asset",
                        }
                    )
                    if len(results) >= limit:
                        break
        return results

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        items = load_merged_items(
            knowledge_base=self.service.knowledge,
            tenant_id=self.service.settings.bootstrap_tenant_id,
        )
        for item in items:
            if item["id"] == item_id:
                return item
        return None

    def stats(self) -> dict[str, Any]:
        items = load_merged_items(
            knowledge_base=self.service.knowledge,
            tenant_id=self.service.settings.bootstrap_tenant_id,
        )
        by_kind: dict[str, int] = {}
        by_source: dict[str, int] = {}
        for item in items:
            by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
            by_source[item["source"]] = by_source.get(item["source"], 0) + 1
        return {"total": len(items), "by_kind": by_kind, "by_source": by_source}


def build_wiki_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    """构建 Wiki 浏览 API 路由（prefix=/v1/wiki，admin 鉴权）。

    对齐项目模式：build_xxx_router(service, require_admin)。
    列表/详情/stats 不依赖 Neo4j（页面本体可用）；搜索复用 /v1/graph/search。
    """
    router = APIRouter(prefix="/v1/wiki", tags=["wiki"])
    wiki: WikiService | None = None

    def _svc() -> WikiService:
        nonlocal wiki
        if wiki is None:
            wiki = WikiService(service)
        return wiki

    @router.get("/items")
    def list_items(
        kind: str | None = Query(
            default=None,
            pattern=r"^(rule|policy|script|faq|product|category|sku|attribute)$",
        ),
        status: str | None = Query(
            default=None, pattern=r"^(active|candidate|retired)$"
        ),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0, le=10000),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        """合并词条列表（分页）：运行时 Q&A（默认 active）+ 资产层实体类。"""
        return _svc().list_items(kind=kind, status=status, limit=limit, offset=offset)

    @router.get("/search")
    def search(
        q: str = Query(min_length=1, max_length=200),
        limit: int = Query(default=20, ge=1, le=50),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        """双通道搜索：运行时知识表（编辑即时可搜）+ 资产层实体类（不依赖 Neo4j）。"""
        return _svc().search(q, limit=limit)

    @router.put("/items/{item_id}")
    def put_item(
        item_id: str,
        request: WikiEditRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """编辑词条（仅 Q&A 类）：走治理生命周期 draft→evaluate→approve。

        - 复用 KnowledgeManagementService.create（knowledge_key=kg-{item_id}）
        - 返回生命周期下一步（candidate/evaluated/approved 状态）
        - 实体类（商品/SKU/品类/属性）只读不可编辑
        """
        item = _svc().get_item(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"词条 {item_id} 不存在")
        if item["kind"] not in RUNTIME_KINDS:
            raise HTTPException(status_code=422, detail=f"{item['kind']} 实体类只读，不可编辑")
        try:
            create_req = KnowledgeCreateRequest(
                category=request.category or item.get("category") or "常见问答",
                intent=request.intent or "wiki-edit",
                question=request.question or item.get("compiled_truth", ""),
                answer=request.answer,
                keywords=request.keywords or "",
                risk_level=request.risk_level,  # type: ignore[arg-type]
                source="wiki://manual",
                layer="store",
                store_id=admin.tenant_id or service.settings.bootstrap_tenant_id,
            )
            created = service.knowledge_management.create(
                admin.tenant_id or service.settings.bootstrap_tenant_id,
                create_req,
                actor=admin.admin_id,
                knowledge_key=f"kg-{item_id}",
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"编辑失败: {exc}") from exc
        return {
            "id": item_id,
            "status": "candidate",
            "next": "evaluate→approve",
            "created": created,
        }

    @router.get("/items/{item_id}")
    def get_item(
        item_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        """词条详情：合并后的单条（含 attributes/timeline/source）。"""
        item = _svc().get_item(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"词条 {item_id} 不存在")
        return item

    @router.get("/stats")
    def stats(admin: AdminPrincipal = Depends(require_admin)) -> dict[str, Any]:
        """概览统计：各类型词条数 / 来源分布。"""
        return _svc().stats()

    return router
