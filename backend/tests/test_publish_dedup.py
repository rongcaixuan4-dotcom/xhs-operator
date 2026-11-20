# -*- coding: utf-8 -*-
"""防重复核查单测（mock 引擎结果，不启真浏览器）。

覆盖（R10）：
1. dedup_hash 计算：同标题同图 → 相同 hash；改标题 → 不同 hash；
2. 重复任务被服务层拒绝（软查重 + UNIQUE 硬约束）；
3. 发布引擎「页面报失败但列表核查已发布」路径不重试。

运行：python -m pytest backend/tests/test_publish_dedup.py -v
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db import init_db, get_engine, get_session  # noqa: E402
from app.models import CreationDraft, PublishTask  # noqa: E402
from app.services.review import (  # noqa: E402
    compute_dedup_hash,
    create_publish_task,
    is_review_required,
)


class TestDedupHash(unittest.TestCase):
    """dedup_hash 计算逻辑。"""

    def test_same_content_same_hash(self) -> None:
        img = BACKEND_DIR / "tests" / "_dedup_img.png"
        img.write_bytes(b"IMGBYTES")
        rel = img.relative_to(BACKEND_DIR.parent).as_posix()
        # compute_dedup_hash 期望相对 data_dir；用绝对路径字符串同样可计算（读不到则跳过，仍确定性）
        h1 = compute_dedup_hash("标题A", [rel])
        h2 = compute_dedup_hash("标题A", [rel])
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 40)  # SHA1 hex
        img.unlink(missing_ok=True)

    def test_diff_title_diff_hash(self) -> None:
        h1 = compute_dedup_hash("标题A", [])
        h2 = compute_dedup_hash("标题B", [])
        self.assertNotEqual(h1, h2)


class TestDuplicateTaskRejected(unittest.TestCase):
    """重复任务创建被拒绝（软查重路径）。"""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()  # 建表 + 种子（测试库）

    def test_duplicate_rejected(self) -> None:
        engine = get_engine()
        img = BACKEND_DIR / "tests" / "_dedup_img2.png"
        img.write_bytes(b"IMG2")
        rel = img.relative_to(BACKEND_DIR.parent).as_posix()

        with get_session() as session:
            draft = CreationDraft(
                account_id=1,
                title="防重测试标题",
                content="内容",
                image_paths=f'["{rel}"]',
                cover_path=rel,
                status="approved",
            )
            session.add(draft)
            session.commit()

            task1 = create_publish_task(session, draft)
            self.assertEqual(task1.status, "queued")

            # 第二次用相同内容创建 → 应拒绝
            draft2 = CreationDraft(
                account_id=1,
                title="防重测试标题",
                content="内容",
                image_paths=f'["{rel}"]',
                cover_path=rel,
                status="approved",
            )
            session.add(draft2)
            session.commit()
            from app.errors import NotFoundError

            with self.assertRaises(NotFoundError):
                create_publish_task(session, draft2)

        img.unlink(missing_ok=True)

    def test_review_default_on(self) -> None:
        """D6：review_required 默认 true。"""
        with get_session() as session:
            self.assertTrue(is_review_required(session, 1))


class TestPublishOutcomePaths(unittest.TestCase):
    """发布引擎结果判定路径（mock _do_publish_flow 的页面交互）。"""

    def _make_task_row(self) -> PublishTask:
        return PublishTask(
            account_id=1, draft_id=None, status="publishing",
            dedup_hash="f" * 40,
            publish_config='{"title":"T","content":"C","images":[],"cover":""}',
        )

    def test_unclear_feedback_verified_as_done(self) -> None:
        """页面反馈不明确但列表核查命中 → done（不重试）。"""
        from sqlmodel import select as _select

        from app.engines.publish import _PublishOutcome, publish_task

        init_db()
        with get_session() as session:
            session.add(self._make_task_row())
            session.commit()
            task_id = session.exec(_select(PublishTask)).all()[-1].id

        async def run() -> str:
            with patch("app.engines.publish._do_publish_flow", new=AsyncMock(
                return_value=_PublishOutcome("done", "http://x/1", "abc", "页面反馈不明确，列表核查确认已发布")
            )), patch("app.engines.browser.check_login_async", new=AsyncMock(
                return_value=(True, "ok")
            )):
                return await publish_task(task_id)

        final = asyncio.run(run())
        self.assertEqual(final, "done")
        with get_session() as session:
            row = session.get(PublishTask, task_id)
            self.assertEqual(row.status, "done")
            self.assertEqual(row.retry_count, 0)

    def test_retry_exhausted_becomes_failed(self) -> None:
        """重试耗尽 → failed。"""
        from app.engines.publish import _PublishOutcome, publish_task

        init_db()
        with get_session() as session:
            row = self._make_task_row()
            row.retry_count = 2  # 再失败一次即耗尽（MAX_RETRY=3）
            session.add(row)
            session.commit()
            task_id = row.id

        async def run() -> str:
            with patch("app.engines.publish._do_publish_flow", new=AsyncMock(
                return_value=_PublishOutcome("retry", error="页面改版")
            )), patch("app.engines.browser.check_login_async", new=AsyncMock(
                return_value=(True, "ok")
            )):
                return await publish_task(task_id)

        final = asyncio.run(run())
        self.assertEqual(final, "failed")
        with get_session() as session:
            row = session.get(PublishTask, task_id)
            self.assertEqual(row.status, "failed")
            self.assertIn("重试耗尽", row.last_error)


if __name__ == "__main__":
    unittest.main()
