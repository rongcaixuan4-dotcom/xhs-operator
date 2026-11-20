# -*- coding: utf-8 -*-
"""FastAPI 入口：lifespan(init_db → 启动 scheduler → 预热 BrowserManager)，
挂载 api routers 与前端静态资源。

安全红线（全项目）：不逆向小红书签名；不导出 Cookie 直连写接口；发布一律真浏览器。
"""
from __future__ import annotations

import logging
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import init_db
from .errors import AppError, fail
from .api import api_router

logger = logging.getLogger("app.main")


def setup_logging() -> None:
    """按架构第 9 节 #4：文件输出 logs/app.log（RotatingFileHandler 5MB×5）+ 控制台。"""
    settings = get_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_dir / "app.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：初始化 DB → 启动调度器（浏览器进程懒启动，不用预热）。"""
    setup_logging()
    logger.info("应用启动：初始化数据库...")
    init_db()

    from .scheduler import start_scheduler, stop_scheduler

    start_scheduler()
    logger.info("应用就绪")
    try:
        yield
    finally:
        stop_scheduler()
        logger.info("应用已停止")


app = FastAPI(title="XHS 运营助手", version="0.1.0", lifespan=lifespan)


# ── 异常兜底：engines/ai 抛出的异常统一转 {code!=0, message}，错误不吞 ──
@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    logger.error("业务异常 [%s]: %s", type(exc).__name__, exc)
    return JSONResponse(status_code=200, content=fail(exc.code, exc.message))


@app.exception_handler(Exception)
async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理异常")
    return JSONResponse(status_code=200, content=fail(5000, f"服务器内部错误：{exc}"))


# ── API 路由 ──
app.include_router(api_router)


# ── 前端静态资源：优先挂载构建产物；无产物时提供引导页 ──
_settings = get_settings()
if (_settings.frontend_dist / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(_settings.frontend_dist), html=True), name="frontend")
else:
    @app.get("/", include_in_schema=False)
    async def index_fallback() -> FileResponse:
        """未构建前端时的引导页（提示先 npm build 或使用 vite dev）。"""
        tip = (
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2>XHS 运营助手后端已启动 ✓</h2>"
            "<p>前端工作台尚未构建。请执行：</p>"
            "<pre>cd frontend\\nnpm install\\nnpm run build</pre>"
            "<p>或开发模式：<pre>cd frontend && npm run dev</pre>（已代理 /api 到本服务）</p>"
            "</body></html>"
        )
        return JSONResponse(content={"html": tip}, status_code=200)
