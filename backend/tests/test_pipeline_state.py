# -*- coding: utf-8 -*-
"""流水线状态机（R13）验收测试：环节失败不阻塞后续环节、不阻塞次日，状态可查询。

全部引擎环节 mock，不发真实浏览器/网络请求。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlmodel import Session, select

from app.db import get_engine
from app.models import PipelineRun
from app.services.pipeline import get_pipeline_status, run_daily_pipeline


class TestStageFailureIsolation(unittest.TestCase):
    """单环节失败 → 后续环节照常执行，overall=error，状态可见。"""

    def test_selecting_failure_does_not_block_later_stages(self) -> None:
        async def run() -> dict:
            with patch("app.engines.collect.collect_topic", new=AsyncMock(return_value=0)), \
                 patch("app.services.scoring.select_today_notes",
                       side_effect=RuntimeError("评分筛选崩溃")), \
                 patch("app.engines.creator.create_draft", new=AsyncMock()) as mock_create, \
                 patch("app.engines.publish.publish_task", new=AsyncMock(return_value="done")) as mock_pub, \
                 patch("app.engines.snapshot.snapshot_daily",
                       new=AsyncMock(return_value={"notes": 0, "account": True})):
                result = await run_daily_pipeline(account_id=1)
                return result, mock_create, mock_pub

        result, mock_create, mock_pub = asyncio.run(run())

        # 失败环节被记录
        self.assertEqual(result["stages"]["selecting"]["status"], "error")
        self.assertIn("评分筛选崩溃", result["stages"]["selecting"]["error"])

        # 后续环节不被阻塞：creating / publishing / snapshot 都执行了
        self.assertIn("creating", result["stages"])
        self.assertIn("publishing", result["stages"])
        self.assertIn("snapshot", result["stages"])
        self.assertEqual(result["stages"]["creating"]["status"], "ok")
        self.assertEqual(result["stages"]["publishing"]["status"], "ok")
        self.assertEqual(result["stages"]["snapshot"]["status"], "ok")
        self.assertEqual(mock_create.call_count, 0)  # 无 selected 笔记，但环节正常走完
        self.assertEqual(mock_pub.call_count, 0)

        self.assertEqual(result["overall"], "error")

        # 状态落库可查询：stage=done, status=error, detail 含失败信息
        with Session(get_engine()) as session:
            run_row = session.exec(
                select(PipelineRun).where(PipelineRun.run_date == date.today())
            ).first()
            self.assertIsNotNone(run_row)
            self.assertEqual(run_row.stage, "done")
            self.assertEqual(run_row.status, "error")
            detail = run_row.detail_dict()
            self.assertEqual(detail.get("selecting", {}).get("status"), "error")

    def test_single_topic_failure_does_not_block_other_topics(self) -> None:
        """逐话题 try/except：第一个话题失败、第二个成功 → 采集环节不整体崩溃。"""
        async def run() -> dict:
            from app.models import Topic

            with Session(get_engine()) as session:
                session.add(Topic(keyword="话题A", enabled=True))
                session.add(Topic(keyword="话题B", enabled=True))
                session.commit()

            with patch("app.engines.collect.collect_topic",
                       new=AsyncMock(side_effect=[Exception("风控拦截"), 3])), \
                 patch("app.services.scoring.select_today_notes", new=AsyncMock(return_value=[])), \
                 patch("app.engines.creator.create_draft", new=AsyncMock()), \
                 patch("app.engines.publish.publish_task", new=AsyncMock(return_value="done")), \
                 patch("app.engines.snapshot.snapshot_daily",
                       new=AsyncMock(return_value={"notes": 0, "account": True})):
                return await run_daily_pipeline(account_id=1)

        result = asyncio.run(run())
        collecting = result["stages"]["collecting"]
        self.assertEqual(collecting["added"], 3, "话题B 的新增不应被话题A 失败吞掉")
        self.assertEqual(len(collecting["errors"]), 1)
        self.assertIn("话题A", collecting["errors"][0])

    def test_single_draft_failure_does_not_block_other_drafts(self) -> None:
        """逐篇 try/except：第一篇二创失败、第二篇成功。"""
        from app.models import CollectedNote

        async def run() -> dict:
            with Session(get_engine()) as session:
                session.add(CollectedNote(xhs_note_id="n1", title="笔记1", status="selected"))
                session.add(CollectedNote(xhs_note_id="n2", title="笔记2", status="selected"))
                session.commit()

            async def create_side_effect(note, *, account_id=1):
                if note.xhs_note_id == "n1":
                    raise RuntimeError("LLM 超时")
                return AsyncMock(id=999)

            with patch("app.engines.collect.collect_topic", new=AsyncMock(return_value=0)), \
                 patch("app.services.scoring.select_today_notes", new=AsyncMock(return_value=[])), \
                 patch("app.engines.creator.create_draft", new=AsyncMock(side_effect=create_side_effect)), \
                 patch("app.engines.publish.publish_task", new=AsyncMock(return_value="done")), \
                 patch("app.engines.snapshot.snapshot_daily",
                       new=AsyncMock(return_value={"notes": 0, "account": True})):
                return await run_daily_pipeline(account_id=1)

        result = asyncio.run(run())
        creating = result["stages"]["creating"]
        self.assertEqual(len(creating["errors"]), 1)
        self.assertIn("LLM 超时", creating["errors"][0])
        self.assertIn("note=", creating["errors"][0])


class TestEmptyEnvironmentGraceful(unittest.TestCase):
    """边界环境下的全流程行为：不崩溃、错误可见、状态可查。"""

    @staticmethod
    def _seed_active_persona() -> None:
        from app.models import Persona

        with Session(get_engine()) as session:
            session.add(Persona(account_id=1, name="空库测试人设", is_active=True))
            session.commit()

    def test_empty_db_full_run(self) -> None:
        """空数据但有生效人设：全流程空转成功。"""
        self._seed_active_persona()

        async def run() -> dict:
            # snapshot 会开浏览器，mock 掉；其余环节在空库下天然空转
            with patch("app.engines.snapshot.snapshot_daily",
                       new=AsyncMock(return_value={"notes": 0, "account": True})):
                return await run_daily_pipeline(account_id=1)

        result = asyncio.run(run())
        self.assertEqual(result["overall"], "ok")
        for stage in ("collecting", "selecting", "creating", "reviewing", "publishing", "snapshot"):
            self.assertEqual(result["stages"][stage]["status"], "ok", f"{stage} 不应失败")

    def test_empty_db_no_persona_error_visible_not_crash(self) -> None:
        """无生效人设：评分环节报可见错误（含指引文案），流程继续走完不崩溃。"""
        async def run() -> dict:
            with patch("app.engines.snapshot.snapshot_daily",
                       new=AsyncMock(return_value={"notes": 0, "account": True})):
                return await run_daily_pipeline(account_id=1)

        result = asyncio.run(run())
        self.assertEqual(result["stages"]["selecting"]["status"], "error")
        self.assertIn("人设", result["stages"]["selecting"]["error"])
        # 后续环节不被阻塞
        self.assertEqual(result["stages"]["creating"]["status"], "ok")
        self.assertEqual(result["stages"]["publishing"]["status"], "ok")
        self.assertEqual(result["overall"], "error")

        status = get_pipeline_status(Session(get_engine()), account_id=1)
        self.assertEqual(status["stage"], "done")
        self.assertEqual(status["status"], "error")
        self.assertIn("stage_order", status)


class TestNextDayNotBlocked(unittest.TestCase):
    """昨日失败不阻塞今日：状态机按日独立（UNIQUE(account_id, run_date)）。"""

    def test_yesterday_error_today_fresh(self) -> None:
        from app.models import Persona

        with Session(get_engine()) as session:
            yesterday = date.today() - timedelta(days=1)
            session.add(PipelineRun(
                account_id=1, run_date=yesterday,
                stage="publishing", status="error",
                detail='{"publishing": {"status": "error", "error": "浏览器崩溃"}}',
            ))
            session.add(Persona(account_id=1, name="次日测试人设", is_active=True))
            session.commit()

        status = get_pipeline_status(Session(get_engine()), account_id=1)
        self.assertEqual(status["run_date"], date.today().isoformat())
        self.assertNotEqual(status["status"], "error", "昨日失败不应污染今日状态")
        self.assertEqual(status["stage"], "idle")

        # 今日全流程可正常跑完
        async def run() -> dict:
            with patch("app.engines.snapshot.snapshot_daily",
                       new=AsyncMock(return_value={"notes": 0, "account": True})):
                return await run_daily_pipeline(account_id=1)

        result = asyncio.run(run())
        self.assertEqual(result["overall"], "ok")


if __name__ == "__main__":
    unittest.main()
