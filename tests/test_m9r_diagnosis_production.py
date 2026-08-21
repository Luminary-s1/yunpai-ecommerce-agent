"""M9-R D-034 生产诊断链路测试（agentops P0 反假绿）。

验证：诊断模型解释器被装配进 OperationsService，生产诊断入口
（operations.diagnose + workbench 路由）真正调用解释器，而非永远 Ruleset。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.business.service import OperationsService
from ecommerce_agent.database import Database
from ecommerce_agent.product_diagnosis.interpreter import DiagnosisModelInterpreter

from conftest import make_settings

ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}


class _MockGateway:
    """mock ModelGateway：返回固定诊断，记录调用次数。"""

    def __init__(self, return_value: dict):
        self._return = return_value
        self.calls = 0

    def generate_json(self, messages, **kwargs):
        self.calls += 1
        return self._return


def test_diagnosis_interpreter_wired_into_operations(tmp_path) -> None:
    """诊断模型解释器被装配：diagnose() 用注入的解释器而非默认 Ruleset。"""
    db = Database(tmp_path / "diag-wire.sqlite3")
    db.initialize()
    gateway = _MockGateway(
        {"diagnosis_type": "evidence_insufficient", "reason": "model said insufficient"}
    )
    interpreter = DiagnosisModelInterpreter(gateway)
    ops = OperationsService(db, diagnosis_interpreter=interpreter)
    # 无数据 → 证据 missing，但解释器应被调用（走模型而非直接 Ruleset）
    result = ops.diagnose(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="no-such-sku"
    )
    assert gateway.calls == 1, f"诊断模型解释器未被调用: {gateway.calls}"
    assert result["diagnosis_type"] == "evidence_insufficient"
    assert result["reason"] == "model said insufficient"


def test_diagnosis_endpoint_returns_structured(tmp_path) -> None:
    """生产诊断路由可达，返回结构化诊断（诊断类型 + 门禁结果）。"""
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get(
            "/v1/products/store-a/item-a/sku-a/diagnosis",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 200, response.text
    data = response.json()
    assert "diagnosis_type" in data
    assert "reason" in data
    assert "degraded" in data
    assert "evidence_facts" in data
    assert "gates" in data
    assert isinstance(data["gates"]["all_passed"], bool)


def test_diagnosis_missing_evidence_fail_closed(tmp_path) -> None:
    """无 SKU 证据 → 诊断 evidence_insufficient，不编造强方向结论。"""
    db = Database(tmp_path / "diag-missing.sqlite3")
    db.initialize()
    ops = OperationsService(db)
    result = ops.diagnose(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="no-such-sku"
    )
    assert result["diagnosis_type"] == "evidence_insufficient"
    assert result["gates"]["all_passed"] is False
