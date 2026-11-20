# -*- coding: utf-8 -*-
"""每日流水线状态机：监测→采集→筛选→二创→审核→发布→快照（R13）。

编排原则（架构 1.1 / 5.1）：
- 逐环节 try/except，任一环节失败写 PipelineRun.detail（stage.error）后继续下一环节，
  绝不阻塞后续天次；
- 每个环节独立可重试（pipeline/retry 重跑失败环节）。
"""
from __future__ import annotations

import logging
import time
import traceback
from datetime import date, datetime
from typing import Optional

from sqlmodel import Session, select

from ..db import get_current_account_id, get_engine
from ..errors import AppError
from ..models import CollectedNote, CreationDraft, PipelineRun, PublishTask, Topic

logger = logging.getLogger("app.services.pipeline")

# 流水线环节（PipelineRun.stage 取值，前端时间轴展示顺序）
STAGE_ORDER = ["collecting", "selecting", "creating", "reviewing", "publishing", "snapshot", "done"]


def upsert_today_run(session: Session, account_id: int) -> PipelineRun:
    """取（或创建）当日 PipelineRun 单行（UNIQUE(account_id, run_date)）。"""
    today = date.today()
    run = session.exec(
        select(PipelineRun).where(
            PipelineRun.account_id == account_id, PipelineRun.run_date == today
        )
    ).first()
    if run is None:
        run = PipelineRun(account_id=account_id, run_date=today, stage="idle", status="idle")
        session.add(run)
        session.commit()
        session.refresh(run)
    return run


def set_stage(session: Session, run: PipelineRun, stage: str, status: str, detail_patch: Optional[dict] = None) -> None:
    """更新状态机：stage/status + detail 合并写入（JSON）。"""
    run.stage = stage
    run.status = status
    detail = run.detail_dict()
    if detail_patch:
        existing = detail.get(stage)
        merged = existing if isinstance(existing, dict) else {}
        merged.update(detail_patch)
        detail[stage] = merged
    run.detail = __import__("json").dumps(detail, ensure_ascii=False)
    run.updated_at = run.updated_at.now()
    session.add(run)
    session.commit()
    logger.info("流水线状态：stage=%s status=%s", stage, status)


def get_pipeline_status(session: Session, account_id: Optional[int] = None) -> dict:
    """当前（当日）PipelineRun 状态（工作台时间轴数据源）。"""
    if account_id is None:
        account_id = get_current_account_id(session)
    run = upsert_today_run(session, account_id)
    return {
        "id": run.id,
        "run_date": run.run_date.isoformat(),
        "stage": run.stage,
        "status": run.status,
        "detail": run.detail_dict(),
        "started_at": run.started_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "stage_order": STAGE_ORDER,
    }


async def run_daily_pipeline(account_id: int = 1) -> dict:
    """执行当日流水线（定时/手动触发）。

    Returns:
        {"run_date", "stages": {stage: {...}}, "overall": ok|error}
    """
    stages_result: dict = {}

    def _record(stage: str, ok: bool, **info) -> None:
        info["status"] = "ok" if ok else "error"
        stages_result[stage] = info

    with Session(get_engine()) as session:
        run = upsert_today_run(session, account_id)
        set_stage(session, run, "monitoring", "running")

    # ── 环节 1：采集（逐话题 try/except） ──
    with Session(get_engine()) as session:
        run = upsert_today_run(session, account_id)
        set_stage(session, run, "collecting", "running")
    t0 = time.time()
    collect_added, collect_errors = 0, []
    try:
        from ..engines.collect import collect_topic

        with Session(get_engine()) as session:
            topics = session.exec(
                select(Topic).where(Topic.account_id == account_id, Topic.enabled == True)  # noqa: E712
            ).all()
        for topic in topics:
            try:
                collect_added += await collect_topic(topic)
            except Exception as exc:  # noqa: BLE001 - 单话题失败不阻塞
                logger.exception("采集失败：topic=%s", topic.keyword)
                collect_errors.append(f"{topic.keyword}: {exc}")
        _record("collecting", not collect_errors or collect_added > 0,
                added=collect_added, errors=collect_errors, elapsed_ms=int((time.time() - t0) * 1000))
    except Exception as exc:  # noqa: BLE001
        logger.exception("采集环节异常")
        _record("collecting", False, error=str(exc), trace=traceback.format_exc()[-500:])
    with Session(get_engine()) as session:
        set_stage(session, upsert_today_run(session, account_id), "collecting",
                  "ok" if "collecting" in stages_result and stages_result["collecting"]["status"] == "ok" else "error",
                  stages_result.get("collecting"))

    # ── 环节 2：评分筛选 ──
    with Session(get_engine()) as session:
        run = upsert_today_run(session, account_id)
        set_stage(session, run, "selecting", "running")
    try:
        from ..services.scoring import select_today_notes

        t0 = time.time()
        selected = select_today_notes(session, account_id=account_id)
        _record("selecting", True, selected=[n.id for n in selected],
                count=len(selected), elapsed_ms=int((time.time() - t0) * 1000))
        with Session(get_engine()) as session:
            set_stage(session, upsert_today_run(session, account_id), "selecting", "ok",
                      stages_result.get("selecting"))
    except Exception as exc:
        logger.exception("评分筛选环节异常")
        _record("selecting", False, error=str(exc), trace=traceback.format_exc()[-500:])
        with Session(get_engine()) as session:
            set_stage(session, upsert_today_run(session, account_id), "selecting", "error",
                      stages_result.get("selecting"))

    # ── 环节 3：二创（逐篇 try/except） ──
    with Session(get_engine()) as session:
        run = upsert_today_run(session, account_id)
        set_stage(session, run, "creating", "running")
    t0 = time.time()
    created_ids, create_errors = [], []
    try:
        from ..engines.creator import create_draft

        with Session(get_engine()) as session:
            selected_notes = session.exec(
                select(CollectedNote).where(
                    CollectedNote.account_id == account_id,
                    CollectedNote.status == "selected",
                )
            ).all()
            existing_note_ids = {
                d.collected_note_id for d in session.exec(
                    select(CreationDraft).where(CreationDraft.account_id == account_id)
                ).all()
            }
        for note in selected_notes:
            if note.id in existing_note_ids:
                continue
            try:
                draft = await create_draft(note, account_id=account_id)
                created_ids.append(draft.id)
            except Exception as exc:  # noqa: BLE001 - 单篇失败不阻塞
                logger.exception("二创失败：note=%s", note.id)
                create_errors.append(f"note={note.id}: {exc}")
        _record("creating", not create_errors, created=created_ids, errors=create_errors,
                elapsed_ms=int((time.time() - t0) * 1000))
    except Exception as exc:
        logger.exception("二创环节异常")
        _record("creating", False, error=str(exc), trace=traceback.format_exc()[-500:])
    with Session(get_engine()) as session:
        set_stage(session, upsert_today_run(session, account_id), "creating",
                  "ok" if stages_result.get("creating", {}).get("status") == "ok" else "error",
                  stages_result.get("creating"))

    # ── 环节 4：审核结算 ──
    try:
        from ..services.review import settle_review

        t0 = time.time()
        with Session(get_engine()) as session:
            pending = session.exec(
                select(CreationDraft).where(
                    CreationDraft.account_id == account_id,
                    CreationDraft.status == "pending_review",
                )
            ).all()
            settled = settle_review(session, list(pending), account_id=account_id)
        _record("reviewing", True, **settled, elapsed_ms=int((time.time() - t0) * 1000))
    except Exception as exc:
        logger.exception("审核结算环节异常")
        _record("reviewing", False, error=str(exc))
    with Session(get_engine()) as session:
        set_stage(session, upsert_today_run(session, account_id), "reviewing",
                  "ok" if stages_result.get("reviewing", {}).get("status") == "ok" else "error",
                  stages_result.get("reviewing"))

    # ── 环节 5：发布（到点任务逐个 try/except） ──
    with Session(get_engine()) as session:
        run = upsert_today_run(session, account_id)
        set_stage(session, run, "publishing", "running")
    t0 = time.time()
    published_ids, publish_errors = [], []
    try:
        from ..engines.publish import publish_task

        with Session(get_engine()) as session:
            due_tasks = session.exec(
                select(PublishTask).where(
                    PublishTask.account_id == account_id,
                    PublishTask.status == "queued",
                )
            ).all()
        due_tasks = [t for t in due_tasks if t.scheduled_at and t.scheduled_at <= datetime.now()]
        for task in due_tasks:
            try:
                final = await publish_task(task.id)
                if final == "done":
                    published_ids.append(task.id)
                elif final != "queued":  # queued=延时重试，不算错误
                    publish_errors.append(f"task={task.id}: 终态 {final}")
            except Exception as exc:  # noqa: BLE001
                logger.exception("发布失败：task=%s", task.id)
                publish_errors.append(f"task={task.id}: {exc}")
        _record("publishing", not publish_errors, published=published_ids,
                errors=publish_errors, elapsed_ms=int((time.time() - t0) * 1000))
    except Exception as exc:
        logger.exception("发布环节异常")
        _record("publishing", False, error=str(exc))
    with Session(get_engine()) as session:
        set_stage(session, upsert_today_run(session, account_id), "publishing",
                  "ok" if stages_result.get("publishing", {}).get("status") == "ok" else "error",
                  stages_result.get("publishing"))

    # ── 环节 6：数据快照 ──
    try:
        from ..engines.snapshot import snapshot_daily

        t0 = time.time()
        snap = await snapshot_daily(account_id)
        _record("snapshot", not snap.get("error"), **snap, elapsed_ms=int((time.time() - t0) * 1000))
    except Exception as exc:
        logger.exception("快照环节异常")
        _record("snapshot", False, error=str(exc))
    with Session(get_engine()) as session:
        set_stage(session, upsert_today_run(session, account_id), "snapshot",
                  "ok" if stages_result.get("snapshot", {}).get("status") == "ok" else "error",
                  stages_result.get("snapshot"))

    # ── 收尾：done ──
    overall = "ok" if all(v.get("status") == "ok" for v in stages_result.values()) else "error"
    with Session(get_engine()) as session:
        run = upsert_today_run(session, account_id)
        set_stage(session, run, "done", overall, {"overall": overall, "finished_at": datetime.now().isoformat()})

    logger.info("流水线结束：overall=%s stages=%s", overall, list(stages_result))
    return {"run_date": str(date.today()), "stages": stages_result, "overall": overall}
