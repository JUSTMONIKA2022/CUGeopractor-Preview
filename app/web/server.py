# -*- coding: utf-8 -*-
"""本地 Web UI 服务（FastAPI，默认绑定 127.0.0.1）。

接口：
    GET  /                       静态页面（对话窗 + 配置向导）
    GET  /api/health             健康/配置状态检查
    POST /api/chat               对话（Web 页面用）{"message"} -> {"reply"}
    POST /api/agent/invoke       Agent API（供其他程序接入）{"message","session_id"} -> {"reply","tools"}
    GET  /api/agent/tools        列出当前可用工具（连接器 + 知识检索）
    POST /api/config             保存配置 {base_url, model, api_key}（密钥加密存储）
    POST /api/knowledge/index    重建知识库索引

安全说明：
    - 默认仅监听 127.0.0.1（config.host），禁止随意对外暴露；
    - API Key 通过 app.secrets 加密保存，接口响应不回显密钥明文；
    - Agent 仅调用白名单工具（知识检索 + 用户配置的连接器）。
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent.core import Agent
from app.agent.memory import SessionMemory
from app.agent.tools import ToolRegistry, create_default_registry
from app.config import Settings, get_settings, update_env_file
from app.llm.client import LLMClient, LLMError
from app.rag.loader import load_documents_from_dir
from app.rag.retriever import Retriever
from app.rag.store import VectorStore
from app.secrets import redact, read_api_key, store_api_key

# 静态资源目录（index.html 所在）
STATIC_DIR = Path(__file__).parent / "static"


class ChatRequest(BaseModel):
    """对话请求体（Web 页面用）。"""

    message: str


class AgentInvokeRequest(BaseModel):
    """Agent API 请求体（供其他程序接入）。

    字段：
        message:    用户/程序发来的问题
        session_id: 会话标识（用于隔离多会话历史；缺省为 web）
    """

    message: str
    session_id: str = "web"


class ConfigRequest(BaseModel):
    """配置保存请求体（api_key 可选，留空则保持原值）。"""

    base_url: str
    model: str
    api_key: str = ""


class CommandRequest(BaseModel):
    """命令执行请求体（供其他程序通过 API 调用 CLI 命令系统）。"""

    command: str
    session_id: str = "web"


class ResearchRequest(BaseModel):
    """综合调研请求体（agent 多来源自主搜集）。"""

    message: str
    session_id: str = "web"


def _build_llm(settings: Settings) -> LLMClient:
    """依据配置构建 LLM 客户端（优先 .env 密钥，其次加密存储）。"""
    api_key = settings.llm_api_key or read_api_key(settings.data_dir)
    return LLMClient(
        base_url=settings.llm_base_url,
        api_key=api_key,
        model=settings.llm_model,
        timeout=settings.llm_timeout,
    )


def _check_api_token(request: Request, settings: Settings) -> JSONResponse | None:
    """Agent API 可选鉴权校验：配置了 GEOPRACTOR_API_TOKEN 时要求 Bearer Token。

    返回 None=通过；返回 JSONResponse=拒绝（401）。未配置 token 时一律放行
    （保持本机默认部署的开箱即用体验）。
    """
    token = settings.api_token.strip()
    if not token:
        return None
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {token}":
        return None
    return JSONResponse(
        {"ok": False, "error": "未授权：需要有效的 Bearer Token（GEOPRACTOR_API_TOKEN）"},
        status_code=401,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建 FastAPI 应用（工厂函数，便于测试注入 mock 配置）。"""
    settings = settings or get_settings()
    app = FastAPI(title="行至大地·Geopractor", version="0.1.0")

    # 依赖初始化（进程级单例：向量库/检索器/工具注册表）
    store = VectorStore(data_dir=settings.data_dir)
    retriever = Retriever(store)
    registry = create_default_registry(retriever)
    # 注册用户配置的校园 HTTP 连接器（无配置文件则跳过）
    from connectors.http_connector import register_connectors
    from connectors.session_connector import register_session_connectors
    from connectors.bilibili_connector import to_tool_spec as bilibili_tool
    from connectors.cug_news_connector import to_tool_spec as cug_news_tool
    from connectors.tieba_connector import to_tool_spec as tieba_tool
    from connectors.xiaohongshu_connector import to_tool_spec as xiaohongshu_tool
    from connectors.zhihu_connector import to_tool_spec as zhihu_tool
    from connectors.zhihu_connector import to_global_tool_spec as zhihu_global_tool
    from connectors.portal_connector import to_tool_specs as portal_specs

    register_connectors(registry)
    # 注册用户配置的会话型连接器（信息门户/教务；默认关闭，配置后启用）
    register_session_connectors(registry)
    # 官方公开渠道：官网实时检索（通知公告/学术动态/地大要闻）
    registry.register(cug_news_tool())
    # 社区渠道：知乎(OpenAPI，站内+全网)、B站(公开接口)、贴吧(公开抓取)、小红书(用户自带Cookie)
    registry.register(zhihu_tool())
    registry.register(zhihu_global_tool())
    registry.register(bilibili_tool())
    registry.register(tieba_tool())
    registry.register(xiaohongshu_tool())
    # 信息门户只读服务（网上厅：办事流程/自习室课表/南望厅服务事项）
    for spec in portal_specs():
        registry.register(spec)

    # 会话级 Agent 缓存：同一 session_id 复用同一记忆（程序可并行多个会话）
    # 使用 OrderedDict 实现简单 LRU，容量上限防止 session_id 无限增长导致内存泄漏
    _AGENT_CACHE_MAX = 50
    _agent_cache: "OrderedDict[str, Agent]" = OrderedDict()

    def _get_agent(session_id: str) -> Agent:
        """按 session_id 获取（或创建）Agent 实例；超出容量时淘汰最久未用的会话。"""
        agent = _agent_cache.pop(session_id, None)
        if agent is None:
            memory = SessionMemory(data_dir=settings.data_dir, session_id=session_id)
            agent = Agent(llm=_build_llm(settings), registry=registry, memory=memory)
        # 重新插入（置于最新），并淘汰最旧的会话
        _agent_cache[session_id] = agent
        while len(_agent_cache) > _AGENT_CACHE_MAX:
            _agent_cache.popitem(last=False)
        return agent

    @app.get("/api/health")
    def health() -> JSONResponse:
        """健康检查：返回配置是否就绪（是否已配置 base_url/model）。"""
        return JSONResponse(
            {
                "ok": True,
                "configured": settings.is_configured,
                "knowledge_blocks": store.count(),
                "connectors": [spec.name for spec in registry.list() if spec.name != "knowledge_search"],
            }
        )

    @app.get("/api/agent/tools")
    def list_tools() -> JSONResponse:
        """列出当前可用工具（供调用方与调试查看）。"""
        return JSONResponse(
            {"tools": [{"name": spec.name, "description": spec.description} for spec in registry.list()]}
        )

    @app.post("/api/chat")
    def chat(req: ChatRequest, request: Request) -> JSONResponse:
        """对话接口（Web 页面用）：调用 Agent 主循环并返回回答。"""
        denied = _check_api_token(request, settings)
        if denied is not None:
            return denied
        if not settings.is_configured:
            return JSONResponse({"reply": "尚未配置模型服务，请先在设置中填写 Base URL 与模型名称。"}, status_code=400)
        try:
            reply = _get_agent("web").chat(req.message)
            return JSONResponse({"reply": reply})
        except LLMError as exc:
            # 错误信息不包含密钥明文
            return JSONResponse({"reply": str(exc)}, status_code=502)

    @app.post("/api/agent/invoke")
    def agent_invoke(req: AgentInvokeRequest, request: Request) -> JSONResponse:
        """Agent API（供其他软件/程序接入）：按会话调用 Agent，返回回答与可用工具。

        说明：这是"通过 Agent API 介入其他程序"的入口；调用方自行传入
        session_id 以隔离会话历史。
        """
        denied = _check_api_token(request, settings)
        if denied is not None:
            return denied
        if not settings.is_configured:
            return JSONResponse({"ok": False, "error": "尚未配置模型服务"}, status_code=400)
        try:
            reply = _get_agent(req.session_id).chat(req.message)
            return JSONResponse(
                {
                    "ok": True,
                    "reply": reply,
                    "session_id": req.session_id,
                    "tools": [spec.name for spec in registry.list()],
                }
            )
        except LLMError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    # ===== CLI 命令系统 API 联动（协议见 docs/api-protocol.md） =====

    @app.get("/api/cache")
    def api_cache_list(request: Request) -> JSONResponse:
        """列出全部缓存渠道状态（供外部程序轮询）。"""
        denied = _check_api_token(request, settings)
        if denied is not None:
            return denied
        from app.cache_store import list_cached

        return JSONResponse({"ok": True, "channels": list_cached()})

    @app.get("/api/cache/{channel}")
    def api_cache_get(channel: str, refresh: int = 0, request: Request = None) -> JSONResponse:
        """读取某渠道缓存；refresh=1 时强制刷新后返回。"""
        denied = _check_api_token(request, settings)
        if denied is not None:
            return denied
        from app.cache_store import CHANNELS, get_or_refresh

        if channel not in CHANNELS:
            return JSONResponse({"ok": False, "error": f"未知渠道：{channel}"}, status_code=404)
        data = get_or_refresh(channel, force=bool(refresh))
        return JSONResponse({"ok": True, "data": data})

    @app.post("/api/commands")
    def api_commands(req: CommandRequest, request: Request) -> JSONResponse:
        """执行 CLI 命令系统的命令，返回文本输出（供其他程序联动）。

        命令与 CLI 一致：/cache_search、/cache_<渠道>、/llm <问题> 等。
        """
        denied = _check_api_token(request, settings)
        if denied is not None:
            return denied
        from app.commands import CmdContext, dispatch_command

        agent = _get_agent(req.session_id)
        output: list[str] = []
        ctx = CmdContext(agent=agent, settings=settings, out=output.append)
        cmd = req.command.strip()
        if not cmd:
            return JSONResponse({"ok": False, "error": "command 不能为空"}, status_code=400)
        if not cmd.startswith("/"):
            cmd = "/" + cmd  # 宽容：允许不带斜杠
        cmd_name, _, arg = cmd.partition(" ")
        # 模型门槛分级（放宽，对齐 CLI 行为）：
        # 仅 /llm、/research 等依赖 LLM 的命令要求已配置模型（未配置返回 400）；
        # /cache_*、/live_*、/schedule、/office_hours、/next、/next_course 等纯命令
        # 不依赖 LLM，未配置模型也可直接执行。dispatch_command 是 CLI 与 Web 的
        # 唯一分发入口，此处仅做 HTTP 语义划分，具体命令行为由 commands.py 统一实现。
        if not settings.is_configured and cmd_name.lstrip("/").lower() in ("llm", "research"):
            return JSONResponse({"ok": False, "error": "尚未配置模型服务"}, status_code=400)
        try:
            # 统一命令分发（/cache_* /live_* /help /llm /research /cron /course 等）
            if not dispatch_command(ctx, cmd_name, arg):
                output.append(f"未知命令：{cmd_name}")
        except Exception as exc:  # noqa: BLE001 命令异常返回可读错误
            return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500)
        return JSONResponse({"ok": True, "output": "\n".join(output), "session_id": req.session_id})

    @app.post("/api/research")
    def api_research(req: ResearchRequest, request: Request) -> JSONResponse:
        """综合调研：agent 多来源（官网/门户/贴吧/知乎/B站）交叉搜集并输出报告。"""
        denied = _check_api_token(request, settings)
        if denied is not None:
            return denied
        if not settings.is_configured:
            return JSONResponse({"ok": False, "error": "尚未配置模型服务"}, status_code=400)
        prompt = (
            f"请对「{req.message}」做一次综合调研：从学校官网、信息门户、百度贴吧、知乎、B站等"
            "渠道尽可能多地搜集相关信息，交叉验证不同来源的差异，输出一份结构化调研报告"
            "（按来源分类列出，每条附标题与链接；来源冲突之处如实说明并提示以官方为准；"
            "社区来源标注『该信息来自社区，仅供参考』）。"
        )
        try:
            reply = _get_agent(req.session_id).chat(prompt)
            return JSONResponse({"ok": True, "reply": reply, "session_id": req.session_id})
        except LLMError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    @app.post("/api/config")
    def save_config(req: ConfigRequest) -> JSONResponse:
        """保存配置：base_url/model 写入 .env；api_key 非空时加密存储。

        说明：.env 写入使用"按 key 合并更新"（update_env_file），
        避免整文件覆盖把用户已有的校园凭据（CUG_USERNAME 等）清掉。
        """
        settings.llm_base_url = req.base_url.strip()
        settings.llm_model = req.model.strip()
        # 非敏感配置写入 .env（供后续进程复用）；.env 已被 .gitignore 排除
        update_env_file(
            {"LLM_BASE_URL": settings.llm_base_url, "LLM_MODEL": settings.llm_model}
        )
        if req.api_key.strip():
            # 密钥走加密存储（不写入 .env 明文）
            store_api_key(req.api_key.strip(), settings.data_dir)
        return JSONResponse({"ok": True, "message": "配置已保存"})

    @app.post("/api/knowledge/index")
    def index_knowledge() -> JSONResponse:
        """重建知识库索引：扫描 KNOWLEDGE_DIR，清空向量库后重新导入。

        说明：知识库目录不存在时自动创建；无文档返回可读提示（indexed=0）。
        """
        Path(settings.knowledge_dir).mkdir(parents=True, exist_ok=True)
        chunks = load_documents_from_dir(settings.knowledge_dir)
        if not chunks:
            return JSONResponse(
                {
                    "ok": True,
                    "indexed": 0,
                    "message": f"知识库目录 {settings.knowledge_dir} 下未找到文档，"
                    "请放入 txt/md/pdf 后重试；示例见 docs/examples/knowledge/。",
                }
            )
        from app.rag.store import build_embedding_function

        store = VectorStore(
            data_dir=settings.data_dir,
            embedding_function=build_embedding_function(settings),
        )
        store.clear()
        count = store.add_chunks(chunks)
        return JSONResponse({"ok": True, "indexed": count})

    # 静态页面挂载
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


def serve(settings: Settings | None = None) -> None:
    """以 uvicorn 启动本地服务（默认 127.0.0.1:8080）。"""
    import uvicorn

    cfg = settings or get_settings()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port)
