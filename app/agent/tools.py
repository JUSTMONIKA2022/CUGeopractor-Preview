# -*- coding: utf-8 -*-
"""工具注册表（白名单机制）。

设计说明（对应可行性报告安全基线 §3.5）：
    - 所有 Agent 可调用的"工具"必须先在此注册，未注册能力一律不可用（默认拒绝）；
    - MVP 仅开放"知识库检索"一项工具，杜绝文件写/命令执行等高危能力；
    - 后续校园连接器（第二阶段）也以"注册一个工具"的方式接入，统一走本白名单。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class ToolSpec:
    """工具描述（注册制、可审计、可暴露为 OpenAI function calling schema）。

    结构化参数说明（对应待办方向 A，路线一）：
        - 每个工具可声明自己的 parameters（OpenAI JSON Schema），
          由 fn 按声明接收具名参数（**kwargs）；
        - 未声明时默认单参数 question（兼容旧工具与简单场景）；
        - fn 签名约定：fn(**kwargs)，参数名与 parameters.properties 的 key 一致。
    """

    name: str                # 工具唯一名
    description: str         # 功能说明（进入 system prompt / tools 描述供模型决策）
    fn: Callable[..., str]   # 调用函数（接收具名参数，返回文本结果）

    # 工具参数 schema：默认单一 question 参数；有特殊参数的工具可覆盖
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "需要查询或整理的请求内容"}},
            "required": ["question"],
        }
    )

    def to_openai_tool(self) -> dict:
        """转换为 OpenAI function calling 标准 tools 描述（供 LLM 决策调用）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """工具注册表：注册/查询/执行（MVP 白名单实现）。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """注册一个工具；重名时直接覆盖（便于测试替换 mock 实现）。"""
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        """按名称查询工具；未注册返回 None（上层据此拒绝调用）。"""
        return self._tools.get(name)

    def list(self) -> list[ToolSpec]:
        """返回全部已注册工具（用于生成 system prompt 与审计）。"""
        return list(self._tools.values())

    def call(self, name: str, arg: str) -> str:
        """执行已注册工具（兼容旧单参调用）；未注册工具直接拒绝并返回错误说明。"""
        spec = self.get(name)
        if spec is None:
            return f"[错误] 工具 {name} 未注册，调用被拒绝"
        # 兼容：以字符串 arg 作为 question 传给 fn（保留旧接口）
        return spec.fn(arg)

    def tools_schemas(self) -> list[dict]:
        """返回全部已注册工具的 OpenAI function calling 描述（供 LLM 决策）。"""
        return [spec.to_openai_tool() for spec in self._tools.values()]

    def run_tool_call(self, name: str, arguments_json: str) -> str:
        """执行一次 LLM 发起的工具调用（解析 JSON 参数并转发）。

        结构化参数说明（对应待办方向 A，路线一）：
            - 解析 arguments（JSON 对象）后，按工具声明的 parameters.properties 的 key
              以 **kwargs 方式调用 fn（具名参数）；
            - 对未声明自定义参数的工具（默认 question 单参），兼容解析 question；
            - 参数缺失/非法时返回可读错误，不让异常中断 Agent 主循环；
            - 未注册工具一律拒绝（白名单安全边界）。
        """
        spec = self.get(name)
        if spec is None:
            return f"[错误] 工具 {name} 未注册，调用被拒绝"
        try:
            args = json.loads(arguments_json or "{}")
        except json.JSONDecodeError:
            return f"[错误] 工具 {name} 的参数不是合法 JSON"
        if not isinstance(args, dict):
            return f"[错误] 工具 {name} 的参数必须是 JSON 对象"

        # 判断是否为"自定义参数工具"（parameters 里不含默认的 question 单参）
        props = (spec.parameters or {}).get("properties", {})
        if "question" in props and len(props) == 1:
            # 默认单参工具：取 question
            question = str(args.get("question", "")).strip()
            if not question:
                return f"[错误] 工具 {name} 缺少 question 参数"
            return spec.fn(question)

        # 结构化工具：按声明的参数名过滤并转发（未知参数忽略，缺失必填项提示）
        required = (spec.parameters or {}).get("required", [])
        kwargs = {k: v for k, v in args.items() if k in props}
        for key in required:
            if key not in kwargs:
                return f"[错误] 工具 {name} 缺少必填参数 {key}"
        return spec.fn(**kwargs)

    @property
    def prompt_block(self) -> str:
        """生成供 LLM system prompt 使用的工具清单文本（空则说明无可调工具）。"""
        if not self._tools:
            return "当前没有可调用的工具。"
        return "\n".join(f"- {t.name}: {t.description}" for t in self._tools.values())


def create_default_registry(retriever) -> ToolRegistry:
    """构建默认工具注册表：MVP 仅注册知识库检索。

    参数：
        retriever: app.rag.retriever.Retriever 实例（或具备 build_context 方法的替身）
    """
    registry = ToolRegistry()

    def rag_search(question: str) -> str:
        # 知识库检索：返回带来源的上下文；无命中时返回提示，由 Agent 如实回答不知道
        context = retriever.build_context(question)
        return context if context else "（知识库中未找到相关内容）"

    registry.register(
        ToolSpec(
            name="knowledge_search",
            description="在本地知识库中检索与问题相关的资料，返回带来源的摘录",
            fn=rag_search,
        )
    )
    return registry
