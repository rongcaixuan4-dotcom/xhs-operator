# -*- coding: utf-8 -*-
"""「XHS 运营助手」启动入口。

职责：
1. 环境自检（Python 版本、依赖可导入性、Playwright chromium 是否安装）；
2. 以 uvicorn 启动 FastAPI 后端（默认 127.0.0.1:8787）；
3. 启动后自动打开系统浏览器进入工作台页面。

安全红线（全项目生效）：
- 绝不逆向小红书签名算法（X-Sign 等）；
- 绝不导出 Cookie 直连写接口；
- 采集/发布一律通过真浏览器（Playwright persistent context）操作页面 DOM。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
import webbrowser
from pathlib import Path

# 保证 `backend/app` 可被导入：把 backend 目录加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

HOST = os.environ.get("XHS_HOST", "127.0.0.1")
PORT = int(os.environ.get("XHS_PORT", "8787"))


def check_environment() -> list[str]:
    """启动前自检，返回提示信息列表（不阻塞启动，仅打印警告）。"""
    warnings: list[str] = []
    if sys.version_info < (3, 11):
        warnings.append(f"当前 Python {sys.version_info.major}.{sys.version_info.minor} 低于要求的 3.11+")

    for module_name in ("fastapi", "sqlmodel", "apscheduler", "httpx", "PIL", "yaml"):
        if importlib.util.find_spec(module_name) is None:
            warnings.append(f"缺少依赖 {module_name}，请先执行：pip install -r backend/requirements.txt")

    if importlib.util.find_spec("playwright") is not None:
        # playwright 已安装，再检查 chromium 内核是否下载过
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401

            with sync_playwright() as p:
                path = p.chromium.executable_path
                if not Path(path).exists():
                    warnings.append("Playwright chromium 未安装，请执行：playwright install chromium")
        except Exception as exc:  # noqa: BLE001 - 自检失败只提示，不阻塞
            warnings.append(f"Playwright 自检异常（不影响启动，联调时处理）：{exc}")
    else:
        warnings.append("缺少依赖 playwright，请先执行：pip install -r backend/requirements.txt")
    return warnings


def open_workbench_when_ready(url: str) -> None:
    """延迟数秒后打开工作台页面（等 uvicorn 就绪）。"""

    def _open() -> None:
        import time

        import httpx

        for _ in range(30):  # 最多等 30 秒
            try:
                httpx.get(f"{url}/api/health", timeout=2.0)
                break
            except Exception:  # noqa: BLE001
                time.sleep(1.0)
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()


def main() -> None:
    print("=" * 60)
    print("「XHS 运营助手」启动中...")
    for w in check_environment():
        print(f"[自检提示] {w}")
    url = f"http://{HOST}:{PORT}/"
    print(f"工作台地址：{url}")
    print("=" * 60)

    open_workbench_when_ready(url)

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
