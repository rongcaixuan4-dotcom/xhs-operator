# -*- coding: utf-8 -*-
"""提示词上下文构建：当前人设 + 检索命中的知识切片 + 复盘改进项注入（架构 1.1）。

产物供 T5 二创引擎、T4 评分、T8 周报使用。
复盘改进项按 scope 优先注入（架构 1.1）：prompt > publish_time > topic_weight。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlmodel import Session, select

from ..ai.prompts import insights_to_text, persona_to_system_text
from ..errors import NotFoundError
from ..knowledge.retrieve import SearchHit, search_chunks
from ..models import KnowledgeChunk, Persona, WeeklyReport

logger = logging.getLogger("app.services.context")


@dataclass
class CreationContext:
    """二创提示词上下文（人设 + 知识 + 复盘改进项）。"""

    persona: Persona
    persona_text: str                     # 人设文本段
    knowledge_text: str = ""              # 检索命中的知识切片拼接
    review_text: str = ""                 # 复盘改进项文本（scope 优先注入）
    hits: list[SearchHit] = field(default_factory=list)

    def to_log_summary(self) -> str:
        """日志摘要（R03 验收：切换人设后提示词中可见对应人设内容）。"""
        return (
            f"persona={self.persona.name} 知识片段数={len(self.hits)} "
            f"含复盘改进项={'是' if self.review_text else '否'}"
        )


def get_active_persona(session: Session, account_id: int) -> Persona:
    """获取当前生效人设（不存在则抛 NotFoundError）。"""
    row = session.exec(
        select(Persona).where(
            Persona.account_id == account_id, Persona.is_active == True  # noqa: E712
        )
    ).first()
    if row is None:
        raise NotFoundError("当前账号未设置生效人设，请先在「人设管理」页启用一个人设")
    return row


def get_review_text(session: Session, account_id: int) -> str:
    """取复盘改进项文本：优先已确认周报的 insights，其次 review 知识切片。"""
    report = session.exec(
        select(WeeklyReport)
        .where(
            WeeklyReport.account_id == account_id,
            WeeklyReport.status == "confirmed",
        )
        .order_by(WeeklyReport.week_start.desc())  # type: ignore[attr-defined]
    ).first()
    if report is not None:
        text = insights_to_text(report.insight_list())
        if text:
            return text
    # 回退：知识库里的 review 切片
    review_chunks = session.exec(
        select(KnowledgeChunk).where(
            KnowledgeChunk.account_id == account_id,
            KnowledgeChunk.source_type == "review",
            KnowledgeChunk.enabled == True,  # noqa: E712
        )
    ).all()
    return "\n".join(f"- {c.content}" for c in review_chunks[:10])


def build_creation_context(
    session: Session,
    *,
    account_id: int,
    query: str,
    persona: Optional[Persona] = None,
    top_k: int = 4,
) -> CreationContext:
    """构建二创上下文。

    Args:
        session: 数据库会话。
        account_id: 账号 id。
        query: 检索查询串（一般用「原稿标题 + 关键词」）。
        persona: 显式指定人设；不传则取当前生效人设。
        top_k: 知识命中条数上限。

    Returns:
        CreationContext（人设文本 + 知识文本 + 复盘文本）。
    """
    if persona is None:
        persona = get_active_persona(session, account_id)
    persona_text = persona_to_system_text(persona)

    # 知识检索：查询串拼上人设风格词，提升命中相关性
    query_full = query + " " + " ".join(persona.style_tag_list())
    hits = search_chunks(session, account_id=account_id, query=query_full, top_k=top_k)
    knowledge_text = "\n".join(f"[{h.source_title}] {h.content}" for h in hits)

    review_text = get_review_text(session, account_id)

    ctx = CreationContext(
        persona=persona,
        persona_text=persona_text,
        knowledge_text=knowledge_text,
        review_text=review_text,
        hits=hits,
    )
    logger.info("二创上下文构建完成：%s", ctx.to_log_summary())
    return ctx
