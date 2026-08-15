# -*- coding: utf-8 -*-
"""Agent 工具调用循环单元测试：用替身 LLM 模拟 function calling 流程。"""

from types import SimpleNamespace

from app.agent.core import Agent
from app.agent.memory import SessionMemory
from app.agent.tools import ToolRegistry, ToolSpec


class FakeToolCall:
    """模拟 OpenAI 的 tool_call 对象。"""

    def __init__(self, tool_id: str, name: str, arguments: str) -> None:
        self.id = tool_id
        self.function = SimpleNamespace(name=name, arguments=arguments)


class FakeMessage:
    """模拟 OpenAI 返回的 message 对象（content + 可选 tool_calls）。"""

    def __init__(self, content: str | None = None, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, exclude_none=True) -> dict:
        data: dict = {"content": self.content, "tool_calls": []}
        if self.tool_calls:
            data["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in self.tool_calls
            ]
        return data


class FakeLLM:
    """替身 LLM：按脚本返回消息（先工具调用、后最终回答）。"""

    def __init__(self, script: list) -> None:
        self._script = script
        self.calls: list[list] = []  # 记录每次收到的 messages，供断言

    def chat_with_tools(self, messages, tools=None, temperature=0.7, max_tokens=None):
        self.calls.append(messages)
        return self._script.pop(0)


def _make_registry() -> ToolRegistry:
    """构造含 knowledge_search 工具的白名单注册表。"""
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="knowledge_search",
            description="检索知识库",
            fn=lambda q: f"[知识库结果] 关于「{q}」的资料",
        )
    )
    return registry


def test_agent_runs_tool_loop(tmp_path):
    """Agent 应执行模型请求的工具调用，并把结果回填后再取最终回答。"""
    fake = FakeLLM(
        [
            FakeMessage(
                tool_calls=[FakeToolCall("call_1", "knowledge_search", '{"question":"校历"}')]
            ),
            FakeMessage(content="根据知识库：校历第一周是 X 月 X 日。"),
        ]
    )
    agent = Agent(
        llm=fake,
        registry=_make_registry(),
        memory=SessionMemory(data_dir=tmp_path, session_id="t1"),
    )
    reply = agent.chat("本学期校历是怎样的？")
    assert "校历第一周" in reply
    # 第二次 LLM 调用应包含 tool 消息回填（工具执行结果进入上下文）
    second_call = fake.calls[1]
    assert any(m.get("role") == "tool" for m in second_call)
    assert any("[知识库结果]" in m.get("content", "") for m in second_call if m.get("role") == "tool")


def test_agent_handles_direct_answer(tmp_path):
    """模型未请求工具时，应直接把首轮内容作为回答。"""
    fake = FakeLLM([FakeMessage(content="你好！")])
    agent = Agent(
        llm=fake,
        registry=_make_registry(),
        memory=SessionMemory(data_dir=tmp_path, session_id="t2"),
    )
    assert agent.chat("你好") == "你好！"


def test_run_tool_call_structured_params():
    """结构化工具应按声明以具名参数调用（待办方向 A 路线一核心行为）。

    验证点：
        1) 正常调用：parameters 声明的参数以 **kwargs 传入 fn；
        2) 可选参数缺省：沿用函数默认值，无需 LLM 每次携带；
        3) 未知参数：按声明过滤忽略，不报错（容忍 LLM 输出冗余字段）；
        4) 缺必填参数 / 非法 JSON / 参数非对象：返回可读错误，不抛异常中断主循环。
    """
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="multi_param",
            description="多参数工具",
            fn=lambda query, count=5: f"query={query};count={count}",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "关键词"},
                    "count": {"type": "integer", "description": "返回条数"},
                },
                "required": ["query"],
            },
        )
    )
    # 1) 正常调用：具名参数传入
    assert registry.run_tool_call("multi_param", '{"query":"宿舍","count":3}') == "query=宿舍;count=3"
    # 2) 可选参数缺省：沿用函数默认值
    assert registry.run_tool_call("multi_param", '{"query":"食堂"}') == "query=食堂;count=5"
    # 3) 未知参数：按声明过滤忽略
    assert registry.run_tool_call("multi_param", '{"query":"a","foo":1}') == "query=a;count=5"
    # 4) 缺必填参数：返回可读错误
    assert "缺少必填参数 query" in registry.run_tool_call("multi_param", '{"count":3}')
    # 5) 非法 JSON：返回可读错误
    assert "不是合法 JSON" in registry.run_tool_call("multi_param", "not-json")
    # 6) 参数非对象：返回可读错误
    assert "必须是 JSON 对象" in registry.run_tool_call("multi_param", '["a"]')


def test_connector_tool_specs_declare_structured_params():
    """全部社区/官网连接器的 to_tool_spec 应声明结构化 parameters（路线一落地检查）。

    背景：此前所有工具统一暴露单参数 question，tieba_search 的 kw（贴吧名）、
    cug_news_search 的 channel（栏目）等可选参数无法传给 LLM；改造后每个连接器
    应声明自己的参数 schema，且 fn 直接引用函数本体（具名参数调用）。
    """
    from connectors.bilibili_connector import to_tool_spec as bilibili_tool
    from connectors.cug_news_connector import CHANNELS, to_tool_spec as cug_news_tool
    from connectors.tieba_connector import to_tool_spec as tieba_tool
    from connectors.xiaohongshu_connector import to_tool_spec as xiaohongshu_tool
    from connectors.zhihu_connector import to_tool_spec as zhihu_tool
    from connectors.zhihu_connector import to_global_tool_spec as zhihu_global_tool

    # 每个工具的 fn 都应直接引用函数本体（不再是 lambda 单参包装），
    # 且函数签名接收的参数名与 parameters 声明的 key 一一对应
    specs = [
        zhihu_tool(), zhihu_global_tool(), bilibili_tool(),
        tieba_tool(), cug_news_tool(), xiaohongshu_tool(),
    ]
    names = {s.name for s in specs}
    assert len(names) == len(specs), "工具名必须全局唯一"

    # 各工具必填参数断言（与连接器真实接口关键字段一致）
    required_by_name = {
        "zhihu_search": ["query"],          # 知乎 OpenAPI 站内：Query/Count
        "zhihu_global_search": ["query"],   # 知乎 OpenAPI 全网：Query/Count/SearchDB
        "bilibili_search": ["query"],       # B站搜索接口：keyword/page/page_size
        "tieba_search": ["keyword"],        # 贴吧：keyword 必填 + kw 可选（贴吧名）
        "cug_news_search": ["keyword"],     # 官网：keyword 必填 + channel 可选（栏目）
        "xiaohongshu_search": ["keyword"],  # 小红书：仅 keyword
    }
    for spec in specs:
        props = spec.parameters["properties"]
        required = spec.parameters["required"]
        assert set(required) == set(required_by_name[spec.name]), f"{spec.name} 必填参数声明异常"
        # 所有声明的参数都必须在 properties 中有描述（供 LLM 决策）
        assert all(props[k].get("description") for k in required_by_name[spec.name])

    # 专项断言：贴吧 kw / 官网 channel 此前无法暴露的可选参数现已声明
    tieba = next(s for s in specs if s.name == "tieba_search")
    assert "kw" in tieba.parameters["properties"], "贴吧 kw（贴吧名）参数必须声明"
    news = next(s for s in specs if s.name == "cug_news_search")
    assert news.parameters["properties"]["channel"]["enum"] == list(CHANNELS), "官网 channel 必须用 enum 限定栏目"

