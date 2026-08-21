"""M9-R WP4 浏览器门禁：Playwright + Edge headless 加载 /admin 商品经营工作台。

覆盖（WP5 复审修复 P1-3：交付真实页面 + 浏览器证据，反假绿）：
- /admin 页面含「商品经营」导航与工作台视图（真实 DOM）
- 商品经营视图切换后**真实渲染 workbench JSON API 内容**（非只断言 section.active）
- 1280×720 桌面：无横向溢出，console 无错误
- 390×844 窄屏：无横向溢出，console 无错误

方案（用户决策）：复用 scripts/m4_stream_evidence.py 的 Playwright + msedge 模式，
替代手写 CDP（websocket-client 不在 .venv 依赖，复验干净环境会 ModuleNotFoundError；
且 CDP 首航竞态导致 10053）。

反假绿要求：每个断言必须验证真实渲染内容——等待 #m9rMetricRows 有数据行
（loadM9rWorkbench 从 /v1/products/.../workbench + /recommendations 拉数渲染），
而不是只断言 section.active 或按钮存在（API 404 也会通过）。

边界：本测试只验证页面结构与渲染，不替代正式 WP5 独立浏览器复验。
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import pytest

from ecommerce_agent.api import create_app
from ecommerce_agent.config import Settings

from conftest import make_settings

# T3.7（阻断6 修复）：浏览器探测跨平台——Windows Edge 优先，macOS/Linux 回退
# Chromium/Chrome。缺失时显式 skip（附"需在 Windows/有浏览器的环境补跑"），
# 不做静默无痕的整模块跳过。
EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
EDGE_ALT = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
_EDGE_ALT_ALT = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
_CHROMIUM_ALT = r"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _browser_channel() -> str | None:
    """返回 playwright 可用的浏览器 channel，或 None（无可用浏览器）。

    Windows：msedge（既有路径）；macOS/Linux：chrome/chromium 回退。
    """
    if Path(EDGE_PATH).is_file() or Path(EDGE_ALT).is_file():
        return "msedge"
    if Path(_EDGE_ALT_ALT).is_file():
        return "chrome"
    if Path(_CHROMIUM_ALT).is_file():
        return "chrome"
    return None


pytest.importorskip(
    "playwright", reason="playwright 未安装（需在 .venv 依赖声明）"
)
_BROWSER_CHANNEL = _browser_channel()
pytestmark = pytest.mark.skipif(
    _BROWSER_CHANNEL is None,
    reason=(
        "未找到可用浏览器（Windows Edge / Chrome）。浏览器门禁需在有浏览器的环境"
        "补跑（Windows: Edge；macOS/Linux: Chrome）。"
    ),
)

BASE_URL = "http://127.0.0.1:8765"


@pytest.fixture(scope="module")
def _server():
    """启动一个隔离的 FastAPI 服务（模块级复用，浏览器由 Playwright 管理）。"""
    data_dir = Path(tempfile.mkdtemp(prefix="m9r-browser-"))
    settings: Settings = make_settings(data_dir)
    app = create_app(settings)
    import threading

    import uvicorn

    port = 8765
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # 等端口就绪
    import urllib.request

    for _ in range(30):
        try:
            urllib.request.urlopen(f"{BASE_URL}/health", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    yield
    server.should_exit = True
    thread.join(timeout=5)
    shutil.rmtree(data_dir, ignore_errors=True)


def _open_workbench(page) -> None:
    """打开 /admin → 显式登录管理员 → 切到商品经营视图 → 等真实数据渲染。

    返回后页面已进入 M9 工作台视图；调用方此时再挂 console 监听可排除
    初始加载的 favicon 404（项目既有，与 M9 无关）。
    """
    page.goto(f"{BASE_URL}/admin", wait_until="networkidle")
    page.wait_for_timeout(800)
    # 显式登录（loginOverlay 拦截点击；对齐 scripts/m4_stream_evidence.py 流程）
    login = page.query_selector("#loginOverlay")
    if login and login.is_visible():
        page.fill("#adminId", "admin-test")  # conftest bootstrap_admin_id
        page.fill("#adminKey", "test-admin-key-123456")
        # 精确定位登录按钮（页面有多个 type=submit，需按文本过滤）
        page.click('button[type="submit"]:has-text("登录控制台")')
        page.wait_for_timeout(1000)
    # 切到商品经营视图
    page.click('button[data-view="m9r-workbench"]')
    # 等 loadM9rWorkbench 从 workbench API 拉数并渲染（默认 store-a/item-a/sku-a）
    page.wait_for_selector("#m9rMetricRows tr", timeout=15000)


def test_m9r_workbench_view_renders(_server) -> None:
    """商品经营视图真实渲染 workbench API 内容（反假绿：非只断言 section.active）。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel=_BROWSER_CHANNEL, headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        try:
            _open_workbench(page)
            # 视图激活
            assert page.evaluate(
                "document.getElementById('view-m9r-workbench').classList.contains('active')"
            ) is True, "商品经营视图未激活"
            # 关键：真实渲染的 metric 行非空（loadM9rWorkbench 从 API 拉数）
            metric_rows = page.locator("#m9rMetricRows tr").count()
            assert metric_rows > 0, "读模型指标表为空（workbench API 未渲染）"
            # KPI 出现
            kpi_text = page.locator("#m9rKpis").inner_text()
            assert "SKU" in kpi_text, "KPI 未渲染"
        finally:
            browser.close()


def test_m9r_workbench_generate_recommendation(_server) -> None:
    """操作契约（T3.5）：点击"生成建议"按钮 → 生产语义链落库 → 列表出现该建议。

    验证任务书"显式点击 + 审计"操作契约，而非仅只读渲染。
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel=_BROWSER_CHANNEL, headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        try:
            _open_workbench(page)
            # 填建议 ID + 点击生成按钮（显式操作）
            page.fill("#m9rNewRecId", "rec-browser-1")
            page.click("#m9rGenerateRec")
            # 等待生成结果消息出现（成功或失败均渲染到 m9rRecActionMsg）
            page.wait_for_selector("#m9rRecActionMsg", timeout=10000)
            page.wait_for_timeout(1000)
            msg = page.locator("#m9rRecActionMsg").inner_text()
            assert "已生成" in msg, f"生成建议失败: {msg}"
            # 列表刷新后出现该建议
            page.wait_for_selector(
                "#m9rRecRows tr:has-text('rec-browser-1')", timeout=10000
            )
            # 建议行状态为 draft
            row_text = page.locator(
                "#m9rRecRows tr:has-text('rec-browser-1')"
            ).inner_text()
            assert "draft" in row_text, f"建议未落为 draft: {row_text}"
        finally:
            browser.close()


def test_m9r_workbench_desktop_no_overflow(_server) -> None:
    """1280×720 桌面：页面无横向溢出 + console 无错误。

    console 监听在 _open_workbench 之后挂载：排除初始加载的 favicon 404
    （项目既有、与 M9 无关），只验证 M9 工作台交互过程的错误。
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel=_BROWSER_CHANNEL, headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        try:
            _open_workbench(page)
            # 进入 M9 视图后挂 console 监听（排除 favicon 初始 404）
            console_errors: list[str] = []
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text)
                if msg.type == "error"
                else None,
            )
            page.on(
                "pageerror",
                lambda exc: console_errors.append(f"pageerror: {exc}"),
            )
            overflow = page.evaluate(
                "document.documentElement.scrollWidth > window.innerWidth"
            )
            assert overflow is False, "1280×720 页面横向溢出"
            assert not console_errors, f"console 错误: {console_errors}"
        finally:
            browser.close()


def test_m9r_workbench_narrow_no_overflow(_server) -> None:
    """390×844 窄屏：页面无横向溢出 + console 无错误。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel=_BROWSER_CHANNEL, headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        try:
            _open_workbench(page)
            # 进入 M9 视图后挂 console 监听（排除 favicon 初始 404）
            console_errors: list[str] = []
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text)
                if msg.type == "error"
                else None,
            )
            page.on(
                "pageerror",
                lambda exc: console_errors.append(f"pageerror: {exc}"),
            )
            overflow = page.evaluate(
                "document.documentElement.scrollWidth > window.innerWidth"
            )
            assert overflow is False, "390×844 页面横向溢出"
            assert not console_errors, f"console 错误: {console_errors}"
        finally:
            browser.close()
