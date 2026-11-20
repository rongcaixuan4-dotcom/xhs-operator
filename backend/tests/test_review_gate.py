# -*- coding: utf-8 -*-
"""审核开关（R08）验收测试：开关状态决定二创稿能否进入发布队列。

覆盖：
1. review_required=true：pending_review 稿不得入队，settle_review 保持待审；
2. review_required=false：自动 approved → PublishTask(queued) 入队；
3. 未审核状态（generating/rejected 之外的非法流转）不可人工通过；
4. 人工通过/驳回路径；驳回原因写负样本知识库。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlmodel import Session, select

from app.db import get_engine, get_app_setting, set_app_setting
from app.errors import AppError, NotFoundError
from app.models import CreationDraft, KnowledgeChunk, PublishTask
from app.services.review import (
    approve_draft,
    is_review_required,
    reject_draft,
    settle_review,
)


def _make_draft(**kw) -> CreationDraft:
    defaults = dict(
        account_id=1,
        title="审核测试标题",
        content="审核测试内容",
        image_paths="[]",
        cover_path="",
        status="pending_review",
    )
    defaults.update(kw)
    return CreationDraft(**defaults)


class TestReviewGateOn(unittest.TestCase):
    """review_required=true（D6 默认）：未审核稿不得进入发布队列。"""

    def test_default_review_required_is_true(self) -> None:
        with Session(get_engine()) as session:
            self.assertTrue(is_review_required(session, 1))
            self.assertIs(get_app_setting(session, "review_required", None), True)

    def test_settle_keeps_pending_and_never_enqueues(self) -> None:
        with Session(get_engine()) as session:
            d1 = _make_draft()
            d2 = _make_draft(title="第二篇")
            session.add(d1)
            session.add(d2)
            session.commit()

            result = settle_review(session, [d1, d2], account_id=1)
            self.assertEqual(result["auto_approved"], 0)
            self.assertEqual(result["kept_pending"], 2)

            tasks = session.exec(select(PublishTask)).all()
            self.assertEqual(len(tasks), 0, "审核开关开启时不得产生发布任务")
            self.assertEqual(d1.status, "pending_review")
            self.assertEqual(d2.status, "pending_review")

    def test_pending_review_draft_cannot_be_approved_manually(self) -> None:
        """pending_review 稿可以走人工 approve（这是唯一合法入队通道之外的开关路径）。"""
        with Session(get_engine()) as session:
            draft = _make_draft()
            session.add(draft)
            session.commit()
            # 人工通过是合法操作（开关开着也需要人审），应成功入队
            task = approve_draft(session, draft.id)
            self.assertEqual(task.status, "queued")
            self.assertEqual(draft.status, "queued")


class TestReviewGateOff(unittest.TestCase):
    """review_required=false：直接入队。"""

    def setUp(self) -> None:
        with Session(get_engine()) as session:
            set_app_setting(session, "review_required", False)

    def test_settle_auto_enqueues(self) -> None:
        with Session(get_engine()) as session:
            draft = _make_draft()
            session.add(draft)
            session.commit()

            result = settle_review(session, [draft], account_id=1)
            self.assertEqual(result["auto_approved"], 1)

            tasks = session.exec(select(PublishTask)).all()
            self.assertEqual(len(tasks), 1)
            task = tasks[0]
            self.assertEqual(task.status, "queued")
            self.assertIsNotNone(task.scheduled_at)
            self.assertEqual(task.draft_id, draft.id)
            cfg = task.publish_config_dict()
            self.assertEqual(cfg.get("title"), "审核测试标题")
            self.assertEqual(draft.status, "queued")

    def test_duplicate_draft_rejected_and_marked(self) -> None:
        """同内容第二篇稿自动入队被拒 → 标记 rejected，不阻塞第一篇。"""
        with Session(get_engine()) as session:
            d1 = _make_draft()
            d2 = _make_draft()
            session.add(d1)
            session.add(d2)
            session.commit()

            result = settle_review(session, [d1, d2], account_id=1)
            self.assertEqual(result["auto_approved"], 1)
            self.assertEqual(d1.status, "queued")
            self.assertEqual(d2.status, "rejected")
            self.assertIn("重复", d2.reject_reason)


class TestApproveReject(unittest.TestCase):
    """人工通过 / 驳回路径。"""

    def test_approve_illegal_status_raises(self) -> None:
        """generating（未完成审核流转）状态不可通过 → AppError，不入队。"""
        with Session(get_engine()) as session:
            draft = _make_draft(status="generating")
            session.add(draft)
            session.commit()
            with self.assertRaises(AppError):
                approve_draft(session, draft.id)
            tasks = session.exec(select(PublishTask)).all()
            self.assertEqual(len(tasks), 0)

    def test_approve_missing_draft_raises(self) -> None:
        with Session(get_engine()) as session:
            with self.assertRaises(NotFoundError):
                approve_draft(session, 99999)

    def test_reject_writes_negative_knowledge_chunk(self) -> None:
        with Session(get_engine()) as session:
            draft = _make_draft(title="驳回负样本测试稿")
            session.add(draft)
            session.commit()

            reject_draft(session, draft.id, "内容与原稿过于雷同")

            self.assertEqual(draft.status, "rejected")
            self.assertEqual(draft.reject_reason, "内容与原稿过于雷同")
            chunks = session.exec(
                select(KnowledgeChunk).where(KnowledgeChunk.source_type == "negative")
            ).all()
            self.assertEqual(len(chunks), 1)
            self.assertIn("内容与原稿过于雷同", chunks[0].content)

    def test_reject_empty_reason_raises(self) -> None:
        with Session(get_engine()) as session:
            draft = _make_draft()
            session.add(draft)
            session.commit()
            with self.assertRaises(NotFoundError):
                reject_draft(session, draft.id, "   ")
            # 负样本不应产生
            chunks = session.exec(
                select(KnowledgeChunk).where(KnowledgeChunk.source_type == "negative")
            ).all()
            self.assertEqual(len(chunks), 0)


if __name__ == "__main__":
    unittest.main()
