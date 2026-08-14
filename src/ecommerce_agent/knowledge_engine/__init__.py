"""云湃知识库融合引擎（gbrain 融合版）。

一个低耦合、可复用的知识库模块：
- 只读任务6交付物（02_clean/），不修改它们
- 数据模型：三层边界 scope + 编译真相/时间线（对齐 gbrain）
- 梦循环：增量摄取 / 一致性校验 / 合并记忆（对齐 gbrain Dream Cycle）
- 图谱检索：Neo4j 客户端 + 检索服务 + API + 四套 Prompt + 评测
- 无第三方依赖，纯标准库实现，可被任何下游复用

快速使用：
    from ecommerce_agent.knowledge_engine import load_clean_dir, run_dream_cycle
    items = load_clean_dir("knowledge_graph_output/02_clean")
    report = run_dream_cycle(items)
"""

from .models import (
    KnowledgeItem,
    KnowledgeKind,
    KnowledgeScope,
    TimelineEntry,
    utc_now_iso,
    coerce_kind,
)
from .loader import (
    load_clean_dir,
    load_record,
    infer_scope,
    stats,
)
from .neo4j_client import (
    Neo4jClient,
    Neo4jError,
)
from .graph_retrieval import (
    GraphRetrievalService,
)
from .prompt_templates import (
    PROMPT_TEMPLATES,
    render_prompt,
)
from .graph_api import (
    build_graph_router,
)
from .wiki_api import (
    build_wiki_router,
    WikiService,
    load_merged_items,
    WIKI_KINDS,
)
from .evaluation_suite import (
    EVALUATION_QUESTIONS,
    run_evaluation,
)
from .dream_cycle import (
    ingest,
    consistency_check,
    consolidate,
    auto_repair,
    run_dream_cycle,
    IngestReport,
    ConsistencyReport,
    ConsolidateReport,
)
from .runtime_bridge import (
    to_knowledge_row,
    import_to_runtime,
    load_from_runtime,
    SCOPE_TO_LAYER,
    RAG_IMPORTABLE,
)
from .wiki_renderer import (
    render_item,
    render_wiki,
)
from .scheduler import (
    run_dream_cycle_once,
    run_loop,
    DEFAULT_INTERVALS,
    DEFAULT_CLEAN_DIR,
)
from .memory_service import (
    KnowledgeMemoryService,
    MEMORY_LAYER,
    MEMORY_CATEGORIES,
)
from .dream_cycle import apply_consolidation
from .security_guard import (
    KnowledgeSecurityGuard,
    get_security_guard,
    GuardDecision,
    GuardResult,
)
from .observability import RetrievalObserver, get_observer

__all__ = [
    # models
    "KnowledgeItem",
    "KnowledgeKind",
    "KnowledgeScope",
    "TimelineEntry",
    "utc_now_iso",
    "coerce_kind",
    # loader
    "load_clean_dir",
    "load_record",
    "infer_scope",
    "stats",
    # dream_cycle
    "ingest",
    "consistency_check",
    "consolidate",
    "run_dream_cycle",
    "auto_repair",
    "IngestReport",
    "ConsistencyReport",
    "ConsolidateReport",
    # runtime_bridge
    "to_knowledge_row",
    "import_to_runtime",
    "load_from_runtime",
    "SCOPE_TO_LAYER",
    "RAG_IMPORTABLE",
    # wiki_renderer
    "render_item",
    "render_wiki",
    # scheduler
    "run_dream_cycle_once",
    "run_loop",
    "DEFAULT_INTERVALS",
    "DEFAULT_CLEAN_DIR",
    # neo4j_client
    "Neo4jClient",
    "Neo4jError",
    # graph_retrieval
    "GraphRetrievalService",
    # prompt_templates
    "PROMPT_TEMPLATES",
    "render_prompt",
    # graph_api
    "build_graph_router",
    # wiki_api
    "build_wiki_router",
    "WikiService",
    "load_merged_items",
    "WIKI_KINDS",
    # evaluation_suite
    "EVALUATION_QUESTIONS",
    "run_evaluation",
    # memory_service
    "KnowledgeMemoryService",
    "MEMORY_LAYER",
    "MEMORY_CATEGORIES",
    # dream_cycle
    "apply_consolidation",
    # security_guard
    "KnowledgeSecurityGuard",
    "get_security_guard",
    "GuardDecision",
    "GuardResult",
    # observability
    "RetrievalObserver",
    "get_observer",
]
