# -*- coding: utf-8 -*-
"""API 路由汇总：统一前缀 /api，包含 /health、/files/{path} 与各资源域 router。

约定（架构第 9 节 #1/#9）：
- 所有响应 {code:0, data, message}；
- data/ 下图片/截图经 /api/files/{path} 静态访问（带路径穿越防护）。
- api/ 不直接调 engines/，跨层只经 services/。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from .account import router as account_router
from .collected import router as collected_router
from .creations import router as creations_router
from .dashboard import pipeline_router, router as dashboard_router
from .knowledge import router as knowledge_router
from .personas import router as personas_router
from .publish import router as publish_router
from .reports import router as reports_router
from .settings import router as settings_router
from .stats import router as stats_router
from .topics import router as topics_router
from ..config import get_settings
from ..errors import fail, ok

logger = logging.getLogger("app.api")

api_router = APIRouter(prefix="/api")

# 各资源域路由（顺序即文档顺序）
api_router.include_router(dashboard_router)
api_router.include_router(pipeline_router)
api_router.include_router(account_router)
api_router.include_router(personas_router)
api_router.include_router(knowledge_router)
api_router.include_router(topics_router)
api_router.include_router(collected_router)
api_router.include_router(creations_router)
api_router.include_router(publish_router)
api_router.include_router(stats_router)
api_router.include_router(reports_router)
api_router.include_router(settings_router)


@api_router.get("/health")
async def health() -> dict:
    """前后端联通检查。"""
    return ok({"status": "up", "service": "xhs-assistant"})


@api_router.get("/files/{file_path:path}")
async def serve_data_file(file_path: str):
    """访问 data/ 目录下的本地文件（图片/截图）。

    防路径穿越：解析后必须仍在 data_dir 内。
    """
    from fastapi.responses import JSONResponse

    settings = get_settings()
    target = (settings.data_dir / file_path).resolve()
    if not str(target).startswith(str(settings.data_dir.resolve())):
        return JSONResponse(status_code=200, content=fail(1404, f"路径非法：{file_path}"))
    if not target.exists() or not target.is_file():
        return JSONResponse(status_code=200, content=fail(1404, f"文件不存在：{file_path}"))
    return FileResponse(str(target))
