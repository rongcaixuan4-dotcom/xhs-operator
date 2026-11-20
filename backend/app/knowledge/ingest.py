# -*- coding: utf-8 -*-
"""知识入库：txt/md/docx 解析 → 按段落/长度切片 → 入库 KnowledgeChunk。

切片策略（架构 1.1）：先按空行/标题切段落，超长段落按 chunk_size 滑窗再切
（带 overlap 保证上下文连续）；默认单片 ~500 字。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from sqlmodel import Session

from ..errors import NotFoundError
from ..models import KnowledgeChunk

logger = logging.getLogger("app.knowledge.ingest")

CHUNK_SIZE = 500     # 单片目标字数
OVERLAP = 50         # 相邻片重叠字数
MIN_CHUNK_LEN = 20   # 短于该值丢弃（碎片噪声）


def parse_file(path: Path) -> str:
    """解析 txt/md/docx 为纯文本。不支持的格式抛 NotFoundError（错误可见）。"""
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".docx":
        # 延迟导入：只有用到 docx 才加载 python-docx
        from docx import Document

        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        # 表格内容也并入
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts)
    raise NotFoundError(f"不支持的文件格式：{suffix}（仅支持 txt/md/docx）")


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = OVERLAP) -> list[str]:
    """文本切片：段落优先，超长段落滑窗再切。"""
    chunks: list[str] = []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|(?m)^#{1,4}\s", text) if p.strip()]
    for para in paragraphs:
        if len(para) <= chunk_size:
            chunks.append(para)
            continue
        step = chunk_size - overlap
        for i in range(0, len(para), step):
            piece = para[i:i + chunk_size].strip()
            if len(piece) >= MIN_CHUNK_LEN:
                chunks.append(piece)
            if i + chunk_size >= len(para):
                break
    return [c for c in chunks if len(c) >= MIN_CHUNK_LEN]


def import_text(
    session: Session,
    *,
    account_id: int,
    source_title: str,
    content: str,
    source_type: str = "experience",
    enabled: bool = True,
) -> list[KnowledgeChunk]:
    """文本导入：切片入库。返回新建切片列表。"""
    if not content or not content.strip():
        raise NotFoundError("导入内容为空")
    if source_type not in ("experience", "review", "negative"):
        raise NotFoundError(f"非法来源类型：{source_type}")

    slices = chunk_text(content)
    rows: list[KnowledgeChunk] = []
    for idx, piece in enumerate(slices):
        row = KnowledgeChunk(
            account_id=account_id,
            source_type=source_type,
            source_title=source_title or (f"导入文本-{idx + 1}"),
            content=piece,
            char_count=len(piece),
            enabled=enabled,
        )
        session.add(row)
        rows.append(row)
    session.commit()
    logger.info("知识导入完成：title=%s 切片数=%s 总字数=%s", source_title, len(rows), len(content))
    return rows


def import_file(
    session: Session,
    *,
    account_id: int,
    file_path: Path,
    source_title: Optional[str] = None,
    source_type: str = "experience",
) -> list[KnowledgeChunk]:
    """文件导入（txt/md/docx）。"""
    text = parse_file(file_path)
    title = source_title or file_path.stem
    return import_text(
        session, account_id=account_id, source_title=title,
        content=text, source_type=source_type,
    )
