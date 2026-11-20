# -*- coding: utf-8 -*-
"""提示词模板：二创改写、评分归因、周报生成（占位符注入 context）。

所有模板均由 services/context.py 组装后传入；LLM 输出约定为 JSON 以便程序解析。
"""
from __future__ import annotations

import json
from typing import Optional


def persona_to_system_text(persona) -> str:
    """把人设行转成提示词用文本段。persona 为 models.Persona。"""
    lines = [
        f"人设名：{persona.name}",
        f"账号昵称定位：{persona.nickname}",
        f"一句话定位：{persona.positioning}",
        f"内容风格标签：{'、'.join(persona.style_tag_list()) or '不限'}",
        f"目标人群：{persona.audience or '不限'}",
        f"禁忌词（绝对不可出现）：{'、'.join(persona.taboo_word_list()) or '无'}",
    ]
    samples = persona.sample_note_list()
    if samples:
        lines.append("示例笔记（模仿其语气与结构）：")
        for i, s in enumerate(samples[:3], 1):
            lines.append(f"  示例{i}：{s[:300]}")
    return "\n".join(lines)


def build_rewrite_messages(
    *,
    persona_text: str,
    knowledge_text: str,
    review_text: str,
    original_title: str,
    original_content: str,
    max_title_len: int = 20,
    max_content_len: int = 600,
) -> list[dict]:
    """二创改写消息（R07）：新标题+新文案，JSON 输出 {"title","content","image_prompt"}。

    image_prompt 供生图二创使用（描述配图画面）。
    """
    system = (
        "你是小红书资深内容创作者，正在为博主做人设化二创。\n"
        "规则：\n"
        "1. 严格保持人设调性，符合目标人群的阅读习惯；\n"
        "2. 标题与文案必须是对原笔记的深度改写，不得照抄原句（与原文重复率须低于 70%）；\n"
        "3. 不得出现禁忌词；\n"
        "4. 文案口语化、有钩子，可适当使用 emoji；\n"
        "5. 优先参考知识库中的运营方法论；\n"
        f"6. 标题不超过 {max_title_len} 字，文案不超过 {max_content_len} 字；\n"
        "7. 输出严格 JSON：{\"title\":\"...\",\"content\":\"...\",\"image_prompt\":\"...\"}"
    )
    user_parts = [
        "【人设】\n" + persona_text,
        "【知识库参考】\n" + (knowledge_text or "（无命中知识，凭人设发挥）"),
    ]
    if review_text:
        user_parts.append("【复盘改进项（优先遵循）】\n" + review_text)
    user_parts.append(
        "【原笔记】\n标题：" + original_title + "\n正文：" + original_content[:1200]
    )
    user_parts.append("请输出二创 JSON（title/content/image_prompt 三个字段）。")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def build_scoring_messages(*, persona_text: str, note_title: str, note_content: str) -> list[dict]:
    """评分归因消息（可解释性）：为什么这篇适合/不适合当前人设。"""
    system = (
        "你是内容运营分析师。判断一篇小红书笔记与博主人设的匹配度，"
        '输出严格 JSON：{"match": true/false, "reason": "一句话归因"}'
    )
    user = f"【人设】\n{persona_text}\n\n【笔记】\n标题：{note_title}\n正文：{note_content[:800]}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_weekly_report_messages(
    *,
    week_start: str,
    week_end: str,
    stats_summary: str,
    top_notes: str,
    knowledge_text: str,
) -> list[dict]:
    """周报生成消息（R12）：Markdown 报告 + ≥3 条结构化改进项。

    输出 JSON：{"report_md": "...", "insights": [{"scope","content","impact"}]}
    scope 取值：topic_weight（调话题权重）/ publish_time（调发布时间）/ prompt（调人设提示词）。
    """
    system = (
        "你是账号复盘分析师。基于一周数据产出复盘报告。\n"
        "报告结构（Markdown）：## 整体表现 / ## 爆款归因 / ## 问题诊断 / ## 下周计划。\n"
        '同时给出至少 3 条可执行改进项，输出严格 JSON：\n'
        '{"report_md": "Markdown全文", "insights": [{"scope": "topic_weight|publish_time|prompt",'
        ' "content": "具体改什么", "impact": "预期影响"}]}'
    )
    user = (
        f"【统计周期】{week_start} ~ {week_end}\n"
        f"【周数据汇总】\n{stats_summary}\n\n"
        f"【表现最佳笔记】\n{top_notes}\n\n"
        f"【知识库方法论参考】\n{knowledge_text or '（无）'}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def insights_to_text(insights: Optional[list]) -> str:
    """把周报改进项列表转成注入提示词的文本（scope 优先级：prompt > publish_time > topic_weight）。"""
    if not insights:
        return ""
    order = {"prompt": 0, "publish_time": 1, "topic_weight": 2}
    sorted_items = sorted(insights, key=lambda x: order.get(x.get("scope", ""), 99))
    lines = []
    for item in sorted_items:
        lines.append(f"- [{item.get('scope', '通用')}] {item.get('content', '')}（影响：{item.get('impact', '')}）")
    return "\n".join(lines)


def json_dumps_safe(obj) -> str:
    """JSON 序列化（中文不转义，供日志/调试用）。"""
    return json.dumps(obj, ensure_ascii=False, indent=2)
