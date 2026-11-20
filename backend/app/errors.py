# -*- coding: utf-8 -*-
"""异常体系 + 统一响应包装（架构第 9 节 #5）。

- EngineError：浏览器/引擎层失败（采集/发布/快照页面操作异常）；
- AiError：AI 适配层失败（LLM/生图全部路由失败）；
- VerifyError：核查失败（发布后内容一致性比对不通过）。

约定：
- engines 抛专属异常 → services 捕获转 PipelineRun.detail / 告警 → api 层兜底转统一响应；
- 禁止裸 `except Exception: pass`——所有失败必须可见（日志 + 状态字段）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("app.errors")


class AppError(Exception):
    """业务异常基类。code 用于前端区分错误类别。"""

    code = 1000
    default_message = "业务异常"

    def __init__(self, message: Optional[str] = None, *, detail: Any = None) -> None:
        self.message = message or self.default_message
        self.detail = detail
        super().__init__(self.message)

    def __str__(self) -> str:  # 便于日志直接输出
        if self.detail is not None:
            return f"{self.message} | detail={self.detail}"
        return self.message


class EngineError(AppError):
    """浏览器引擎层异常（登录态丢失、选择器失效、页面超时等）。"""

    code = 1010
    default_message = "浏览器引擎操作失败"


class AiError(AppError):
    """AI 适配层异常（所有路由均失败、配置缺失、响应解析失败）。"""

    code = 1020
    default_message = "AI 服务调用失败"


class VerifyError(AppError):
    """核查失败（发布后笔记不存在或内容不一致）。"""

    code = 1030
    default_message = "发布结果核查失败"


class NotFoundError(AppError):
    """资源不存在（API 层转 404 语义）。"""

    code = 1040
    default_message = "资源不存在"


# ────────────────────────── 统一响应包装 ──────────────────────────
def ok(data: Any = None, message: str = "ok") -> dict:
    """成功响应：{"code": 0, "data": ..., "message": "ok"}。"""
    return {"code": 0, "data": data, "message": message}


def fail(code: int, message: str, data: Any = None) -> dict:
    """业务失败响应：{"code": !=0, "data": ..., "message": ...}。"""
    return {"code": code, "data": data, "message": message}
