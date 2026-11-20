# -*- coding: utf-8 -*-
"""生图适配：双路由 + 失败交叉降级（D1 决策）。

路由 A：POST {base_url}/images/generations —— JSON 请求（纯文生图），
        响应 data[0].b64_json 或 data[0].url（dataURI 兼容）。
路由 B：POST {base_url}/images/edits —— multipart 上传参考图（改绘/换风格），
        字段按 OpenAI 规范（image/prompt/model/size），预留 extra 配置兜底。

降级策略：
- 带参考图：edits → generations（改绘失败则纯重绘）；
- 无参考图：generations（可依次尝试 extra.fallback_models 备选模型）。
失败自动交叉降级；全部路由失败才返回 ok=False。
"""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Optional

import httpx

from ..config import get_settings
from .llm import AiResult

logger = logging.getLogger("app.ai.image")

MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _endpoint(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _relative_to_data(abs_path: Path) -> str:
    """把绝对路径转成 data/ 相对路径（正斜杠），供前端 /api/files/ 访问。"""
    data_root = get_settings().data_dir.resolve()
    try:
        rel = abs_path.resolve().relative_to(data_root)
        return rel.as_posix()
    except ValueError:
        return abs_path.name


async def _try_generations(
    client: httpx.AsyncClient, config, prompt: str, model: str, size: Optional[str]
) -> tuple[Optional[bytes], Optional[str]]:
    """路由 A：images/generations。返回 (image_bytes, error)。"""
    payload: dict = {"model": model, "prompt": prompt, "n": 1}
    if size:
        payload["size"] = size
    try:
        resp = await client.post(
            _endpoint(config.base_url, "/images/generations"),
            headers={"Authorization": f"Bearer {config.api_key}"},
            json=payload,
        )
        if resp.status_code != 200:
            return None, f"generations HTTP {resp.status_code}: {resp.text[:200]}"
        item = resp.json()["data"][0]
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"]), None
        if item.get("url"):
            # 兼容 dataURI 与 http URL 两种返回
            url = item["url"]
            if url.startswith("data:"):
                _, _, b64part = url.partition(",")
                return base64.b64decode(b64part), None
            dl = await client.get(url)
            if dl.status_code == 200:
                return dl.content, None
            return None, f"图片下载失败 HTTP {dl.status_code}"
        return None, "generations 响应缺少 b64_json/url 字段"
    except httpx.HTTPError as exc:
        return None, f"generations 网络异常：{exc}"
    except (KeyError, IndexError, ValueError) as exc:
        return None, f"generations 响应解析失败：{exc}"


async def _try_edits(
    client: httpx.AsyncClient, config, prompt: str, ref_path: Path, size: Optional[str]
) -> tuple[Optional[bytes], Optional[str]]:
    """路由 B：images/edits（multipart 参考图改绘）。返回 (image_bytes, error)。"""
    mime = MIME_BY_SUFFIX.get(ref_path.suffix.lower(), "image/png")
    data: dict = {"prompt": prompt, "model": config.model, "n": "1"}
    if size:
        data["size"] = size
    extra = config.extra_dict()
    # extra 兜底：个别网关 multipart 字段名不同（架构 UNCLEAR #2，联调时校准）
    image_field = str(extra.get("edits_image_field", "image"))
    mask_field = extra.get("edits_mask_field")
    try:
        files = {image_field: (ref_path.name, ref_path.read_bytes(), mime)}
        if mask_field and extra.get("edits_mask_path"):
            mask_path = Path(str(extra.get("edits_mask_path")))
            if mask_path.exists():
                files[mask_field] = (mask_path.name, mask_path.read_bytes(), "image/png")
        resp = await client.post(
            _endpoint(config.base_url, "/images/edits"),
            headers={"Authorization": f"Bearer {config.api_key}"},
            data=data,
            files=files,
        )
        if resp.status_code != 200:
            return None, f"edits HTTP {resp.status_code}: {resp.text[:200]}"
        item = resp.json()["data"][0]
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"]), None
        if item.get("url"):
            url = item["url"]
            if url.startswith("data:"):
                _, _, b64part = url.partition(",")
                return base64.b64decode(b64part), None
            dl = await client.get(url)
            if dl.status_code == 200:
                return dl.content, None
            return None, f"图片下载失败 HTTP {dl.status_code}"
        return None, "edits 响应缺少 b64_json/url 字段"
    except httpx.HTTPError as exc:
        return None, f"edits 网络异常：{exc}"
    except (KeyError, IndexError, ValueError, OSError) as exc:
        return None, f"edits 请求/响应异常：{exc}"


def _save_image(image_bytes: bytes, out_dir: Path) -> Path:
    """保存图片到 out_dir，返回绝对路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"img_{ts}_{int(time.time() * 1000) % 10000}.png"
    path.write_bytes(image_bytes)
    return path


async def generate_image(
    prompt: str,
    *,
    config,
    ref_image_path: Optional[str] = None,
    out_dir: Optional[Path] = None,
) -> AiResult:
    """生图双路由（失败自动交叉降级）。

    Args:
        prompt: 生图/改绘提示词。
        config: ai_config 表 image 行。
        ref_image_path: 参考图路径（原稿首图）；提供时优先走 edits 改绘。
        out_dir: 输出目录，默认 data/creations/。

    Returns:
        AiResult：成功时 image_bytes/image_path（data/ 相对路径）齐备。
    """
    if not config.base_url or not config.api_key or not config.model:
        return AiResult(ok=False, error="生图配置不完整（base_url/api_key/model 均必填）")

    extra = config.extra_dict()
    size = extra.get("size") or None
    fallback_models = list(extra.get("fallback_models") or [])
    out_path = out_dir or get_settings().creations_dir

    errors: list[str] = []

    async with httpx.AsyncClient(timeout=int(extra.get("timeout", 180))) as client:
        ref: Optional[Path] = None
        if ref_image_path:
            candidate = Path(ref_image_path)
            if not candidate.is_absolute():
                candidate = get_settings().data_dir / candidate
            ref = candidate if candidate.exists() else None
            if ref is None:
                errors.append(f"参考图不存在，跳过 edits 路由：{ref_image_path}")

        if ref is not None:
            # 带参考图：edits → generations 交叉降级
            img, err = await _try_edits(client, config, prompt, ref, size)
            if img is not None:
                abs_path = _save_image(img, out_path)
                logger.info("生图成功（edits 改绘）：%s", abs_path.name)
                return AiResult(ok=True, image_bytes=img, image_path=_relative_to_data(abs_path))
            errors.append(f"edits({config.model}): {err}")
        else:
            errors.append("无参考图，跳过 edits 路由")

        # generations：主模型 + 备选模型依次尝试
        for model in [config.model] + fallback_models:
            img, err = await _try_generations(client, config, prompt, model, size)
            if img is not None:
                abs_path = _save_image(img, out_path)
                logger.info("生图成功（generations 模型=%s）：%s", model, abs_path.name)
                return AiResult(ok=True, image_bytes=img, image_path=_relative_to_data(abs_path))
            errors.append(f"generations({model}): {err}")

    logger.error("生图全部路由失败：%s", "；".join(errors))
    return AiResult(ok=False, error="生图全部路由失败：" + "；".join(errors))
