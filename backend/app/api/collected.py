# -*- coding: utf-8 -*-
"""已采集列表 API：双列表（collected/selected）、人工加选/排除（R06，P1 候选池接口预留）。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from ..db import get_current_account_id, get_session
from ..errors import NotFoundError, ok
from ..models import CollectedNote

logger = logging.getLogger("app.api.collected")

router = APIRouter(prefix="/collected")

STATUS_SET = {"collected", "selected", "discarded"}


def _dump(row: CollectedNote) -> dict:
    return {
        "id": row.id,
        "topic_id": row.topic_id,
        "xhs_note_id": row.xhs_note_id,
        "title": row.title,
        "content": row.content[:200],
        "author": row.author,
        "image_urls": row.image_url_list(),
        "local_images": row.local_image_list(),
        "likes": row.likes,
        "collects": row.collects,
        "comments": row.comments,
        "match_score": row.match_score,
        "score_detail": row.score_detail,
        "status": row.status,
        "collected_at": row.collected_at.isoformat(),
    }


@router.get("")
async def list_collected(
    status: str | None = None,
    page: int = 1,
    page_size: int = 30,
    session: Session = Depends(get_session),
) -> dict:
    """已采集列表：status=collected 已采集 / selected 已入选 / discarded 已淘汰。"""
    account_id = get_current_account_id(session)
    stmt = select(CollectedNote).where(CollectedNote.account_id == account_id)
    if status:
        if status not in STATUS_SET:
            return {"code": 1400, "data": None, "message": f"非法 status：{status}"}
        stmt = stmt.where(CollectedNote.status == status)
    stmt = stmt.order_by(CollectedNote.collected_at.desc())  # type: ignore[attr-defined]
    all_rows = session.exec(stmt).all()
    start = (max(page, 1) - 1) * page_size
    return ok({
        "total": len(all_rows),
        "items": [_dump(r) for r in all_rows[start:start + page_size]],
    })


@router.post("/{note_id}/select")
async def select_note(note_id: int, session: Session = Depends(get_session)) -> dict:
    """人工加选（collected → selected）。"""
    row = session.get(CollectedNote, note_id)
    if row is None:
        raise NotFoundError(f"采集笔记不存在：{note_id}")
    row.status = "selected"
    session.add(row)
    session.commit()
    logger.info("人工加选：note_id=%s", note_id)
    return ok({"id": note_id, "status": row.status}, "已加入入选")


@router.post("/{note_id}/discard")
async def discard_note(note_id: int, session: Session = Depends(get_session)) -> dict:
    """人工排除（→ discarded）。"""
    row = session.get(CollectedNote, note_id)
    if row is None:
        raise NotFoundError(f"采集笔记不存在：{note_id}")
    row.status = "discarded"
    session.add(row)
    session.commit()
    logger.info("人工排除：note_id=%s", note_id)
    return ok({"id": note_id, "status": row.status}, "已排除")
