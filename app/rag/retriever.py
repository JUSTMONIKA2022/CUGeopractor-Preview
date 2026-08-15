# -*- coding: utf-8 -*-
"""检索器：把"知识库检索"能力以统一接口暴露给 Agent 使用。

设计说明：
    - 引入"最小上下文拼装"：命中块不足或为空时返回空 context，
      Agent 层据此如实回答"不知道"，避免幻觉（对应可行性报告 RAG 风险缓解）；
    - 检索结果附带来源，供回答引用与审计。
"""

from __future__ import annotations

from app.rag.loader import DocumentChunk
from app.rag.store import VectorStore


class Retriever:
    """知识库检索器（对 VectorStore 的轻量封装）。"""

    def __init__(self, store: VectorStore, top_k: int = 4) -> None:
        self._store = store
        self._top_k = top_k

    def retrieve(self, question: str) -> list[DocumentChunk]:
        """检索与问题最相关的知识块（相关性降序）。"""
        return self._store.query(question, top_k=self._top_k)

    def build_context(self, question: str, max_chars: int = 4000) -> str:
        """把检索结果拼装为供 LLM 使用的上下文文本；无命中返回空串。

        参数：
            question:  用户问题
            max_chars: 上下文最大字符数（防止超长）
        返回：
            形如 "[来源: xxx]\n内容……" 的多块文本；无命中为空串。
        """
        hits = self.retrieve(question)
        if not hits:
            return ""
        parts: list[str] = []
        used = 0
        for hit in hits:
            block = f"[来源: {hit.source}]\n{hit.text}"
            used += len(block)
            if used > max_chars:
                break
            parts.append(block)
        return "\n\n".join(parts)
