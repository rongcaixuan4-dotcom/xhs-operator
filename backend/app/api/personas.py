# -*- coding: utf-8 -*-
"""人设 API：CRUD、启停、设为生效（R03）。同账号至多一条 is_active=1（服务层保证）。"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import get_current_account_id, get_session
from ..errors import NotFoundError, ok
from ..models import Persona

logger = logging.getLogger("app.api.personas")

router = APIRouter(prefix="/personas")


def _dump(row: Persona) -> dict:
    """Persona → 前端字典（JSON 字段反序列化）。"""
    return {
        "id": row.id,
        "name": row.name,
        "nickname": row.nickname,
        "positioning": row.positioning,
        "style_tags": row.style_tag_list(),
        "audience": row.audience,
        "taboo_words": row.taboo_word_list(),
        "sample_notes": row.sample_note_list(),
        "is_active": row.is_active,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _parse_str_list(value, field_name: str) -> list[str]:
    """容忍前端传字符串数组或逗号分隔字符串。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [x.strip() for x in value.split("、|,|，|\n") if x.strip()]
    raise NotFoundError(f"{field_name} 应为字符串数组")


class PersonaPayload(BaseModel):
    """人设创建/更新载荷。"""

    name: str = ""
    nickname: str = ""
    positioning: str = ""
    style_tags: list[str] = []
    audience: str = ""
    taboo_words: list[str] = []
    sample_notes: list[str] = []
    is_active: bool | None = None


@router.get("")
async def list_personas(session: Session = Depends(get_session)) -> dict:
    """人设列表（含生效标记）。"""
    account_id = get_current_account_id(session)
    rows = session.exec(
        select(Persona).where(Persona.account_id == account_id)
        .order_by(Persona.updated_at.desc())  # type: ignore[attr-defined]
    ).all()
    return ok([_dump(r) for r in rows])


@router.post("")
async def create_persona(payload: PersonaPayload, session: Session = Depends(get_session)) -> dict:
    """创建人设（is_active=true 时先取消其他生效人设）。"""
    account_id = get_current_account_id(session)
    if not payload.name.strip():
        return {"code": 1400, "data": None, "message": "人设名不能为空"}
    row = Persona(
        account_id=account_id,
        name=payload.name.strip(),
        nickname=payload.nickname.strip(),
        positioning=payload.positioning.strip(),
        style_tags=json.dumps(_parse_str_list(payload.style_tags, "style_tags"), ensure_ascii=False),
        audience=payload.audience.strip(),
        taboo_words=json.dumps(_parse_str_list(payload.taboo_words, "taboo_words"), ensure_ascii=False),
        sample_notes=json.dumps(payload.sample_notes, ensure_ascii=False),
        is_active=False,
    )
    if payload.is_active:
        _deactivate_others(session, account_id, None)
        row.is_active = True
    session.add(row)
    session.commit()
    session.refresh(row)
    logger.info("人设已创建：id=%s name=%s active=%s", row.id, row.name, row.is_active)
    return ok(_dump(row), "人设已创建")


@router.put("/{persona_id}")
async def update_persona(persona_id: int, payload: PersonaPayload, session: Session = Depends(get_session)) -> dict:
    """更新人设。"""
    row = session.get(Persona, persona_id)
    if row is None:
        raise NotFoundError(f"人设不存在：{persona_id}")
    row.name = payload.name.strip() or row.name
    row.nickname = payload.nickname.strip()
    row.positioning = payload.positioning.strip()
    row.style_tags = json.dumps(_parse_str_list(payload.style_tags, "style_tags"), ensure_ascii=False)
    row.audience = payload.audience.strip()
    row.taboo_words = json.dumps(_parse_str_list(payload.taboo_words, "taboo_words"), ensure_ascii=False)
    row.sample_notes = json.dumps(payload.sample_notes, ensure_ascii=False)
    row.updated_at = row.updated_at.now()
    if payload.is_active is True and not row.is_active:
        _deactivate_others(session, row.account_id, persona_id)
        row.is_active = True
    elif payload.is_active is False:
        row.is_active = False
    session.add(row)
    session.commit()
    logger.info("人设已更新：id=%s", persona_id)
    return ok(_dump(row), "人设已保存")


@router.delete("/{persona_id}")
async def delete_persona(persona_id: int, session: Session = Depends(get_session)) -> dict:
    """删除人设（生效中的人设不允许删，先切换）。"""
    row = session.get(Persona, persona_id)
    if row is None:
        raise NotFoundError(f"人设不存在：{persona_id}")
    if row.is_active:
        return {"code": 1400, "data": None, "message": "生效中人设不可删除，请先切换到其他人设"}
    session.delete(row)
    session.commit()
    logger.info("人设已删除：id=%s", persona_id)
    return ok({"id": persona_id}, "人设已删除")


@router.post("/{persona_id}/activate")
async def activate_persona(persona_id: int, session: Session = Depends(get_session)) -> dict:
    """设为当前生效人设（同账号其余置 inactive，R03 切换验收）。"""
    row = session.get(Persona, persona_id)
    if row is None:
        raise NotFoundError(f"人设不存在：{persona_id}")
    _deactivate_others(session, row.account_id, persona_id)
    row.is_active = True
    row.updated_at = row.updated_at.now()
    session.add(row)
    session.commit()
    logger.info("人设已设为生效：id=%s name=%s", row.id, row.name)
    return ok(_dump(row), f"已切换生效人设：{row.name}")


def _deactivate_others(session: Session, account_id: int, keep_id: int | None) -> None:
    """把同账号其余人设置为 inactive。"""
    rows = session.exec(
        select(Persona).where(
            Persona.account_id == account_id, Persona.is_active == True  # noqa: E712
        )
    ).all()
    for r in rows:
        if r.id != keep_id:
            r.is_active = False
            session.add(r)
