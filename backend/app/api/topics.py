# -*- coding: utf-8 -*-
"""话题 API：CRUD、时段/每日篇数、启停、手动触发采集（R05）。"""
from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import get_current_account_id, get_session
from ..engines.collect import collect_topic
from ..errors import NotFoundError, ok
from ..models import Topic

logger = logging.getLogger("app.api.topics")

router = APIRouter(prefix="/topics")

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _reload_scheduler() -> None:
    """话题变更后重载调度器的采集任务（失败仅记日志，不阻塞 API）。"""
    try:
        from ..scheduler import reload_topic_jobs

        reload_topic_jobs()
    except Exception:  # noqa: BLE001 - 调度器未启动（如测试环境）时跳过
        logger.debug("调度器任务重载跳过（未启动）")


def _dump(row: Topic) -> dict:
    return {
        "id": row.id,
        "keyword": row.keyword,
        "priority_weight": row.priority_weight,
        "daily_quota": row.daily_quota,
        "collect_times": row.collect_time_list(),
        "enabled": row.enabled,
        "created_at": row.created_at.isoformat(),
    }


class TopicPayload(BaseModel):
    """话题创建/更新载荷。"""

    keyword: str = ""
    priority_weight: float = 1.0
    daily_quota: int = 5
    collect_times: list[str] = []
    enabled: bool = True


def _validate_payload(payload: TopicPayload) -> str | None:
    """参数校验，错误时返回 message。"""
    if not payload.keyword.strip():
        return "话题关键词不能为空"
    if not (0.1 <= payload.priority_weight <= 3.0):
        return "priority_weight 取值范围 0.1~3.0"
    if not (1 <= payload.daily_quota <= 50):
        return "daily_quota 取值范围 1~50"
    for t in payload.collect_times:
        if not TIME_RE.match(t):
            return f"采集时刻格式非法：{t}（应为 HH:MM）"
    return None


@router.get("")
async def list_topics(session: Session = Depends(get_session)) -> dict:
    """话题列表。"""
    account_id = get_current_account_id(session)
    rows = session.exec(
        select(Topic).where(Topic.account_id == account_id)
        .order_by(Topic.created_at.desc())  # type: ignore[attr-defined]
    ).all()
    return ok([_dump(r) for r in rows])


@router.post("")
async def create_topic(payload: TopicPayload, session: Session = Depends(get_session)) -> dict:
    """创建话题（采集时段/每日篇数/启停）。"""
    err = _validate_payload(payload)
    if err:
        return {"code": 1400, "data": None, "message": err}
    account_id = get_current_account_id(session)
    row = Topic(
        account_id=account_id,
        keyword=payload.keyword.strip(),
        priority_weight=payload.priority_weight,
        daily_quota=payload.daily_quota,
        collect_times=json.dumps(payload.collect_times or ["09:00", "14:00", "19:00"], ensure_ascii=False),
        enabled=payload.enabled,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    _reload_scheduler()
    logger.info("话题已创建：id=%s keyword=%s", row.id, row.keyword)
    return ok(_dump(row), "话题已创建")


@router.put("/{topic_id}")
async def update_topic(topic_id: int, payload: TopicPayload, session: Session = Depends(get_session)) -> dict:
    """更新话题。"""
    err = _validate_payload(payload)
    if err:
        return {"code": 1400, "data": None, "message": err}
    row = session.get(Topic, topic_id)
    if row is None:
        raise NotFoundError(f"话题不存在：{topic_id}")
    row.keyword = payload.keyword.strip() or row.keyword
    row.priority_weight = payload.priority_weight
    row.daily_quota = payload.daily_quota
    row.collect_times = json.dumps(payload.collect_times, ensure_ascii=False)
    row.enabled = payload.enabled
    session.add(row)
    session.commit()
    _reload_scheduler()
    logger.info("话题已更新：id=%s", topic_id)
    return ok(_dump(row), "话题已保存")


@router.delete("/{topic_id}")
async def delete_topic(topic_id: int, session: Session = Depends(get_session)) -> dict:
    """删除话题（已有采集记录的话题仅停用更安全，这里允许物理删除）。"""
    row = session.get(Topic, topic_id)
    if row is None:
        raise NotFoundError(f"话题不存在：{topic_id}")
    session.delete(row)
    session.commit()
    _reload_scheduler()
    return ok({"id": topic_id}, "话题已删除")


@router.post("/collect-now")
async def collect_now(payload: dict | None = None, session: Session = Depends(get_session)) -> dict:
    """手动触发一轮采集：遍历全部启用话题各采一轮（R05 手动触发）。"""
    account_id = get_current_account_id(session)
    topics = session.exec(
        select(Topic).where(Topic.account_id == account_id, Topic.enabled == True)  # noqa: E712
    ).all()
    if not topics:
        return ok({"results": [], "total_added": 0}, "没有启用中的话题，请先配置话题")
    results = []
    total_added = 0
    for topic in topics:
        try:
            added = await collect_topic(topic)
            results.append({"topic": topic.keyword, "ok": True, "added": added})
            total_added += added
        except Exception as exc:  # noqa: BLE001 - 单话题失败不影响其他话题
            logger.exception("手动采集失败：topic=%s", topic.keyword)
            results.append({"topic": topic.keyword, "ok": False, "added": 0, "error": str(exc)})
    # 采集后顺手评分筛选（有新料才选）
    selected_count = 0
    try:
        from ..services.scoring import select_today_notes

        selected_count = len(select_today_notes(session, account_id=account_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("采集后评分筛选失败（不影响采集结果）：%s", exc)
    return ok(
        {"results": results, "total_added": total_added, "newly_selected": selected_count},
        f"采集完成：新增 {total_added} 篇，新入选 {selected_count} 篇",
    )
