# -*- coding: utf-8 -*-
"""文档导入模块：读取用户自备数据源（txt / md / pdf），并做分块。

设计说明：
    - 数据源完全由用户自备（对应决策"RAG 数据源用户自配"），项目不内置任何校园数据；
    - 分块采用"按段落/固定窗口"的轻量策略，MVP 不引入重型分块器，
      保持低技术债务；后续可在 loader 内替换为语义分块实现（接口不变）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

# 支持的文本类后缀
TEXT_SUFFIXES = {".txt", ".md", ".markdown"}
# 单个文档块的最大字符数（粗略控制 token 规模）
CHUNK_SIZE = 800
# 相邻块的重叠字符数（减少切断语义，提升检索召回）
CHUNK_OVERLAP = 100


@dataclass
class DocumentChunk:
    """知识库文档块（一条可检索的最小单元）。

    字段：
        text:   块文本内容
        source: 来源文件相对路径（用于回答时的引用溯源）
    """

    text: str
    source: str


def _read_file(path: Path) -> str:
    """按后缀读取文本内容（txt/md 直接读 UTF-8，pdf 用 pypdf 抽取）。"""
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    return ""


def _split_chunks(text: str) -> list[str]:
    """按固定窗口 + 重叠切分文本为若干块（MVP 轻量策略）。"""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks


def load_documents_from_dir(directory: str | Path) -> list[DocumentChunk]:
    """扫描目录下所有受支持文档，返回分块后的 DocumentChunk 列表。

    参数：
        directory: 用户自备知识库文档目录（对应 .env 的 KNOWLEDGE_DIR）
    返回：
        全部文档的分块列表；无文档时返回空列表（上层据此提示）。
    """
    base = Path(directory)
    if not base.exists():
        return []
    chunks: list[DocumentChunk] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in TEXT_SUFFIXES and suffix != ".pdf":
            continue
        content = _read_file(path)
        for piece in _split_chunks(content):
            # 用相对路径作为引用来源，便于用户定位原文
            chunks.append(DocumentChunk(text=piece, source=str(path.relative_to(base))))
    return chunks
