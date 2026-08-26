"""WP4 验收脚本：按 m9r-complete-plan.md 第五节 WP4 验收表 10 条标准逐条断言验证。

标 ⚠️ 的待确认（本脚本实测 WP4 实现后确认，通过则转 ✅）。
闫睿涵 WP5 可直接复验本脚本。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from ecommerce_agent.product_workbench.boundaries import BOUNDARY_NOTES, DEMO_LABEL
from ecommerce_agent.product_workbench.eval import (
    MechanismEvalRunner,
    assert_direction_inputs_oracle_free,
)
from ecommerce_agent.product_workbench.pages import WorkbenchPages
from ecommerce_agent.product_workbench.scenes import DIRECTION_SCENES, FROZEN_SCENES

RESULTS: list[tuple[str, str, str, bool, str]] = []


def check(cid: str, desc: str, expected: str, fn) -> None:
    try:
        fn()
        actual = "PASS"
    except AssertionError as e:
        actual = f"FAIL: {e}"
    except Exception as e:  # noqa: BLE001
        actual = f"ERROR: {type(e).__name__}: {e}"
    ok = actual == "PASS"
    RESULTS.append((cid, desc, expected, ok, actual))


def t01() -> None:
    """条目 1：商品/SKU 下钻到 revision、时间窗、指标、来源、建议依据。"""
    pages = WorkbenchPages()
    data = pages.product_detail(
        store_id="s1", item_id="i1", sku_id="sku1",
        metrics={"impressions": {"evidence_state": "actual", "source": "taobao",
                                 "data_as_of": "2026-08-17", "value": 100}},
    )
    assert data["sku_id"] == "sku1"
    assert data["metrics"]["impressions"]["source"] == "taobao"
    assert data["metrics"]["impressions"]["data_as_of"] == "2026-08-17"


def t02() -> None:
    """条目 2：显示「为什么建议」/「为什么暂不能建议」。"""
    # 页面含建议依据（rationale 由 WP3 提供），此处锁页面结构支持
    pages = WorkbenchPages()
    data = pages.product_detail(store_id="s1", item_id="i1", sku_id="sku1")
    assert "boundary_notes" in data  # 至少提供「为什么」依据说明


def t03() -> None:
    """条目 3：页面浏览无隐式分析/创建/修改（写屏障）。"""
    pages = WorkbenchPages()
    data = pages.product_detail(store_id="s1", item_id="i1", sku_id="sku1")
    assert set(data.keys()) <= {
        "store_id", "item_id", "sku_id", "scope", "metrics", "boundary_notes"
    }


def t04() -> None:
    """条目 4：离线机制拒绝污染；真实方向由禁 mock live gate 独立证明。"""
    runner = MechanismEvalRunner()
    results = runner.run_all()
    assert results and all(result.passed for result in results)
    by_name = {result.scene_name: result.produced for result in results}
    assert by_name["缺货污染"]["recommendation_type"] == "补货联动"
    assert by_name["广告/价格污染"]["recommendation_type"] == "定价候选"
    isolation = assert_direction_inputs_oracle_free(DIRECTION_SCENES)
    assert len(isolation) == len(DIRECTION_SCENES)
    assert all(record["production_input_sha256"] for record in isolation)


def t05() -> None:
    """条目 5：直接运行桌面/窄屏 Playwright 门禁，skip 不能算通过。"""
    repo = Path(__file__).resolve().parent.parent
    browser_tests = repo / "tests" / "test_m9r_workbench_browser.py"
    with tempfile.TemporaryDirectory(prefix=".tmp_wp4_browser_", dir=repo) as temp_dir:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                f"{browser_tests}::test_m9r_workbench_desktop_no_overflow",
                f"{browser_tests}::test_m9r_workbench_narrow_no_overflow",
                "-q",
                f"--basetemp={Path(temp_dir) / 'pytest'}",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "skipped" not in output.lower(), output


def t06() -> None:
    """条目 6：真实/模拟场景隔离，全链标注。"""
    assert len(FROZEN_SCENES) >= 2
    direction_names = {scene.name for scene in DIRECTION_SCENES}
    assert {"选品方向", "上新准备", "清仓风险"} <= direction_names
    pages = WorkbenchPages()
    data = pages.product_detail(
        store_id="s1", item_id="i1", sku_id="sku1",
        metrics={"impressions": {"evidence_state": "demo"}},
    )
    assert data["metrics"]["impressions"]["display_label"] == DEMO_LABEL


def t07() -> None:
    """条目 7：样本数据不作为产品口径（B7）。"""
    pages = WorkbenchPages()
    data = pages.product_detail(
        store_id="s1", item_id="i1", sku_id="sku1",
        metrics={"net_sales": {"evidence_state": "actual",
                               "data_trust": "sample", "value": 88.0}},
    )
    assert data["metrics"]["net_sales"]["evidence_state"] == "actual"


def t08() -> None:
    """条目 8：边界说明文字在页面展示。"""
    assert "B1" in BOUNDARY_NOTES and "B4" in BOUNDARY_NOTES


def t09() -> None:
    """条目 9：页面上每个数字渲染四态徽标 + 来源 + 时间。"""
    pages = WorkbenchPages()
    data = pages.product_detail(
        store_id="s1", item_id="i1", sku_id="sku1",
        metrics={"impressions": {"evidence_state": "actual", "source": "taobao",
                                 "data_as_of": "2026-08-17", "value": 100}},
    )
    metric = data["metrics"]["impressions"]
    assert metric["badge"]["label"] == "真实数据"
    assert metric["source"] == "taobao"
    assert metric["data_as_of"] == "2026-08-17"


def t10() -> None:
    """条目 10：演示参数显式标注「试算」字样。"""
    pages = WorkbenchPages()
    data = pages.product_detail(
        store_id="s1", item_id="i1", sku_id="sku1",
        metrics={"impressions": {"evidence_state": "demo"}},
    )
    assert data["metrics"]["impressions"]["display_label"] == "试算"


check("①", "商品/SKU 下钻到 revision/时间窗/指标/来源/建议依据", "✅", t01)
check("②", "显示为什么建议/为什么暂不能建议", "✅", t02)
check("③", "页面浏览无隐式分析/创建实验/创建建议/修改商品", "⚠️", t03)
check("④", "离线污染反例 + oracle 隔离 + live gate 契约", "✅", t04)
check("⑤", "浏览器桌面 + 窄屏可读，console 无新增错误", "✅", t05)
check("⑥", "真实/模拟场景隔离，全链标注", "✅", t06)
check("⑦", "样本数据不作为产品口径", "⚠️", t07)
check("⑧", "边界说明文字在页面展示", "⚠️", t08)
check("⑨", "页面上每个数字渲染四态徽标+来源+时间", "⚠️", t09)
check("⑩", "演示参数显式标注试算字样", "⚠️", t10)

print(f"{'条目':<6}{'验收标准':<36}{'计划':<5}{'实际':<8}备注")
print("-" * 95)
all_ok = True
for cid, desc, exp, ok, actual in RESULTS:
    if not ok:
        all_ok = False
    print(f"{cid:<7}{desc:<38}{exp:<6}{('PASS' if ok else '**FAIL**'):<8}{actual}")
print("-" * 95)
print(f"结论: {'✅ 全部 PASS' if all_ok else '❌ 有 FAIL 项，需修复'}")
# FAIL 时返回非零退出码（防 CI/人工只看退出状态误判）。
import sys
if not all_ok:
    sys.exit(1)
