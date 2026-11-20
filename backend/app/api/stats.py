# -*- coding: utf-8 -*-
"""数据看板 API：账号指标卡、趋势折线、Top10、成功率统计、手动快照（R11）。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from ..db import get_current_account_id, get_session
from ..errors import ok
from ..models import AccountSnapshot, NoteStatSnapshot, PublishTask

logger = logging.getLogger("app.api.stats")

router = APIRouter(prefix="/stats")


@router.get("/account")
async def account_stats(session: Session = Depends(get_session)) -> dict:
    """账号指标卡：粉丝数/周曝光/周互动（最新快照）+ 发帖数。"""
    account_id = get_current_account_id(session)
    snap = session.exec(
        select(AccountSnapshot).where(AccountSnapshot.account_id == account_id)
        .order_by(AccountSnapshot.captured_at.desc())  # type: ignore[attr-defined]
    ).first()
    publish_count = len(session.exec(
        select(PublishTask).where(
            PublishTask.account_id == account_id, PublishTask.status == "done"
        )
    ).all())
    return ok({
        "followers": snap.followers if snap else 0,
        "week_exposure": snap.week_exposure if snap else 0,
        "week_interaction": snap.week_interaction if snap else 0,
        "published_notes": publish_count,
        "captured_at": snap.captured_at.isoformat() if snap else None,
    })


@router.get("/trend")
async def trend(days: int = 14, session: Session = Depends(get_session)) -> dict:
    """互动趋势折线数据：按天聚合快照增量（简单口径：当日最大值）。"""
    account_id = get_current_account_id(session)
    days = max(1, min(days, 90))
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
    rows = session.exec(
        select(NoteStatSnapshot).where(
            NoteStatSnapshot.account_id == account_id,
            NoteStatSnapshot.captured_at >= since,
        )
        .order_by(NoteStatSnapshot.captured_at)  # type: ignore[attr-defined]
    ).all()

    # 按天聚合：取当日各指标最大值（避免快照多次叠加重复计数）
    daily: dict[str, dict[str, int]] = {}
    for r in rows:
        key = r.captured_at.strftime("%Y-%m-%d")
        d = daily.setdefault(key, {"exposure": 0, "likes": 0, "collects": 0, "comments": 0})
        d["exposure"] = max(d["exposure"], r.exposure)
        d["likes"] = max(d["likes"], r.likes)
        d["collects"] = max(d["collects"], r.collects)
        d["comments"] = max(d["comments"], r.comments)
    items = [
        {
            "date": k,
            "interaction": v["likes"] + v["collects"] + v["comments"],
            **v,
        }
        for k, v in sorted(daily.items())
    ]
    return ok(items)


@router.get("/top-notes")
async def top_notes(session: Session = Depends(get_session)) -> dict:
    """笔记表现 Top10：取每篇笔记最新一次快照，按互动总量排序。"""
    account_id = get_current_account_id(session)
    rows = session.exec(
        select(NoteStatSnapshot).where(NoteStatSnapshot.account_id == account_id)
        .order_by(NoteStatSnapshot.captured_at.desc())  # type: ignore[attr-defined]
    ).all()
    latest_by_note: dict[str, NoteStatSnapshot] = {}
    for r in rows:
        key = r.xhs_note_id or r.title
        if key not in latest_by_note:
            latest_by_note[key] = r
    ranked = sorted(
        latest_by_note.values(),
        key=lambda r: r.likes + r.collects + r.comments,
        reverse=True,
    )[:10]
    return ok([{
        "xhs_note_id": r.xhs_note_id,
        "title": r.title,
        "exposure": r.exposure,
        "likes": r.likes,
        "collects": r.collects,
        "comments": r.comments,
        "captured_at": r.captured_at.isoformat(),
    } for r in ranked])


@router.get("/publish-success")
async def publish_success(session: Session = Depends(get_session)) -> dict:
    """发布成功率与失败原因统计（R10 可观测）。"""
    account_id = get_current_account_id(session)
    tasks = session.exec(
        select(PublishTask).where(PublishTask.account_id == account_id)
    ).all()
    by_status: dict[str, int] = {}
    fail_reasons: dict[str, int] = {}
    for t in tasks:
        by_status[t.status] = by_status.get(t.status, 0) + 1
        if t.status == "failed" and t.last_error:
            # 取失败原因第一行归组
            key = t.last_error.split("；")[0].split("：")[0][:60]
            fail_reasons[key] = fail_reasons.get(key, 0) + 1
    terminal = by_status.get("done", 0) + by_status.get("failed", 0)
    success_rate = round(by_status.get("done", 0) / terminal, 4) if terminal else None
    return ok({
        "by_status": by_status,
        "success_rate": success_rate,
        "fail_reasons": fail_reasons,
    })


@router.post("/snapshot-now")
async def snapshot_now(session: Session = Depends(get_session)) -> dict:
    """手动触发一次快照（R11）。"""
    from ..engines.snapshot import snapshot_daily

    account_id = get_current_account_id(session)
    result = await snapshot_daily(account_id)
    return ok(result, f"快照完成：新增笔记快照 {result['notes']} 条")
