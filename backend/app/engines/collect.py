# -*- coding: utf-8 -*-
"""采集引擎：双通道采集（签名直连 API / Playwright 浏览器），下载图片到本地。

通道模式（AppSetting.collect_read_mode：browser | api | hybrid，默认 hybrid）：
- api：仅走签名直连只读接口（engines/xhs_api.py，签名由开源库 xhshow 提供）；
- browser：仅走浏览器页面解析（原有路径）；
- hybrid：API 优先，API 通道失败（无 Cookie/风控/网络）自动回退浏览器。

流程（R06）：
1. 按通道获取搜索结果 → 解析笔记卡片：note_id / 封面图 / 标题 / 作者 / 互动数；
2. 图片经 CDN URL 下载到 data/collected/（仅公开图片 URL）；
3. CollectedNote 入库（UNIQUE(account_id, xhs_note_id) 防重采，重复直接跳过）。

安全红线（全项目）：两通道均为只读；任何变更类（写）接口绝不经 API 直连，
发布一律真浏览器；浏览器通道只读页面 DOM，不逆向签名。
"""
from __future__ import annotations

import asyncio
import logging
import math
import re

import httpx
from sqlmodel import Session

from ..config import get_settings
from ..db import get_app_setting, get_engine
from ..errors import EngineError
from ..models import CollectedNote, Topic
from .browser import acquire_page, release_page, screenshot_async
from .xhs_api import (
    ApiChannelError,
    DEFAULT_USER_AGENT,
    ScrapedNote,
    XhsApiChannel,
    _parse_count,
    fetch_login_state,
)

logger = logging.getLogger("app.engines.collect")

SEARCH_URL = "https://www.xiaohongshu.com/search_result?keyword={keyword}&source=web_explore_feed"
NOTE_ID_RE = re.compile(r"/(?:search_result|explore|discovery/item)/([0-9a-f]{24})")

# 小红书搜索页卡片选择器（改版时按真机校准，此处用宽松匹配兜底）
CARD_SELECTOR = "section.note-item, div.note-item"
COVER_IMG_SELECTOR = "img.cover, img[src*='sns-img'], img"
TITLE_SELECTOR = "a.title span, .title, a.title"
AUTHOR_SELECTOR = ".author .name, .author-wrapper .name, .name"
LIKE_SELECTOR = ".like-wrapper .count, .count"

VALID_READ_MODES = ("browser", "api", "hybrid")


def _json_list(items: list[str]) -> str:
    import json

    return json.dumps(items, ensure_ascii=False)


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
    note = ScrapedNote(source="browser")
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


# ────────────────────────── 入库（两通道共用） ──────────────────────────
async def _save_notes(topic: Topic, scraped: list[ScrapedNote]) -> int:
    """CollectedNote 入库（UNIQUE 防重采）+ 图片本地化，返回新增条数。"""
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
    logger.info("采集入库完成：topic=%s 解析=%s 新增=%s", topic.keyword, len(scraped), added)
    return added


# ────────────────────────── 通道模式读取 ──────────────────────────
def get_read_mode() -> str:
    """采集通道模式：AppSetting.collect_read_mode 优先，回退 yaml/默认 hybrid。"""
    mode = None
    with Session(get_engine()) as session:
        mode = get_app_setting(session, "collect_read_mode", None)
    if mode not in VALID_READ_MODES:
        mode = get_settings().collect_read_mode
    if mode not in VALID_READ_MODES:
        mode = "hybrid"
    return mode


# ────────────────────────── API 直连通道 ──────────────────────────
async def _collect_via_api(topic: Topic, limit: int) -> int:
    """签名直连只读 API 采集（xhs_api 通道），返回新增入库条数。

    Raises:
        ApiChannelError / NoCookieError：无 Cookie、风控、接口改版等（hybrid 由
        collect_topic 捕获后回退浏览器；api 模式向上抛可见错误）。
    """
    cookie_str, user_agent = await fetch_login_state()
    channel = XhsApiChannel(
        cookie_str=cookie_str,
        user_agent=user_agent or DEFAULT_USER_AGENT,
    )
    scraped: list[ScrapedNote] = []
    try:
        page_size = 20
        total_pages = max(1, math.ceil(limit / page_size))
        for page in range(1, total_pages + 1):
            if page > 1:
                await channel.page_gap()  # 搜索翻页间隔 ≥10s + 抖动
            batch = await channel.search_notes(topic.keyword, page=page, page_size=page_size)
            if not batch:
                break
            scraped.extend(batch)
        scraped = scraped[:limit]

        # 补充正文与完整互动数（feed 详情）；单条失败用搜索卡片数据兜底，不阻塞整轮
        enriched: list[ScrapedNote] = []
        for note in scraped:
            try:
                detail = await channel.note_detail(note.xhs_note_id, note.xsec_token)
                note.title = note.title or detail.title
                note.content = detail.content
                note.author = note.author or detail.author
                note.likes = detail.likes or note.likes
                note.collects = detail.collects
                note.comments = detail.comments
                if detail.image_urls:
                    note.image_urls = detail.image_urls
            except ApiChannelError as exc:
                logger.warning(
                    "API 详情获取失败（用搜索卡片数据兜底）：note=%s err=%s",
                    note.xhs_note_id, exc,
                )
            if note.title:
                enriched.append(note)
        if not enriched:
            raise ApiChannelError(
                f"API 通道未解析到有效笔记（关键词：{topic.keyword}，可能风控或接口改版）"
            )
    finally:
        await channel.aclose()
    return await _save_notes(topic, enriched)


# ────────────────────────── 浏览器通道 ──────────────────────────
async def _collect_topic_browser(topic: Topic, limit: int) -> int:
    """浏览器页面解析采集（原路径），返回新增入库条数。"""
    page = await acquire_page()
    scraped: list[ScrapedNote] = []
    try:
        url = SEARCH_URL.format(keyword=topic.keyword)
        logger.info("采集开始（浏览器通道）：topic=%s url=%s limit=%s", topic.keyword, url, limit)
        await asyncio.to_thread(page.goto, url, wait_until="domcontentloaded")
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

    return await _save_notes(topic, scraped)


# ────────────────────────── 对外入口（调度/流水线统一调用） ──────────────────────────
async def collect_topic(topic: Topic, *, limit_multiplier: float = 2.0) -> int:
    """执行一次话题采集（R05/R06，双通道调度）。

    Args:
        topic: Topic 行（须 enabled）。
        limit_multiplier: 解析卡片数 = daily_quota * multiplier（给筛选留余量）。

    Returns:
        本次新增入库条数（重复采到的不计入）。

    Raises:
        EngineError: 所选通道失败且无法回退（api 模式 / browser 模式直抛）。
    """
    limit = max(3, int(topic.daily_quota * limit_multiplier))
    mode = get_read_mode()

    if mode in ("api", "hybrid"):
        try:
            added = await _collect_via_api(topic, limit)
            logger.info("采集完成（API 直连）：topic=%s 新增=%s", topic.keyword, added)
            return added
        except ApiChannelError as exc:
            if mode == "api":
                raise EngineError(f"API 直连采集失败：{exc}") from exc
            logger.warning(
                "API 直连采集失败，自动回退浏览器通道：topic=%s err=%s", topic.keyword, exc
            )
    return await _collect_topic_browser(topic, limit=limit)


async def check_note_detail_content(page, xhs_note_id: str) -> ScrapedNote:
    """打开笔记详情页补充正文（列表卡片只有标题；正文用于相似度比对与评分）。

    失败时返回仅含标题的 ScrapedNote（不阻塞采集主流程）。
    """
    url = f"https://www.xiaohongshu.com/explore/{xhs_note_id}"
    note = ScrapedNote(xhs_note_id=xhs_note_id, source="browser")
    try:
        await asyncio.to_thread(page.goto, url, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        content_el = await asyncio.to_thread(page.query_selector, "#detail-desc, .desc, .note-text")
        if content_el is not None:
            note.content = (await asyncio.to_thread(content_el.inner_text)).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("详情页正文获取失败：note=%s err=%s", xhs_note_id, exc)
    return note
