# -*- coding: utf-8 -*-
"""账号 API：小红书登录态查询、发起扫码登录、关闭登录窗口。

登录机制：persistent context（data/profiles/account_1）的登录态长期有效；
「发起登录」在持久化浏览器里打开小红书首页供人工扫码，前端轮询 /status
直到 logged_in=true。本模块只做读态与页面导航，不涉及任何写接口（红线）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from ..engines.browser import (
    check_login_async,
    close_interactive_login_async,
    open_interactive_login_async,
)
from ..errors import ok

logger = logging.getLogger("app.api.account")

router = APIRouter(prefix="/account")


@router.get("/status")
async def login_status() -> dict:
    """查询小红书登录态（前端轮询入口）。"""
    logged_in, message = await check_login_async()
    return ok({"logged_in": bool(logged_in), "message": message})


@router.post("/login/start")
async def login_start() -> dict:
    """打开持久化浏览器窗口并进入小红书首页，供人工扫码登录。"""
    try:
        detail = await open_interactive_login_async()
    except Exception as exc:  # noqa: BLE001 - 引擎错误转可见提示
        logger.exception("发起登录失败")
        return ok({"started": False, "message": f"发起登录失败：{exc}"})
    return ok({"started": True, "message": detail})


@router.post("/login/close")
async def login_close() -> dict:
    """关闭登录窗口（已保存的登录态不受影响）。"""
    await close_interactive_login_async()
    return ok({"closed": True, "message": "登录窗口已关闭；若已扫码，登录态仍然生效"})
