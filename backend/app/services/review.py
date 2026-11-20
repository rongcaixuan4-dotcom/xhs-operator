# -*- coding: utf-8 -*-
"""审核流转服务：开关判断自动通过、驳回原因转负样本知识条目（R08）。

- AppSetting.review_required=true（D6 默认）：二创稿保持 pending_review 等人工；
- review_required=false：自动 approved → 创建 PublishTask（queued，按 publish_slots 排期）；
- 驳回：reject_reason 写入 CreationDraft，并转 KnowledgeChunk(source_type='negative')
  供后续评分/提示词注入参考。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta

from sqlmodel import Session, select

from ..db import get_app_setting, get_current_account_id
from ..errors import NotFoundError
from ..knowledge.ingest import import_text
from ..models import CreationDraft, KnowledgeChunk, PublishTask

logger = logging.getLogger("app.services.review")

MAX_RETRY = 3  # 发布重试上限


def is_review_required(session: Session, account_id: int = 1) -> bool:
    """读审核开关（D6 默认 true）。"""
    return bool(get_app_setting(session, "review_required", True))


def compute_dedup_hash(title: str, image_paths: list[str]) -> str:
    """SHA1(标题 + 图片字节指纹)——发布前查重的硬约束输入（R10）。

    图片按文件字节内容指纹（不存在/读不到的路径跳过，不影响哈希生成）。
    """
    from ..config import get_settings

    hasher = hashlib.sha1()
    hasher.update(title.strip().encode("utf-8"))
    settings = get_settings()
    for rel in image_paths:
        abs_path = settings.data_dir / rel
        if abs_path.exists():
            try:
                hasher.update(abs_path.read_bytes())
            except OSError:
                logger.warning("dedup 图片指纹读取失败：%s", abs_path)
    return hasher.hexdigest()


def next_publish_slot(session: Session, account_id: int) -> datetime:
    """按 publish_slots（如 ["10:00","15:00","19:00"]）取下一个未来时段；今日耗尽则顺延明日首个时段。"""
    slots = get_app_setting(session, "publish_slots", ["10:00", "15:00", "19:00"])
    now = datetime.now()
    for slot in slots:
        hh, mm = slot.split(":")
        candidate = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if candidate > now + timedelta(minutes=10):  # 至少留 10 分钟余量
            return candidate
    # 今日时段耗尽 → 明日首个时段
    first = slots[0] if slots else "10:00"
    hh, mm = first.split(":")
    tomorrow = (now + timedelta(days=1)).replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    return tomorrow


def create_publish_task(session: Session, draft: CreationDraft, *, scheduled_at: datetime | None = None) -> PublishTask:
    """由二创稿创建发布任务（queued）。dedup_hash 唯一约束冲突时抛错并告警日志。"""
    account_id = draft.account_id
    image_paths = draft.image_path_list()
    dedup = compute_dedup_hash(draft.title, image_paths)

    # 软查重先行（UNIQUE 硬约束兜底）：同 hash 已有非 canceled 任务 → 拒绝
    dup = session.exec(
        select(PublishTask).where(
            PublishTask.account_id == account_id,
            PublishTask.dedup_hash == dedup,
            PublishTask.status != "canceled",
        )
    ).first()
    if dup is not None:
        logger.warning("防重复：同 dedup_hash=%s 已存在任务 status=%s，拒绝创建", dedup[:12], dup.status)
        raise NotFoundError("重复发布被拒绝：相同标题与图片的任务已存在")

    if scheduled_at is None:
        scheduled_at = next_publish_slot(session, account_id)

    config = {
        "title": draft.title,
        "content": draft.content,
        "images": image_paths,
        "cover": draft.cover_path,
        "topics": [],            # 话题标签（发布引擎按配置填写）
        "location": "",          # 位置
        "source_declare": False, # 声明来源
        "collection": "",        # 合集
        "mention_users": [],     # @好友
        "is_schedule": True,     # 定时发布
    }
    task = PublishTask(
        account_id=account_id,
        draft_id=draft.id,
        status="queued",
        scheduled_at=scheduled_at,
        publish_config=json.dumps(config, ensure_ascii=False),
        dedup_hash=dedup,
    )
    session.add(task)
    draft.status = "queued"
    draft.updated_at = draft.updated_at.now()
    session.add(draft)
    session.commit()
    session.refresh(task)
    logger.info("发布任务已创建：task=%s draft=%s scheduled_at=%s", task.id, draft.id, scheduled_at)
    return task


def settle_review(session: Session, drafts: list[CreationDraft], *, account_id: int = 1) -> dict:
    """审核结算（流水线 creating 阶段后调用）。

    开关关：全部 approved → 创建 PublishTask；开关开：保持 pending_review。
    Returns: {"auto_approved": n, "kept_pending": n}
    """
    if is_review_required(session, account_id):
        logger.info("审核开关开启：%s 篇保持待人工审核", len(drafts))
        return {"auto_approved": 0, "kept_pending": len(drafts)}
    auto = 0
    for draft in drafts:
        if draft.status != "pending_review":
            continue
        try:
            create_publish_task(session, draft)
            auto += 1
        except NotFoundError as exc:
            # 重复稿：标记 rejected（不阻塞其他稿）
            draft.status = "rejected"
            draft.reject_reason = str(exc)
            session.add(draft)
            logger.warning("自动入队被拒（重复）:draft=%s err=%s", draft.id, exc)
    session.commit()
    logger.info("审核开关关闭：自动通过 %s 篇", auto)
    return {"auto_approved": auto, "kept_pending": 0}


def approve_draft(session: Session, draft_id: int) -> PublishTask:
    """人工通过（R08）：approved → 创建 PublishTask 入发布队列。"""
    draft = session.get(CreationDraft, draft_id)
    if draft is None:
        raise NotFoundError(f"二创稿不存在：{draft_id}")
    if draft.status not in ("pending_review", "approved", "rejected"):
        from ..errors import AppError

        raise AppError(f"当前状态不可通过：{draft.status}")
    return create_publish_task(session, draft)


def reject_draft(session: Session, draft_id: int, reason: str) -> CreationDraft:
    """人工驳回（R08）：rejected + 原因转负样本知识条目（R12 闭环负样本入口）。"""
    draft = session.get(CreationDraft, draft_id)
    if draft is None:
        raise NotFoundError(f"二创稿不存在：{draft_id}")
    if not reason.strip():
        raise NotFoundError("驳回原因不能为空（将作为负样本写入知识库）")
    draft.status = "rejected"
    draft.reject_reason = reason.strip()
    draft.updated_at = draft.updated_at.now()
    session.add(draft)
    import_text(
        session,
        account_id=draft.account_id,
        source_title=f"驳回负样本：{draft.title[:50]}",
        content=(
            f"二创稿《{draft.title}》被人工驳回。驳回原因：{draft.reject_reason}。"
            f"教训：后续创作应避免该问题。"
        ),
        source_type="negative",
    )
    session.commit()
    logger.info("二创稿已驳回并写入负样本：draft=%s reason=%s", draft_id, draft.reject_reason)
    return draft


def count_negative_chunks(session: Session, account_id: int) -> int:
    """负样本条数（Dashboard 可视化用）。"""
    return len(session.exec(
        select(KnowledgeChunk).where(
            KnowledgeChunk.account_id == account_id,
            KnowledgeChunk.source_type == "negative",
        )
    ).all())
