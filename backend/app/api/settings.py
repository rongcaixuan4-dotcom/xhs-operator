# -*- coding: utf-8 -*-
"""设置 API：AI 双配置读写、连通性测试、审核开关等 AppSetting（R02/R08）。

安全红线：ai_config.api_key 返回时脱敏为 sk-***last4（架构第 9 节 #10）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from ..ai import llm as llm_client
from ..ai import image as image_client
from ..db import get_app_setting, get_session, set_app_setting
from ..errors import NotFoundError, fail, ok
from ..models import AiConfig
from ..ai.prompts import json_dumps_safe

logger = logging.getLogger("app.api.settings")

router = APIRouter(prefix="/settings")

# AppSetting 中允许 API 修改的键白名单
ALLOWED_APP_KEYS = {"review_required", "publish_slots", "daily_min_notes", "collect_read_mode"}

# 采集读取通道合法取值（browser | api | hybrid）
VALID_READ_MODES = ("browser", "api", "hybrid")

# 连通性测试结果落库键（Dashboard 状态灯读取）
AI_TEST_KEY = "ai_test_{kind}"


def _mask(row: AiConfig) -> dict:
    """配置行 → 前端字典（密钥脱敏）。"""
    return {
        "id": row.id,
        "kind": row.kind,
        "base_url": row.base_url,
        "api_key_masked": llm_client.mask_key(row.api_key),
        "has_api_key": bool(row.api_key),
        "model": row.model,
        "extra": row.extra_dict(),
        "enabled": row.enabled,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


class AiConfigPayload(BaseModel):
    """AI 配置更新载荷。api_key 传 None 表示不修改。"""

    base_url: str = ""
    api_key: str | None = None
    model: str = ""
    extra: dict = {}
    enabled: bool = False


def _get_ai_row(session: Session, kind: str) -> AiConfig:
    if kind not in ("llm", "image"):
        raise NotFoundError(f"未知 AI 配置类别：{kind}（应为 llm 或 image）")
    row = session.exec(
        session_query_ai(session, kind)
    ).first()
    if row is None:
        raise NotFoundError(f"AI 配置 {kind} 不存在（应由种子数据创建）")
    return row


def session_query_ai(session: Session, kind: str):
    from sqlmodel import select

    return select(AiConfig).where(AiConfig.kind == kind, AiConfig.account_id == 1)


@router.get("/ai/{kind}")
async def get_ai_config(kind: str, session: Session = Depends(get_session)) -> dict:
    """读取 AI 配置（密钥脱敏）。"""
    row = _get_ai_row(session, kind)
    test_info = get_app_setting(session, AI_TEST_KEY.format(kind=kind), {})
    return ok({"config": _mask(row), "last_test": test_info})


@router.put("/ai/{kind}")
async def put_ai_config(kind: str, payload: AiConfigPayload, session: Session = Depends(get_session)) -> dict:
    """更新 AI 配置（api_key 为 None/空串时保留原值）。"""
    row = _get_ai_row(session, kind)
    row.base_url = payload.base_url.strip() or row.base_url
    if payload.api_key:  # 只有明确传了新 key 才覆盖
        row.api_key = payload.api_key.strip()
    row.model = payload.model.strip()
    row.extra = json_dumps_safe(payload.extra).replace("\n", "").replace("  ", "")
    row.enabled = payload.enabled
    row.updated_at = row.updated_at.now()
    session.add(row)
    session.commit()
    logger.info("AI 配置已更新：kind=%s enabled=%s", kind, row.enabled)
    return ok(_mask(row), "配置已保存")


@router.post("/ai/{kind}/test")
async def test_ai_config(kind: str, session: Session = Depends(get_session)) -> dict:
    """连通性测试（R02）：各返回一次真实调用结果，并把结果落 AppSetting 供状态灯展示。"""
    row = _get_ai_row(session, kind)
    result = None
    if kind == "llm":
        result = await llm_client.chat(
            "请回复一句不超过 20 字的问候语，用于连通性测试。",
            config=row,
            temperature=0.1,
        )
    else:
        # out_dir 不传：image.py 内部默认写入 data/creations/
        result = await image_client.generate_image(
            "一张简洁的测试图：浅粉色背景上有一颗红色爱心，扁平插画风格。",
            config=row,
        )

    test_info = {
        "ok": bool(result.ok),
        "error": result.error,
        "tested_at": row.updated_at.now().isoformat(),
    }
    if kind == "image" and result.ok:
        test_info["image_path"] = result.image_path
    if kind == "llm" and result.ok:
        test_info["reply"] = (result.content or "")[:100]
    set_app_setting(session, AI_TEST_KEY.format(kind=kind), test_info)

    if not result.ok:
        return fail(1020, f"连通性测试失败：{result.error}", {"last_test": test_info})
    return ok({"last_test": test_info}, "连通性测试通过")


class AppSettingPayload(BaseModel):
    """AppSetting 更新载荷（只允许白名单键）。"""

    review_required: bool | None = None
    publish_slots: list[str] | None = None
    daily_min_notes: int | None = None
    collect_read_mode: str | None = None


@router.get("/app")
async def get_app_settings(session: Session = Depends(get_session)) -> dict:
    """读取应用设置（审核开关、发布时段、每日最少入选数、采集通道模式）。"""
    return ok({
        "review_required": get_app_setting(session, "review_required", True),
        "publish_slots": get_app_setting(session, "publish_slots", ["10:00", "15:00", "19:00"]),
        "daily_min_notes": get_app_setting(session, "daily_min_notes", 3),
        "collect_read_mode": get_app_setting(session, "collect_read_mode", "hybrid"),
    })


@router.put("/app")
async def put_app_settings(payload: AppSettingPayload, session: Session = Depends(get_session)) -> dict:
    """更新应用设置（D6：审核开关默认开启，可配置）。"""
    if payload.review_required is not None:
        set_app_setting(session, "review_required", payload.review_required)
    if payload.publish_slots is not None:
        if not isinstance(payload.publish_slots, list) or not all(isinstance(x, str) for x in payload.publish_slots):
            return fail(1400, "publish_slots 必须为字符串数组（如 [\"10:00\",\"15:00\"]）")
        set_app_setting(session, "publish_slots", payload.publish_slots)
    if payload.daily_min_notes is not None:
        if payload.daily_min_notes < 1 or payload.daily_min_notes > 20:
            return fail(1400, "daily_min_notes 取值范围 1~20")
        set_app_setting(session, "daily_min_notes", payload.daily_min_notes)
    if payload.collect_read_mode is not None:
        if payload.collect_read_mode not in VALID_READ_MODES:
            return fail(1400, "collect_read_mode 取值须为 browser | api | hybrid")
        set_app_setting(session, "collect_read_mode", payload.collect_read_mode)
    logger.info(
        "AppSetting 已更新：review_required=%s publish_slots=%s daily_min_notes=%s collect_read_mode=%s",
        payload.review_required, payload.publish_slots, payload.daily_min_notes, payload.collect_read_mode,
    )
    return ok(await get_app_settings(session), "设置已保存")
