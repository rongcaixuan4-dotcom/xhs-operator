# -*- coding: utf-8 -*-
"""采集引擎：打开搜索结果页解析列表项（图/题/文/作者/互动数），下载图片到本地。

流程（R06）：
1. BrowserManager 租借页面 → 打开小红书搜索结果页（关键词）；
2. 解析笔记卡片：note_id（href 提取）/ 封面图 / 标题 / 作者 / 互动数；
3. 图片经 CDN URL 下载到 data/collected/（仅公开图片 URL，不涉及签名）；
4. CollectedNote 入库（UNIQUE(account_id, xhs_note_id) 防重采，重复直接跳过）。

安全红线：只读页面 DOM 与公开图片 URL；绝不逆向签名、绝不 Cookie 直连。
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

import httpx
from sqlmodel import Session

from ..config import get_settings
from ..db import get_engine
from ..errors import EngineError
from ..models import CollectedNote, Topic
from .browser import acquire_page, release_page, screenshot_async

logger = logging.getLogger("app.engines.collect")

SEARCH_URL = "https://www.xiaohongshu.com/search_result?keyword={keyword}&source=web_explore_feed"
NOTE_ID_RE = re.compile(r"/(?:search_result|explore|discovery/item)/([0-9a-f]{24})")

# 小红书搜索页卡片选择器（改版时按真机校准，此处用宽松匹配兜底）
CARD_SELECTOR = "section.note-item, div.note-item"
COVER_IMG_SELECTOR = "img.cover, img[src*='sns-img'], img"
TITLE_SELECTOR = "a.title span, .title, a.title"
AUTHOR_SELECTOR = ".author .name, .author-wrapper .name, .name"
LIKE_SELECTOR = ".like-wrapper .count, .count"


@dataclass
class ScrapedNote:
    """单条采集结果（解析产物）。"""

    xhs_note_id: str = ""
    title: str = ""
    content: str = ""
    author: str = ""
    image_urls: list[str] = field(default_factory=list)
    likes: int = 0
    collects: int = 0
    comments: int = 0


def _parse_count(text: str) -> int:
    """互动数文本转 int（"1.2万" → 12000）。"""
    text = (text or "").strip()
    if not text:
        return 0
    try:
        if text.endswith("万"):
            return int(float(text[:-1]) * 10000)
        if text.endswith("w"):
            return int(float(text[:-1]) * 10000)
        return int(re.sub(r"[^\d]", "", text) or 0)
    except ValueError:
        return 0


async def _download_images(urls: list[str], xhs_note_id: str, limit: int = 3) -> list[str]:
    """下载公开图片到 data/collected/，返回 data/ 相对路径列表。失败图片跳过并记日志。"""
    settings = get_settings()
    out_dir = settings.collected_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    local_paths: list[str] = []
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for i, url in enumerate(urls[:limit]):
            try:
                resp = await client.get(url)
                if resp.status_code != 200 or len(resp.content) < 1024:
                    logger.warning("图片下载异常：note=%s i=%s status=%s", xhs_note_id, i, resp.status_code)
                    continue
                suffix = ".jpg"
                path = out_dir / f"{xhs_note_id}_{i}{suffix}"
                path.write_bytes(resp.content)
                local_paths.append(path.relative_to(settings.data_dir).as_posix())
            except httpx.HTTPError as exc:
                logger.warning("图片下载失败：note=%s url=%s err=%s", xhs_note_id, url, exc)
    return local_paths


def _extract_from_card(card) -> ScrapedNote:
    """从单个卡片 DOM 节点提取 ScrapedNote（选择器逐项容错，缺失字段置空）。"""
    note = ScrapedNote()
    link = card.query_selector("a[href]")
    if link is not None:
        href = link.get_attribute("href") or ""
        m = NOTE_ID_RE.search(href)
        if m:
            note.xhs_note_id = m.group(1)
    img = card.query_selector(COVER_IMG_SELECTOR)
    if img is not None:
        src = img.get_attribute("src") or img.get_attribute("data-src") or ""
        if src.startswith("http"):
            note.image_urls.append(src)
    title_el = card.query_selector(TITLE_SELECTOR)
    note.title = (title_el.inner_text() if title_el else "").strip()
    author_el = card.query_selector(AUTHOR_SELECTOR)
    note.author = (author_el.inner_text() if author_el else "").strip()
    like_el = card.query_selector(LIKE_SELECTOR)
    note.likes = _parse_count(like_el.inner_text() if like_el else "0")
    return note


async def collect_topic(topic: Topic, *, limit_multiplier: float = 2.0) -> int:
    """执行一次话题采集（R05/R06）。

    Args:
        topic: Topic 行（须 enabled）。
        limit_multiplier: 解析卡片数 = daily_quota * multiplier（给筛选留余量）。

    Returns:
        本次新增入库条数（重复采到的不计入）。

    Raises:
        EngineError: 浏览器/页面级失败（调用方捕获写 PipelineRun）。
    """
    settings = get_settings()
    limit = max(3, int(topic.daily_quota * limit_multiplier))
    page = await acquire_page()
    scraped: list[ScrapedNote] = []
    try:
        url = SEARCH_URL.format(keyword=topic.keyword)
        logger.info("采集开始：topic=%s url=%s limit=%s", topic.keyword, url, limit)
        await asyncio.to_thread(page.goto, url, None, "domcontentloaded")
        await asyncio.sleep(3)  # 等瀑布流首屏

        # 滚动加载更多卡片
        for _ in range(3):
            await asyncio.to_thread(page.mouse.wheel, 0, 2000)
            await asyncio.sleep(1.5)

        cards = await asyncio.to_thread(page.query_selector_all, CARD_SELECTOR)
        if not cards:
            shot = await screenshot_async(page, f"collect_empty_{topic.id}")
            raise EngineError(
                f"搜索页未解析到笔记卡片（可能改版或风控），关键词：{topic.keyword}",
                detail={"screenshot": shot, "url": url},
            )

        for card in cards[:limit]:
            try:
                note = await asyncio.to_thread(_extract_from_card, card)
            except Exception as exc:  # noqa: BLE001 - 单卡片解析失败不影响整轮
                logger.warning("卡片解析失败（跳过）：%s", exc)
                continue
            if note.xhs_note_id and note.title:
                scraped.append(note)

        if not scraped:
            shot = await screenshot_async(page, f"collect_parse_fail_{topic.id}")
            raise EngineError(
                f"卡片解析全部失败（结构可能变化），关键词：{topic.keyword}",
                detail={"screenshot": shot},
            )
    except EngineError:
        raise
    except Exception as exc:
        shot = await screenshot_async(page, f"collect_error_{topic.id}")
        raise EngineError(f"采集页面操作失败：{exc}", detail={"screenshot": shot}) from exc
    finally:
        await release_page(page)

    # 入库（UNIQUE 防重采）+ 图片本地化
    added = 0
    with Session(get_engine()) as session:
        for note in scraped:
            exists = session.query(CollectedNote).filter_by(
                account_id=topic.account_id, xhs_note_id=note.xhs_note_id
            ).first()
            if exists is not None:
                continue
            local_imgs = await _download_images(note.image_urls, note.xhs_note_id)
            row = CollectedNote(
                account_id=topic.account_id,
                topic_id=topic.id,
                xhs_note_id=note.xhs_note_id,
                title=note.title[:200],
                content=note.content,
                author=note.author,
                image_urls=_json_list(note.image_urls),
                local_images=_json_list(local_imgs),
                likes=note.likes,
                collects=note.collects,
                comments=note.comments,
                status="collected",
            )
            session.add(row)
            added += 1
        session.commit()

    logger.info("采集完成：topic=%s 解析=%s 新增=%s", topic.keyword, len(scraped), added)
    return added


def _json_list(items: list[str]) -> str:
    import json

    return json.dumps(items, ensure_ascii=False)


async def check_note_detail_content(page, xhs_note_id: str) -> ScrapedNote:
    """打开笔记详情页补充正文（列表卡片只有标题；正文用于相似度比对与评分）。

    失败时返回仅含标题的 ScrapedNote（不阻塞采集主流程）。
    """
    url = f"https://www.xiaohongshu.com/explore/{xhs_note_id}"
    note = ScrapedNote(xhs_note_id=xhs_note_id)
    try:
        await asyncio.to_thread(page.goto, url, None, "domcontentloaded")
        await asyncio.sleep(2)
        content_el = await asyncio.to_thread(page.query_selector, "#detail-desc, .desc, .note-text")
        if content_el is not None:
            note.content = (await asyncio.to_thread(content_el.inner_text)).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("详情页正文获取失败：note=%s err=%s", xhs_note_id, exc)
    return note
