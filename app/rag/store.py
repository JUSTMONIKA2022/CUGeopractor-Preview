# -*- coding: utf-8 -*-
"""向量库封装（本地 ChromaDB）。

设计说明：
    - 使用 chromadb 的本地持久化模式，全部数据存放在 data/ 目录，不入库、不出本机；
    - 未配置任何嵌入模型/API：MVP 采用 ChromaDB 内置的默认嵌入函数（本地 sentence-transformers
      自动下载最小模型）以保持零密钥可用；文档中说明如需更强效果可切换嵌入后端。
    - 接口保持极简：add_chunks / query / clear，便于上层（retriever / CLI）调用。
"""

from __future__ import annotations

from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

from app.rag.loader import DocumentChunk


class VectorStore:
    """本地向量库（持久化于 data/vectorstore）。"""

    def __init__(self, data_dir: str | Path = "data", collection_name: str = "knowledge") -> None:
        """初始化并加载（或创建）指定 collection。

        参数：
            data_dir:       数据根目录（向量库存放于其下 vectorstore/ 子目录）
            collection_name: 集合名，默认 knowledge
        """
        base = Path(data_dir)
        base.mkdir(parents=True, exist_ok=True)
        # 使用默认嵌入函数：本地 ONNX 小模型，无需 API Key（首次使用会下载模型权重）
        ef = embedding_functions.DefaultEmbeddingFunction()
        self._client = chromadb.PersistentClient(path=str(base / "vectorstore"))
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},  # 余弦相似度检索
        )

    def add_chunks(self, chunks: list[DocumentChunk]) -> int:
        """批量写入文档块；重复调用前建议先 clear（MVP 简化：按 id 覆盖）。

        返回：
            本次实际写入的块数量。
        """
        if not chunks:
            return 0
        ids = [f"{chunk.source}::{idx}" for idx, chunk in enumerate(chunks)]
        docs = [chunk.text for chunk in chunks]
        metas = [{"source": chunk.source} for chunk in chunks]
        self._collection.upsert(ids=ids, documents=docs, metadatas=metas)
        return len(chunks)

    def query(self, text: str, top_k: int = 4) -> list[DocumentChunk]:
        """按文本相似度检索最相关的文档块。

        返回：
            按相关性降序的 DocumentChunk 列表（携带来源用于引用）。
        """
        if not self._collection.count():
            return []
        result = self._collection.query(query_texts=[text], n_results=min(top_k, self._collection.count()))
        out: list[DocumentChunk] = []
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        for doc, meta in zip(documents, metadatas):
            out.append(DocumentChunk(text=doc or "", source=(meta or {}).get("source", "")))
        return out

    def count(self) -> int:
        """返回知识库块总数（用于 CLI 索引状态展示）。"""
        return self._collection.count()

    def clear(self) -> None:
        """清空当前集合（重建索引前调用）。"""
        self._collection.delete(where={})
