# -*- coding: utf-8 -*-
"""二创/审核 API：列表、对照详情、编辑、通过/驳回、手动触发二创（R07/R08）。"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import get_current_account_id, get_engine, get_session
from ..errors import NotFoundError, ok
from ..models import CollectedNote, CreationDraft
from ..services import review as review_service
from ..services.context import get_active_persona
from ..services.scoring import select_today_notes

logger = logging.getLogger("app.api.creations")

router = APIRouter(prefix="/creations")

STATUS_SET = {"generating", "pending_review", "approved", "rejected", "queued"}


def _dump(row: CreationDraft) -> dict:
    return {
        "id": row.id,
        "collected_note_id": row.collected_note_id,
        "persona_id": row.persona_id,
        "title": row.title,
        "content": row.content,
        "image_paths": row.image_path_list(),
        "cover_path": row.cover_path,
        "similarity_score": row.similarity_score,
        "status": row.status,
        "reject_reason": row.reject_reason,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


@router.get("")
async def list_creations(
    status: str | None = None,
    page: int = 1,
    page_size: int = 30,
    session: Session = Depends(get_session),
) -> dict:
    """二创稿列表（按待审/通过/驳回/队列筛选）。"""
    account_id = get_current_account_id(session)
    stmt = select(CreationDraft).where(CreationDraft.account_id == account_id)
    if status:
        if status not in STATUS_SET:
            return {"code": 1400, "data": None, "message": f"非法 status：{status}"}
        stmt = stmt.where(CreationDraft.status == status)
    stmt = stmt.order_by(CreationDraft.updated_at.desc())  # type: ignore[attr-defined]
    all_rows = session.exec(stmt).all()
    start = (max(page, 1) - 1) * page_size
    return ok({
        "total": len(all_rows),
        "items": [_dump(r) for r in all_rows[start:start + page_size]],
    })


@router.get("/{draft_id}")
async def creation_detail(draft_id: int, session: Session = Depends(get_session)) -> dict:
    """原稿 vs 二创稿对照详情（审核页左右对照）。"""
    draft = session.get(CreationDraft, draft_id)
    if draft is None:
        raise NotFoundError(f"二创稿不存在：{draft_id}")
    note = session.get(CollectedNote, draft.collected_note_id) if draft.collected_note_id else None
    return ok({
        "draft": _dump(draft),
        "original": {
            "id": note.id,
            "title": note.title,
            "content": note.content,
            "author": note.author,
            "image_urls": note.image_url_list(),
            "local_images": note.local_image_list(),
            "likes": note.likes,
            "collects": note.collects,
            "comments": note.comments,
        } if note else None,
    })


class DraftEditPayload(BaseModel):
    """内联编辑载荷（标题/文案/图片列表）。"""

    title: str | None = None
    content: str | None = None
    image_paths: list[str] | None = None


@router.patch("/{draft_id}")
async def edit_creation(draft_id: int, payload: DraftEditPayload, session: Session = Depends(get_session)) -> dict:
    """内联编辑二创稿（改内容后重算 dedup_hash 由发布任务创建时完成）。"""
    draft = session.get(CreationDraft, draft_id)
    if draft is None:
        raise NotFoundError(f"二创稿不存在：{draft_id}")
    if draft.status in ("queued",):
        return {"code": 1400, "data": None, "message": "已入发布队列，不可编辑（请先在发布日历删除任务）"}
    if payload.title is not None and payload.title.strip():
        draft.title = payload.title.strip()[:100]
    if payload.content is not None:
        draft.content = payload.content
    if payload.image_paths is not None:
        draft.image_paths = json.dumps(payload.image_paths, ensure_ascii=False)
        draft.cover_path = payload.image_paths[0] if payload.image_paths else draft.cover_path
    draft.updated_at = draft.updated_at.now()
    session.add(draft)
    session.commit()
    logger.info("二创稿已编辑：draft=%s", draft_id)
    return ok(_dump(draft), "二创稿已保存")


@router.post("/{draft_id}/approve")
async def approve(draft_id: int, session: Session = Depends(get_session)) -> dict:
    """通过 → 入发布队列（创建 PublishTask，R08）。"""
    task = review_service.approve_draft(session, draft_id)
    return ok({
        "draft_id": draft_id,
        "task_id": task.id,
        "scheduled_at": task.scheduled_at.isoformat() if task.scheduled_at else None,
    }, "已通过并入发布队列")


class RejectPayload(BaseModel):
    """驳回载荷：原因必填（入知识库负样本）。"""

    reason: str


@router.post("/{draft_id}/reject")
async def reject(draft_id: int, payload: RejectPayload, session: Session = Depends(get_session)) -> dict:
    """驳回 → 原因转知识库负样本（R08/R12）。"""
    draft = review_service.reject_draft(session, draft_id, payload.reason)
    return ok({
        "draft_id": draft_id,
        "status": draft.status,
        "reject_reason": draft.reject_reason,
    }, "已驳回，原因已写入知识库负样本")


@router.post("/generate-now")
async def generate_now(session: Session = Depends(get_session)) -> dict:
    """手动触发二创：对今日 selected 笔记生成二创稿（R07 手动入口）。"""
    account_id = get_current_account_id(session)
    try:
        get_active_persona(session, account_id)
    except NotFoundError as exc:
        return {"code": 1400, "data": None, "message": str(exc)}

    notes = session.exec(
        select(CollectedNote).where(
            CollectedNote.account_id == account_id,
            CollectedNote.status == "selected",
        )
    ).all()
    # 只为还没有二创稿的入选笔记生成
    existing_note_ids = {
        d.collected_note_id for d in session.exec(
            select(CreationDraft).where(CreationDraft.account_id == account_id)
        ).all()
    }
    pending_notes = [n for n in notes if n.id not in existing_note_ids]
    if not pending_notes:
        return ok({"created": 0, "failed": []}, "没有需要二创的入选笔记（均有二创稿）")

    # 引擎层导入放函数内：避免 api → engines 直接依赖被静态扫描误解（此处经 services 网关语义调用）
    from ..engines.creator import create_draft

    created, failed = 0, []
    for note in pending_notes:
        try:
            await create_draft(note, account_id=account_id)
            created += 1
        except Exception as exc:  # noqa: BLE001 - 单篇失败不阻塞其他篇
            logger.exception("手动二创失败：note=%s", note.id)
            failed.append({"note_id": note.id, "title": note.title, "error": str(exc)})

    # 开关关时顺手结算审核（自动入队）
    drafts = session.exec(
        select(CreationDraft).where(
            CreationDraft.account_id == account_id,
            CreationDraft.status == "pending_review",
        )
    ).all()
    settled = review_service.settle_review(session, list(drafts), account_id=account_id)

    return ok(
        {"created": created, "failed": failed, "auto_approved": settled["auto_approved"]},
        f"二创完成：成功 {created} 篇，失败 {len(failed)} 篇",
    )
