# -*- coding: utf-8 -*-
"""LLM 适配：OpenAI 兼容 /chat/completions 客户端，统一 AiResult 返回。

配置来源：ai_config 表 kind=llm 行（用户在设置页维护，不走 yaml——架构第 9 节 #2）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx
from sqlmodel import Session, select

from ..errors import AiError
from ..models import AiConfig

logger = logging.getLogger("app.ai.llm")


@dataclass
class AiResult:
    """AI 调用统一结果（架构第 9 节 #7）。调用方只判 ok，不感知具体服务商。"""

    ok: bool = False
    content: Optional[str] = None
    image_bytes: Optional[bytes] = None
    image_path: Optional[str] = None       # data/ 相对路径（正斜杠）
    raw: Optional[dict] = field(default=None, repr=False)
    error: Optional[str] = None


def load_llm_config(session: Session, account_id: int = 1) -> AiConfig:
    """加载 llm 配置（未启用或未配置时抛 AiError，错误可见不吞）。"""
    row = session.exec(
        select(AiConfig).where(AiConfig.kind == "llm", AiConfig.account_id == account_id)
    ).first()
    if row is None:
        raise AiError("LLM 配置不存在，请先在设置页配置")
    if not row.enabled:
        raise AiError("LLM 配置未启用，请先在设置页完成配置并开启")
    return row


def load_image_config(session: Session, account_id: int = 1) -> AiConfig:
    """加载 image 配置（未启用时抛 AiError）。"""
    row = session.exec(
        select(AiConfig).where(AiConfig.kind == "image", AiConfig.account_id == account_id)
    ).first()
    if row is None:
        raise AiError("生图配置不存在，请先在设置页配置")
    if not row.enabled:
        raise AiError("生图配置未启用，请先在设置页完成配置并开启")
    return row


def mask_key(api_key: str) -> str:
    """密钥脱敏（架构第 9 节 #10）：sk-***last4。"""
    if not api_key:
        return ""
    tail = api_key[-4:] if len(api_key) >= 4 else api_key
    return f"sk-***{tail}"


def _endpoint(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


async def chat(
    prompt: str,
    *,
    config: AiConfig,
    system: Optional[str] = None,
    temperature: float = 0.8,
    json_mode: bool = False,
    history: Optional[list] = None,
) -> AiResult:
    """调用 OpenAI 兼容 /chat/completions。

    Args:
        prompt: 用户消息。
        config: ai_config 表 llm 行（须 enabled）。
        system: 系统提示词（人设/知识上下文）。
        json_mode: 要求模型输出 JSON（通过消息约定，不依赖 response_format 兼容性）。
        history: 可选多轮历史 [{"role","content"}, ...]。

    Returns:
        AiResult；失败时 ok=False 且带 error（不抛异常，便于连通性测试与降级）。
    """
    if not config.base_url or not config.model or not config.api_key:
        return AiResult(ok=False, error="LLM 配置不完整（base_url/api_key/model 均必填）")

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": prompt})
    if json_mode:
        messages.append({
            "role": "system",
            "content": "请严格输出合法 JSON，不要输出任何 JSON 以外的文字（包括 markdown 代码围栏）。",
        })

    extra = config.extra_dict()
    timeout_seconds = int(extra.get("timeout", 120))
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }

    url = _endpoint(config.base_url, "/chat/completions")
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {config.api_key}"},
                json=payload,
            )
        if resp.status_code != 200:
            logger.error("LLM 调用失败 HTTP %s：%s", resp.status_code, resp.text[:500])
            return AiResult(ok=False, error=f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        logger.info("LLM 调用成功：model=%s content_len=%s", config.model, len(content or ""))
        return AiResult(ok=True, content=content, raw=data)
    except httpx.HTTPError as exc:
        logger.error("LLM 网络异常：%s", exc)
        return AiResult(ok=False, error=f"LLM 网络异常：{exc}")
    except (KeyError, IndexError, ValueError) as exc:
        logger.error("LLM 响应解析失败：%s", exc)
        return AiResult(ok=False, error=f"LLM 响应结构异常：{exc}")


def parse_json_content(content: str) -> Optional[dict]:
    """从 LLM 输出中解析 JSON（容错：剥离 markdown 围栏）。失败返回 None 并记日志。"""
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        # 剥离 ```json ... ``` 围栏
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        # 兜底：截取第一个 { 到最后一个 } 之间的内容
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except (ValueError, TypeError):
                pass
    logger.error("LLM 输出 JSON 解析失败：%s", content[:200])
    return None
