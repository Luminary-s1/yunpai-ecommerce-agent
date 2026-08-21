from __future__ import annotations

import base64
import json
import shutil
import unittest.mock as mock
import urllib.error
from pathlib import Path

import pytest

from ecommerce_agent.config import Settings
from ecommerce_agent.auth import Principal
from ecommerce_agent.knowledge_engine.neo4j_client import Neo4jClient


@pytest.fixture(scope="session")
def _migration_template():
    """全量测试提速：预建一个跑完全部迁移的模板库。

    311 个测试每个调用 db.initialize() 都会跑 36 个迁移（实测单次 0.757s）。
    本 fixture 建一次模板库，autouse 后用 shutil.copy 复用（0.003s），
    把迁移开销从 ~240s 降到 ~1s。
    """
    import tempfile

    from ecommerce_agent.database import Database

    template = Path(tempfile.mkdtemp()) / "template.sqlite3"
    Database(template).initialize()
    yield template


@pytest.fixture(autouse=True)
def _fast_database_initialize(_migration_template, monkeypatch):
    """monkeypatch Database.initialize 为复制模板库（零测试改动提速）。

    只在测试环境生效（tests/conftest.py），生产不受影响。
    复制用 shutil.copy：SQLite WAL 已 checkpoint 合并到主文件，复制完整。
    关键豁免：目标库已存在（如迁移升级测试先铺旧库）时
    必须走真实 initialize 触发升级，不能覆盖复制模板。
    """
    from ecommerce_agent.database import Database

    _template = _migration_template
    _real_initialize = Database.initialize

    def _fast_initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 库已存在（升级测试/迁移测试预建旧库）→ 走真实迁移逻辑，不复制模板
        if self.path.exists() and self.path.stat().st_size > 0:
            return _real_initialize(self)
        shutil.copy(_template, self.path)

    monkeypatch.setattr(Database, "initialize", _fast_initialize)


def make_settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        model_provider="glm",
        model_base_url="http://127.0.0.1:9/v1",
        model_name="test-14b",
        model_api_key="",
        model_timeout_seconds=0.2,
        model_max_output_tokens=200,
        model_temperature=0.0,
        model_thinking_enabled=False,
        model_streaming=False,
        model_retry_attempts=0,
        model_enabled=False,
        model_mock_mode=True,
        model_context_limit_tokens=128000,
        context_budget_ratio=0.7,
        rag_top_k=5,
        rag_min_score=0.08,
        rag_direct_approved_answer=True,
        rag_direct_approved_min_score=0.6,
        handoff_confidence_threshold=0.6,
        max_input_chars=2000,
        session_history_limit=6,
        admin_api_key="test-admin-key-123456",
        admin_auth_required=True,
        bootstrap_admin_id="admin-test",
        auth_required=True,
        bootstrap_tenant_id="tenant-test",
        bootstrap_client_id="client-test",
        bootstrap_client_key="test-client-key-12345",
        bootstrap_client_can_supply_order_context=False,
        subject_hash_key="test-subject-hash-key-12345",
        session_idle_timeout_minutes=120,
        message_retention_days=30,
        audit_retention_days=180,
        max_request_body_bytes=2048,
        rate_limit_requests_per_minute=100,
        min_free_disk_mb=1,
        rag_scene_prompts=True,
        kg_import_enabled=False,  # 测试不导入 02_clean 资产（提速；测试数据自己导入）
        kg_dream_worker_enabled=False,  # 测试不启动知识库调度线程（防测试抢线程）
    )


def principal_for(service, subject_id: str = "buyer-1") -> Principal:
    return service.auth.authenticate(
        service.settings.bootstrap_client_id,
        service.settings.bootstrap_client_key,
        subject_id,
    )


# ---------- 图谱检索 mock（mock Neo4jClient.query，独立验收环境无 Neo4j 也能跑） ----------

# 负例词：评测/测试中应检索不到的内容（与 evaluation_suite 负例一致）
_NEGATIVE_TERMS = (
    "量子力学", "外星人", "火星移民", "不存在的商品", "NO-SUCH",
    "量子力学外星人",
)

# 预设图谱数据（按 test_graph_retrieval 断言反向设计）
_ENTITY_ROW = [
    "QC-AF5-WHITE", "SKU",
    {"title": "晴川空气炸锅 AF5", "sku_id": "QC-AF5-WHITE", "color": "云白", "capacity": "5L"},
]
_REL_ROWS = [
    ["QC-AF5-WHITE", "BELONGS_TO", "air_fryer", "Category"],
    ["QC-AF5-WHITE", "HAS_ATTR", "QC-AF5-WHITE|brand", "Attribute"],
]
_MULTI_HOP_ROWS = [
    ["RETURN-38e401d2", "Policy",
     {"policy_name": "七天无理由退货", "policy_code": "RETURN-38e401d2"}],
    ["RULE-TAOBAO-7DAYS", "Rule",
     {"rule_title": "七天无理由法规", "rule_code": "RULE-TAOBAO-7DAYS"}],
]
_NODE_COUNTS = [
    ["Category", 10], ["Product", 8], ["SKU", 12], ["Attribute", 51],
    ["Policy", 9], ["Script", 52], ["FAQ", 63], ["Rule", 17],
]
_REL_COUNTS = [
    ["BELONGS_TO", 19], ["HAS_ATTR", 51], ["APPLIES_TO", 36],
    ["REFERS_TO", 66], ["RELATED_TO", 69],
]


def _fake_neo4j_query(
    self, statement: str, *, params: dict | None = None, timeout: int = 30
) -> list[list]:
    """按 Cypher 语句形态返回预设行（不真解析 SQL，只按关键词路由）。

    覆盖 graph_retrieval 的全部查询形态：
      entity_query / relation_traverse / multi_hop / search / stats
    与 test_graph_retrieval.py 的硬编码断言对齐。
    """
    if "RETURN 1" in statement or "RETURN count(n)" in statement:
        return [[1]]
    if "MATCH (n:" in statement:
        # entity_query：NO-SUCH 返回空，其余返回 QC-AF5-WHITE SKU
        value = str((params or {}).get("value", ""))
        if any(t in value for t in _NEGATIVE_TERMS):
            return []
        return [list(_ENTITY_ROW)]
    if "MATCH (n {id:" in statement and ("-[r" in statement or "-[r:" in statement):
        # relation_traverse：BELONGS_TO + HAS_ATTR（含非 BELONGS_TO，满足 test_relation_traverse_all）
        return [list(r) for r in _REL_ROWS]
    if "MATCH (start {id:" in statement:
        # multi_hop：返回 Policy + Rule（覆盖"七天"断言 + 评测 expected 类型）
        return [list(r) for r in _MULTI_HOP_ROWS]
    if "MATCH (n) WHERE" in statement:
        # search：负例返回空；否则返回标题含查询词的 FAQ（SKU 标签供多跳起点选择）
        t0 = str((params or {}).get("t0", ""))
        if any(t in t0 for t in _NEGATIVE_TERMS):
            return []
        return [[f"FAQ-{t0}", "SKU", f"{t0} 相关词条"]]
    if "MATCH (n) RETURN labels(n)[0]" in statement:
        return [list(r) for r in _NODE_COUNTS]
    if "MATCH ()-[r]->()" in statement:
        return [list(r) for r in _REL_COUNTS]
    return []


@pytest.fixture
def mock_neo4j_query():
    """mock Neo4jClient.query：图谱检索/API/评测测试不依赖本机 Neo4j。

    用法：测试文件头加 pytestmark = pytest.mark.usefixtures("mock_neo4j_query")。
    """
    with mock.patch.object(Neo4jClient, "query", _fake_neo4j_query):
        yield


class _FakeHTTPResponse:
    """模拟 urllib 响应对象（含 read() 和上下文管理器）。"""

    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


@pytest.fixture
def mock_neo4j_transport():
    """mock urllib.request.urlopen：验证 Neo4jClient 请求构建/解析/防注入。

    错误密码 → 抛 HTTPError（Neo4jError）；正常 → 返回 [[1]]。
    仅 test_neo4j_client.py 使用（不污染其他文件）。
    """
    wrong_token = base64.b64encode(b"neo4j:wrong-password").decode()

    def _fake_urlopen(request, timeout=30):
        auth = request.headers.get("Authorization", "")
        if wrong_token in auth:
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)
        return _FakeHTTPResponse({"results": [{"data": [{"row": [1]}]}]})

    with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        yield
