# -*- coding: utf-8 -*-
"""API 直连采集通道单测。

覆盖：
- 签名头存在（x-s / x-t / x-s-common 由真实 xhshow 离线签名生成，不 mock 签名库）；
- 搜索/详情解析结构与浏览器采集 ScrapedNote 对齐；
- hybrid 降级触发（API 失败 → 回退浏览器）、api 模式失败向上抛可见错误；
- 限速间隔生效（相邻请求 ≥2.5s + 抖动、搜索翻页 ≥10s）；
- 无 Cookie 时 API 通道不可用且报错可见；
- 写操作永久红线：xhs_api 模块只允许出现只读白名单接口。
"""
from __future__ import annotations

import asyncio
import re
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
    NoCookieError,
    RateLimiter,
    READ_ENDPOINTS,
    ScrapedNote,
    XhsApiChannel,
    parse_note_detail,
    parse_search_items,
)
from app.errors import EngineError
from app.models import Topic

COOKIE = "a1=test_a1_value; web_session=test_web_session_value"

# 变更类（写）接口片段：绝不允许出现在 xhs_api 模块的任何接口字面量里
WRITE_ENDPOINT_SUFFIXES = ("/note/post", "/comment/post", "/note/like", "/note/collect")


def _mk_channel(handler, **kwargs) -> XhsApiChannel:
    return XhsApiChannel(
        cookie_str=COOKIE,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) TestUA",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


SEARCH_DATA = {
    "items": [
        {
            "id": "noteid0000000000000001",
            "xsec_token": "tok1",
            "note_card": {
                "display_title": "搜索标题一",
                "user": {"nickname": "作者甲"},
                "interact_info": {"liked_count": "1.2万"},
                "cover": {"url": "https://sns-img.example/cover1.jpg"},
                "image_list": [{"url": "https://sns-img.example/img1.jpg"}],
            },
        },
        {
            "id": "noteid0000000000000002",
            "xsec_token": "tok2",
            "note_card": {
                "display_title": "搜索标题二",
                "user": {"nickname": "作者乙"},
                "interact_info": {"liked_count": "345"},
                "cover": {"url": "https://sns-img.example/cover2.jpg"},
            },
        },
    ]
}

FEED_DATA = {
    "items": [
        {
            "id": "noteid0000000000000001",
            "note_card": {
                "note_id": "noteid0000000000000001",
                "title": "详情标题",
                "desc": "这里是笔记正文内容",
                "user": {"nickname": "作者甲"},
                "interact_info": {
                    "liked_count": "1.2万",
                    "collected_count": "3456",
                    "comment_count": "78",
                },
                "image_list": [
                    {"url": "https://sns-img.example/detail1.jpg"},
                    {"url": "https://sns-img.example/detail2.jpg"},
                ],
            },
        }
    ]
}


class TestSignedHeaders(unittest.TestCase):
    """签名头存在性：请求必须携带 xhshow 生成的完整签名头。"""

    def test_get_request_carries_signature_headers(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"code": 0, "success": True, "data": {"items": []}})

        async def run() -> None:
            channel = _mk_channel(handler)
            try:
                await channel.search_notes("测试关键词")
            finally:
                await channel.aclose()

        asyncio.run(run())
        headers = seen["headers"]
        # xhshow 签名头（小写传输）：x-s / x-t / x-s-common 均必须存在
        self.assertIn("x-s", headers)
        self.assertIn("x-t", headers)
        self.assertIn("x-s-common", headers)
        self.assertTrue(headers["x-s"], "x-s 不能为空")
        self.assertTrue(headers["x-s-common"], "x-s-common 不能为空")
        # 登录态 Cookie 随请求携带
        self.assertIn("test_web_session_value", headers.get("cookie", ""))
        # UA 伪装正常浏览器
        self.assertIn("Mozilla/5.0", headers.get("user-agent", ""))
        self.assertIn("edith.xiaohongshu.com", seen["url"])

    def test_post_feed_carries_signature_headers(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"code": 0, "success": True, "data": FEED_DATA})

        async def run() -> None:
            channel = _mk_channel(handler)
            try:
                await channel.note_detail("noteid0000000000000001", "tok1")
            finally:
                await channel.aclose()

        asyncio.run(run())
        self.assertIn("x-s", seen["headers"])
        self.assertIn("edith.xiaohongshu.com/api/sns/web/v1/feed", seen["url"])


class TestParsing(unittest.TestCase):
    """解析结构与浏览器采集 ScrapedNote 对齐。"""

    def test_parse_search_items(self) -> None:
        notes = parse_search_items(SEARCH_DATA)
        self.assertEqual(len(notes), 2)
        first = notes[0]
        self.assertIsInstance(first, ScrapedNote)
        self.assertEqual(first.xhs_note_id, "noteid0000000000000001")
        self.assertEqual(first.title, "搜索标题一")
        self.assertEqual(first.author, "作者甲")
        self.assertEqual(first.likes, 12000)  # "1.2万" → 12000
        self.assertIn("https://sns-img.example/cover1.jpg", first.image_urls)
        self.assertEqual(first.xsec_token, "tok1")

    def test_parse_search_items_skips_invalid(self) -> None:
        data = {"items": [{"id": ""}, "bad", {"id": "noteid0000000000000009",
                "note_card": {"display_title": "标题"}}]}
        notes = parse_search_items(data)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].xhs_note_id, "noteid0000000000000009")

    def test_note_detail_parses_feed(self) -> None:
        note = parse_note_detail(FEED_DATA["items"][0])
        self.assertEqual(note.xhs_note_id, "noteid0000000000000001")
        self.assertEqual(note.title, "详情标题")
        self.assertEqual(note.content, "这里是笔记正文内容")
        self.assertEqual(note.author, "作者甲")
        self.assertEqual(note.likes, 12000)
        self.assertEqual(note.collects, 3456)
        self.assertEqual(note.comments, 78)
        self.assertEqual(note.image_urls,
                         ["https://sns-img.example/detail1.jpg", "https://sns-img.example/detail2.jpg"])

    def test_channel_note_detail_matches_requested_id(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 0, "success": True, "data": FEED_DATA})

        async def run() -> ScrapedNote:
            channel = _mk_channel(handler)
            try:
                return await channel.note_detail("noteid0000000000000001", "tok1")
            finally:
                await channel.aclose()

        note = asyncio.run(run())
        self.assertEqual(note.content, "这里是笔记正文内容")
        self.assertEqual(note.collects, 3456)

    def test_api_error_is_visible(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": -100, "success": False, "msg": "风控拦截"})

        async def run() -> None:
            channel = _mk_channel(handler)
            try:
                await channel.search_notes("关键词")
            finally:
                await channel.aclose()

        with self.assertRaises(ApiChannelError) as ctx:
            asyncio.run(run())
        self.assertIn("风控拦截", str(ctx.exception))


class TestRateLimit(unittest.TestCase):
    """限速间隔生效（注入时钟/睡眠，零耗时验证）。"""

    def _limiter(self, sleeps: list, random_value: float = 0.0) -> RateLimiter:
        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        return RateLimiter(
            base_interval=2.5,
            jitter_ratio=0.35,
            gap_interval=10.0,
            random_fn=lambda: random_value,
            sleep_fn=fake_sleep,
        )

    def test_adjacent_requests_wait_base_interval(self) -> None:
        sleeps: list = []
        limiter = self._limiter(sleeps)

        async def run() -> None:
            await limiter.wait()
            await limiter.wait()
            await limiter.wait()

        asyncio.run(run())
        self.assertEqual(len(sleeps), 2, "首次请求不等待，之后每次请求都要限速")
        self.assertTrue(all(s >= 2.5 - 0.05 for s in sleeps), f"间隔须 ≥2.5s（时钟误差容忍 0.05s）：{sleeps}")

    def test_jitter_is_applied(self) -> None:
        sleeps: list = []
        limiter = self._limiter(sleeps, random_value=1.0)  # 抖动取最大 35%

        async def run() -> None:
            await limiter.wait()
            await limiter.wait()

        asyncio.run(run())
        self.assertTrue(sleeps[0] >= 2.5 * 1.35 - 0.05, f"最大抖动 35% 应生效：{sleeps}")

    def test_page_gap_interval(self) -> None:
        sleeps: list = []
        limiter = self._limiter(sleeps)

        async def run() -> None:
            await limiter.wait()
            await limiter.wait(gap=True)

        asyncio.run(run())
        self.assertTrue(sleeps[-1] >= 10.0 - 0.05, f"搜索翻页间隔须 ≥10s（时钟误差容忍 0.05s）：{sleeps}")

    def test_channel_applies_limiter_between_requests(self) -> None:
        sleeps: list = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        limiter = RateLimiter(
            base_interval=2.5, jitter_ratio=0.35, gap_interval=10.0,
            random_fn=lambda: 0.0, sleep_fn=fake_sleep,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 0, "success": True, "data": FEED_DATA})

        async def run() -> None:
            channel = _mk_channel(handler, rate_limiter=limiter)
            try:
                await channel.note_detail("noteid0000000000000001")
                await channel.note_detail("noteid0000000000000001")
            finally:
                await channel.aclose()

        asyncio.run(run())
        self.assertTrue(len(sleeps) >= 1 and sleeps[0] >= 2.5 - 0.05,
                        f"通道内相邻请求须限速 ≥2.5s：{sleeps}")


class TestNoCookie(unittest.TestCase):
    """无 Cookie 时 API 通道不可用，报错可见。"""

    def test_empty_cookie_raises_visible_error(self) -> None:
        channel = XhsApiChannel(cookie_str="")

        async def run() -> None:
            await channel.search_notes("关键词")

        with self.assertRaises(NoCookieError) as ctx:
            asyncio.run(run())
        self.assertIn("登录", str(ctx.exception), "提示须包含登录指引")
        self.assertIn("API", str(ctx.exception), "提示须说明是 API 通道不可用")


class TestHybridFallback(unittest.TestCase):
    """hybrid 降级：API 失败自动回退浏览器通道。"""

    def _mk_topic(self) -> Topic:
        with Session(get_engine()) as session:
            topic = Topic(account_id=1, keyword="降级测试词", daily_quota=3)
            session.add(topic)
            session.commit()
            session.refresh(topic)
            return topic

    def _set_mode(self, mode: str) -> None:
        with Session(get_engine()) as session:
            set_app_setting(session, "collect_read_mode", mode)

    def test_hybrid_falls_back_to_browser_on_api_error(self) -> None:
        topic = self._mk_topic()
        self._set_mode("hybrid")
        with patch.object(collect_mod, "_collect_via_api",
                          new=AsyncMock(side_effect=ApiChannelError("API 通道模拟失败"))), \
             patch.object(collect_mod, "_collect_topic_browser",
                          new=AsyncMock(return_value=5)) as mock_browser:
            added = asyncio.run(collect_mod.collect_topic(topic))
        self.assertEqual(added, 5)
        mock_browser.assert_awaited_once()

    def test_api_mode_raises_visible_engine_error(self) -> None:
        topic = self._mk_topic()
        self._set_mode("api")
        with patch.object(collect_mod, "_collect_via_api",
                          new=AsyncMock(side_effect=ApiChannelError("API 通道模拟失败"))), \
             patch.object(collect_mod, "_collect_topic_browser",
                          new=AsyncMock(return_value=5)) as mock_browser:
            with self.assertRaises(EngineError) as ctx:
                asyncio.run(collect_mod.collect_topic(topic))
        self.assertIn("API", str(ctx.exception))
        mock_browser.assert_not_awaited()  # api 模式绝不静默回退

    def test_hybrid_no_cookie_falls_back_to_browser(self) -> None:
        topic = self._mk_topic()
        self._set_mode("hybrid")
        with patch.object(collect_mod, "_collect_via_api",
                          new=AsyncMock(side_effect=NoCookieError())), \
             patch.object(collect_mod, "_collect_topic_browser",
                          new=AsyncMock(return_value=3)) as mock_browser:
            added = asyncio.run(collect_mod.collect_topic(topic))
        self.assertEqual(added, 3)
        mock_browser.assert_awaited_once()

    def test_browser_mode_skips_api(self) -> None:
        topic = self._mk_topic()
        self._set_mode("browser")
        with patch.object(collect_mod, "_collect_via_api", new=AsyncMock(return_value=99)) as mock_api, \
             patch.object(collect_mod, "_collect_topic_browser",
                          new=AsyncMock(return_value=2)) as mock_browser:
            added = asyncio.run(collect_mod.collect_topic(topic))
        self.assertEqual(added, 2)
        mock_api.assert_not_awaited()
        mock_browser.assert_awaited_once()


class TestWriteRedline(unittest.TestCase):
    """写操作永久红线：xhs_api 模块只允许出现只读白名单接口。"""

    def test_xhs_api_module_endpoints_are_read_only(self) -> None:
        src = (BACKEND_DIR / "app" / "engines" / "xhs_api.py").read_text(encoding="utf-8")
        literals = set(re.findall(r"['\"](/api/sns/web[^'\"]*)['\"]", src))
        self.assertTrue(literals, "应存在只读接口字面量")
        for literal in literals:
            self.assertIn(literal, READ_ENDPOINTS,
                          f"接口 {literal} 不在只读白名单内")
        for literal in literals:
            for suffix in WRITE_ENDPOINT_SUFFIXES:
                self.assertFalse(literal.endswith(suffix),
                                 f"出现变更类（写）接口字面量：{literal}")

    def test_module_documents_read_only_redline(self) -> None:
        src = (BACKEND_DIR / "app" / "engines" / "xhs_api.py").read_text(encoding="utf-8")
        self.assertIn("只读", src)
        self.assertIn("写操作永久红线", src)


if __name__ == "__main__":
    unittest.main()
