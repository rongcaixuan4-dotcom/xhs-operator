# -*- coding: utf-8 -*-
"""小红书 API 只读直连通道（签名由公开开源签名库 xhshow 提供）。

定位与边界（重要，写进代码头，测试用例固化断言）：
- 本模块只实现 **读接口**：搜索 / 笔记详情 / 评论分页（见 READ_ENDPOINTS）；
- 签名调用 PyPI 公开开源库 xhshow（与 MediaCrawler / Spider_XHS 同一公开生态），
  本仓库不含任何自研签名或逆向代码；
- **写操作永久红线**：发布/评论/点赞/收藏等一切变更类接口绝不经本通道直连，
  一律走 Playwright 真浏览器（engines/publish.py）；
- Cookie 来源：复用浏览器 persistent context 登录态（engines/browser.py 只读导出）；
  无 Cookie 时本通道不可用，抛 NoCookieError 给出可见提示；
- 限速：同任务相邻请求 ≥2.5s + 0~35% 抖动；搜索翻页间隔 ≥10s（RateLimiter）。

解析产物与浏览器采集（engines/collect.py）对齐为同一 ScrapedNote 结构，
复用现有的评分筛选与入库链路（CollectedNote），不另起炉灶。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from ..errors import EngineError

logger = logging.getLogger("app.engines.xhs_api")

_API_HOST = "https://edith.xiaohongshu.com"
# 正常浏览器 UA 伪装（风控按客户端特征识别，UA 必须像真浏览器）
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 只读接口白名单（含平台用 POST 承载的读接口，如 feed）。
# 变更类（写）接口绝不允许出现在此——红线测试逐字扫描全仓源码。
READ_ENDPOINTS: frozenset[str] = frozenset({
    "/api/sns/web/v2/search/notes",   # 搜索（GET，参数 keyword/page/page_size）
    "/api/sns/web/v1/feed",           # 笔记详情（POST 带 source_note_id）
    "/api/sns/web/v2/comment/page",   # 评论分页（GET，note_id + cursor）
})


class ApiChannelError(EngineError):
    """API 直连通道异常（签名/网络/风控/接口改版），message 面向用户可见。"""

    code = 1011
    default_message = "API 直连通道失败"


class NoCookieError(ApiChannelError):
    """登录态缺失：API 通道依赖浏览器登录态 Cookie，不可用时给出可见提示。"""

    code = 1012
    default_message = (
        "API 直连通道不可用：未检测到小红书登录态（Cookie）。"
        "请先在工作台浏览器中扫码登录小红书，再使用 API 采集通道。"
    )


class RateLimiter:
    """相邻请求限速器：base_interval（+0~jRatio 抖动）。

    时钟/睡眠/随机源均可注入，便于单测在零耗时下验证间隔逻辑。
    """

    def __init__(
        self,
        base_interval: float = 2.5,
        jitter_ratio: float = 0.35,
        gap_interval: float = 10.0,
        *,
        random_fn: Optional[Any] = None,
        monotonic_fn: Optional[Any] = None,
        sleep_fn: Optional[Any] = None,
    ) -> None:
        self.base_interval = float(base_interval)
        self.jitter_ratio = float(jitter_ratio)
        self.gap_interval = float(gap_interval)
        self._random = random_fn or random.random
        self._now = monotonic_fn or time.monotonic
        self._sleep = sleep_fn or asyncio.sleep
        self._last: Optional[float] = None

    def next_delay(self, *, gap: bool = False) -> float:
        """下一次请求应等待的总时长（基准间隔 + 0~35% 抖动）。"""
        base = self.gap_interval if gap else self.base_interval
        return base + base * self.jitter_ratio * self._random()

    async def wait(self, *, gap: bool = False) -> float:
        """限速等待（幂等：距上次请求不足间隔时补足剩余时间）。"""
        delay = self.next_delay(gap=gap)
        if self._last is not None:
            elapsed = self._now() - self._last
            remaining = delay - elapsed
            if remaining > 0:
                await self._sleep(remaining)
        self._last = self._now()
        return delay


@dataclass
class ScrapedNote:
    """单条采集结果（API 通道与浏览器通道共用的解析产物结构）。"""

    xhs_note_id: str = ""
    title: str = ""
    content: str = ""
    author: str = ""
    image_urls: list[str] = field(default_factory=list)
    likes: int = 0
    collects: int = 0
    comments: int = 0
    xsec_token: str = ""                 # 详情接口访问凭证（搜索结果携带，只读透传）
    source: str = "api"                  # 通道来源标记：api | browser


def _parse_count(text: str) -> int:
    """互动数文本转 int（"1.2万" → 12000），与浏览器通道口径一致。"""
    import re

    text = (text or "").strip()
    if not text:
        return 0
    try:
        if text.endswith("万") or text.endswith("w"):
            return int(float(text[:-1]) * 10000)
        return int(re.sub(r"[^\d]", "", text) or 0)
    except ValueError:
        return 0


def _pick_image_url(img: Any) -> str:
    """image_list 项取最优可用图片 URL（url > info_list 最后一档）。"""
    if not isinstance(img, dict):
        return ""
    url = img.get("url") or img.get("url_default") or ""
    if not url:
        infos = img.get("info_list") or []
        if infos:
            url = infos[-1].get("image_url") or ""
    return str(url or "")


def parse_search_items(data: dict) -> list[ScrapedNote]:
    """搜索接口 data → ScrapedNote 列表（缺失字段容错置空）。"""
    notes: list[ScrapedNote] = []
    items = data.get("items") or []
    if not isinstance(items, list):
        return notes
    for item in items:
        if not isinstance(item, dict):
            continue
        card = item.get("note_card") or {}
        if not isinstance(card, dict):
            card = {}
        note_id = str(item.get("id") or card.get("note_id") or card.get("id") or "")
        if not note_id:
            continue
        note = ScrapedNote(xhs_note_id=note_id)
        note.title = str(card.get("display_title") or card.get("title") or "").strip()
        user = card.get("user") or {}
        if isinstance(user, dict):
            note.author = str(user.get("nickname") or user.get("nick_name") or "").strip()
        interact = card.get("interact_info") or {}
        if isinstance(interact, dict):
            note.likes = _parse_count(str(interact.get("liked_count") or "0"))
        cover_url = _pick_image_url(card.get("cover"))
        if cover_url:
            note.image_urls.append(cover_url)
        for img in (card.get("image_list") or [])[:5]:
            url = _pick_image_url(img)
            if url and url not in note.image_urls:
                note.image_urls.append(url)
        note.xsec_token = str(item.get("xsec_token") or card.get("xsec_token") or "")
        note.source = "api"
        notes.append(note)
    return notes


def parse_note_detail(item: dict, fallback_note_id: str = "") -> ScrapedNote:
    """feed 详情 item（含 note_card）→ ScrapedNote（标题/正文/图/作者/互动数）。"""
    card = item.get("note_card") if isinstance(item, dict) else None
    if not isinstance(card, dict):
        card = {}
    note_id = str(card.get("note_id") or card.get("id") or item.get("id") or fallback_note_id or "")
    note = ScrapedNote(xhs_note_id=note_id)
    note.title = str(card.get("title") or card.get("display_title") or "").strip()
    note.content = str(card.get("desc") or "").strip()
    user = card.get("user") or {}
    if isinstance(user, dict):
        note.author = str(user.get("nickname") or user.get("nick_name") or "").strip()
    interact = card.get("interact_info") or {}
    if isinstance(interact, dict):
        note.likes = _parse_count(str(interact.get("liked_count") or "0"))
        note.collects = _parse_count(str(interact.get("collected_count") or "0"))
        note.comments = _parse_count(str(interact.get("comment_count") or "0"))
    for img in (card.get("image_list") or []):
        url = _pick_image_url(img)
        if url and url not in note.image_urls:
            note.image_urls.append(url)
    note.xsec_token = str(item.get("xsec_token") or card.get("xsec_token") or "")
    note.source = "api"
    return note


class XhsApiChannel:
    """小红书签名直连 **只读** 客户端（httpx + xhshow 签名）。

    可注入点（保持可 mock）：
    - transport: httpx.AsyncTransport（单测用 httpx.MockTransport）；
    - rate_limiter: RateLimiter（单测注入时钟/睡眠验证限速）。
    """

    def __init__(
        self,
        cookie_str: str = "",
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        rate_limiter: Optional[RateLimiter] = None,
        transport: Optional[httpx.AsyncTransport] = None,
    ) -> None:
        self.cookie_str = (cookie_str or "").strip()
        self.user_agent = user_agent
        self.timeout = timeout
        self._limiter = rate_limiter or RateLimiter()
        self._transport = transport
        self._signer: Optional[Any] = None   # xhshow.Xhshow 惰性创建
        self._client: Optional[httpx.AsyncClient] = None
        self._base_headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "content-type": "application/json;charset=UTF-8",
            "origin": "https://www.xiaohongshu.com",
            "referer": "https://www.xiaohongshu.com/",
            "user-agent": self.user_agent,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        }

    # ── 就绪检查 ──
    def ensure_ready(self) -> None:
        """前置校验：无 Cookie 即抛 NoCookieError（可见提示），xhshow 缺失给安装指引。"""
        if not self.cookie_str:
            raise NoCookieError()
        if self._signer is None:
            try:
                from xhshow import Xhshow
            except ImportError as exc:  # noqa: F841 - 仅用于链路追踪
                raise ApiChannelError(
                    "签名库 xhshow 未安装：python -m pip install xhshow"
                ) from exc
            self._signer = Xhshow()

    # ── 请求底层 ──
    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                transport=self._transport,
                follow_redirects=False,
            )
        return self._client

    def _check_read_only(self, uri: str) -> None:
        """只读白名单硬校验（显式抛错，不依赖 assert——python -O 下 assert 会被剥离）。

        变更类（写）接口一律拒绝：写操作永久红线，只走 Playwright 真浏览器。
        """
        if uri not in READ_ENDPOINTS:
            raise ApiChannelError(
                f"非只读白名单接口，拒绝请求：{uri}（红线：变更类接口一律真浏览器）",
                detail={"uri": uri},
            )

    async def _get(self, uri: str, params: dict[str, Any]) -> dict:
        """签名 GET（uri 必须属于 READ_ENDPOINTS 白名单）。"""
        self.ensure_ready()
        self._check_read_only(uri)
        await self._limiter.wait()
        sign = self._signer.sign_headers_get(uri, self.cookie_str, params=params)
        url = _API_HOST + self._signer.build_url(uri, params)
        headers = {**self._base_headers, **sign, "Cookie": self.cookie_str}
        client = await self._ensure_client()
        try:
            resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            # 网络异常（超时/断连/DNS 失败）统一转译为 ApiChannelError：
            # 保持错误可见语义，并让 hybrid 模式能捕获后自动回退浏览器通道
            raise ApiChannelError(
                f"网络异常（GET {uri}）：{type(exc).__name__} {exc}",
                detail={"uri": uri, "error_type": type(exc).__name__},
            ) from exc
        return self._unwrap(resp, uri)

    async def _post(self, uri: str, payload: dict[str, Any]) -> dict:
        """签名 POST（仅用于平台以 POST 承载的读接口，白名单校验）。"""
        self.ensure_ready()
        self._check_read_only(uri)
        await self._limiter.wait()
        sign = self._signer.sign_headers_post(uri, self.cookie_str, payload=payload)
        headers = {**self._base_headers, **sign, "Cookie": self.cookie_str}
        client = await self._ensure_client()
        try:
            resp = await client.post(
                _API_HOST + uri,
                headers=headers,
                json=payload,
            )
        except httpx.HTTPError as exc:
            # 同 _get：网络异常转译，hybrid 降级可达
            raise ApiChannelError(
                f"网络异常（POST {uri}）：{type(exc).__name__} {exc}",
                detail={"uri": uri, "error_type": type(exc).__name__},
            ) from exc
        return self._unwrap(resp, uri)

    @staticmethod
    def _unwrap(resp: httpx.Response, uri: str) -> dict:
        """统一解包 {code, success, data}；失败抛 ApiChannelError（信息可见）。"""
        if resp.status_code != 200:
            raise ApiChannelError(
                f"接口 HTTP {resp.status_code}（{uri}），可能触发风控或接口改版",
                detail={"status_code": resp.status_code, "uri": uri},
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise ApiChannelError(
                f"接口响应非 JSON（{uri}），可能被风控页拦截",
                detail={"body_head": resp.text[:200]},
            ) from exc
        code = body.get("code")
        success = body.get("success")
        if code not in (0, None) or success is False:
            raise ApiChannelError(
                f"接口返回失败（{uri}）：code={code} msg={body.get('msg') or body.get('message')}",
                detail={"code": code, "uri": uri},
            )
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    # ── 业务读接口 ──
    async def search_notes(self, keyword: str, page: int = 1, page_size: int = 20) -> list[ScrapedNote]:
        """搜索笔记（GET /api/sns/web/v2/search/notes）。"""
        params: dict[str, Any] = {
            "keyword": keyword,
            "page": max(1, int(page or 1)),
            "page_size": max(10, min(40, int(page_size or 20))),
            "search_id": self._signer.get_search_id() if self._signer else "",
            "sort": "general",
            "note_type": 0,
        }
        data = await self._get("/api/sns/web/v2/search/notes", params)
        return parse_search_items(data)

    async def note_detail(self, note_id: str, xsec_token: str = "") -> ScrapedNote:
        """笔记详情（POST /api/sns/web/v1/feed，带 source_note_id）。"""
        payload: dict[str, Any] = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": "1"},
            "xsec_source": "pc_search",
            "xsec_token": xsec_token or "",
        }
        data = await self._post("/api/sns/web/v1/feed", payload)
        items = data.get("items") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            card = item.get("note_card") or {}
            actual_id = ""
            if isinstance(card, dict):
                actual_id = str(card.get("note_id") or card.get("id") or "")
            if not actual_id:
                actual_id = str(item.get("id") or "")
            if actual_id == note_id:
                return parse_note_detail(item, fallback_note_id=note_id)
        if items:
            logger.warning("feed 返回条目与请求 ID 不一致：note=%s", note_id)
        return ScrapedNote(xhs_note_id=note_id, source="api")

    async def fetch_comments(self, note_id: str, cursor: str = "", xsec_token: str = "") -> dict:
        """评论分页（GET /api/sns/web/v2/comment/page），返回原始 data 供调用方解析。"""
        params: dict[str, Any] = {
            "note_id": note_id,
            "cursor": cursor or "",
            "top_comment_id": "",
            "image_formats": "jpg,webp,avif",
        }
        if xsec_token:
            params["xsec_token"] = xsec_token
        return await self._get("/api/sns/web/v2/comment/page", params)

    # ── 限速辅助 ──
    async def page_gap(self) -> None:
        """搜索翻页间隔（≥10s + 抖动），在分页循环中、翻页请求前调用。"""
        await self._limiter.wait(gap=True)

    async def aclose(self) -> None:
        """关闭底层 httpx 客户端（幂等）。"""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001 - 关闭失败仅记日志
                logger.exception("API 客户端关闭异常")
            self._client = None


async def fetch_login_state() -> tuple[str, str]:
    """从浏览器 persistent context 只读导出小红书 Cookie 串与 UA。

    Returns:
        (cookie_str, user_agent)。未登录时 cookie_str 可能为空或不含
        web_session——由 XhsApiChannel.ensure_ready / 接口响应给出可见错误。
    """
    from .browser import export_login_state_async

    return await export_login_state_async()
