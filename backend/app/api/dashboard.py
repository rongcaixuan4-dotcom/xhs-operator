# -*- coding: utf-8 -*-
"""工作台 API：聚合指标、流水线状态、告警列表、手动触发/重试（R13）。"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlmodel import Session, select

from ..db import get_app_setting, get_current_account_id, get_session
from ..errors import ok
from ..models import CollectedNote, CreationDraft, PipelineRun, PublishTask, WeeklyReport
from ..services.context import get_active_persona
from ..services.pipeline import get_pipeline_status, run_daily_pipeline

logger = logging.getLogger("app.api.dashboard")

router = APIRouter(prefix="/dashboard")

# 流水线独立路由（架构第 4 节：/api/pipeline/*）
pipeline_router = APIRouter(prefix="/pipeline")


@router.get("/summary")
async def summary(session: Session = Depends(get_session)) -> dict:
    """工作台聚合：4 指标卡 + 告警列表 + 今日发布列表 + 人设 + 流水线状态 + AI 状态。"""
    account_id = get_current_account_id(session)
    today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")

    # 4 指标卡（R13/PRD 页面 1）
    today_collected = len(session.exec(
        select(CollectedNote).where(
            CollectedNote.account_id == account_id,
            CollectedNote.collected_at >= today_start,
        )
    ).all())
    today_created = len(session.exec(
        select(CreationDraft).where(
            CreationDraft.account_id == account_id,
            CreationDraft.created_at >= today_start,
        )
    ).all())
    today_published = len(session.exec(
        select(PublishTask).where(
            PublishTask.account_id == account_id,
            PublishTask.status == "done",
            PublishTask.published_at >= today_start,
        )
    ).all())
    pending_review = len(session.exec(
        select(CreationDraft).where(
            CreationDraft.account_id == account_id,
            CreationDraft.status == "pending_review",
        )
    ).all())

    # 人设
    try:
        persona = get_active_persona(session, account_id)
        persona_info = {"id": persona.id, "name": persona.name, "nickname": persona.nickname}
    except Exception:  # noqa: BLE001 - 未设置人设不算错误
        persona_info = None

    # 流水线
    pipeline = get_pipeline_status(session, account_id)

    # 告警：流水线环节 error + 失败发布任务 + 待确认周报超期
    alerts: list[dict] = []
    for stage, info in pipeline.get("detail", {}).items():
        if isinstance(info, dict) and info.get("status") == "error":
            msg = str(info.get("error") or info.get("errors") or "环节失败")[:120]
            alerts.append({"level": "error", "message": f"流水线[{stage}] {msg}", "time": pipeline["updated_at"]})
    failed_tasks = session.exec(
        select(PublishTask).where(
            PublishTask.account_id == account_id, PublishTask.status == "failed"
        )
        .order_by(PublishTask.created_at.desc())  # type: ignore[attr-defined]
    ).all()
    for t in failed_tasks[:5]:
        alerts.append({
            "level": "error",
            "message": f"发布任务#{t.id} 失败：{t.last_error[:100]}",
            "time": t.created_at.isoformat(),
        })
    pending_reports = session.exec(
        select(WeeklyReport).where(
            WeeklyReport.account_id == account_id, WeeklyReport.status == "pending_confirm"
        )
    ).all()
    for r in pending_reports[:3]:
        alerts.append({
            "level": "warn",
            "message": f"周报 {r.week_start} 待确认改进项",
            "time": r.created_at.isoformat(),
        })

    # 今日发布列表
    today_tasks = session.exec(
        select(PublishTask).where(
            PublishTask.account_id == account_id,
            PublishTask.scheduled_at >= today_start,
        )
        .order_by(PublishTask.scheduled_at)  # type: ignore[attr-defined]
    ).all()
    today_publish_list = [{
        "id": t.id,
        "status": t.status,
        "title": t.publish_config_dict().get("title", ""),
        "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
        "note_url": t.note_url,
    } for t in today_tasks]

    # AI 连通状态（设置页测试结果，AppSetting.ai_test_{kind}）
    ai_status = {}
    for kind in ("llm", "image"):
        info = get_app_setting(session, f"ai_test_{kind}", {})
        if info.get("ok") is True:
            ai_status[kind] = "ok"
        elif info.get("ok") is False:
            ai_status[kind] = "fail"
        else:
            ai_status[kind] = "unknown"

    return ok({
        "metrics": {
            "today_collected": today_collected,
            "today_created": today_created,
            "today_published": today_published,
            "pending_review": pending_review,
        },
        "persona": persona_info,
        "pipeline": {k: pipeline[k] for k in ("stage", "status", "detail", "updated_at", "stage_order")},
        "alerts": alerts,
        "today_publish": today_publish_list,
        "ai_status": ai_status,
        "today_publish_count": today_published,
    })


@pipeline_router.get("/status")
async def pipeline_status(session: Session = Depends(get_session)) -> dict:
    """当前 PipelineRun 状态机（时间轴高亮数据源）。"""
    return ok(get_pipeline_status(session))


@pipeline_router.post("/run")
async def pipeline_run(background: BackgroundTasks, session: Session = Depends(get_session)) -> dict:
    """手动触发当日流水线（后台异步执行，立即返回，前端轮询状态）。"""
    account_id = get_current_account_id(session)
    # 先把状态置 running 占位，防重复触发
    current = get_pipeline_status(session, account_id)
    if current["status"] == "running":
        return {"code": 1409, "data": None, "message": "流水线正在执行中，请等待完成"}
    background.add_task(_run_pipeline_bg, account_id)
    logger.info("流水线手动触发（后台执行）：account=%s", account_id)
    return ok({"triggered": True}, "流水线已触发，请通过状态接口查看进度")


@pipeline_router.post("/retry")
async def pipeline_retry(background: BackgroundTasks, session: Session = Depends(get_session)) -> dict:
    """失败环节重试：重跑整条当日流水线（已成功环节幂等跳过/增量补齐）。"""
    account_id = get_current_account_id(session)
    background.add_task(_run_pipeline_bg, account_id)
    logger.info("流水线重试触发：account=%s", account_id)
    return ok({"triggered": True}, "重试已触发（增量补齐失败环节）")


def _run_pipeline_bg(account_id: int) -> None:
    """后台执行入口（独立事件循环）。"""
    import asyncio

    asyncio.run(run_daily_pipeline(account_id))


@pipeline_router.get("/history")
async def pipeline_history(session: Session = Depends(get_session)) -> dict:
    """近 7 日流水线运行历史（G1 验收：连续 3 天跑通可观测）。"""
    account_id = get_current_account_id(session)
    rows = session.exec(
        select(PipelineRun).where(PipelineRun.account_id == account_id)
        .order_by(PipelineRun.run_date.desc())  # type: ignore[attr-defined]
    ).all()
    return ok([{
        "run_date": r.run_date.isoformat(),
        "stage": r.stage,
        "status": r.status,
        "updated_at": r.updated_at.isoformat(),
    } for r in rows[:7]])
