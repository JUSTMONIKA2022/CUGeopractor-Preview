# -*- coding: utf-8 -*-
"""向量库封装（本地 ChromaDB）。

设计说明：
    - 使用 chromadb 的本地持久化模式，全部数据存放在 data/ 目录，不入库、不出本机；
    - 向量化默认使用 ChromaDB 内置的默认嵌入函数（本地 ONNX 小模型，免密钥，
      首次使用自动下载权重）；也可通过 build_embedding_function 按配置切换到
      OpenAI 兼容 /embeddings 外部接口（更强模型或远程向量化）。
    - 接口保持极简：add_chunks / query / clear，便于上层（retriever / CLI）调用。
"""

from __future__ import annotations

from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

from app.rag.loader import DocumentChunk


def build_embedding_function(settings) -> object:
    """按配置构建向量化函数（EmbeddingFunction）。

    设计：
        - 默认：ChromaDB 内置 ONNX 小模型（免密钥、本地运行、数据不出本机），
          首次使用自动下载权重到本机缓存；
        - 若配置了 EMBEDDING_BASE_URL：改用 OpenAI 兼容 /embeddings 接口
          （api_key 留空时使用占位符，适配本地服务如 Ollama 的免鉴权模式）。

    参数：
        settings: 全局配置（读取 embedding_base_url / api_key / model 字段）
    返回：
        chromadb 可用的 EmbeddingFunction 实例。
    """
    base_url = (settings.embedding_base_url or "").strip()
    if not base_url:
        # 零密钥默认路径：本地内置模型
        return embedding_functions.DefaultEmbeddingFunction()
    api_key = (settings.embedding_api_key or "").strip() or "not-needed"
    model = (settings.embedding_model or "").strip() or "text-embedding-3-small"
    return embedding_functions.OpenAIEmbeddingFunction(
        api_key=api_key,
        api_base=base_url.rstrip("/"),
        model_name=model,
    )


class VectorStore:
    """本地向量库（持久化于 data/vectorstore）。"""

    def __init__(
        self,
        data_dir: str | Path = "data",
        collection_name: str = "knowledge",
        embedding_function: object | None = None,
    ) -> None:
        """初始化并加载（或创建）指定 collection。

        参数：
            data_dir:         数据根目录（向量库存放于其下 vectorstore/ 子目录）
            collection_name:  集合名，默认 knowledge
            embedding_function: 向量化函数（默认 ChromaDB 内置 ONNX 小模型；
                                也可传入 build_embedding_function(settings) 的结果）
        """
        base = Path(data_dir)
        base.mkdir(parents=True, exist_ok=True)
        ef = embedding_function or embedding_functions.DefaultEmbeddingFunction()
        self._client = chromadb.PersistentClient(path=str(base / "vectorstore"))
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            embedding_function=ef,  # type: ignore[arg-type]
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
        """清空当前集合（重建索引前调用）。

        说明：新版 chromadb 不再支持 delete(where={}) 全量删除，
        改为先取全部 id 再按 id 删除，兼容性更好。
        """
        ids = self._collection.get()["ids"]
        if ids:
            self._collection.delete(ids=ids)
