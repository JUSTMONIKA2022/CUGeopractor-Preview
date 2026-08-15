# -*- coding: utf-8 -*-
"""Agent 对话主循环：LLM 决策 + 工具（function calling）执行编排。

核心机制（对应"用户提供 LLM API，由 LLM 通过工具访问/整理信息"的项目定位）：
    1. 组装 system prompt（角色约束 + 行为边界，防幻觉/防越权）；
    2. 把已注册工具（RAG 知识检索、HTTP 校园连接器等）以 OpenAI tools schema
       暴露给 LLM，由模型自主决定是否调用及调用哪个工具；
    3. 若模型返回 tool_calls，Agent 依次执行（白名单校验）并把结果回填为
       tool 消息，再次请求 LLM，直到模型给出最终文本回答（最多 MAX_ROUNDS 轮）；
    4. 保存会话历史，返回最终回答。

安全约束：
    - 仅白名单工具可被调用（tools.py ToolRegistry）；
    - 未注册/参数非法一律拒绝，不让异常中断循环；
    - 工具调用次数受限，防止模型死循环消耗配额。
"""

from __future__ import annotations

from app.agent.memory import SessionMemory
from app.agent.tools import ToolRegistry
from app.llm.client import LLMClient

# 单轮对话中允许的最大工具调用轮数（防止死循环与配额滥用）
MAX_ROUNDS = 6

# 系统提示词：约束角色与行为边界（提示词层第一道防线）
SYSTEM_PROMPT = (
    "你是一位谨慎、务实的中文校园助手（行至大地·Geopractor）。\n"
    "回答规则：\n"
    "1. 当用户问题需要校园信息时，优先调用可用工具获取资料；引用资料时注明来源。\n"
    "2. 工具没有返回相关内容时，如实说明'未查询到相关内容'，不要编造。\n"
    "3. 不回答与国家法律法规相悖的内容，不冒充任何官方机构。\n"
    "4. 若用户要求执行文件操作、联网等未开放能力，说明'当前版本未开放该能力'。\n"
    "来源引用规则：\n"
    "5. 凡基于工具返回的信息作答，必须标注来源（工具名或标题+链接）；\n"
    "   多条信息来自不同来源时，分别注明，不得混为一谈。\n"
    "6. 社区/个人发布的非权威内容（如贴吧、论坛帖）只做摘要式转述，\n"
    "   不直接转载长文，并提示'该信息来自社区，仅供参考'。\n"
    "多工具协同规则：\n"
    "7. 可一次调用多个工具交叉验证（如官网+社区同查一个问题）；\n"
    "   若多个工具结果冲突或矛盾，如实呈现差异并提示用户自行判断，\n"
    "   不要擅自认定某一边正确。\n"
)


class Agent:
    """对话 Agent 主循环（LLM + 工具调用编排）。"""

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        memory: SessionMemory,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._memory = memory

    def set_llm(self, llm: LLMClient) -> None:
        """热替换 LLM 客户端（多方案切换：/configure use <方案> 后
        当前会话立即改用新方案的模型，无需重启 CLI）。"""
        self._llm = llm

    def _build_messages(self, user_question: str) -> list[dict]:
        """构造对话初始消息（system + 最近历史 + 用户问题）。

        说明：MVP 不在首轮注入检索上下文，而是交由 LLM 自主决定调用
        knowledge_search 工具（与校园连接器统一走 function calling 机制）。
        """
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(self._memory.history()[-10:])
        messages.append({"role": "user", "content": user_question})
        return messages

    def chat(self, user_question: str) -> str:
        """处理一轮对话：工具调用循环 + 保存历史，返回最终回答。"""
        question = user_question.strip()
        if not question:
            return "请输入有效内容。"

        messages = self._build_messages(question)
        tools = self._registry.tools_schemas()

        for _round in range(MAX_ROUNDS):
            # 请求 LLM（带工具描述），模型可能返回 tool_calls
            message = self._llm.chat_with_tools(messages, tools=tools)
            messages.append({"role": "assistant", **message.model_dump(exclude_none=True)})

            if not getattr(message, "tool_calls", None):
                # 模型未请求工具 → 当前内容即为最终回答
                break

            # 依次执行模型请求的工具调用，并把结果回填为 tool 消息
            for tool_call in message.tool_calls:
                result = self._registry.run_tool_call(
                    tool_call.function.name, tool_call.function.arguments
                )
                messages.append(
                    {"role": "tool", "tool_call_id": tool_call.id, "content": result}
                )
        else:
            # 达到最大轮数仍未收敛：返回已有内容，避免无限循环
            return "（已达到工具调用次数上限，请尝试把问题拆细）"

        answer = message.content or ""
        if not answer.strip():
            answer = "（模型未返回可显示内容）"

        # 保存历史（供多轮对话使用）
        self._memory.add("user", question)
        self._memory.add("assistant", answer)
        return answer
