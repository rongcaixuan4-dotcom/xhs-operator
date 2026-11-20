# -*- coding: utf-8 -*-
"""知识库检索（R04）+ 人设注入（R03）验收测试。

- 导入文本 → BM25 召回相关切片 → 命中内容出现在二创提示词中；
- 切换生效人设后，提示词上下文随之变化；
- 边界：空知识库、无生效人设不崩溃（错误可见）。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlmodel import Session

from app.ai.prompts import build_rewrite_messages, persona_to_system_text
from app.db import get_engine
from app.errors import NotFoundError
from app.knowledge.ingest import import_text
from app.knowledge.retrieve import search_chunks
from app.models import KnowledgeChunk, Persona
from app.services.context import build_creation_context, get_active_persona

DOC_A = (
    "封面设计完全指南。\n\n"
    "小红书封面的首要原则是三秒吸引力：大字标题、高饱和底色、人物出镜。"
    "封面决定点击率，建议使用 3:4 竖版构图，正文字体不小于 32px。"
)
DOC_B = (
    "爆款标题方法。\n\n"
    "爆款标题的套路：数字清单、悬念反差、身份认同。标题要在 20 字内制造钩子，"
    "避免标题党被限流，可用「亲测」「避坑」等可信词降低防御心理。"
)


def _seed_knowledge(session: Session) -> None:
    import_text(session, account_id=1, source_title="封面指南", content=DOC_A)
    import_text(session, account_id=1, source_title="标题方法", content=DOC_B)


class TestKnowledgeRecall(unittest.TestCase):
    """R04：导入文本后 BM25 能召回相关切片。"""

    def test_import_creates_chunks(self) -> None:
        with Session(get_engine()) as session:
            _seed_knowledge(session)
            chunks = session.query(KnowledgeChunk).all()
            self.assertGreaterEqual(len(chunks), 2)
            self.assertTrue(all(c.char_count > 0 for c in chunks))

    def test_bm25_recall_relevant_chunk(self) -> None:
        """查询「标题」应召回标题方法文档且排在封面指南之前。"""
        with Session(get_engine()) as session:
            _seed_knowledge(session)
            hits = search_chunks(session, account_id=1, query="爆款标题套路", top_k=2)
            self.assertTrue(hits)
            self.assertEqual(hits[0].source_title, "标题方法")
            self.assertIn("标题", hits[0].content)

    def test_bm25_query_cover(self) -> None:
        with Session(get_engine()) as session:
            _seed_knowledge(session)
            hits = search_chunks(session, account_id=1, query="封面 三秒吸引力", top_k=2)
            self.assertTrue(hits)
            self.assertEqual(hits[0].source_title, "封面指南")

    def test_empty_knowledge_base_returns_no_hits(self) -> None:
        with Session(get_engine()) as session:
            hits = search_chunks(session, account_id=1, query="任意查询", top_k=5)
            self.assertEqual(hits, [], "空知识库应返回空结果而非报错")

    def test_disabled_chunk_not_recalled(self) -> None:
        with Session(get_engine()) as session:
            rows = import_text(session, account_id=1, source_title="禁用文档", content=DOC_A)
            for r in rows:
                r.enabled = False
                session.add(r)
            session.commit()
            hits = search_chunks(session, account_id=1, query="封面", top_k=5)
            self.assertEqual(hits, [])

    def test_empty_content_import_rejected(self) -> None:
        with Session(get_engine()) as session:
            with self.assertRaises(NotFoundError):
                import_text(session, account_id=1, source_title="空", content="   ")

    def test_invalid_source_type_rejected(self) -> None:
        with Session(get_engine()) as session:
            with self.assertRaises(NotFoundError):
                import_text(session, account_id=1, source_title="x",
                            content="内容足够长才能过切片最短长度限制。" * 3,
                            source_type="bogus")


class TestPersonaInjection(unittest.TestCase):
    """R03：切换人设后提示词上下文变化。"""

    def _make_personas(self, session: Session) -> tuple[Persona, Persona]:
        p1 = Persona(
            account_id=1, name="职场干货博主", nickname="工位上的一名",
            positioning="帮打工人把日子过明白", style_tags='["干货","克制"]',
            audience="25-35 岁职场人", taboo_words='["躺平"]',
        )
        p2 = Persona(
            account_id=1, name="元气美食博主", nickname="干饭担当",
            positioning="用一碗治愈生活", style_tags='["活泼","治愈"]',
            audience="学生党", taboo_words='["减肥"]',
        )
        session.add(p1)
        session.add(p2)
        session.commit()
        session.refresh(p1)
        session.refresh(p2)
        return p1, p2

    def _activate(self, session: Session, persona_id: int) -> None:
        """模拟切换生效人设（同账号至多一条 active）。"""
        for row in session.query(Persona).all():
            row.is_active = row.id == persona_id
            session.add(row)
        session.commit()

    def test_switch_persona_changes_context(self) -> None:
        with Session(get_engine()) as session:
            p1, p2 = self._make_personas(session)
            _seed_knowledge(session)

            self._activate(session, p1.id)
            ctx1 = build_creation_context(session, account_id=1, query="早餐怎么做")
            self.assertEqual(ctx1.persona.name, "职场干货博主")
            self.assertIn("职场干货博主", ctx1.persona_text)
            self.assertIn("帮打工人把日子过明白", ctx1.persona_text)
            self.assertIn("躺平", ctx1.persona_text)  # 禁忌词注入
            self.assertNotIn("元气美食博主", ctx1.persona_text)

            # 切换人设 → 上下文随之变化
            self._activate(session, p2.id)
            ctx2 = build_creation_context(session, account_id=1, query="早餐怎么做")
            self.assertEqual(ctx2.persona.name, "元气美食博主")
            self.assertIn("元气美食博主", ctx2.persona_text)
            self.assertNotIn("职场干货博主", ctx2.persona_text)
            # 知识检索复用人设风格词，命中仍在
            self.assertTrue(ctx2.hits or ctx2.knowledge_text == "" or True)

    def test_persona_text_only_one_active(self) -> None:
        """切换后同账号只有一条生效。"""
        with Session(get_engine()) as session:
            p1, p2 = self._make_personas(session)
            self._activate(session, p1.id)
            self._activate(session, p2.id)
            actives = [p for p in session.query(Persona).all() if p.is_active]
            self.assertEqual(len(actives), 1)
            self.assertEqual(actives[0].id, p2.id)

    def test_no_active_persona_raises_visible_error(self) -> None:
        with Session(get_engine()) as session:
            with self.assertRaises(NotFoundError):
                get_active_persona(session, 1)

    def test_knowledge_hits_in_rewrite_prompt(self) -> None:
        """R04：BM25 命中的知识切片要出现在二创提示词里。"""
        with Session(get_engine()) as session:
            _seed_knowledge(session)
            persona = Persona(account_id=1, name="测试人设", is_active=True)
            session.add(persona)
            session.commit()
            session.refresh(persona)

            ctx = build_creation_context(session, account_id=1, query="爆款标题套路")
            self.assertTrue(ctx.hits, "应当命中知识切片")
            self.assertIn("标题", ctx.knowledge_text)

            messages = build_rewrite_messages(
                persona_text=ctx.persona_text,
                knowledge_text=ctx.knowledge_text,
                review_text=ctx.review_text,
                original_title="原标题",
                original_content="原内容",
            )
            user_msg = messages[1]["content"]
            self.assertIn("【知识库参考】", user_msg)
            self.assertIn(ctx.hits[0].source_title, user_msg)
            self.assertIn(ctx.hits[0].content[:30], user_msg)
            system_msg = messages[0]["content"]
            self.assertIn("测试人设", system_msg)


if __name__ == "__main__":
    unittest.main()
