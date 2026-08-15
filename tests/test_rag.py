# -*- coding: utf-8 -*-
"""RAG 模块单元测试：
- loader 的分块与文档读取；
- retriever 的上下文拼装与"无命中"行为（用替身 store，避免测试依赖下载嵌入模型）。
"""

from app.rag.loader import DocumentChunk, _split_chunks, load_documents_from_dir
from app.rag.retriever import Retriever


class FakeStore:
    """测试替身：返回固定命中结果，避免 chromadb 下载嵌入模型。"""

    def __init__(self, hits: list[DocumentChunk]) -> None:
        self._hits = hits

    def query(self, text: str, top_k: int = 4) -> list[DocumentChunk]:
        return self._hits[:top_k]


def test_split_chunks():
    """长文本应被切分为多个块且保留来源语义。"""
    text = "甲" * 900
    chunks = _split_chunks(text)
    assert len(chunks) >= 2, "超过单块上限应切分为至少两块"
    assert all(c for c in chunks), "分块结果不应包含空串"


def test_load_documents_from_dir(tmp_path):
    """目录扫描应只导入受支持类型（txt/md/pdf），忽略其他文件。"""
    (tmp_path / "a.txt").write_text("hello knowledge", encoding="utf-8")
    (tmp_path / "b.log").write_text("not supported", encoding="utf-8")
    chunks = load_documents_from_dir(tmp_path)
    assert len(chunks) == 1
    assert chunks[0].source == "a.txt"


def test_retriever_no_hit():
    """无命中时 build_context 应返回空串，由 Agent 层如实回答不知道。"""
    retriever = Retriever(FakeStore([]))
    assert retriever.build_context("不存在的知识") == ""


def test_retriever_with_hits():
    """命中时应拼装带来源的上下文。"""
    hits = [DocumentChunk(text="校历第一周", source="guide.md")]
    retriever = Retriever(FakeStore(hits))
    context = retriever.build_context("第一周是什么时候")
    assert "guide.md" in context
    assert "校历第一周" in context
