# -*- coding: utf-8 -*-
"""AI 适配层：统一调用入口。

- llm.py     OpenAI 兼容 /chat/completions 文案客户端；
- image.py   生图双路由（generations JSON+dataURI ⇄ edits multipart），失败交叉降级；
- prompts.py 提示词模板。

约定（架构第 9 节 #7）：统一返回 AiResult(ok, content, raw, error)，
生图额外带 image_bytes/image_path；调用方只判 ok，不感知具体服务商。
D1 决策：OpenAI 兼容双路由，base_url/api_key/model 全可配（默认 lk888.ai / tt-image-2.5 / banana 系）。
"""
from .llm import AiResult, chat, load_llm_config, load_image_config
from .image import generate_image

__all__ = ["AiResult", "chat", "generate_image", "load_llm_config", "load_image_config"]
