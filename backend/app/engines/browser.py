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
import queue as _queue
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
        self._login_page = None          # 交互式扫码登录专用页（保持打开）
        self._account_id = 1             # MVP 单账号（D2）；P1 扩展为多 profile
        # sync Playwright 线程亲和：所有调用必须落在唯一工作线程（见 _worker_loop）
        self._task_q: "_queue.Queue" = _queue.Queue()
        self._worker: Optional[threading.Thread] = None

    # ── 专用工作线程（sync Playwright 线程亲和的解决方案） ──
    def _worker_loop(self) -> None:
        """唯一执行 Playwright 调用的线程：串行消费任务队列，永不退出。"""
        while True:
            fn, args, kwargs, reply = self._task_q.get()
            try:
                reply.put((True, fn(*args, **kwargs)))
            except BaseException as exc:  # noqa: BLE001 - 异常原样回传给等待方
                reply.put((False, exc))

    def _ensure_worker(self) -> None:
        """惰性启动工作线程（线程安全）。"""
        with self._thread_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._worker_loop, daemon=True, name="playwright-worker",
                )
                self._worker.start()
                logger.info("Playwright 专用工作线程已启动")

    def submit(self, fn, *args, **kwargs):
        """在专用工作线程上串行执行 fn（阻塞直到完成，禁止在工作线程内调用）。"""
        self._ensure_worker()
        reply: "_queue.Queue" = _queue.Queue()
        self._task_q.put((fn, args, kwargs, reply))
        done, val = reply.get()
        if not done:
            raise val
        return val

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
            if self._login_page is not None:
                self._login_page = None
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

    # ── 交互式扫码登录 ──
    def open_interactive_login(self) -> str:
        """打开专用页面到小红书首页供人工扫码（sync，须在线程中调用）。

        页面保持打开（不进租借池、不自动关闭），扫码后登录态写入 persistent
        profile 长期有效。重复调用会复用已开的登录页并前置窗口。
        """
        context = self._ensure_context()
        if self._login_page is not None:
            try:
                self._login_page.goto(XHS_HOME, wait_until="domcontentloaded")
                self._login_page.bring_to_front()
                return "登录窗口已存在，已前置并回到小红书首页；请在窗口内扫码"
            except Exception:  # noqa: BLE001 - 旧页面可能已被人工关闭
                self._login_page = None
        self._login_page = context.new_page()
        self._login_page.goto(XHS_HOME, wait_until="domcontentloaded")
        self._login_page.bring_to_front()
        logger.info("交互式登录页已打开（保持常驻，等待人工扫码）")
        return "浏览器窗口已打开小红书首页：请点击页面右上角「登录」，用小红书 App 扫码确认；登录态会长期保存，无需重复登录"

    def close_interactive_login(self) -> None:
        """关闭交互式登录页（登录态已存 profile，关闭不影响已登录状态）。"""
        if self._login_page is not None:
            try:
                self._login_page.close()
            except Exception:  # noqa: BLE001
                logger.exception("登录页关闭异常（忽略）")
            self._login_page = None
            logger.info("交互式登录页已关闭")

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

    # ── 登录态 Cookie 导出（API 只读通道专用） ──
    def export_login_state(self) -> tuple[str, str]:
        """导出小红书 Cookie 串与 User-Agent（sync，须在线程中调用）。

        只读导出 persistent context 的 Cookie（含 a1 / web_session），供签名直连
        **只读** API 通道（engines/xhs_api.py）使用；本方法不做任何写操作，
        导出的 Cookie 绝不用于变更类（写）接口——写操作一律真浏览器（红线）。

        Returns:
            (cookie_str, user_agent)。UA 获取失败时返回空串（调用方回退默认 UA）。
        """
        context = self._ensure_context()
        page = context.new_page()
        user_agent = ""
        try:
            page.goto(XHS_HOME, wait_until="domcontentloaded")
            time.sleep(1)
            user_agent = page.evaluate("navigator.userAgent") or ""
        except Exception as exc:  # noqa: BLE001 - UA 拿不到不影响 Cookie 导出
            logger.warning("UA 导出失败（回退默认 UA）：%s", exc)
        finally:
            self.release_page(page)
        cookies = context.cookies("https://www.xiaohongshu.com")
        cookie_str = "; ".join(
            f"{c.get('name')}={c.get('value')}" for c in cookies if c.get("name")
        )
        logger.info("登录态 Cookie 导出完成：cookie 项数=%s", len(cookies))
        return cookie_str, user_agent

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


async def bp_call(fn, *args, **kwargs):
    """引擎层统一入口：把任意 Playwright 对象调用派发到专用工作线程。

    sync Playwright 有线程亲和（greenlet 绑定创建线程），跨线程调用会
    TargetClosedError——所以**所有** Playwright 调用（含 page.goto/fill 等
    页面对象方法）都必须经此函数，禁止直接 asyncio.to_thread。
    """
    return await asyncio.to_thread(get_browser_manager().submit, fn, *args, **kwargs)


async def acquire_page():
    """异步租借页面（经专用工作线程）。"""
    return await bp_call(get_browser_manager().acquire_page)


async def release_page(page) -> None:
    """异步归还页面。"""
    await bp_call(get_browser_manager().release_page, page)


async def check_login_async() -> tuple[bool, str]:
    """异步登录态检查。"""
    return await bp_call(get_browser_manager().check_login)


async def export_login_state_async() -> tuple[str, str]:
    """异步导出小红书 Cookie 串与 UA（API 只读通道专用）。"""
    return await bp_call(get_browser_manager().export_login_state)


async def open_interactive_login_async() -> str:
    """异步打开交互式扫码登录页。"""
    return await bp_call(get_browser_manager().open_interactive_login)


async def close_interactive_login_async() -> None:
    """异步关闭交互式登录页。"""
    await bp_call(get_browser_manager().close_interactive_login)


async def screenshot_async(page, name: str) -> str:
    """异步截图留痕。"""
    return await bp_call(get_browser_manager().screenshot, page, name)
