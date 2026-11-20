# -*- coding: utf-8 -*-
"""安全红线静态复查（全项目约定）：不逆向签名 / 不导出 Cookie 直连写接口 / 发布一律真浏览器。

作为 pytest 用例固化，防回归：后续提交若引入违规模式将直接测试失败。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

APP_DIR = BACKEND_DIR / "app"

# 禁止出现的模式：签名逆向 / 内部签名算法 / Cookie 直连写接口痕迹
FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    ("签名头 x-s-common", r"x-s-common"),
    ("签名头 X-s", r"[xX]-[sS]ign"),
    ("本地签名函数", r"_webmsxyw|mn_qv|window\._webms|b1\s*=|compute.*xhs.*sign"),
    ("XHS 内部写接口", r"/api/sns/web/v1/(note|comment|like|collect)"),
    ("创作者中心私有接口", r"edith|web_api/sns|/web/creator"),
    ("显式 Cookie 头直连", r"[cC]ookie\s*[:=]\s*['\"]web_session|set_cookie\("),
]


def iter_app_sources():
    yield from sorted(APP_DIR.rglob("*.py"))


class TestSecurityRedline(unittest.TestCase):
    """红线 grep 复查。"""

    def test_no_signature_reverse_or_cookie_direct_write(self) -> None:
        violations: list[str] = []
        for path in iter_app_sources():
            text = path.read_text(encoding="utf-8", errors="replace")
            # 排除合规声明行（"不逆向 X-Sign" 这类红线说明本身合法）
            code_lines = [
                ln for ln in text.splitlines()
                if not any(kw in ln for kw in ("不逆向", "禁止", "红线", "安全"))
            ]
            code_text = "\n".join(code_lines)
            for label, pattern in FORBIDDEN_PATTERNS:
                if re.search(pattern, code_text, flags=re.IGNORECASE):
                    violations.append(f"{path.name}: 命中 {label}（{pattern}）")
        self.assertEqual(violations, [], "安全红线违规：\n" + "\n".join(violations))

    def test_publish_engine_uses_real_browser(self) -> None:
        """发布引擎必须走 Playwright 真浏览器，禁止 httpx 直连提交。"""
        publish_src = (APP_DIR / "engines" / "publish.py").read_text(encoding="utf-8")
        self.assertIn("acquire_page", publish_src, "发布引擎应使用真浏览器页面")
        self.assertNotIn("import httpx", publish_src, "发布引擎不得用 httpx 直连")
        # 核心交互必须经由 page 对象（DOM 操作）
        self.assertRegex(publish_src, r"page\.query_selector")

    def test_collect_engine_uses_real_browser_for_content(self) -> None:
        """采集正文必须走真浏览器 DOM；httpx 仅允许用于下载公开图片。"""
        collect_src = (APP_DIR / "engines" / "collect.py").read_text(encoding="utf-8")
        self.assertIn("acquire_page", collect_src)
        httpx_usage = re.findall(r"async with httpx\.AsyncClient.*", collect_src)
        for line in httpx_usage:
            # 用法必须处于 _download_images 函数体内（公开图片下载）
            fn_start = collect_src.rfind("def _download_images")
            usage_pos = collect_src.find(line)
            self.assertGreater(usage_pos, fn_start,
                               "httpx 只允许出现在 _download_images（公开图片下载）中")

    def test_ai_module_has_no_xhs_endpoints(self) -> None:
        """AI 适配层只允许调用配置的 OpenAI 兼容网关，不含小红书域名。"""
        for name in ("llm.py", "image.py"):
            src = (APP_DIR / "ai" / name).read_text(encoding="utf-8")
            self.assertNotIn("xiaohongshu", src, f"ai/{name} 不应出现小红书域名")


class TestAppStartupImport(unittest.TestCase):
    """应用入口可导入（启动自检）。"""

    def test_main_module_imports_cleanly(self) -> None:
        import importlib

        mod = importlib.import_module("app.main")
        self.assertIsNotNone(mod.app, "FastAPI app 实例应可创建")


if __name__ == "__main__":
    unittest.main()
