# -*- coding: utf-8 -*-
"""发布 API：日历视图、任务 CRUD、重试、试发预检、防重复说明（R09/R10）。"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import get_current_account_id, get_session
from ..engines.publish import MAX_RETRY, precheck as engine_precheck, publish_task
from ..errors import NotFoundError, ok
from ..models import CreationDraft, PublishTask

logger = logging.getLogger("app.api.publish")

router = APIRouter(prefix="/publish")

STATUS_SET = {"queued", "publishing", "verifying", "done", "failed", "canceled"}


def _dump(row: PublishTask) -> dict:
    return {
        "id": row.id,
        "draft_id": row.draft_id,
        "status": row.status,
        "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at else None,
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "publish_config": row.publish_config_dict(),
        "dedup_hash": row.dedup_hash,
        "note_url": row.note_url,
        "xhs_note_id": row.xhs_note_id,
        "retry_count": row.retry_count,
        "last_error": row.last_error,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/calendar")
async def calendar(month: str | None = None, session: Session = Depends(get_session)) -> dict:
    """月历视图数据：month=YYYY-MM，返回当月每日任务分组的色块数据（R16 月历，P1 拖拽另议）。"""
    account_id = get_current_account_id(session)
    if not month:
        month = datetime.now().strftime("%Y-%m")
    try:
        year, mon = (int(x) for x in month.split("-"))
    except ValueError:
        return {"code": 1400, "data": None, "message": f"month 格式非法：{month}（应为 YYYY-MM）"}

    start = date(year, mon, 1)
    if mon == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, mon + 1, 1)

    tasks = session.exec(
        select(PublishTask).where(
            PublishTask.account_id == account_id,
            PublishTask.scheduled_at >= f"{start} 00:00:00",
            PublishTask.scheduled_at < f"{end} 00:00:00",
        )
        .order_by(PublishTask.scheduled_at)  # type: ignore[attr-defined]
    ).all()

    days: dict[str, list] = {}
    for t in tasks:
        key = t.scheduled_at.strftime("%Y-%m-%d") if t.scheduled_at else ""
        if key:
            days.setdefault(key, []).append({
                "id": t.id,
                "status": t.status,
                "title": t.publish_config_dict().get("title", ""),
                "cover": t.publish_config_dict().get("cover", ""),
                "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
            })
    return ok({"month": month, "days": days})


class PublishTaskPayload(BaseModel):
    """发布任务创建/更新载荷（全配置项，字段与创作者服务中心一致）。"""

    draft_id: int
    scheduled_at: str | None = None           # ISO 8601；不传则自动取下一个发布时段
    topics: list[str] = []
    location: str = ""
    source_declare: bool = False
    collection: str = ""
    mention_users: list[str] = []
    is_schedule: bool = True


@router.get("/tasks")
async def list_tasks(
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
    session: Session = Depends(get_session),
) -> dict:
    """发布任务列表。"""
    account_id = get_current_account_id(session)
    stmt = select(PublishTask).where(PublishTask.account_id == account_id)
    if status:
        if status not in STATUS_SET:
            return {"code": 1400, "data": None, "message": f"非法 status：{status}"}
        stmt = stmt.where(PublishTask.status == status)
    stmt = stmt.order_by(PublishTask.created_at.desc())  # type: ignore[attr-defined]
    all_rows = session.exec(stmt).all()
    start = (max(page, 1) - 1) * page_size
    return ok({
        "total": len(all_rows),
        "items": [_dump(r) for r in all_rows[start:start + page_size]],
    })


@router.post("/tasks")
async def create_task(payload: PublishTaskPayload, session: Session = Depends(get_session)) -> dict:
    """创建发布任务：由二创稿入队（dedup 查重 + publish_slots 排期）。"""
    draft = session.get(CreationDraft, payload.draft_id)
    if draft is None:
        raise NotFoundError(f"二创稿不存在：{payload.draft_id}")
    if draft.status == "rejected":
        return {"code": 1400, "data": None, "message": "已驳回稿件不可发布"}

    # 覆盖全配置项到 publish_config
    draft.image_paths = json.dumps(draft.image_path_list(), ensure_ascii=False)
    scheduled: Optional[datetime] = None
    if payload.scheduled_at:
        try:
            scheduled = datetime.fromisoformat(payload.scheduled_at)
        except ValueError:
            return {"code": 1400, "data": None, "message": f"scheduled_at 格式非法：{payload.scheduled_at}"}

    from ..services.review import create_publish_task

    task = create_publish_task(session, draft, scheduled_at=scheduled)
    # 应用抽屉里的扩展配置（topics/location 等）
    cfg = task.publish_config_dict()
    cfg.update({
        "topics": payload.topics,
        "location": payload.location,
        "source_declare": payload.source_declare,
        "collection": payload.collection,
        "mention_users": payload.mention_users,
        "is_schedule": payload.is_schedule,
    })
    task.publish_config = json.dumps(cfg, ensure_ascii=False)
    session.add(task)
    session.commit()
    logger.info("发布任务已创建（含扩展配置）：task=%s", task.id)
    return ok(_dump(session.get(PublishTask, task.id)), "发布任务已创建")


@router.put("/tasks/{task_id}")
async def update_task(task_id: int, payload: PublishTaskPayload | None = None,
                      session: Session = Depends(get_session)) -> dict:
    """更新发布任务（排期时间/配置项）。done/failed 任务不可改。"""
    task = session.get(PublishTask, task_id)
    if task is None:
        raise NotFoundError(f"发布任务不存在：{task_id}")
    if task.status in ("done", "failed"):
        return {"code": 1400, "data": None, "message": f"任务已终态（{task.status}）不可修改"}
    if payload is not None:
        if payload.scheduled_at:
            try:
                task.scheduled_at = datetime.fromisoformat(payload.scheduled_at)
            except ValueError:
                return {"code": 1400, "data": None, "message": f"scheduled_at 格式非法：{payload.scheduled_at}"}
        cfg = task.publish_config_dict()
        cfg.update({
            "topics": payload.topics or cfg.get("topics", []),
            "location": payload.location,
            "source_declare": payload.source_declare,
            "collection": payload.collection,
            "mention_users": payload.mention_users or cfg.get("mention_users", []),
            "is_schedule": payload.is_schedule,
        })
        task.publish_config = json.dumps(cfg, ensure_ascii=False)
        session.add(task)
        session.commit()
    return ok(_dump(task), "任务已更新")


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: int, session: Session = Depends(get_session)) -> dict:
    """删除发布任务（实际为 canceled，保留记录便于审计与防重复）。"""
    task = session.get(PublishTask, task_id)
    if task is None:
        raise NotFoundError(f"发布任务不存在：{task_id}")
    if task.status == "done":
        return {"code": 1400, "data": None, "message": "已发布任务不可删除"}
    task.status = "canceled"
    session.add(task)
    session.commit()
    return ok({"id": task_id}, "任务已取消")


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: int, session: Session = Depends(get_session)) -> dict:
    """失败重试（≤3 次，R10）。手动触发立即执行一次。"""
    task = session.get(PublishTask, task_id)
    if task is None:
        raise NotFoundError(f"发布任务不存在：{task_id}")
    if task.status != "failed":
        return {"code": 1400, "data": None, "message": f"仅失败任务可重试（当前 {task.status}）"}
    if task.retry_count >= MAX_RETRY:
        return {"code": 1400, "data": None, "message": f"重试次数已耗尽（{MAX_RETRY}），请检查失败原因后新建任务"}
    final = await publish_task(task_id)
    refreshed = session.get(PublishTask, task_id)
    return ok(_dump(refreshed), f"重试完成，最终状态：{final}")


@router.post("/precheck")
async def precheck_endpoint(session: Session = Depends(get_session)) -> dict:
    """立即试发预检：登录态 + 表单可达性，不提交（R09 抽屉底部按钮）。"""
    result = await engine_precheck()
    return ok(result, "预检完成" if result.get("ok") else f"预检未通过：{result.get('detail')}")


@router.get("/dedup-explain")
async def dedup_explain() -> dict:
    """防重复机制说明（前端抽屉提示文案数据源）。"""
    return ok({
        "hash_rule": "SHA1(标题 + 图片字节指纹)",
        "hard_constraint": "UNIQUE(account_id, dedup_hash) 数据库级防重发",
        "soft_check": "任务创建与发布前双重查重，命中即拒绝并告警",
        "post_check": "提交后打开笔记列表核查；反馈不明确时核查命中才算成功，绝不盲目重试",
        "max_retry": MAX_RETRY,
    })
