# -*- coding: utf-8 -*-
"""LLM 适配层（OpenAI 兼容协议）。

设计说明：
    - 所有主流模型服务（DeepSeek / Qwen / Kimi / GLM / 本地 Ollama / vLLM 等）
      均提供 OpenAI 兼容的 /v1/chat/completions 接口，因此本模块仅依赖
      openai SDK，通过可配置 base_url 实现"多厂商、单一适配层"；
    - 与 config/secrets 解耦：API Key 由调用方注入，本层不感知来源，
      便于测试时注入 mock 值；
    - 错误处理统一转换为可读异常，Web/CLI 可友好提示。
"""

from __future__ import annotations

from dataclasses import dataclass

from openai import OpenAI


class LLMError(RuntimeError):
    """LLM 调用统一异常类型（鉴权失败/超时/网络错误等）。"""


@dataclass
class LLMClient:
    """OpenAI 兼容协议客户端。

    字段：
        base_url: 模型服务地址（如 https://api.deepseek.com/v1）
        api_key:  用户自配的 API 密钥
        model:    模型名称（如 deepseek-chat）
        timeout:  请求超时秒数
    """

    base_url: str
    api_key: str
    model: str
    timeout: int = 60

    def _client(self) -> OpenAI:
        """构建底层 openai 客户端（每次调用新建，避免长连接状态泄漏）。"""
        return OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout)

    def chat(self, messages: list[dict], temperature: float = 0.7, max_tokens: int | None = None) -> str:
        """执行一次非流式对话补全。

        参数：
            messages: OpenAI 消息列表，如 [{"role":"user","content":"你好"}]
            temperature: 采样温度（0~2，越小越确定）
            max_tokens: 输出最大 token 数（None 表示使用服务端默认）
        返回：
            模型生成的文本内容。
        异常：
            LLMError：统一包装鉴权失败/网络错误/超时等，便于上层友好提示。
        """
        message = self.chat_with_tools(messages, tools=None, temperature=temperature, max_tokens=max_tokens)
        return message.content or ""

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ):
        """带工具（function calling）的对话补全。

        说明（对应"LLM 通过工具访问/整理信息"的核心机制）：
            - 传入 OpenAI 标准 tools 描述（由工具注册表生成），模型可返回 tool_calls；
            - 返回值是 OpenAI 的 message 对象，调用方需解析：
              message.content 为文本；message.tool_calls 为工具调用列表
              （每个含 id、function.name、function.arguments(JSON 字符串)）。
        参数：
            messages: OpenAI 消息列表（含 system/user 及可选的 tool 结果回填）
            tools:    OpenAI 标准工具 schema 列表（None 表示纯对话）
        返回：
            OpenAI chat completion 返回的 message 对象。
        """
        if not self.api_key:
            raise LLMError("未配置 API Key，请先运行配置命令或填写 .env")
        try:
            kwargs: dict = {"model": self.model, "messages": messages, "temperature": temperature}
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens
            if tools:
                kwargs["tools"] = tools
            resp = self._client().chat.completions.create(**kwargs)
            return resp.choices[0].message
        except Exception as exc:  # 网络/鉴权/超时等统一转为可读错误
            raise LLMError(f"LLM 调用失败：{exc}") from exc

    def check(self) -> bool:
        """连通性自检：发送空问题，返回是否可正常调用（供配置向导使用）。"""
        try:
            self.chat([{"role": "user", "content": "ping"}], max_tokens=4)
            return True
        except LLMError:
            return False
