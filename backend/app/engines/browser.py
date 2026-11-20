# -*- coding: utf-8 -*-
"""BrowserManager 单例：chromium persistent context、页面租借、登录态检查、截图留痕。

设计（架构 1.1）：
- 全局仅一个 BrowserManager；浏览器进程常驻不销毁 profile；
- 页面通过 acquire/release 租借归还，避免并发开多浏览器；
- Playwright 用 sync API，统一经 asyncio.to_thread 在线程池执行（架构第 9 节 #6）。

安全红线（D3，写进代码注释头）：
- 不逆向 X-Sign/签名算法；
- 不使用 Cookie 直连写接口；
- 发布一律真浏览器。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from ..config import get_settings
from ..errors import EngineError

logger = logging.getLogger("app.engines.browser")

XHS_HOME = "https://www.xiaohongshu.com"
XHS_CREATOR = "https://creator.xiaohongshu.com"

# 登录态判定：小红书首页右上角出现头像/登录用户元素；未登录时出现「登录」按钮
LOGIN_SELECTOR_OK = ".user.side-bar-avatar, .side-bar-avatar, img.user-avatar"
LOGIN_SELECTOR_BAD = "button:has-text('登录'), .login-btn"


class BrowserManager:
    """全局浏览器管理器单例（sync Playwright，放线程池执行）。"""

    _instance: Optional["BrowserManager"] = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> "BrowserManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._playwright = None          # sync_playwright 句柄
        self._context = None             # persistent context
        self._thread_lock = threading.Lock()  # 保护 _context 惰性创建
        self._account_id = 1             # MVP 单账号（D2）；P1 扩展为多 profile

    # ── 生命周期 ──
    def _ensure_context(self):
        """惰性创建 persistent context（线程安全）。"""
        with self._thread_lock:
            if self._context is not None:
                return self._context
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise EngineError("playwright 未安装：pip install playwright && playwright install chromium") from exc

            settings = get_settings()
            profile_dir = settings.profiles_dir / f"account_{self._account_id}"
            profile_dir.mkdir(parents=True, exist_ok=True)
            logger.info("启动浏览器 persistent context：profile=%s", profile_dir)
            self._playwright = sync_playwright().start()
            try:
                self._context = self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=settings.headless,
                    slow_mo=settings.slow_mo_ms,
                    viewport={"width": 1440, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"],  # 常规反检测，不涉及签名
                )
                self._context.set_default_timeout(settings.nav_timeout_seconds * 1000)
                return self._context
            except Exception as exc:
                self._playwright.stop()
                self._playwright = None
                raise EngineError(f"浏览器启动失败：{exc}") from exc

    def stop(self) -> None:
        """关闭浏览器进程（应用退出时调用；profile 保留）。"""
        with self._thread_lock:
            if self._context is not None:
                try:
                    self._context.close()
                except Exception:  # noqa: BLE001 - 退出清理失败仅记日志
                    logger.exception("浏览器关闭异常")
                self._context = None
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:  # noqa: BLE001
                    logger.exception("playwright stop 异常")
                self._playwright = None
            logger.info("浏览器已停止")

    # ── 页面租借 ──
    def acquire_page(self):
        """租借一个新页面（sync，须在线程中调用）。用完必须 release_page。"""
        context = self._ensure_context()
        page = context.new_page()
        logger.debug("页面租借：当前上下文页面数=%s", len(context.pages))
        return page

    def release_page(self, page) -> None:
        """归还页面（关闭页面，浏览器进程与 profile 复用不销毁）。"""
        try:
            page.close()
        except Exception:  # noqa: BLE001
            logger.exception("页面关闭异常（忽略）")

    # ── 登录态检查 ──
    def check_login(self) -> tuple[bool, str]:
        """检查小红书登录态（sync，须在线程中调用）。

        Returns:
            (logged_in, message)。未登录时 message 提示需人工扫码。
        """
        page = self.acquire_page()
        try:
            page.goto(XHS_HOME, wait_until="domcontentloaded")
            time.sleep(2)  # 等前端渲染登录组件
            if page.query_selector(LOGIN_SELECTOR_OK):
                return True, "登录态正常"
            if page.query_selector(LOGIN_SELECTOR_BAD):
                return False, "小红书未登录：请在弹出的浏览器窗口中扫码登录后重试"
            # 元素都不在：以 URL 是否跳转登录页兜底判断
            if "login" in page.url:
                return False, "小红书未登录：页面跳转到登录页，请人工扫码"
            return True, "登录态正常（兜底判定）"
        except Exception as exc:  # noqa: BLE001 - 页面异常也要给出清晰结论
            logger.exception("登录态检查异常")
            return False, f"登录态检查异常：{exc}"
        finally:
            self.release_page(page)

    # ── 截图留痕 ──
    def screenshot(self, page, name: str) -> str:
        """截图存 data/screenshots/，返回 data/ 相对路径（供 PipelineRun.detail/前端查看）。"""
        settings = get_settings()
        out = settings.screenshots_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{name}.png"
        try:
            page.screenshot(path=str(out), full_page=False)
            logger.info("截图留痕：%s", out.name)
            return out.relative_to(settings.data_dir).as_posix()
        except Exception as exc:  # noqa: BLE001
            logger.error("截图失败：name=%s err=%s", name, exc)
            return ""


# ── 异步包装（engines 统一走这些入口，内部自动放线程池） ──
_manager: Optional[BrowserManager] = None


def get_browser_manager() -> BrowserManager:
    """获取 BrowserManager 单例。"""
    global _manager
    if _manager is None:
        _manager = BrowserManager()
    return _manager


async def acquire_page():
    """异步租借页面（asyncio.to_thread 包装 sync API）。"""
    return await asyncio.to_thread(get_browser_manager().acquire_page)


async def release_page(page) -> None:
    """异步归还页面。"""
    await asyncio.to_thread(get_browser_manager().release_page, page)


async def check_login_async() -> tuple[bool, str]:
    """异步登录态检查。"""
    return await asyncio.to_thread(get_browser_manager().check_login)


async def screenshot_async(page, name: str) -> str:
    """异步截图留痕。"""
    return await asyncio.to_thread(get_browser_manager().screenshot, page, name)
