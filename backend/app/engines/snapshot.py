# -*- coding: utf-8 -*-
"""快照引擎：读创作者中心数据页，写 NoteStatSnapshot / AccountSnapshot（R11）。

- 笔记快照：对每个 done 的发布任务，读笔记管理列表页的赞/藏/评
  （曝光数口径需真机确认——架构 UNCLEAR #4；读不到时退化为列表页可见的赞/藏/评，exposure 记 0）；
- 账号快照：读创作者中心数据中心页的粉丝数/周曝光/周互动；
- 调度每小时触发一次，验收要求每篇笔记每日 ≥1 条快照。
"""
from __future__ import annotations

import asyncio
import logging
import re

from sqlmodel import Session, select

from ..db import get_engine
from ..errors import EngineError
from ..models import AccountSnapshot, NoteStatSnapshot, PublishTask
from .browser import XHS_CREATOR, acquire_page, bp_call, release_page, screenshot_async

logger = logging.getLogger("app.engines.snapshot")

# 创作者中心数据页（改版时校准选择器）
NOTE_MANAGE_URL = f"{XHS_CREATOR}/publish/noteManage"
DATA_CENTER_URL = f"{XHS_CREATOR}/data-center"

NUM_RE = re.compile(r"[\d,\.万]+")
NOTE_ROW_SELECTOR = ".note-item, table tbody tr"
STAT_CELL_SELECTOR = ".count, td"


def _parse_count(text: str) -> int:
    """互动数文本转 int（"1,234" / "1.2万"）。"""
    text = (text or "").strip().replace(",", "")
    if not text:
        return 0
    try:
        if text.endswith("万"):
            return int(float(text[:-1]) * 10000)
        return int(float(re.sub(r"[^\d.]", "", text) or 0))
    except ValueError:
        return 0


async def snapshot_daily(account_id: int = 1) -> dict:
    """执行一轮数据快照（笔记级 + 账号级）。

    Returns:
        {"notes": 新增笔记快照数, "account": bool, "error": 累计错误（可空）}
    """
    errors: list[str] = []
    page = await acquire_page()
    note_count = 0
    account_ok = False
    try:
        # ── 1) 笔记级快照 ──
        with Session(get_engine()) as session:
            done_tasks = session.exec(
                select(PublishTask).where(
                    PublishTask.account_id == account_id,
                    PublishTask.status == "done",
                    PublishTask.xhs_note_id != "",  # 有笔记 id 才可关联数据
                )
            ).all()

        await bp_call(page.goto, NOTE_MANAGE_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)
        rows = await bp_call(page.query_selector_all, NOTE_ROW_SELECTOR)
        stats_by_title: dict[str, tuple[int, int, int]] = {}
        for row in rows[:50]:
            try:
                text = await bp_call(row.inner_text)
                cells = [c.strip() for c in text.split("\n") if c.strip()]
                if len(cells) < 2:
                    continue
                title = cells[0]
                # 列表行内按可见数字解析 赞/藏/评（顺序以真机为准，宽松取后三个数字）
                nums = [_parse_count(c) for c in cells[1:] if NUM_RE.search(c)]
                if len(nums) >= 3 and title:
                    stats_by_title[title[:20]] = (nums[-3], nums[-2], nums[-1])
            except Exception as exc:  # noqa: BLE001 - 单行解析失败跳过
                logger.warning("快照行解析失败（跳过）：%s", exc)

        if stats_by_title:
            with Session(get_engine()) as session:
                for t in done_tasks:
                    cfg = t.publish_config_dict()
                    title = str(cfg.get("title", ""))
                    stats = stats_by_title.get(title[:20])
                    if stats is None:
                        continue
                    likes, collects, comments = stats
                    session.add(NoteStatSnapshot(
                        account_id=account_id,
                        publish_task_id=t.id,
                        xhs_note_id=t.xhs_note_id,
                        title=title,
                        exposure=0,  # 曝光口径待真机确认（UNCLEAR #4），退化记 0
                        likes=likes,
                        collects=collects,
                        comments=comments,
                    ))
                    note_count += 1
                session.commit()

        # ── 2) 账号级快照 ──
        try:
            await bp_call(page.goto, DATA_CENTER_URL, wait_until="domcontentloaded")
            await asyncio.sleep(3)
            body_text = await bp_call(page.inner_text, "body")
            followers = _parse_count(_find_after(body_text, ["粉丝数", "粉丝"]))
            week_exposure = _find_after(body_text, ["曝光", "浏览量"])
            week_interaction = _find_after(body_text, ["互动"])
            with Session(get_engine()) as session:
                session.add(AccountSnapshot(
                    account_id=account_id,
                    followers=followers,
                    week_exposure=_parse_count(week_exposure) if week_exposure else 0,
                    week_interaction=_parse_count(week_interaction) if week_interaction else 0,
                ))
                session.commit()
            account_ok = True
        except Exception as exc:  # noqa: BLE001 - 账号级失败不影响笔记级
            logger.exception("账号级快照失败")
            errors.append(f"账号快照失败：{exc}")

        if note_count == 0 and not done_tasks:
            logger.info("快照完成：无已发布笔记，跳过笔记级快照")
        elif note_count == 0:
            shot = await screenshot_async(page, "snapshot_no_match")
            errors.append(f"笔记快照未匹配到数据（页面结构变化？）screenshot={shot}")

    except Exception as exc:  # noqa: BLE001 - 浏览器级异常包装后抛出
        shot = await screenshot_async(page, "snapshot_error")
        raise EngineError(f"快照页面操作失败：{exc}", detail={"screenshot": shot}) from exc
    finally:
        await release_page(page)

    logger.info("快照完成：notes=%s account=%s errors=%s", note_count, account_ok, errors)
    return {"notes": note_count, "account": account_ok, "error": "；".join(errors)}


def _find_after(text: str, keywords: list[str]) -> str:
    """在页面文本中找关键词后最近的数字串（宽松解析）。"""
    for kw in keywords:
        idx = text.find(kw)
        if idx >= 0:
            seg = text[idx + len(kw): idx + len(kw) + 20]
            m = NUM_RE.search(seg)
            if m:
                return m.group(0)
    return ""
