# -*- coding: utf-8 -*-
"""LLM 适配层包入口：对外仅暴露 LLMClient 与 LLMError。"""

from app.llm.client import LLMClient, LLMError

__all__ = ["LLMClient", "LLMError"]
