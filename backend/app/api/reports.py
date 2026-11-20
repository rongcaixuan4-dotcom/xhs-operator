# -*- coding: utf-8 -*-
"""复盘 API：周报列表、详情、确认改进项写入知识库（R12）。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import get_current_account_id, get_session
from ..errors import NotFoundError, ok
from ..models import WeeklyReport
from ..services import report_service

logger = logging.getLogger("app.api.reports")

router = APIRouter(prefix="/reports")


def _dump(row: WeeklyReport) -> dict:
    return {
        "id": row.id,
        "week_start": row.week_start.isoformat(),
        "week_end": row.week_end.isoformat(),
        "status": row.status,
        "insights": row.insight_list(),
        "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
        "created_at": row.created_at.isoformat(),
        # 列表页不返回全文 report_md（详情页再取）
    }


@router.get("")
async def list_reports(page: int = 1, page_size: int = 20, session: Session = Depends(get_session)) -> dict:
    """周报列表（分页）。"""
    account_id = get_current_account_id(session)
    rows = session.exec(
        select(WeeklyReport).where(WeeklyReport.account_id == account_id)
        .order_by(WeeklyReport.week_start.desc())  # type: ignore[attr-defined]
    ).all()
    start = (max(page, 1) - 1) * page_size
    return ok({
        "total": len(rows),
        "items": [_dump(r) for r in rows[start:start + page_size]],
    })


@router.get("/{report_id}")
async def report_detail(report_id: int, session: Session = Depends(get_session)) -> dict:
    """周报详情（含 Markdown 全文，前端 marked 渲染）。"""
    row = session.get(WeeklyReport, report_id)
    if row is None:
        raise NotFoundError(f"周报不存在：{report_id}")
    data = _dump(row)
    data["report_md"] = row.report_md
    return ok(data)


class ConfirmPayload(BaseModel):
    """确认载荷：勾选的改进项下标列表。"""

    insight_indexes: list[int]


@router.post("/{report_id}/confirm")
async def confirm(report_id: int, payload: ConfirmPayload, session: Session = Depends(get_session)) -> dict:
    """确认改进项 → 写入知识库（source_type=review，R12 回流）。"""
    result = report_service.confirm_report(session, report_id, payload.insight_indexes)
    return ok(result, f"已确认 {result['confirmed']} 条改进项，知识库新增 {result['chunks_written']} 个切片")


@router.post("/generate-now")
async def generate_now(session: Session = Depends(get_session)) -> dict:
    """手动触发一次周报生成（上周数据）。"""
    account_id = get_current_account_id(session)
    report = await report_service.generate_weekly_report(account_id=account_id)
    return ok(_dump(report), f"周报已生成（{report.week_start} ~ {report.week_end}）")
