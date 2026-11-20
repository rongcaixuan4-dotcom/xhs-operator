# -*- coding: utf-8 -*-
"""周复盘服务：周数据汇总 + LLM 生成复盘报告 + 改进项确认回流（R12）。

流程：
1. 每周一定时（scheduler 触发）汇总上周 7 日快照 + 发布记录 + 知识库；
2. LLM 产出 Markdown 报告 + ≥3 条结构化改进项（scope: topic_weight/publish_time/prompt）；
3. WeeklyReport 入库（status=pending_confirm）；
4. 用户勾选改进项确认 → 写 KnowledgeChunk(source_type='review')，
   下次二创提示词经 services/context.get_review_text 注入生效；
5. 超期未确认（>14 天）归档 expired。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from ..ai.llm import parse_json_content
from ..ai.prompts import build_weekly_report_messages
from ..db import get_current_account_id, get_engine
from ..errors import AiError, NotFoundError
from ..knowledge.ingest import import_text
from ..knowledge.retrieve import search_chunks
from ..models import NoteStatSnapshot, PublishTask, WeeklyReport

logger = logging.getLogger("app.services.report")

EXPIRE_DAYS = 14  # 超期未确认归档天数


def _week_range(week_start: date) -> tuple[date, date]:
    """自然周：周一为 week_start，周日为 week_end。"""
    return week_start, week_start + timedelta(days=6)


def last_week_start(today: Optional[date] = None) -> date:
    """取上周一（生成报告的目标周期起点）。"""
    if today is None:
        today = date.today()
    monday = today - timedelta(days=today.weekday())  # 本周一
    return monday - timedelta(days=7)


def _aggregate_week(session: Session, account_id: int, start: date, end: date) -> tuple[str, str]:
    """汇总周数据：返回 (stats_summary_md, top_notes_md)。"""
    since = f"{start} 00:00:00"
    until = f"{end} 23:59:59"

    snaps = session.exec(
        select(NoteStatSnapshot).where(
            NoteStatSnapshot.account_id == account_id,
            NoteStatSnapshot.captured_at >= since,
            NoteStatSnapshot.captured_at <= until,
        )
    ).all()
    total_likes = sum(s.likes for s in snaps)
    total_collects = sum(s.collects for s in snaps)
    total_comments = sum(s.comments for s in snaps)
    note_count = len({s.xhs_note_id or s.title for s in snaps})

    tasks = session.exec(
        select(PublishTask).where(
            PublishTask.account_id == account_id,
            PublishTask.created_at >= since,
            PublishTask.created_at <= until,
        )
    ).all()
    done_count = sum(1 for t in tasks if t.status == "done")
    failed_count = sum(1 for t in tasks if t.status == "failed")

    stats_summary = (
        f"- 快照覆盖笔记数：{note_count}\n"
        f"- 总点赞：{total_likes} / 总收藏：{total_collects} / 总评论：{total_comments}\n"
        f"- 本周发布成功：{done_count} 篇，失败：{failed_count} 篇\n"
        f"- 发布成功率：{(done_count / (done_count + failed_count) * 100):.0f}%"
        if (done_count + failed_count)
        else f"- 快照覆盖笔记数：{note_count}\n- 本周无发布记录"
    )

    # Top3 笔记（按互动总量）
    by_note: dict[str, int] = {}
    for s in snaps:
        key = s.title or s.xhs_note_id
        by_note[key] = by_note.get(key, 0) + s.likes + s.collects + s.comments
    top = sorted(by_note.items(), key=lambda x: x[1], reverse=True)[:3]
    top_notes = "\n".join(
        f"- {title}（互动 {score}）" for title, score in top
    ) or "- （本周无快照数据）"
    return stats_summary, top_notes


async def generate_weekly_report(
    *,
    account_id: Optional[int] = None,
    week_start: Optional[date] = None,
) -> WeeklyReport:
    """生成（或复用）指定周的复盘报告。"""
    if week_start is None:
        week_start = last_week_start()
    if account_id is None:
        with Session(get_engine()) as session:
            account_id = get_current_account_id(session)

    start, end = _week_range(week_start)
    with Session(get_engine()) as session:
        # 同周已有报告：直接返回（UNIQUE(account_id, week_start)）
        existing = session.exec(
            select(WeeklyReport).where(
                WeeklyReport.account_id == account_id, WeeklyReport.week_start == start
            )
        ).first()
        if existing is not None:
            return existing

        stats_summary, top_notes = _aggregate_week(session, account_id, start, end)

        # 知识库参考：检索"复盘 运营"相关切片
        hits = search_chunks(session, account_id=account_id, query="复盘 运营 爆款 发布时间 话题", top_k=3)
        knowledge_text = "\n".join(h.content for h in hits)

        from ..ai.llm import load_llm_config

        config = load_llm_config(session, account_id)

    from ..ai.llm import chat

    messages = build_weekly_report_messages(
        week_start=str(start), week_end=str(end),
        stats_summary=stats_summary, top_notes=top_notes,
        knowledge_text=knowledge_text,
    )
    result = await chat(messages[-1]["content"], config=config, system=messages[0]["content"], json_mode=True)
    if not result.ok:
        raise AiError(f"周报 LLM 调用失败：{result.error}")
    parsed = parse_json_content(result.content or "")
    if not parsed or not parsed.get("report_md"):
        raise AiError("周报输出缺少 report_md 字段")
    insights = parsed.get("insights") or []
    for item in insights:
        item.setdefault("scope", "prompt")
        item.setdefault("content", "")
        item.setdefault("impact", "")
        item["confirmed"] = False

    with Session(get_engine()) as session:
        report = WeeklyReport(
            account_id=account_id,
            week_start=start,
            week_end=end,
            report_md=parsed["report_md"],
            insights=__import__("json").dumps(insights, ensure_ascii=False),
            status="pending_confirm",
        )
        session.add(report)
        session.commit()
        session.refresh(report)
    logger.info("周报已生成：week=%s~%s insights=%s 条", start, end, len(insights))
    return report


def confirm_report(session: Session, report_id: int, insight_indexes: list[int]) -> dict:
    """确认改进项 → 写入知识库（source_type=review），R12 回流闭环。

    Args:
        insight_indexes: 用户勾选的改进项下标（report.insights 数组下标）。

    Returns:
        {"confirmed": n, "chunks_written": n}
    """
    report = session.get(WeeklyReport, report_id)
    if report is None:
        raise NotFoundError(f"周报不存在：{report_id}")
    if report.status != "pending_confirm":
        raise NotFoundError(f"周报状态不可确认：{report.status}")

    insights = report.insight_list()
    chosen = []
    for idx in insight_indexes:
        if 0 <= idx < len(insights):
            insights[idx]["confirmed"] = True
            chosen.append(insights[idx])

    chunks_written = 0
    if chosen:
        content = "\n".join(
            f"[{it.get('scope', '通用')}] {it.get('content', '')}（预期影响：{it.get('impact', '')}）"
            for it in chosen
        )
        rows = import_text(
            session,
            account_id=report.account_id,
            source_title=f"周复盘 {report.week_start}~{report.week_end} 改进项",
            content=content,
            source_type="review",
        )
        chunks_written = len(rows)

    report.insights = __import__("json").dumps(insights, ensure_ascii=False)
    report.status = "confirmed"
    report.confirmed_at = report.created_at.now()
    session.add(report)
    session.commit()
    logger.info("周报已确认：report=%s 改进项=%s 知识切片=%s", report_id, len(chosen), chunks_written)
    return {"confirmed": len(chosen), "chunks_written": chunks_written}


def expire_stale_reports(session: Session) -> int:
    """超期未确认归档 expired（scheduler 周任务附带调用）。"""
    deadline = date.today() - timedelta(days=EXPIRE_DAYS)
    stale = session.exec(
        select(WeeklyReport).where(
            WeeklyReport.status == "pending_confirm",
            WeeklyReport.week_start < deadline,
        )
    ).all()
    for r in stale:
        r.status = "expired"
        session.add(r)
    session.commit()
    if stale:
        logger.info("周报超期归档：%s 份", len(stale))
    return len(stale)


def dump_json(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)
