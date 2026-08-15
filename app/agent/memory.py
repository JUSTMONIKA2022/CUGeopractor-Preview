# -*- coding: utf-8 -*-
"""会话记忆模块：把多轮对话历史持久化到本地 JSON 文件。

设计说明：
    - 会话历史仅存用户本机（data/session_store/），不入库、不出本机；
    - 提供精简窗口（最近 N 条）供 Agent 构造上下文，控制 token 成本；
    - 可一键清理（隐私友好）。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

# 单个会话保留的最大消息条数（超出丢弃最早的，控制上下文长度）
MAX_MESSAGES = 20


class SessionMemory:
    """本地会话记忆（按会话 id 存储消息列表）。

    线程安全说明：Web 服务可能对同一会话并发读写，add/clear 通过 self._lock
    串行化文件读写，避免并发下消息丢失或文件损坏。
    """

    def __init__(self, data_dir: str | Path = "data", session_id: str = "default") -> None:
        self._dir = Path(data_dir) / "session_store"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._session_id = session_id
        self._file = self._dir / f"{session_id}.json"
        # 会话读写锁（文件 I/O 串行化）
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        """从磁盘读取会话历史；文件缺失或损坏时返回空列表。"""
        if not self._file.exists():
            return []
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def add(self, role: str, content: str) -> None:
        """追加一条消息（role: system/user/assistant），并裁剪超长历史。

        线程安全：持锁完成"读-改-写"，防止并发覆盖。
        """
        with self._lock:
            messages = self._load()
            messages.append({"role": role, "content": content})
            if len(messages) > MAX_MESSAGES:
                messages = messages[-MAX_MESSAGES:]
            self._file.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")

    def history(self) -> list[dict]:
        """返回当前会话消息列表（供 Agent 构造 messages 参数）。"""
        with self._lock:
            return self._load()

    def clear(self) -> None:
        """清空当前会话历史（隐私清理）。"""
        with self._lock:
            if self._file.exists():
                self._file.unlink()
