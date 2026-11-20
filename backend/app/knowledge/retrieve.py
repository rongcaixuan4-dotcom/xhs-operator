# -*- coding: utf-8 -*-
"""关键词/BM25 打分检索（纯 Python，无重依赖）；Retriever 协议预留向量扩展（D5）。

分词策略：中文按单字 bigram 切（足够关键词检索场景），ASCII 连续串按整词。
BM25 参数 k1=1.5 b=0.75（经典值）。
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Optional, Protocol

from sqlmodel import Session, select

from ..models import KnowledgeChunk

logger = logging.getLogger("app.knowledge.retrieve")

BM25_K1 = 1.5
BM25_B = 0.75


def tokenize(text: str) -> list[str]:
    """中文 bigram + ASCII 词 的简易分词。"""
    text = text.lower()
    tokens: list[str] = []
    # ASCII 词
    for word in re.findall(r"[a-z0-9]+", text):
        tokens.append(word)
    # 中文按连续段切 bigram（单字也保留，短查询可命中）
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(seg) == 1:
            tokens.append(seg)
            continue
        for i in range(len(seg)):
            tokens.append(seg[i:i + 2])  # bigram（末尾单字自然丢弃）
        tokens.append(seg)  # 整词加分
    return tokens


@dataclass
class SearchHit:
    """检索命中项。"""

    chunk_id: int
    source_type: str
    source_title: str
    content: str
    score: float

    def to_dict(self) -> dict:
        return {
            "id": self.chunk_id,
            "source_type": self.source_type,
            "source_title": self.source_title,
            "content": self.content,
            "score": round(self.score, 4),
        }


class Retriever(Protocol):
    """检索协议（D5：预留向量方案扩展点）。"""

    def search(self, query: str, top_k: int = 5) -> list[SearchHit]:
        ...


class KeywordRetriever:
    """BM25 关键词检索器（MVP 实现）。"""

    def __init__(self, chunks: list[KnowledgeChunk]) -> None:
        self.chunks = [c for c in chunks if c.enabled and c.content]
        self.doc_tokens: list[list[str]] = [tokenize(c.content) for c in self.chunks]
        self.doc_lens = [len(t) for t in self.doc_tokens]
        self.avg_len = (sum(self.doc_lens) / len(self.doc_lens)) if self.doc_lens else 0.0
        self._build_idf()

    def _build_idf(self) -> None:
        """按文档频率计算 IDF。"""
        n = len(self.chunks)
        df: dict[str, int] = {}
        for tokens in self.doc_tokens:
            for t in set(tokens):
                df[t] = df.get(t, 0) + 1
        # 平滑 IDF（BM25+ 变体，避免高频词负值）
        self.idf = {
            t: math.log(1 + (n - freq + 0.5) / (freq + 0.5))
            for t, freq in df.items()
        }

    def search(self, query: str, top_k: int = 5) -> list[SearchHit]:
        """BM25 打分，返回 top_k 命中（score 降序）。"""
        if not self.chunks or self.avg_len == 0:
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        scores = [0.0] * len(self.chunks)
        for tokens in q_tokens:
            idf = self.idf.get(tokens)
            if idf is None:
                continue
            for idx, doc_tokens in enumerate(self.doc_tokens):
                freq = doc_tokens.count(tokens)
                if freq == 0:
                    continue
                denom = freq + BM25_K1 * (1 - BM25_B + BM25_B * self.doc_lens[idx] / self.avg_len)
                scores[idx] += idf * (freq * (BM25_K1 + 1)) / denom
        ranked = sorted(
            ((score, idx) for idx, score in enumerate(scores) if score > 0),
            key=lambda x: x[0], reverse=True,
        )[:top_k]
        return [
            SearchHit(
                chunk_id=self.chunks[idx].id or 0,
                source_type=self.chunks[idx].source_type,
                source_title=self.chunks[idx].source_title,
                content=self.chunks[idx].content,
                score=score,
            )
            for score, idx in ranked
        ]


def load_enabled_chunks(session: Session, account_id: int) -> list[KnowledgeChunk]:
    """加载账号下全部启用切片。"""
    return list(session.exec(
        select(KnowledgeChunk).where(
            KnowledgeChunk.account_id == account_id,
            KnowledgeChunk.enabled == True,  # noqa: E712 - SQLModel 需要 == True
        )
    ).all())


def search_chunks(
    session: Session,
    *,
    account_id: int,
    query: str,
    top_k: int = 5,
    source_types: Optional[list[str]] = None,
) -> list[SearchHit]:
    """便捷入口：加载启用切片并 BM25 检索（可选按 source_type 过滤）。"""
    chunks = load_enabled_chunks(session, account_id)
    if source_types:
        chunks = [c for c in chunks if c.source_type in source_types]
    retriever = KeywordRetriever(chunks)
    hits = retriever.search(query, top_k=top_k)
    logger.debug("检索完成：query=%s 命中=%s", query[:50], len(hits))
    return hits
