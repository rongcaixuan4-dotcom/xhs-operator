# -*- coding: utf-8 -*-
"""AI 适配层补充边界测试（mock httpx，不发真实请求）+ 配置缺失优雅降级。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.ai.llm import chat, load_llm_config, mask_key, parse_json_content
from app.db import get_engine
from app.errors import AiError


def make_config() -> MagicMock:
    cfg = MagicMock()
    cfg.base_url = "https://fake.ai/v1"
    cfg.api_key = "sk-test-abcd"
    cfg.model = "fake-model"
    cfg.extra_dict.return_value = {"timeout": 30}
    return cfg


def _patched_client(post_result) -> patch:
    """构造 AsyncClient mock 的 patch。

    post_result 为 Exception → side_effect 抛出；否则作为响应返回值。
    （不能统一用 side_effect：MagicMock 可被调用，会被 AsyncMock 当函数执行。）
    """
    client = MagicMock()
    if isinstance(post_result, Exception):
        client.post = AsyncMock(side_effect=post_result)
    else:
        client.post = AsyncMock(return_value=post_result)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return patch("app.ai.llm.httpx.AsyncClient", return_value=client)


class TestKeyMasking(unittest.TestCase):
    def test_mask_normal(self) -> None:
        self.assertEqual(mask_key("sk-1234567890"), "sk-***7890")

    def test_mask_short_key(self) -> None:
        self.assertEqual(mask_key("abc"), "sk-***abc")

    def test_mask_empty(self) -> None:
        self.assertEqual(mask_key(""), "")


class TestLlmErrorPaths(unittest.TestCase):
    """LLM 调用各类失败：ok=False 且错误可见（不抛异常、不吞错）。"""

    def test_network_error(self) -> None:
        async def run():
            with _patched_client(httpx.ConnectError("connection refused")):
                return await chat("ping", config=make_config())

        result = asyncio.run(run())
        self.assertFalse(result.ok)
        self.assertIn("网络异常", result.error or "")

    def test_malformed_response(self) -> None:
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = {"unexpected": True}

        async def run():
            with _patched_client(fake_resp):
                return await chat("ping", config=make_config())

        result = asyncio.run(run())
        self.assertFalse(result.ok)
        self.assertIn("响应结构异常", result.error or "")

    def test_timeout_error(self) -> None:
        async def run():
            with _patched_client(httpx.ReadTimeout("timed out")):
                return await chat("ping", config=make_config())

        result = asyncio.run(run())
        self.assertFalse(result.ok)
        self.assertIn("网络异常", result.error or "")


class TestParseJsonContent(unittest.TestCase):
    def test_plain_json(self) -> None:
        self.assertEqual(parse_json_content('{"a": 1}'), {"a": 1})

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(parse_json_content("完全不是 JSON 的输出"))
        self.assertIsNone(parse_json_content(""))
        self.assertIsNone(parse_json_content(None))

    def test_json_embedded_in_text(self) -> None:
        text = '好的，结果如下：{"title": "T", "content": "C"} 请查收'
        parsed = parse_json_content(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["title"], "T")


class TestConfigMissingGraceful(unittest.TestCase):
    """AI key 缺失/未启用：报错可见（AiError），不崩溃、不静默。

    种子数据默认写入 llm 行（enabled=False、api_key 空）——正是「用户未配置 key」的初始态。
    """

    def test_seeded_config_not_enabled_raises(self) -> None:
        from sqlmodel import Session

        from app.db import get_engine

        with Session(get_engine()) as session:
            with self.assertRaises(AiError) as ctx:
                load_llm_config(session, 1)
            self.assertIn("未启用", str(ctx.exception))

    def test_config_row_missing_raises(self) -> None:
        from sqlmodel import Session, select

        from app.db import get_engine
        from app.models import AiConfig

        with Session(get_engine()) as session:
            for row in session.exec(select(AiConfig)).all():
                session.delete(row)
            session.commit()
            with self.assertRaises(AiError):
                load_llm_config(session, 1)


if __name__ == "__main__":
    unittest.main()
