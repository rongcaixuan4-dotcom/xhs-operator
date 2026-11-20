# -*- coding: utf-8 -*-
"""QA 边界攻击测试（发布就绪度终审 · Round 独立增补）。

覆盖既有 75 用例之外的攻击面：
- hybrid 降级：HTTP 403/461（风控）/ 网络超时 / 畸形 JSON / 空列表 → 必须落到浏览器通道；
- READ_ENDPOINTS 白名单：非白名单（写）uri 必须被拒绝；
- feed POST 只承载读语义（source_note_id 查询），payload 无写字段；
- RateLimiter：并发调用下间隔仍生效；0 / 负值注入时钟边界不崩溃；
- export_login_state：优雅行为 + Cookie 只进内存不落盘日志；
- 配置链路：collect_read_mode 非法值经 /api/settings/app PUT 必须被校验拒绝（1400）。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from sqlmodel import Session

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db import get_engine, set_app_setting
from app.engines import collect as collect_mod
from app.engines.xhs_api import (
    ApiChannelError,
    RateLimiter,
    READ_ENDPOINTS,
    XhsApiChannel,
)
from app.engines.browser import BrowserManager
from app.models import Topic

COOKIE = "a1=test_a1_value; web_session=test_web_session_value"


def _mk_channel(handler, **kwargs) -> XhsApiChannel:
    return XhsApiChannel(
        cookie_str=COOKIE,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) TestUA",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


class TestHybridFallbackAttack(unittest.TestCase):
    """hybrid 降级路径攻击：各类 API 通道失败必须真的落到浏览器通道。"""

    def _mk_topic(self) -> Topic:
        with Session(get_engine()) as session:
            topic = Topic(account_id=1, keyword="攻击测试词", daily_quota=3)
            session.add(topic)
            session.commit()
            session.refresh(topic)
            return topic

    def _set_mode(self, mode: str) -> None:
        with Session(get_engine()) as session:
            set_app_setting(session, "collect_read_mode", mode)

    def _run_hybrid(self, api_side_effect, browser_return=7):
        topic = self._mk_topic()
        self._set_mode("hybrid")
        with patch.object(collect_mod, "fetch_login_state",
                          new=AsyncMock(return_value=(COOKIE, "TestUA"))), \
             patch.object(collect_mod.XhsApiChannel, "search_notes",
                          new=AsyncMock(side_effect=api_side_effect)), \
             patch.object(collect_mod, "_collect_topic_browser",
                          new=AsyncMock(return_value=browser_return)) as mock_browser:
            added = asyncio.run(collect_mod.collect_topic(topic))
        mock_browser.assert_awaited_once()
        return added

    def test_http_403_falls_back_to_browser(self) -> None:
        """403 风控：channel 层抛 ApiChannelError（可见），hybrid 层回退浏览器。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="forbidden")

        async def run() -> None:
            channel = _mk_channel(handler)
            try:
                await channel.search_notes("关键词")
            finally:
                await channel.aclose()

        with self.assertRaises(ApiChannelError) as ctx:
            asyncio.run(run())
        self.assertIn("403", str(ctx.exception))
        self._run_hybrid(ApiChannelError("HTTP 403 风控"))

    def test_http_461_falls_back_to_browser(self) -> None:
        """461 风控变体：同 403 处理。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(461, text="risk control")

        async def run() -> None:
            channel = _mk_channel(handler)
            try:
                await channel.search_notes("关键词")
            finally:
                await channel.aclose()

        with self.assertRaises(ApiChannelError):
            asyncio.run(run())
        self._run_hybrid(ApiChannelError("HTTP 461 风控"))

    def test_network_timeout_falls_back_to_browser(self) -> None:
        """网络超时属于「网络失败」，hybrid 设计语义要求回退浏览器。"""
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("simulated timeout", request=request)

        async def run() -> None:
            channel = _mk_channel(handler)
            try:
                await channel.search_notes("关键词")
            finally:
                await channel.aclose()

        # 通道层：网络异常应转译为 ApiChannelError（而非裸 httpx 异常）
        with self.assertRaises(ApiChannelError):
            asyncio.run(run())
        # 调度层：hybrid 必须回退浏览器
        self._run_hybrid(ApiChannelError("网络超时"))

    def test_malformed_json_falls_back_to_browser(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>risk page</html>")

        async def run() -> None:
            channel = _mk_channel(handler)
            try:
                await channel.search_notes("关键词")
            finally:
                await channel.aclose()

        with self.assertRaises(ApiChannelError) as ctx:
            asyncio.run(run())
        self.assertIn("JSON", str(ctx.exception))
        self._run_hybrid(ApiChannelError("畸形 JSON"))

    def test_empty_search_list_falls_back_to_browser(self) -> None:
        """API 返回空列表 → _collect_via_api 判「未解析到有效笔记」→ 回退浏览器。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 0, "success": True, "data": {"items": []}})

        topic = self._mk_topic()
        self._set_mode("hybrid")
        with patch.object(collect_mod, "fetch_login_state",
                          new=AsyncMock(return_value=(COOKIE, "TestUA"))), \
             patch.object(collect_mod.XhsApiChannel, "search_notes",
                          new=AsyncMock(return_value=[])), \
             patch.object(collect_mod, "_collect_topic_browser",
                          new=AsyncMock(return_value=4)) as mock_browser:
            added = asyncio.run(collect_mod.collect_topic(topic))
        self.assertEqual(added, 4)
        mock_browser.assert_awaited_once()


class TestReadEndpointsWhitelist(unittest.TestCase):
    """READ_ENDPOINTS 白名单：非白名单 uri 必须被拒绝，feed POST 只承载读语义。"""

    def test_non_whitelist_get_uri_rejected(self) -> None:
        """BUG-5 修复后：白名单校验为显式抛 ApiChannelError（不依赖 assert，
        python -O 下同样生效）。"""
        async def run() -> None:
            channel = _mk_channel(lambda r: httpx.Response(200, json={"code": 0}))
            try:
                await channel._get("/api/sns/web/v1/note/post", {})
            finally:
                await channel.aclose()

        with self.assertRaises(ApiChannelError):
            asyncio.run(run())

    def test_non_whitelist_post_uri_rejected(self) -> None:
        """BUG-5 修复后：POST 同样显式抛 ApiChannelError。"""
        async def run() -> None:
            channel = _mk_channel(lambda r: httpx.Response(200, json={"code": 0}))
            try:
                await channel._post("/api/sns/web/v1/comment/post", {"content": "x"})
            finally:
                await channel.aclose()

        with self.assertRaises(ApiChannelError):
            asyncio.run(run())

    def test_feed_post_carries_read_only_semantics(self) -> None:
        """feed POST payload 仅含查询语义字段，不得出现写操作字段。"""
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url"] = str(request.url)
            seen["json"] = request.read().decode("utf-8")
            return httpx.Response(200, json={"code": 0, "success": True, "data": {"items": []}})

        async def run() -> None:
            channel = _mk_channel(handler)
            try:
                await channel.note_detail("noteid0000000000000099", "tok")
            finally:
                await channel.aclose()

        asyncio.run(run())
        self.assertEqual(seen["method"], "POST")
        self.assertIn("/api/sns/web/v1/feed", seen["url"])
        body = seen["json"]
        for read_field in ("source_note_id", "xsec_token", "xsec_source"):
            self.assertIn(read_field, body)
        for write_field in ("content", "title", "desc", "comment", "target"):
            self.assertNotIn(f'"{write_field}"', body,
                             f"feed POST 不得携带写语义字段 {write_field}")


class TestRateLimiterBoundaries(unittest.TestCase):
    """RateLimiter 并发与边界：并发间隔仍生效；0/负值不崩溃。"""

    def test_concurrent_waits_still_respect_interval(self) -> None:
        sleeps: list = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            await asyncio.sleep(0)  # 模拟让出事件循环

        limiter = RateLimiter(base_interval=2.5, jitter_ratio=0.0, gap_interval=10.0,
                              random_fn=lambda: 0.0, sleep_fn=fake_sleep)

        async def run() -> None:
            await asyncio.gather(*(limiter.wait() for _ in range(5)))

        asyncio.run(run())
        self.assertEqual(len(sleeps), 4, "首个请求不等待，其余 4 个并发请求都须限速")
        for s in sleeps:
            self.assertGreaterEqual(s, 2.5 - 0.05, f"并发下间隔不得被压缩：{sleeps}")

    def test_zero_and_negative_intervals_do_not_crash(self) -> None:
        sleeps: list = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        async def run() -> None:
            zero = RateLimiter(base_interval=0.0, jitter_ratio=0.0, gap_interval=0.0,
                               random_fn=lambda: 0.0, sleep_fn=fake_sleep)
            await zero.wait()
            await zero.wait(gap=True)
            neg = RateLimiter(base_interval=-1.0, jitter_ratio=0.0, gap_interval=-5.0,
                              random_fn=lambda: 0.0, sleep_fn=fake_sleep)
            await neg.wait()
            await neg.wait(gap=True)

        asyncio.run(run())
        self.assertEqual(sleeps, [], "0/负值间隔不应触发任何睡眠（不崩溃即可）")


class TestExportLoginState(unittest.TestCase):
    """export_login_state：优雅行为 + Cookie 只进内存不落盘日志。"""

    def _fake_context(self, cookies: list[dict], ua: str = "TestUA/1.0"):
        class FakePage:
            def goto(self, *a, **k):
                pass

            def evaluate(self, _expr):
                return ua

        class FakeContext:
            def new_page(self):
                return FakePage()

            def cookies(self, _url):
                return cookies

        return FakeContext()

    def test_cookie_string_built_in_memory_only(self) -> None:
        mgr = BrowserManager()
        fake = self._fake_context([
            {"name": "a1", "value": "v_a1"},
            {"name": "web_session", "value": "v_sess"},
        ])
        with patch.object(mgr, "_ensure_context", return_value=fake):
            cookie_str, user_agent = mgr.export_login_state()
        self.assertEqual(cookie_str, "a1=v_a1; web_session=v_sess")
        self.assertEqual(user_agent, "TestUA/1.0")

    def test_ensure_context_failure_raises_visible_engine_error(self) -> None:
        from app.errors import EngineError

        mgr = BrowserManager()
        with patch.object(mgr, "_ensure_context",
                          side_effect=EngineError("浏览器启动失败：mock")):
            with self.assertRaises(EngineError) as ctx:
                mgr.export_login_state()
        self.assertIn("浏览器启动失败", str(ctx.exception))

    def test_cookie_value_never_logged_by_api_channel(self) -> None:
        """携带 Cookie 发请求后，日志记录中不得出现 Cookie 明文值。"""
        records: list[logging.LogRecord] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        cap = CaptureHandler()
        api_logger = logging.getLogger("app.engines.xhs_api")
        api_logger.addHandler(cap)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 0, "success": True, "data": {"items": []}})

        async def run() -> None:
            channel = _mk_channel(handler)
            try:
                await channel.search_notes("关键词")
                await channel.note_detail("noteid0000000000000001")
            finally:
                await channel.aclose()

        try:
            asyncio.run(run())
        finally:
            api_logger.removeHandler(cap)

        for rec in records:
            self.assertNotIn("test_web_session_value", rec.getMessage(),
                             "日志不得输出 Cookie 明文值")
            self.assertNotIn("v_sess", rec.getMessage())


class TestSettingsApiValidation(unittest.TestCase):
    """配置链路：collect_read_mode 非法值必须被 /api/settings/app 校验拒绝。"""

    def _client(self):
        from httpx import ASGITransport, AsyncClient
        from app.main import app

        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    def test_invalid_mode_rejected_with_1400(self) -> None:
        async def run() -> dict:
            async with self._client() as client:
                resp = await client.put("/api/settings/app", json={"collect_read_mode": "hack"})
                return resp.json()

        body = asyncio.run(run())
        self.assertEqual(body.get("code"), 1400, f"非法模式必须被拒绝：{body}")

    def test_valid_mode_accepted_and_persisted(self) -> None:
        async def run() -> dict:
            async with self._client() as client:
                resp = await client.put("/api/settings/app", json={"collect_read_mode": "api"})
                return resp.json()

        body = asyncio.run(run())
        self.assertEqual(body.get("code"), 0, f"合法模式应被接受：{body}")
        with Session(get_engine()) as session:
            from app.db import get_app_setting
            self.assertEqual(get_app_setting(session, "collect_read_mode", None), "api")


if __name__ == "__main__":
    unittest.main()
