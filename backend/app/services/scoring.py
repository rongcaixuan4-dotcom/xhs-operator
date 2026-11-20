# -*- coding: utf-8 -*-
"""评分筛选服务：人设+知识库评分给 CollectedNote 打 match_score 决定入选（R06）。

评分构成（写入 score_detail 便于归因）：
- 人设匹配：标题/正文命中风格标签或定位关键词（+20/项，封顶 40）；
- 禁忌词：命中则直接 0 分并标注（不入选）；
- 知识库加分：BM25 命中数（+10/条，封顶 20）；
- 互动加权：log 缩放的赞/藏/评（0~30）；
- 话题权重：priority_weight 乘数（0.5~2.0 截断）。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date
from typing import Optional

from sqlmodel import Session, select

from ..db import get_app_setting, get_current_account_id
from ..errors import NotFoundError
from ..knowledge.retrieve import search_chunks
from ..models import CollectedNote, Persona, Topic
from .context import get_active_persona, get_review_text

logger = logging.getLogger("app.services.scoring")


def compute_match_score(
    session: Session,
    *,
    account_id: int,
    persona: Persona,
    note: CollectedNote,
    topic: Optional[Topic] = None,
) -> tuple[float, dict]:
    """计算单篇笔记匹配分。

    Returns:
        (match_score, score_detail)。命中禁忌词时 score=0。
    """
    detail: dict = {}
    text = f"{note.title} {note.content}".strip()

    # 1) 禁忌词：一票否决
    taboo_hits = [w for w in persona.taboo_word_list() if w and w in text]
    if taboo_hits:
        return 0.0, {"taboo_hits": taboo_hits, "reason": "命中禁忌词，一票否决"}

    # 2) 人设匹配（风格标签 + 定位关键词）
    persona_keywords = [t for t in persona.style_tag_list() if t]
    positioning_words = [w for w in persona.positioning.split() if len(w) >= 2]
    matched = [k for k in set(persona_keywords + positioning_words) if k in text]
    persona_score = min(40.0, len(matched) * 20.0)
    detail["persona_matched"] = matched
    detail["persona_score"] = persona_score

    # 3) 知识库加分（BM25 命中数）
    try:
        hits = search_chunks(session, account_id=account_id, query=text[:200], top_k=5)
        knowledge_score = min(20.0, len(hits) * 10.0)
        detail["knowledge_hits"] = [h.source_title for h in hits[:3]]
    except Exception as exc:  # noqa: BLE001 - 检索失败不阻塞评分，记日志
        logger.warning("评分时知识检索失败（按 0 分处理）：%s", exc)
        knowledge_score = 0.0
    detail["knowledge_score"] = knowledge_score

    # 4) 互动加权（log 缩放：1 赞 ≈ 1.5 分，封顶 30）
    engagement = note.likes * 1.5 + note.collects * 2.0 + note.comments * 1.0
    engagement_score = min(30.0, math.log1p(max(engagement, 0)) * 6.0)
    detail["engagement_score"] = round(engagement_score, 2)

    # 5) 话题权重乘数（0.5~2.0）
    weight = 1.0
    if topic is not None:
        weight = max(0.5, min(topic.priority_weight, 2.0))
    detail["topic_weight"] = weight

    score = round((persona_score + knowledge_score + engagement_score) * weight, 2)
    return score, detail


def select_today_notes(
    session: Session,
    *,
    account_id: Optional[int] = None,
    min_notes: Optional[int] = None,
) -> list[CollectedNote]:
    """当日评分筛选：给 status=collected 的笔记打分，选出今日入选（≥min_notes）。

    已入选数与 min_notes 之差为本次目标数；今日新增不足时按现有分数选出全部可用的。
    """
    if account_id is None:
        account_id = get_current_account_id(session)
    if min_notes is None:
        min_notes = int(get_app_setting(session, "daily_min_notes", 3))

    persona = get_active_persona(session, account_id)
    today = date.today()

    # 当日已入选数
    already_selected = len(session.exec(
        select(CollectedNote).where(
            CollectedNote.account_id == account_id,
            CollectedNote.status == "selected",
            CollectedNote.collected_at >= f"{today} 00:00:00",  # SQLite datetime 字符串比较
        )
    ).all())
    need = max(0, min_notes - already_selected)
    if need == 0:
        logger.info("今日入选已达标（%s/%s），跳过筛选", already_selected, min_notes)
        return []

    # 待评分：当日 collected 状态
    candidates = session.exec(
        select(CollectedNote).where(
            CollectedNote.account_id == account_id,
            CollectedNote.status == "collected",
            CollectedNote.collected_at >= f"{today} 00:00:00",
        )
        .order_by(CollectedNote.collected_at.desc())  # type: ignore[attr-defined]
    ).all()
    if not candidates:
        logger.info("今日无可评分的采集笔记")
        return []

    # 批量取话题权重
    topics = {t.id: t for t in session.exec(select(Topic).where(Topic.account_id == account_id)).all()}

    scored: list[tuple[float, CollectedNote]] = []
    for note in candidates:
        score, detail = compute_match_score(
            session, account_id=account_id, persona=persona,
            note=note, topic=topics.get(note.topic_id),
        )
        note.match_score = score
        note.score_detail = json.dumps(detail, ensure_ascii=False)
        session.add(note)
        scored.append((score, note))
    session.commit()

    # 排序选前 need 篇（0 分/禁忌词不入）
    scored.sort(key=lambda x: x[0], reverse=True)
    selected: list[CollectedNote] = []
    for score, note in scored:
        if len(selected) >= need:
            break
        if score <= 0:
            continue
        note.status = "selected"
        session.add(note)
        selected.append(note)
    session.commit()

    logger.info(
        "评分筛选完成：候选=%s 目标=%s 入选=%s（今日累计 %s/%s）",
        len(candidates), need, len(selected), already_selected + len(selected), min_notes,
    )
    return selected
