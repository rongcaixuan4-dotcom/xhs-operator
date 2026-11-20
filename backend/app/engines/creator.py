# -*- coding: utf-8 -*-
"""二创引擎：调 ai 层改写标题/文案、生图二创、相似度粗检（<70%）。

流程（R07）：
1. services/context.build_creation_context 构建人设+知识+复盘上下文；
2. ai.llm.chat 产出新标题/文案/生图提示词（JSON）；
3. ai.image.generate_image 生图二创（带原稿首图走 edits 改绘，交叉降级）；
4. Pillow 后处理：尺寸校验（长边≤1080）、封面 3:4 裁剪；
5. 相似度粗检（difflib，标题+文案 vs 原稿）：≥0.70 自动重试一次（提高改写幅度）；
6. CreationDraft 入库（status=pending_review，交给审核服务结算）。
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
from pathlib import Path

from PIL import Image
from sqlmodel import Session

from ..ai import chat, generate_image
from ..ai.llm import parse_json_content
from ..ai.prompts import build_rewrite_messages
from ..config import get_settings
from ..db import get_app_setting, get_engine
from ..errors import AiError, EngineError
from ..models import CollectedNote, CreationDraft
from ..services.context import build_creation_context
from ..services.review import compute_dedup_hash, is_review_required

logger = logging.getLogger("app.engines.creator")

SIMILARITY_LIMIT = 0.70   # R07：相似度 <70%
MAX_IMAGE_EDGE = 1080     # 长边上限


def similarity_ratio(text_a: str, text_b: str) -> float:
    """标题+文案相似度（difflib quick ratio，粗检用）。"""
    return difflib.SequenceMatcher(None, text_a.strip(), text_b.strip()).ratio()


def _postprocess_image(abs_path: Path) -> tuple[Path, Path]:
    """Pillow 后处理：长边压到 MAX_IMAGE_EDGE；生成 3:4 封面。

    Returns:
        (处理后图片路径, 封面路径)。处理失败时抛 EngineError（错误可见）。
    """
    try:
        img = Image.open(abs_path).convert("RGB")
        # 长边缩放
        w, h = img.size
        if max(w, h) > MAX_IMAGE_EDGE:
            scale = MAX_IMAGE_EDGE / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        img.save(abs_path, quality=90)

        # 封面：居中裁 3:4
        w, h = img.size
        target_ratio = 3 / 4
        if w / h > target_ratio:  # 太宽 → 裁宽
            new_w = int(h * target_ratio)
            left = (w - new_w) // 2
            cover = img.crop((left, 0, left + new_w, h))
        else:  # 太高 → 裁高
            new_h = int(w / target_ratio)
            top = (h - new_h) // 2
            cover = img.crop((0, top, w, top + new_h))
        cover_path = abs_path.parent / f"cover_{abs_path.stem}.jpg"
        cover.save(cover_path, quality=90)
        return abs_path, cover_path
    except OSError as exc:
        raise EngineError(f"图片后处理失败：{exc}") from exc


def _to_relative(abs_path: Path) -> str:
    data_root = get_settings().data_dir.resolve()
    try:
        return abs_path.resolve().relative_to(data_root).as_posix()
    except ValueError:
        return abs_path.name


async def create_draft(note: CollectedNote, *, account_id: int = 1) -> CreationDraft:
    """对一篇采集笔记生成二创稿（R07）。

    Returns:
        CreationDraft（status=pending_review）。

    Raises:
        AiError: LLM/生图全部失败；
        EngineError: 图片处理失败。
    """
    settings = get_settings()
    query = note.title

    # 1) 上下文（人设 + 知识 + 复盘改进项）
    with Session(get_engine()) as session:
        ctx = build_creation_context(session, account_id=account_id, query=query)
        review_required = is_review_required(session, account_id)

    # 2) LLM 改写（相似度不达标自动重试一次，加重改写幅度）
    rewrite = None
    last_error = ""
    for attempt in (1, 2):
        messages = build_rewrite_messages(
            persona_text=ctx.persona_text,
            knowledge_text=ctx.knowledge_text,
            review_text=ctx.review_text,
            original_title=note.title,
            original_content=note.content,
        )
        if attempt == 2:
            messages[1]["content"] += (
                "\n\n注意：上一次改写与原文过于相似。请大幅调整句式、结构与叙述角度，"
                "只保留核心信息点，重复率必须低于 70%。"
            )
        logger.info("二创改写：note=%s attempt=%s 上下文[%s]", note.id, attempt, ctx.to_log_summary())
        rewrite, last_error = await _call_llm(account_id, messages)
        if rewrite is None:
            continue
        ratio = similarity_ratio(
            f"{note.title} {note.content}",
            f"{rewrite.get('title', '')} {rewrite.get('content', '')}",
        )
        logger.info("二创相似度粗检：attempt=%s ratio=%.2f", attempt, ratio)
        if ratio < SIMILARITY_LIMIT:
            rewrite["_similarity"] = ratio
            break
        rewrite = None
        last_error = f"相似度 {ratio:.2f} ≥ {SIMILARITY_LIMIT}（重试）"
    if rewrite is None:
        raise AiError(f"二创改写失败：{last_error}")

    # 3) 生图二创（带原稿首图 → edits 改绘，交叉降级）
    image_prompt = rewrite.get("image_prompt") or f"小红书风格配图：{rewrite.get('title', '')}"
    ref_image = None
    local_imgs = note.local_image_list()
    if local_imgs:
        ref_image = str(settings.data_dir / local_imgs[0])
    img_result = await generate_image(image_prompt, config=_load_img_cfg(account_id), ref_image_path=ref_image)
    if not img_result.ok or not img_result.image_path:
        raise AiError(f"生图二创失败：{img_result.error}")

    abs_img = settings.data_dir / img_result.image_path
    abs_img, abs_cover = await asyncio.to_thread(_postprocess_image, abs_img)

    # 4) 入库
    draft = CreationDraft(
        account_id=account_id,
        collected_note_id=note.id,
        persona_id=ctx.persona.id,
        title=rewrite.get("title", "")[:100],
        content=rewrite.get("content", ""),
        image_paths=json.dumps([_to_relative(abs_img)], ensure_ascii=False),
        cover_path=_to_relative(abs_cover),
        similarity_score=round(float(rewrite.get("_similarity", 0.0)), 4),
        status="pending_review" if review_required else "approved",
    )
    with Session(get_engine()) as session:
        session.add(draft)
        session.commit()
        session.refresh(draft)
    logger.info("二创稿已生成：draft=%s note=%s similarity=%.2f", draft.id, note.id, draft.similarity_score)
    return draft


async def _call_llm(account_id: int, messages: list[dict]) -> tuple[dict | None, str]:
    """调用 LLM 并解析 JSON。失败返回 (None, error)。"""
    from ..ai.llm import load_llm_config
    from sqlmodel import Session as _Session

    with _Session(get_engine()) as session:
        config = load_llm_config(session, account_id)
    result = await chat(
        messages[-1]["content"],
        config=config,
        system=messages[0]["content"] if messages and messages[0]["role"] == "system" else None,
        json_mode=True,
    )
    if not result.ok:
        return None, result.error or "LLM 调用失败"
    parsed = parse_json_content(result.content or "")
    if not parsed or not parsed.get("title") or not parsed.get("content"):
        return None, "LLM 输出缺少 title/content 字段"
    return parsed, ""


def _load_llm_cfg(account_id: int):
    from ..ai.llm import load_llm_config
    from sqlmodel import Session as _Session

    with _Session(get_engine()) as session:
        return load_llm_config(session, account_id)


def _load_img_cfg(account_id: int):
    from ..ai.llm import load_image_config
    from sqlmodel import Session as _Session

    with _Session(get_engine()) as session:
        return load_image_config(session, account_id)


def draft_dedup_hash(draft: CreationDraft) -> str:
    """由二创稿计算 dedup_hash（标题 + 图片字节指纹）。"""
    image_paths = draft.image_path_list()
    return compute_dedup_hash(draft.title, image_paths)


def get_app_setting_json(session: Session, key: str, default=None):
    """转发 db.get_app_setting（保持 creator 内部不再依赖 db 细节时的一致命名）。"""
    return get_app_setting(session, key, default)
