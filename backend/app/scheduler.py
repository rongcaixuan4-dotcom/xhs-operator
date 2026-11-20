# -*- coding: utf-8 -*-
"""APScheduler 编排（T9 终版）：全天候任务调度。

任务清单：
- 话题时段采集：每启用话题按 collect_times cron 触发（T4）；
- 每日流水线：daily_run_time 触发 run_daily_pipeline（T9）；
- 数据快照：每小时触发 snapshot_daily（T7，验收每日 ≥1 条/篇）；
- 每周复盘：每周 weekly_report_day/time 触发周报生成 + 超期归档（T8）；
- 发布到点扫描：publish_scan_interval_minutes 扫描 queued 且到点的任务（T6/T9）；
- 失败重试扫描：retry_scan_interval_minutes 扫描可重试任务（≤3 次）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import get_settings

logger = logging.getLogger("app.scheduler")

_scheduler: AsyncIOScheduler | None = None


# ────────────────────────── 任务实现（各自独立事件循环） ──────────────────────────
def _run_topic_collection(topic_id: int) -> None:
    """话题时段采集任务。"""

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
            keyword = topic.keyword
        try:
            added = await collect_topic(topic)
            logger.info("定时采集完成：topic=%s 新增=%s", keyword, added)
        except Exception as exc:  # noqa: BLE001 - 失败记录日志，不阻塞调度
            logger.exception("定时采集失败：topic_id=%s err=%s", topic_id, exc)
            return
        try:
            with Session(get_engine()) as session:
                selected = select_today_notes(session, account_id=account_id)
                logger.info("定时评分筛选完成：topic=%s 新入选=%s", keyword, len(selected))
        except Exception as exc:  # noqa: BLE001
            logger.warning("定时评分筛选失败（不影响采集）：%s", exc)

    asyncio.run(_job())


def _run_daily_pipeline() -> None:
    """每日流水线任务（监测→采集→筛选→二创→审核→发布→快照）。"""
    from .services.pipeline import run_daily_pipeline

    async def _job() -> None:
        try:
            result = await run_daily_pipeline(account_id=1)
            logger.info("每日流水线完成：overall=%s", result["overall"])
        except Exception:  # noqa: BLE001
            logger.exception("每日流水线异常（不阻塞后续天次）")

    asyncio.run(_job())


def _run_hourly_snapshot() -> None:
    """每小时数据快照任务。"""
    from .engines.snapshot import snapshot_daily

    async def _job() -> None:
        try:
            result = await snapshot_daily(account_id=1)
            logger.info("定时快照完成：notes=%s", result["notes"])
        except Exception:  # noqa: BLE001
            logger.exception("定时快照异常")

    asyncio.run(_job())


def _run_weekly_report() -> None:
    """每周复盘任务：生成上周报告 + 超期归档。"""
    from sqlmodel import Session

    from .db import get_engine
    from .services.report_service import expire_stale_reports, generate_weekly_report

    async def _job() -> None:
        try:
            report = await generate_weekly_report(account_id=1)
            logger.info("周报已生成：id=%s week=%s", report.id, report.week_start)
        except Exception:  # noqa: BLE001
            logger.exception("周报生成异常（下周一重试生成）")
        try:
            with Session(get_engine()) as session:
                expired = expire_stale_reports(session)
                if expired:
                    logger.info("超期周报已归档：%s 份", expired)
        except Exception:  # noqa: BLE001
            logger.exception("周报归档异常")

    asyncio.run(_job())


def _run_publish_due_scan() -> None:
    """发布任务到点扫描：拉起 queued 且 scheduled_at 到点的任务。"""
    from sqlmodel import Session, select

    from .db import get_engine
    from .engines.publish import publish_task
    from .models import PublishTask

    async def _job() -> None:
        with Session(get_engine()) as session:
            due = session.exec(
                select(PublishTask).where(
                    PublishTask.account_id == 1,
                    PublishTask.status == "queued",
                )
            ).all()
        due_ids = [t.id for t in due if t.scheduled_at and t.scheduled_at <= datetime.now()]
        if not due_ids:
            return
        logger.info("发布到点扫描：%s 个任务待发布", len(due_ids))
        for task_id in due_ids:
            try:
                final = await publish_task(task_id)
                logger.info("到点发布完成：task=%s final=%s", task_id, final)
            except Exception:  # noqa: BLE001
                logger.exception("到点发布失败：task=%s", task_id)

    asyncio.run(_job())


def _run_retry_scan() -> None:
    """失败重试扫描：failed 且 retry_count<3 的任务重排入队（延后一个扫描周期）。"""
    from sqlmodel import Session, select

    from .db import get_engine
    from .models import PublishTask

    with Session(get_engine()) as session:
        failed = session.exec(
            select(PublishTask).where(
                PublishTask.account_id == 1,
                PublishTask.status == "failed",
            )
        ).all()
        requeued = 0
        for t in failed:
            if t.retry_count < 3:
                t.status = "queued"
                # 顺延 30 分钟再试
                from datetime import timedelta

                t.scheduled_at = max(
                    datetime.now() + timedelta(minutes=30),
                    t.scheduled_at or datetime.now(),
                )
                session.add(t)
                requeued += 1
        session.commit()
    if requeued:
        logger.info("失败重试扫描：重排 %s 个任务", requeued)


# ────────────────────────── 调度器生命周期 ──────────────────────────
def start_scheduler() -> None:
    """启动调度器并注册全部任务。"""
    global _scheduler
    if _scheduler is not None:
        return
    settings = get_settings()
    _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    # 1) 话题采集 cron（每话题按 collect_times）
    _register_topic_jobs()

    # 2) 每日流水线
    hh, mm = settings.daily_run_time.split(":")
    _scheduler.add_job(
        _run_daily_pipeline, CronTrigger(hour=int(hh), minute=int(mm)),
        id="daily_pipeline", replace_existing=True, misfire_grace_time=1800,
    )

    # 3) 每小时快照
    _scheduler.add_job(
        _run_hourly_snapshot, IntervalTrigger(minutes=settings.snapshot_interval_minutes),
        id="hourly_snapshot", replace_existing=True, next_run_time=datetime.now(),
    )

    # 4) 每周复盘
    day_map = {"mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu", "fri": "fri", "sat": "sat", "sun": "sun"}
    day_of_week = day_map.get(settings.weekly_report_day.lower(), "mon")
    hh, mm = settings.weekly_report_time.split(":")
    _scheduler.add_job(
        _run_weekly_report, CronTrigger(day_of_week=day_of_week, hour=int(hh), minute=int(mm)),
        id="weekly_report", replace_existing=True, misfire_grace_time=3600,
    )

    # 5) 发布到点扫描
    _scheduler.add_job(
        _run_publish_due_scan, IntervalTrigger(minutes=settings.publish_scan_interval_minutes),
        id="publish_due_scan", replace_existing=True,
    )

    # 6) 失败重试扫描
    _scheduler.add_job(
        _run_retry_scan, IntervalTrigger(minutes=settings.retry_scan_interval_minutes),
        id="retry_scan", replace_existing=True,
    )

    _scheduler.start()
    logger.info("调度器已启动：任务数=%s", len(_scheduler.get_jobs()))


def _register_topic_jobs() -> None:
    """注册（重建）话题采集 cron 任务。"""
    from sqlmodel import Session, select

    from .db import get_engine
    from .models import Topic

    assert _scheduler is not None
    with Session(get_engine()) as session:
        topics = session.exec(select(Topic).where(Topic.enabled == True)).all()  # noqa: E712
        for topic in topics:
            for t in topic.collect_time_list():
                try:
                    hh, mm = t.split(":")
                except ValueError:
                    logger.warning("非法采集时刻，跳过：topic=%s time=%s", topic.keyword, t)
                    continue
                _scheduler.add_job(
                    _run_topic_collection,
                    CronTrigger(hour=int(hh), minute=int(mm)),
                    args=[topic.id],
                    id=f"collect_topic_{topic.id}_{t}",
                    replace_existing=True,
                    misfire_grace_time=600,
                )
                logger.info("注册采集任务：topic=%s time=%s", topic.keyword, t)


def reload_topic_jobs() -> None:
    """话题增删改后重载采集任务（供 topics API 调用）。"""
    global _scheduler
    if _scheduler is None:
        return
    for job in list(_scheduler.get_jobs()):
        if job.id.startswith("collect_topic_"):
            job.remove()
    _register_topic_jobs()
    logger.info("话题采集任务已重载")


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
