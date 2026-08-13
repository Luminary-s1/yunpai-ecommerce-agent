"""资产层热更新 API 测试（P1-3）：POST /v1/admin/knowledge/import-assets。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app

from conftest import make_settings


ADMIN_HEADERS = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as test_client:
        yield test_client


def test_import_assets_requires_admin(client: TestClient) -> None:
    """无鉴权 → 401/503。"""
    resp = client.post("/v1/admin/knowledge/import-assets")
    assert resp.status_code in (401, 503)


def test_import_assets_loads_clean(client: TestClient) -> None:
    """导入 02_clean 资产（幂等）：重复调用 skipped_existing 递增。"""
    first = client.post("/v1/admin/knowledge/import-assets", headers=ADMIN_HEADERS)
    assert first.status_code == 200
    data = first.json()
    assert data["ok"] is True
    assert data["imported"] >= 100, "应导入 kg-* 资产知识"
    second = client.post("/v1/admin/knowledge/import-assets", headers=ADMIN_HEADERS)
    assert second.status_code == 200
    assert second.json()["imported"] == 0, "重复导入应全部跳过（幂等）"
    assert second.json()["skipped_existing"] >= data["imported"]


def test_import_assets_update_true_updates_content(client: TestClient) -> None:
    """A6 修复：?update=true 时端点更新已存在内容（不再是假热更新）。

    此前 update_existing 只在 runtime_bridge 层支持，端点从未接线——
    改 02_clean 后重导不更新内容。现在端点显式传 update=true 生效。
    """
    service = getattr(client.app.state, "agent", None)
    assert service is not None, "create_app 应暴露 app.state.agent"

    # 第一次幂等导入
    first = client.post("/v1/admin/knowledge/import-assets", headers=ADMIN_HEADERS)
    assert first.status_code == 200
    assert first.json()["imported"] >= 100

    # 手动改一条已导入的运行时知识（模拟 02_clean 变更）
    from ecommerce_agent.knowledge_engine.runtime_bridge import load_from_runtime

    rows = service.knowledge.retrieve(
        "保修", top_k=1, min_score=0.0, tenant_id="tenant-test"
    )
    assert rows, "应有保修相关知识"
    target_id = rows[0]["id"]
    with service.db.connect() as conn:
        conn.execute(
            "UPDATE knowledge SET answer=? WHERE id=?", ("【已修改】保修政策", target_id)
        )

    # 默认（update 未传）：幂等，不更新
    default = client.post("/v1/admin/knowledge/import-assets", headers=ADMIN_HEADERS)
    assert default.status_code == 200
    assert default.json()["updated"] == 0, "默认应保持幂等（不更新内容）"

    # update=true：更新已存在内容（P1-2 多租户语义：租户端点不得改写全局行——
    # 全局行内容保持手动修改值不变；本租户行正常热更新）
    upd = client.post(
        "/v1/admin/knowledge/import-assets?update=true", headers=ADMIN_HEADERS
    )
    assert upd.status_code == 200
    body = upd.json()
    assert body["imported"] == 0, "不应新增重复行"
    assert body["update_failed"] == 0, "热更新不应报错"
    # P1-2 验证：目标行是全局行（tenant_id IS NULL），租户端点不得改写——
    # 手动修改的答案必须保持原样（此前会越权重写回资产层内容）
    with service.db.connect() as conn:
        row = conn.execute(
            "SELECT answer, tenant_id FROM knowledge WHERE id=?", (target_id,)
        ).fetchone()
    if row["tenant_id"] is None:
        assert row["answer"] == "【已修改】保修政策", "全局行不得被租户端点热更新改写"
    else:
        # 本租户行：正常热更新（恢复资产层内容）
        assert row["answer"] != "【已修改】保修政策", "本租户行应被热更新恢复"
        assert body["updated"] >= 1
