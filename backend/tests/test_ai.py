# -*- coding: utf-8 -*-
"""AI 适配层降级逻辑单测（mock httpx，不发真实网络请求）。

覆盖：
1. LLM 成功解析 / HTTP 失败 → AiResult(ok=False)；
2. 生图 edits 失败 → generations 交叉降级成功；
3. 生图全部路由失败 → ok=False 且错误信息完整。

运行：python -m pytest backend/tests/test_ai.py -v
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.ai.image import generate_image  # noqa: E402
from app.ai.llm import AiResult, chat, parse_json_content  # noqa: E402


def make_config(kind: str = "image") -> MagicMock:
    """构造 AiConfig 假对象（绕过数据库）。"""
    cfg = MagicMock()
    cfg.kind = kind
    cfg.base_url = "https://fake.ai/v1"
    cfg.api_key = "sk-test-1234"
    cfg.model = "fake-model"
    cfg.extra_dict.return_value = {"size": "512x512", "fallback_models": ["banana-1"]}
    return cfg


class TestLlm(unittest.TestCase):
    """LLM 适配层测试。"""

    def test_chat_success(self) -> None:
        cfg = make_config("llm")
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = {"choices": [{"message": {"content": "你好，测试成功"}}]}

        async def run() -> AiResult:
            with patch("app.ai.llm.httpx.AsyncClient") as mock_client_cls:
                client = MagicMock()
                client.post = AsyncMock(return_value=fake_resp)
                client.__aenter__ = AsyncMock(return_value=client)
                client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = client
                return await chat("ping", config=cfg)

        result = asyncio.run(run())
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "你好，测试成功")

    def test_chat_http_error(self) -> None:
        cfg = make_config("llm")
        fake_resp = MagicMock(status_code=401, text="unauthorized")

        async def run() -> AiResult:
            with patch("app.ai.llm.httpx.AsyncClient") as mock_client_cls:
                client = MagicMock()
                client.post = AsyncMock(return_value=fake_resp)
                client.__aenter__ = AsyncMock(return_value=client)
                client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = client
                return await chat("ping", config=cfg)

        result = asyncio.run(run())
        self.assertFalse(result.ok)
        self.assertIn("401", result.error or "")

    def test_chat_config_incomplete(self) -> None:
        cfg = make_config("llm")
        cfg.api_key = ""
        result = asyncio.run(chat("ping", config=cfg))
        self.assertFalse(result.ok)
        self.assertIn("不完整", result.error or "")

    def test_parse_json_content_strips_fence(self) -> None:
        text = "```json\n{\"title\": \"a\", \"content\": \"b\"}\n```"
        parsed = parse_json_content(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["title"], "a")


class TestImageFallback(unittest.TestCase):
    """生图双路由降级测试。"""

    def test_edits_fail_fallback_generations(self) -> None:
        """带参考图：edits 路由失败 → generations 交叉降级成功。"""
        cfg = make_config("image")
        fake_img = b"PNGDATA"
        b64 = base64.b64encode(fake_img).decode()

        edits_resp = MagicMock(status_code=500, text="server error")
        gen_resp = MagicMock(status_code=200)
        gen_resp.json.return_value = {"data": [{"b64_json": b64}]}

        async def run() -> AiResult:
            with patch("app.ai.image.httpx.AsyncClient") as mock_client_cls:
                client = MagicMock()
                # 第一次调用是 edits（POST files），第二次是 generations（POST json）
                client.post = AsyncMock(side_effect=[edits_resp, gen_resp])
                client.__aenter__ = AsyncMock(return_value=client)
                client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = client

                # 写一个临时参考图
                ref = Path(BACKEND_DIR / "tests" / "_tmp_ref.png")
                ref.write_bytes(b"REFIMG")
                try:
                    out_dir = BACKEND_DIR / "tests" / "_tmp_out"
                    result = await generate_image(
                        "改绘成水彩风", config=cfg, ref_image_path=str(ref), out_dir=out_dir
                    )
                finally:
                    ref.unlink(missing_ok=True)
                return result

        result = asyncio.run(run())
        self.assertTrue(result.ok, f"期望降级成功，实际错误：{result.error}")
        self.assertEqual(result.image_bytes, fake_img)
        self.assertIsNotNone(result.image_path)
        # 清理测试输出
        out_dir = BACKEND_DIR / "tests" / "_tmp_out"
        for p in out_dir.glob("*"):
            p.unlink(missing_ok=True)
        out_dir.rmdir()

    def test_all_routes_fail(self) -> None:
        """全部路由失败：ok=False 且错误信息覆盖每个失败路由。"""
        cfg = make_config("image")
        bad_resp = MagicMock(status_code=400, text="bad request")

        async def run() -> AiResult:
            with patch("app.ai.image.httpx.AsyncClient") as mock_client_cls:
                client = MagicMock()
                # generations 主模型 + fallback 模型 共 2 次失败
                client.post = AsyncMock(return_value=bad_resp)
                client.__aenter__ = AsyncMock(return_value=client)
                client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = client
                out_dir = BACKEND_DIR / "tests" / "_tmp_out2"
                return await generate_image("测试", config=cfg, out_dir=out_dir)

        result = asyncio.run(run())
        self.assertFalse(result.ok)
        self.assertIn("generations(fake-model)", result.error or "")
        self.assertIn("generations(banana-1)", result.error or "")
        out_dir = BACKEND_DIR / "tests" / "_tmp_out2"
        if out_dir.exists():
            for p in out_dir.glob("*"):
                p.unlink(missing_ok=True)
            out_dir.rmdir()

    def test_generations_datauri_url(self) -> None:
        """generations 返回 dataURI 形式的 url 也能解析。"""
        cfg = make_config("image")
        fake_img = b"PNG-X"
        datauri = "data:image/png;base64," + base64.b64encode(fake_img).decode()
        gen_resp = MagicMock(status_code=200)
        gen_resp.json.return_value = {"data": [{"url": datauri}]}

        async def run() -> AiResult:
            with patch("app.ai.image.httpx.AsyncClient") as mock_client_cls:
                client = MagicMock()
                client.post = AsyncMock(return_value=gen_resp)
                client.__aenter__ = AsyncMock(return_value=client)
                client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = client
                out_dir = BACKEND_DIR / "tests" / "_tmp_out3"
                return await generate_image("测试", config=cfg, out_dir=out_dir)

        result = asyncio.run(run())
        self.assertTrue(result.ok)
        self.assertEqual(result.image_bytes, fake_img)
        out_dir = BACKEND_DIR / "tests" / "_tmp_out3"
        for p in out_dir.glob("*"):
            p.unlink(missing_ok=True)
        out_dir.rmdir()


if __name__ == "__main__":
    unittest.main()
