"""M4 ① 流式实跑 —— 浏览器实跑截图脚本（Playwright + Edge headless）。

流程：
1. 打开 /admin 管理台
2. 切到「智能客服」页签
3. 输入"晴川 AF5 空气炸锅保修多久？"并发送
4. 等待流式回复渲染完成
5. 截取整个对话工作台画面作为实跑证据
"""
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8080"
OUT = "docs/works/13-feature-m4-customer-service/m4-browser-evidence.png"
MSG = "晴川 AF5 空气炸锅保修多久？"
ADMIN_ID = "local-admin"
ADMIN_KEY = "local-admin-key-change-me"

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 960})
    # 打开管理台
    page.goto(f"{BASE}/admin", wait_until="networkidle")
    page.wait_for_timeout(1500)
    # 登录管理台（如果出现登录遮罩）
    login = page.query_selector("#loginOverlay")
    if login and login.is_visible():
        page.fill("#adminId", ADMIN_ID)
        page.fill("#adminKey", ADMIN_KEY)
        page.click('button[type="submit"]:has-text("登录控制台")')
        page.wait_for_timeout(1500)
    # 切到智能客服页签
    try:
        page.click('button[data-view="service"]')
    except Exception:
        pass
    page.wait_for_timeout(1000)
    # 在顾客消息框输入问题
    page.fill("#chatMessage", MSG)
    page.wait_for_timeout(300)
    # 点击发送
    page.click("#sendChat")
    # 等待流式回复渲染（delta 逐段出现，轮询等待消息区非空且稳定）
    result_sel = "#chatResult"
    for _ in range(60):  # 最长 60 秒
        page.wait_for_timeout(1000)
        try:
            text = page.inner_text(result_sel)
            if len(text) > 20 and not text.rstrip().endswith(("…", "…")):
                # 连续两次内容相同则视为流式结束
                if getattr(page, "_last_text", "") == text:
                    break
                page._last_text = text
        except Exception:
            continue
    page.wait_for_timeout(1500)
    # 截图智能客服工作台区域
    page.screenshot(path=OUT, full_page=False)
    # 同时记录页面上的最终对话文本作为旁证
    text = page.inner_text(result_sel)
    with open(
        "docs/works/13-feature-m4-customer-service/m4-browser-text.txt",
        "w",
        encoding="utf-8",
    ) as f:
        f.write(f"输入: {MSG}\n\n回复:\n{text}\n")
    browser.close()
    print("截图已保存:", OUT)
    print("对话文本已保存，回复长度:", len(text))
