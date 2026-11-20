# -*- coding: utf-8 -*-
"""知识库 API：文件/粘贴导入、列表、启停、检索预览（R04）。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, select

from ..config import get_settings
from ..db import get_current_account_id, get_session
from ..errors import NotFoundError, ok
from ..knowledge import ingest as knowledge_ingest
from ..knowledge import retrieve as knowledge_retrieve
from ..models import KnowledgeChunk

logger = logging.getLogger("app.api.knowledge")

router = APIRouter(prefix="/knowledge")

ALLOWED_SUFFIX = {".txt", ".md", ".docx"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB


def _dump(row: KnowledgeChunk) -> dict:
    return {
        "id": row.id,
        "source_type": row.source_type,
        "source_title": row.source_title,
        "content": row.content,
        "char_count": row.char_count,
        "enabled": row.enabled,
        "created_at": row.created_at.isoformat(),
    }


@router.post("/import-file")
async def import_file(
    file: UploadFile = File(...),
    source_type: str = Form("experience"),
    session: Session = Depends(get_session),
) -> dict:
    """拖拽上传 txt/md/docx，切片入库（R04）。"""
    account_id = get_current_account_id(session)
    if not file.filename:
        return {"code": 1400, "data": None, "message": "未选择文件"}
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIX:
        return {"code": 1400, "data": None, "message": f"仅支持 {'/'.join(ALLOWED_SUFFIX)}，收到：{suffix}"}

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        return {"code": 1400, "data": None, "message": "文件超过 20MB 上限"}

    # 先落盘到 data/knowledge_imports/（留痕，方便排查）
    import_dir = get_settings().data_dir / "knowledge_imports"
    import_dir.mkdir(parents=True, exist_ok=True)
    saved = import_dir / f"{int(time.time())}_{file.filename}"
    saved.write_bytes(raw)

    try:
        rows = knowledge_ingest.import_file(
            session, account_id=account_id, file_path=saved, source_type=source_type
        )
    except Exception:
        logger.exception("知识文件导入失败：%s", saved)
        raise
    return ok({"count": len(rows), "title": file.filename}, f"导入成功：{len(rows)} 个切片")


class ImportTextPayload(BaseModel):
    """文本粘贴导入载荷。"""

    title: str = ""
    content: str = ""
    source_type: str = "experience"  # experience | review | negative


@router.post("/import-text")
async def import_text(payload: ImportTextPayload, session: Session = Depends(get_session)) -> dict:
    """文本粘贴导入（含来源标注，R04）。"""
    account_id = get_current_account_id(session)
    rows = knowledge_ingest.import_text(
        session,
        account_id=account_id,
        source_title=payload.title or "粘贴文本",
        content=payload.content,
        source_type=payload.source_type,
    )
    return ok({"count": len(rows)}, f"导入成功：{len(rows)} 个切片")


@router.get("/chunks")
async def list_chunks(
    source_type: str | None = None,
    enabled: bool | None = None,
    page: int = 1,
    page_size: int = 50,
    session: Session = Depends(get_session),
) -> dict:
    """知识切片列表（分页，可按来源/启用状态筛选）。"""
    account_id = get_current_account_id(session)
    stmt = select(KnowledgeChunk).where(KnowledgeChunk.account_id == account_id)
    if source_type:
        stmt = stmt.where(KnowledgeChunk.source_type == source_type)
    if enabled is not None:
        stmt = stmt.where(KnowledgeChunk.enabled == enabled)
    stmt = stmt.order_by(KnowledgeChunk.created_at.desc())  # type: ignore[attr-defined]
    all_rows = session.exec(stmt).all()
    total = len(all_rows)
    start = (max(page, 1) - 1) * page_size
    return ok({
        "total": total,
        "items": [_dump(r) for r in all_rows[start:start + page_size]],
    })


@router.patch("/chunks/{chunk_id}/toggle")
async def toggle_chunk(chunk_id: int, session: Session = Depends(get_session)) -> dict:
    """切片启停（停用切片不参与检索与注入）。"""
    row = session.get(KnowledgeChunk, chunk_id)
    if row is None:
        raise NotFoundError(f"知识切片不存在：{chunk_id}")
    row.enabled = not row.enabled
    session.add(row)
    session.commit()
    return ok({"id": chunk_id, "enabled": row.enabled})


@router.delete("/chunks/{chunk_id}")
async def delete_chunk(chunk_id: int, session: Session = Depends(get_session)) -> dict:
    """删除切片。"""
    row = session.get(KnowledgeChunk, chunk_id)
    if row is None:
        raise NotFoundError(f"知识切片不存在：{chunk_id}")
    session.delete(row)
    session.commit()
    return ok({"id": chunk_id}, "切片已删除")


class SearchPayload(BaseModel):
    """检索预览载荷。"""

    query: str
    top_k: int = 5


@router.post("/search")
async def search(payload: SearchPayload, session: Session = Depends(get_session)) -> dict:
    """检索预览（返回命中片段 + BM25 得分）。"""
    account_id = get_current_account_id(session)
    if not payload.query.strip():
        return {"code": 1400, "data": None, "message": "查询串为空"}
    hits = knowledge_retrieve.search_chunks(
        session, account_id=account_id, query=payload.query, top_k=max(1, min(payload.top_k, 20))
    )
    return ok([h.to_dict() for h in hits])
