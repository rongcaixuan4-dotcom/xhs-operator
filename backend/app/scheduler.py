# -*- coding: utf-8 -*-
"""APScheduler 编排（初版，T4）：话题时段采集触发。

后续任务在此扩展：每日流水线（T9）、每小时快照、每周复盘（T8）、
发布任务到点扫描 / 失败重试扫描（T9）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import get_settings

logger = logging.getLogger("app.scheduler")

_scheduler: AsyncIOScheduler | None = None


def _run_topic_collection(topic_id: int) -> None:
    """话题时段采集任务（独立事件循环执行，供 APScheduler 线程调用）。"""

    async def _job() -> None:
        from sqlmodel import Session

        from .db import get_engine
        from .engines.collect import collect_topic
        from .models import Topic
        from .services.scoring import select_today_notes

        with Session(get_engine()) as session:
            topic = session.get(Topic, topic_id)
            if topic is None or not topic.enabled:
                logger.info("话题已停用/删除，跳过采集：topic_id=%s", topic_id)
                return
            account_id = topic.account_id
        try:
            added = await collect_topic(topic)
            logger.info("定时采集完成：topic=%s 新增=%s", topic.keyword, added)
        except Exception as exc:  # noqa: BLE001 - 失败记录日志，不阻塞调度
            logger.exception("定时采集失败：topic_id=%s err=%s", topic_id, exc)
            return
        # 采集后评分筛选
        try:
            with Session(get_engine()) as session:
                selected = select_today_notes(session, account_id=account_id)
                logger.info("定时评分筛选完成：topic=%s 新入选=%s", topic.keyword, len(selected))
        except Exception as exc:  # noqa: BLE001
            logger.warning("定时评分筛选失败（不影响采集）：%s", exc)

    asyncio.run(_job())


def _collect_all_enabled_topics() -> None:
    """兜底整轮采集：扫描全部启用话题（每日流水线主流程也调用，此处供手动兜底）。"""
    pass  # T9 用 services/pipeline.run_daily_pipeline 替换整轮编排


def start_scheduler() -> None:
    """启动调度器：注册话题采集 cron（每话题按 collect_times）。"""
    global _scheduler
    if _scheduler is not None:
        return
    settings = get_settings()
    _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    from sqlmodel import Session, select

    from .db import get_engine
    from .models import Topic

    with Session(get_engine()) as session:
        topics = session.exec(select(Topic).where(Topic.enabled == True)).all()  # noqa: E712
        for topic in topics:
            for t in topic.collect_time_list():
                hh, mm = t.split(":")
                _scheduler.add_job(
                    _run_topic_collection,
                    CronTrigger(hour=int(hh), minute=int(mm)),
                    args=[topic.id],
                    id=f"collect_topic_{topic.id}_{t}",
                    replace_existing=True,
                    misfire_grace_time=600,
                )
                logger.info("注册采集任务：topic=%s time=%s", topic.keyword, t)

    _scheduler.start()
    logger.info("调度器已启动（话题采集任务 %s 组）", len(topics))


def stop_scheduler() -> None:
    """停止调度器。"""
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            logger.exception("调度器停止异常")
        _scheduler = None
        logger.info("调度器已停止")


def reload_topic_jobs() -> None:
    """话题增删改后重载采集任务（供 topics API 调用）。"""
    global _scheduler
    if _scheduler is None:
        return
    # 移除全部采集任务后按当前话题重建
    for job in list(_scheduler.get_jobs()):
        if job.id.startswith("collect_topic_"):
            job.remove()
    from sqlmodel import Session, select

    from .db import get_engine
    from .models import Topic

    with Session(get_engine()) as session:
        topics = session.exec(select(Topic).where(Topic.enabled == True)).all()  # noqa: E712
        for topic in topics:
            for t in topic.collect_time_list():
                hh, mm = t.split(":")
                _scheduler.add_job(
                    _run_topic_collection,
                    CronTrigger(hour=int(hh), minute=int(mm)),
                    args=[topic.id],
                    id=f"collect_topic_{topic.id}_{t}",
                    replace_existing=True,
                    misfire_grace_time=600,
                )
    logger.info("话题采集任务已重载")
