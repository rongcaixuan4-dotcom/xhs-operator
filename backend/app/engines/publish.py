# -*- coding: utf-8 -*-
"""发布引擎：创作者中心表单全配置项填写与提交 + 结果核查（防重复）+ 重试（R09/R10）。

状态机：queued → publishing → verifying → done / failed / canceled。

防重复（R10，架构 5.2）：
1. 发布前：dedup_hash 查重（UNIQUE(account_id, dedup_hash) 硬约束 + 服务层软查重）；
2. 提交后反馈不明确或失败但可能已提交：打开笔记列表页核查，
   标题存在 → 视为已发布（done），绝不盲目重试造成重复发布。

安全红线（D3）：全部经真浏览器 DOM 操作；不逆向签名；不 Cookie 直连。
选择器基于创作者中心当前结构编写，改版时按真机校准（见架构 UNCLEAR #3）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from sqlmodel import Session, select

from ..config import get_settings
from ..db import get_engine
from ..errors import EngineError, VerifyError
from ..models import PublishTask
from .browser import XHS_CREATOR, acquire_page, release_page, screenshot_async

logger = logging.getLogger("app.engines.publish")

MAX_RETRY = 3  # 重试上限（架构 5.2）

# ── 创作者中心页面元素选择器（改版时在此校准） ──
PUBLISH_URL = f"{XHS_CREATOR}/publish/publish?source=official"
NOTE_MANAGE_URL = f"{XHS_CREATOR}/publish/noteManage"  # 笔记管理列表页
TAB_IMAGE_SELECTOR = "div.tab-btn:has-text('上传图文')"          # 图文发布 tab
IMAGE_INPUT_SELECTOR = "input[type='file']"                      # 图片上传 input
TITLE_INPUT_SELECTOR = "input#title-input, input[placeholder*='标题'], .d-title input"
CONTENT_EDITOR_SELECTOR = "div.ql-editor[contenteditable='true'], textarea#post-text, .content-editor"
TOPIC_INPUT_SELECTOR = "input[placeholder*='搜索添加话题'], .hash-tag input"
TOPIC_SUGGEST_SELECTOR = ".topic-item, .hash-tag-list li"
LOCATION_INPUT_SELECTOR = "input[placeholder*='位置'], .location input"
DECLARE_CHECKBOX_SELECTOR = "input[type='checkbox'][class*='source'], .declare-source"
SCHEDULE_SWITCH_SELECTOR = "div.switch-button, input[type='checkbox'][class*='schedule']"
SCHEDULE_TIME_INPUT_SELECTOR = "input[placeholder*='定时'], .schedule-time input"
PUBLISH_BTN_SELECTOR = "button:has-text('发布'), button.css-1ydiho3"
SUCCESS_HINT_SELECTOR = "text=发布成功, text=已发布"
ERROR_HINT_SELECTOR = "text=发布失败, text=异常"


async def publish_task(task_id: int) -> str:
    """执行一个发布任务（R09/R10），全程状态回写。

    Returns:
        最终状态：done | failed | queued(延时重试)。
    """
    with Session(get_engine(), expire_on_commit=False) as session:
        # expire_on_commit=False：commit 后 ORM 实例不失效，后续 _do_publish_flow
        # 才能继续读取 task.publish_config_dict()/id/scheduled_at（QA BUG-3）
        task = session.get(PublishTask, task_id)
        if task is None:
            raise EngineError(f"发布任务不存在：{task_id}")

        # ── 1) 发布前防重查重（R10 硬约束） ──
        dup = session.exec(
            select(PublishTask).where(
                PublishTask.account_id == task.account_id,
                PublishTask.dedup_hash == task.dedup_hash,
                PublishTask.status == "done",
                PublishTask.id != task.id,
            )
        ).first()
        if dup is not None:
            task.status = "failed"
            task.last_error = "防重复：相同内容（dedup_hash 一致）的任务已发布成功，拒绝二次发布"
            session.add(task)
            session.commit()
            logger.warning("防重复拦截：task=%s dup_task=%s", task.id, dup.id)
            return task.status

        # ── 2) 起浏览器，登录态检查 ──
        from .browser import check_login_async

        logged_in, msg = await check_login_async()
        if not logged_in:
            task.last_error = f"登录态检查失败：{msg}"
            task.retry_count += 1
            # 保持 queued 等人工扫码后重试（不立即 failed）
            session.add(task)
            session.commit()
            logger.warning("发布暂停（需人工扫码）：task=%s msg=%s", task.id, msg)
            return "queued"

        task.status = "publishing"
        session.add(task)
        session.commit()

    config = task.publish_config_dict()
    outcome = await _do_publish_flow(task, config)

    # ── 6) 结果回写 ──
    with Session(get_engine()) as session:
        task = session.get(PublishTask, task_id)
        if outcome.status == "done":
            task.status = "done"
            task.published_at = task.published_at or task.created_at.now()
            task.note_url = outcome.note_url or task.note_url
            task.xhs_note_id = outcome.xhs_note_id or task.xhs_note_id
            task.last_error = ""
        elif outcome.status == "retry":
            task.retry_count += 1
            if task.retry_count >= MAX_RETRY:
                task.status = "failed"
                task.last_error = f"重试耗尽（{MAX_RETRY} 次）：{outcome.error}"
            else:
                task.status = "queued"  # 延时重试（扫描器稍后拉起）
                task.last_error = outcome.error
        else:  # failed
            task.status = "failed"
            task.retry_count += 1
            task.last_error = outcome.error
        session.add(task)
        session.commit()
        logger.info("发布结果回写：task=%s status=%s retry=%s", task.id, task.status, task.retry_count)
        return task.status


class _PublishOutcome:
    """发布执行结果（内部传递）。"""

    def __init__(self, status: str, note_url: str = "", xhs_note_id: str = "", error: str = "") -> None:
        self.status = status
        self.note_url = note_url
        self.xhs_note_id = xhs_note_id
        self.error = error


async def _do_publish_flow(task: PublishTask, config: dict) -> _PublishOutcome:
    """浏览器发布主流程：填表 → 提交 → 结果判定 → 不确定时列表核查。"""
    page = await acquire_page()
    title = str(config.get("title", ""))
    try:
        await asyncio.to_thread(page.goto, PUBLISH_URL, None, "domcontentloaded")
        await asyncio.sleep(2)

        # 切到「上传图文」tab
        tab = await asyncio.to_thread(page.query_selector, TAB_IMAGE_SELECTOR)
        if tab is not None:
            await asyncio.to_thread(tab.click)
            await asyncio.sleep(1)

        # 上传图片（含封面：封面排首位）
        image_input = await asyncio.to_thread(page.query_selector, IMAGE_INPUT_SELECTOR)
        image_paths = [str(p) for p in _resolve_local_images(config)]
        if image_input is not None and image_paths:
            await asyncio.to_thread(
                image_input.set_input_files, image_paths, None
            )
            await asyncio.sleep(3)  # 等上传完成（多图）
            logger.info("图片已上传：task=%s count=%s", task.id, len(image_paths))
        elif not image_paths:
            return _PublishOutcome("failed", error="无本地图片可上传（publish_config.images 为空）")
        else:
            shot = await screenshot_async(page, f"publish_no_input_{task.id}")
            return _PublishOutcome("retry", error=f"未找到图片上传控件（页面改版？）screenshot={shot}")

        # 标题
        title_input = await asyncio.to_thread(page.query_selector, TITLE_INPUT_SELECTOR)
        if title_input is None:
            shot = await screenshot_async(page, f"publish_no_title_{task.id}")
            return _PublishOutcome("retry", error=f"未找到标题输入框 screenshot={shot}")
        await asyncio.to_thread(title_input.fill, title)

        # 正文
        editor = await asyncio.to_thread(page.query_selector, CONTENT_EDITOR_SELECTOR)
        if editor is None:
            shot = await screenshot_async(page, f"publish_no_editor_{task.id}")
            return _PublishOutcome("retry", error=f"未找到正文编辑器 screenshot={shot}")
        await asyncio.to_thread(editor.click)
        await asyncio.to_thread(editor.type, str(config.get("content", "")))

        # 话题标签（多选，逐个搜索添加）
        for tag in config.get("topics") or []:
            await _fill_topic(page, str(tag))

        # 位置
        location = str(config.get("location") or "")
        if location:
            loc_input = await asyncio.to_thread(page.query_selector, LOCATION_INPUT_SELECTOR)
            if loc_input is not None:
                await asyncio.to_thread(loc_input.fill, location)

        # 声明来源
        if config.get("source_declare"):
            declare = await asyncio.to_thread(page.query_selector, DECLARE_CHECKBOX_SELECTOR)
            if declare is not None:
                await asyncio.to_thread(declare.check)

        # 定时发布
        scheduled = task.scheduled_at
        if config.get("is_schedule") and scheduled:
            await _fill_schedule(page, task.id, scheduled)

        # 合集/@好友（依赖账号实际数据，选择器需真机固化——架构 UNCLEAR #3）
        await _fill_optional(page, config, task.id)

        # ── 提交 ──
        btn = await asyncio.to_thread(page.query_selector, PUBLISH_BTN_SELECTOR)
        if btn is None:
            shot = await screenshot_async(page, f"publish_no_btn_{task.id}")
            return _PublishOutcome("retry", error=f"未找到发布按钮 screenshot={shot}")
        await asyncio.to_thread(btn.click)
        await asyncio.sleep(3)

        # ── 结果判定 ──
        success_el = await asyncio.to_thread(page.query_selector, SUCCESS_HINT_SELECTOR)
        error_el = await asyncio.to_thread(page.query_selector, ERROR_HINT_SELECTOR)
        if success_el is not None and error_el is None:
            # 明确成功 → verifying → 核查内容一致
            task.status = "verifying"
            return await _verify_after_submit(page, task, title)
        if error_el is not None:
            shot = await screenshot_async(page, f"publish_err_{task.id}")
            # 页面明确报失败，但可能已提交 → 列表核查，绝不盲目重试
            verified = await _verify_in_note_list(page, task, title)
            if verified.found:
                return _PublishOutcome("done", verified.note_url, verified.note_id,
                                       error=f"页面报失败但列表核查已发布（告警）screenshot={shot}")
            return _PublishOutcome("retry", error=f"页面报发布失败 screenshot={shot}")
        # 反馈不明确 → 列表核查
        verified = await _verify_in_note_list(page, task, title)
        if verified.found:
            return _PublishOutcome("done", verified.note_url, verified.note_id,
                                   error="页面反馈不明确，列表核查确认已发布")
        shot = await screenshot_async(page, f"publish_unclear_{task.id}")
        return _PublishOutcome("retry", error=f"提交后反馈不明确且列表未找到 screenshot={shot}")

    except EngineError:
        raise
    except Exception as exc:  # noqa: BLE001 - 浏览器异常包装为 retry，不吞
        shot = await screenshot_async(page, f"publish_exception_{task.id}")
        return _PublishOutcome("retry", error=f"浏览器操作异常：{exc} screenshot={shot}")
    finally:
        await release_page(page)


async def _verify_after_submit(page, task: PublishTask, title: str) -> _PublishOutcome:
    """明确成功后核查：笔记列表页确认笔记存在且标题一致（R09 核查）。"""
    await asyncio.sleep(2)
    verified = await _verify_in_note_list(page, task, title)
    if verified.found:
        return _PublishOutcome("done", verified.note_url, verified.note_id)
    raise VerifyError(f"页面提示成功但笔记列表核查未找到：{title}")


class _VerifyResult:
    def __init__(self, found: bool, note_url: str = "", note_id: str = "") -> None:
        self.found = found
        self.note_url = note_url
        self.note_id = note_id


async def _verify_in_note_list(page, task: PublishTask, title: str) -> _VerifyResult:
    """打开笔记管理列表页核查标题存在（防重复发布的关键，架构 5.2）。"""
    try:
        await asyncio.to_thread(page.goto, NOTE_MANAGE_URL, None, "domcontentloaded")
        await asyncio.sleep(3)
        # 列表项标题元素（宽松匹配）
        items = await asyncio.to_thread(
            page.query_selector_all, ".note-item .title, table tbody tr, .video-info .title"
        )
        for item in items[:30]:
            try:
                text = (await asyncio.to_thread(item.inner_text)).strip()
            except Exception:  # noqa: BLE001
                continue
            if title[:12] in text:
                link = await asyncio.to_thread(item.query_selector, "a[href]")
                href = link.get_attribute("href") if link is not None else ""
                note_id = ""
                if href and "/explore/" in str(href):
                    note_id = str(href).split("/explore/")[-1].split("?")[0]
                logger.info("列表核查命中：task=%s title=%s", task.id, title[:20])
                return _VerifyResult(True, href or "", note_id)
        return _VerifyResult(False)
    except Exception as exc:  # noqa: BLE001 - 核查失败记日志，按未找到处理
        logger.error("笔记列表核查异常：task=%s err=%s", task.id, exc)
        return _VerifyResult(False)


async def _fill_topic(page, tag: str) -> None:
    """话题标签逐个搜索添加（失败仅记日志，不阻塞发布）。"""
    try:
        inp = await asyncio.to_thread(page.query_selector, TOPIC_INPUT_SELECTOR)
        if inp is None:
            return
        await asyncio.to_thread(inp.click)
        await asyncio.to_thread(inp.type, tag)
        await asyncio.sleep(1)
        sug = await asyncio.to_thread(page.query_selector, TOPIC_SUGGEST_SELECTOR)
        if sug is not None:
            await asyncio.to_thread(sug.click)
        await asyncio.sleep(0.5)
    except Exception as exc:  # noqa: BLE001
        logger.warning("话题填写失败（跳过）：tag=%s err=%s", tag, exc)


async def _fill_schedule(page, task_id: int, scheduled) -> None:
    """定时发布开关与时间填写。"""
    try:
        switch = await asyncio.to_thread(page.query_selector, SCHEDULE_SWITCH_SELECTOR)
        if switch is not None:
            await asyncio.to_thread(switch.click)
            await asyncio.sleep(0.5)
        time_input = await asyncio.to_thread(page.query_selector, SCHEDULE_TIME_INPUT_SELECTOR)
        if time_input is not None:
            await asyncio.to_thread(time_input.fill, scheduled.strftime("%Y-%m-%d %H:%M"))
            await asyncio.to_thread(time_input.press, "Enter")
        logger.info("定时发布已设置：task=%s at=%s", task_id, scheduled)
    except Exception as exc:  # noqa: BLE001
        logger.warning("定时设置失败（改为立即发布语义）：%s", exc)


async def _fill_optional(page, config: dict, task_id: int) -> None:
    """合集/@好友（预留：真机调试后固化 selector，架构 UNCLEAR #3）。"""
    # 合集与 @好友的选择器依赖账号数据，此处留扩展位；不影响核心发布流程
    if config.get("collection") or config.get("mention_users"):
        logger.info("合集/@好友配置存在但选择器待真机固化：task=%s（架构 UNCLEAR #3）", task_id)


def _resolve_local_images(config: dict) -> list:
    """把 publish_config.images/cover 转成本地绝对路径列表（封面排首位，去重）。"""
    settings = get_settings()
    paths: list[str] = []
    cover = config.get("cover")
    if cover:
        p = settings.data_dir / str(cover)
        if p.exists():
            paths.append(str(p))
    for rel in config.get("images") or []:
        p = settings.data_dir / str(rel)
        if p.exists() and str(p) not in paths:
            paths.append(str(p))
    return paths


async def precheck() -> dict:
    """试发预检（R09 抽屉）：登录态 + 发布表单可达性检查，不提交。

    Returns:
        {"ok": bool, "login": str, "form_reachable": bool, "detail": str, "screenshot": str}
    """
    from .browser import check_login_async

    logged_in, msg = await check_login_async()
    if not logged_in:
        return {"ok": False, "login": msg, "form_reachable": False, "detail": "先完成扫码登录", "screenshot": ""}

    page = await acquire_page()
    try:
        await asyncio.to_thread(page.goto, PUBLISH_URL, None, "domcontentloaded")
        await asyncio.sleep(2)
        has_input = await asyncio.to_thread(page.query_selector, IMAGE_INPUT_SELECTOR) is not None
        has_btn = await asyncio.to_thread(page.query_selector, PUBLISH_BTN_SELECTOR) is not None
        shot = await screenshot_async(page, "precheck")
        reachable = has_input and has_btn
        return {
            "ok": reachable,
            "login": msg,
            "form_reachable": reachable,
            "detail": "发布页表单可达" if reachable else "发布页元素缺失（可能改版）",
            "screenshot": shot,
        }
    except Exception as exc:  # noqa: BLE001
        shot = await screenshot_async(page, "precheck_error")
        return {"ok": False, "login": msg, "form_reachable": False,
                "detail": f"预检异常：{exc}", "screenshot": shot}
    finally:
        await release_page(page)
