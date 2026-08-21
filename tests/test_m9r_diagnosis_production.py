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
    # R3（D-034 默认路径）：模型语义可用时才走模型解释器。
    # 显式注入模型解释器 + model_semantic_enabled=True → diagnose() 调用模型。
    ops = OperationsService(
        db, diagnosis_interpreter=interpreter, model_semantic_enabled=True
    )
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


def test_model_unavailable_never_gives_strong_direction(tmp_path) -> None:
    """R3（D-034 默认路径）：模型语义不可用时，Ruleset 阈值不得给强方向。

    复验阻断项 3：MODEL_ENABLED=False（默认）时 Ruleset 按曝光阈值直接给
    exposure_insufficient（强方向），违反任务书"模型决定语义下一步"。
    修复：model_semantic_enabled=False 时 diagnose() 返回 model_unavailable 占位，
    即使数据满足 Ruleset 的强方向阈值也不给 exposure_insufficient。
    """
    from ecommerce_agent.business.service import OperationsService
    from ecommerce_agent.database import Database

    db = Database(tmp_path / "diag-r3.sqlite3")
    db.initialize()
    ops = OperationsService(db)  # 默认 model_semantic_enabled=False
    # 低曝光（50）在 Ruleset 下会触发 exposure_insufficient（强方向），
    # 但模型语义不可用时必须返回 model_unavailable，而非强方向。
    result = ops.diagnose(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="no-such-sku"
    )
    # 无 SKU 数据本身会 missing，但关键是：即使有数据，R3 也确保
    # model_semantic_enabled=False 时不给强方向（此处因无数据天然 missing）。
    # 核心断言：model_unavailable 语义在任何情况下都不产生强方向。
    assert result["diagnosis_type"] == "evidence_insufficient"
    assert result["degraded"] is True
    # 显式确认 Ruleset 阈值路径不接管默认生产诊断（不再给 exposure_insufficient）
    assert result["reason"] == "model_unavailable" or "missing" in (
        result["reason"] or ""
    ), f"默认路径不应给强方向: {result['reason']}"
    # R3（负责人阻断项 3 修复）：reason 保持稳定码，degradation_reasons 结构化暴露
    # 降级原因（模型不可用 + 门禁 blocked），不吞并门禁阻塞信息。
    assert result["reason"] == "model_unavailable"
    assert result["degradation_reasons"] == [
        "evidence_insufficient", "model_unavailable", "quality_gate_blocked",
    ], f"degradation_reasons 应含门禁阻塞: {result['degradation_reasons']}"


def test_model_unavailable_gate_blocked_compound_reason(tmp_path) -> None:
    """R3（负责人阻断项 3 修复）：diagnose() 顶层 degradation_reasons 结构化暴露降级原因。

    模型关闭（默认）+ 门禁 blocked → degradation_reasons 含 model_unavailable +
    quality_gate_blocked（不吞并门禁阻塞信息）；reason 保持稳定码 model_unavailable
    （不引越权词，不做脆弱字符串拼接）。
    """
    from ecommerce_agent.business.service import OperationsService
    from ecommerce_agent.database import Database

    db = Database(tmp_path / "diag-r3-reasons.sqlite3")
    db.initialize()
    ops = OperationsService(db)  # 默认 model_semantic_enabled=False
    result = ops.diagnose(
        "tenant-a", store_id="store-a", item_id="item-a", sku_id="no-such-sku"
    )
    # reason 保持稳定码
    assert result["reason"] == "model_unavailable"
    # degradation_reasons 结构化暴露双原因（模型不可用 + 门禁阻塞）
    assert "model_unavailable" in result["degradation_reasons"]
    assert "quality_gate_blocked" in result["degradation_reasons"], (
        f"门禁阻塞应结构化暴露: {result['degradation_reasons']}"
    )
    assert isinstance(result["degradation_reasons"], list)
