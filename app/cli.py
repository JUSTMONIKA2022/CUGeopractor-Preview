# -*- coding: utf-8 -*-
"""命令行入口（Typer）。

命令：
    configure   交互式配置 LLM（base_url / model / api_key，密钥加密存储）
    chat        交互式对话（多轮）
    index       重建知识库索引（扫描 KNOWLEDGE_DIR 导入向量库）
    status      查看配置与索引状态
    serve       启动本地 Web UI（默认 127.0.0.1:8080）

说明：CLI 与 Web UI 共用同一套 app 核心代码（配置/密钥/LLM/RAG/Agent）。
"""

from __future__ import annotations

from app.agent.core import Agent
from app.agent.memory import SessionMemory
from app.agent.tools import create_default_registry
from app.config import Settings, get_settings
from app.llm.client import LLMClient, LLMError
from app.rag.loader import load_documents_from_dir
from app.rag.retriever import Retriever
from app.rag.store import VectorStore
from app.secrets import read_api_key, store_api_key

try:
    import typer
except ImportError:  # 依赖未安装时给出友好提示
    typer = None  # type: ignore[assignment]

# Typer 应用实例（typer 不可用时为 None，走手动分发）
app = typer.Typer(help="行至大地·Geopractor —— 本地部署、用户自配密钥的开源校园 Agent") if typer is not None else None


def _build_llm(settings: Settings) -> LLMClient:
    """构建 LLM 客户端：优先活动方案（data/llm_profiles.json），其次 .env。

    多方案机制（需求）：/configure 保存多套 Base URL/模型/密钥，
    启动时按当前活动方案构建；无已存方案时回退 .env + 加密存储密钥（旧行为）。
    密钥按方案 scope 命名空间读取（secrets_<方案名>.enc）。
    """
    from app.config import active_profile_name, load_llm_profiles

    name = active_profile_name(settings.data_dir)
    profile = load_llm_profiles(settings.data_dir)["profiles"].get(name)
    if profile:
        # 活动方案：Base URL/模型来自方案文件，密钥按方案 scope 加密读取
        base_url = profile["base_url"]
        model = profile["model"]
        api_key = read_api_key(settings.data_dir, scope=profile.get("scope", name))
    else:
        # 默认方案：.env 优先，其次加密存储密钥（兼容旧配置）
        base_url = settings.llm_base_url
        model = settings.llm_model
        api_key = settings.llm_api_key or read_api_key(settings.data_dir)
    return LLMClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=settings.llm_timeout,
    )


def _make_agent(settings: Settings) -> Agent:
    """组装完整 Agent（向量库/检索器/工具/连接器/记忆/LLM）。"""
    store = VectorStore(data_dir=settings.data_dir)
    retriever = Retriever(store)
    registry = create_default_registry(retriever)
    # 注册用户配置的校园 HTTP 连接器（无配置文件则跳过；对应"LLM 调工具访问校园信息"）
    from connectors.http_connector import register_connectors
    from connectors.session_connector import register_session_connectors
    from connectors.bilibili_connector import to_tool_spec as bilibili_tool
    from connectors.cug_news_connector import to_tool_spec as cug_news_tool
    from connectors.cug_news_connector import to_navigation_tool_spec as cug_nav_tool
    from connectors.college_connector import to_tool_spec as college_tool
    from connectors.tieba_connector import to_tool_spec as tieba_tool
    from connectors.xiaohongshu_connector import to_tool_spec as xiaohongshu_tool
    from connectors.zhihu_connector import to_tool_spec as zhihu_tool
    from connectors.zhihu_connector import to_global_tool_spec as zhihu_global_tool
    from connectors.portal_connector import to_tool_specs as portal_specs
    from app.course_schedule import to_office_hours_tool_spec as office_hours_tool

    register_connectors(registry)
    # 注册用户配置的会话型连接器（信息门户/教务；默认关闭，配置后启用）
    register_session_connectors(registry)
    # 官方公开渠道：官网实时检索（通知公告/学术动态/地大要闻）
    registry.register(cug_news_tool())
    # 学院网站检索（各学院官网栏目：通知/新闻/动态等， 需求）
    registry.register(college_tool())
    # 社区渠道：知乎(OpenAPI，站内+全网)、B站(公开接口)、贴吧(公开抓取)、小红书(用户自带Cookie)
    registry.register(zhihu_tool())
    registry.register(zhihu_global_tool())
    registry.register(bilibili_tool())
    registry.register(tieba_tool())
    registry.register(xiaohongshu_tool())
    # 信息门户只读服务（网上厅：办事流程/自习室课表/南望厅服务事项）
    for spec in portal_specs():
        registry.register(spec)
    # 时间编排：办公时间判断（基于当前校区方案的办公时间表，LLM 问"现在是办公时间吗"时调用）
    registry.register(office_hours_tool())
    memory = SessionMemory(data_dir=settings.data_dir, session_id="cli")
    return Agent(llm=_build_llm(settings), registry=registry, memory=memory)


def _warn_data_dir(settings: Settings) -> None:
    """若数据目录被外部环境变量覆盖到项目外，给出可读提示（防 DATA_DIR 污染）。"""
    from pathlib import Path

    data = Path(settings.data_dir).resolve()
    proj = Path.cwd().resolve()
    if str(data).lower().startswith(str(proj).lower()):
        return  # 目录在项目内，正常
    print(
        f"[提示] 数据目录指向项目外：{data}\n"
        "  若由系统环境变量 DATA_DIR/KNOWLEDGE_DIR 导致，可在 .env 设置带前缀的\n"
        "  GEOPRACTOR_DATA_DIR=data（带前缀的变量优先，可避免被系统同名变量污染）。"
    )


def configure() -> None:
    """交互式配置：写入 .env 非敏感项 + 加密存储 API Key。"""
    settings = get_settings()
    if typer is not None:
        base_url = typer.prompt("模型服务 Base URL", default=settings.llm_base_url)
        model = typer.prompt("模型名称", default=settings.llm_model)
        api_key = typer.prompt("API 密钥（可留空）", default="", hide_input=True)
    else:
        base_url = input("模型服务 Base URL: ").strip()
        model = input("模型名称: ").strip()
        api_key = input("API 密钥（可留空）: ").strip()
    # 非敏感配置写入 .env（.gitignore 已排除）；密钥不落明文。
    # 用"按 key 合并更新"而非整文件覆盖，避免清掉 .env 中已有的校园凭据
    # （CUG_USERNAME / JWGL_COOKIE / XHS_COOKIE 等）。
    from app.config import update_env_file

    update_env_file({"LLM_BASE_URL": base_url, "LLM_MODEL": model})
    if api_key:
        # 密钥加密存储（不写入 .env 明文）
        store_api_key(api_key, settings.data_dir)
    print("配置已保存；密钥已加密存储在本机。")


def status() -> None:
    """查看配置、知识库索引与渠道缓存状态。"""
    settings = get_settings()
    store = VectorStore(data_dir=settings.data_dir)
    print(f"Base URL : {settings.llm_base_url or '（未配置）'}")
    print(f"模型     : {settings.llm_model or '（未配置）'}")
    print(f"密钥     : {'已配置' if (settings.llm_api_key or read_api_key(settings.data_dir)) else '未配置'}")
    print(f"知识库   : {store.count()} 块（目录 {settings.knowledge_dir}）")
    # 渠道缓存状态一览（新增，便于确认各渠道是否已生成缓存）
    # 注意：状态标记用文本而非 emoji（Windows GBK 控制台无法输出 ⏳/✅，会 UnicodeEncodeError）
    from app.cache_store import list_cached

    def _fmt_ts(ts):
        """时间戳转可读时间（无则显示 —）。"""
        import datetime

        if not ts:
            return "—"
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

    print("渠道缓存 :")
    for entry in list_cached():
        mark = "[已缓存]" if entry["cached"] else "[未生成]"
        updated = f"｜更新 {_fmt_ts(entry['updated'])}"
        print(f"  {mark} {entry['name']}（{entry['command']}）{updated}")
    # 会话登录态提示（门户/教务工具依赖）
    print("提示     : 门户/教务工具需先运行 geopractor session-login 登录一次")


def index() -> None:
    """重建知识库索引：扫描 KNOWLEDGE_DIR 并导入向量库。"""
    settings = get_settings()
    chunks = load_documents_from_dir(settings.knowledge_dir)
    if not chunks:
        print(f"知识库目录 {settings.knowledge_dir} 下未找到 txt/md/pdf 文档，请先放入资料。")
        return
    store = VectorStore(data_dir=settings.data_dir)
    store.clear()
    count = store.add_chunks(chunks)
    print(f"索引完成：共导入 {count} 个知识块。")


def serve() -> None:
    """启动本地 Web UI（默认 127.0.0.1:8080）。"""
    from app.web.server import serve as web_serve

    settings = get_settings()
    _warn_data_dir(settings)  # 防 DATA_DIR 系统变量污染（发现）
    try:
        web_serve(settings)
    except OSError as exc:
        # 端口占用等启动失败给出可读提示（新增，避免裸报错退出）
        print(f"[错误] 无法启动本地服务：{exc}")
        print("  若端口被占用：编辑 .env 修改 PORT 后重试；或先关闭占用该端口的进程。")
        raise SystemExit(1) from exc

def _suggest_command(cmd: str) -> str:
    """未知命令相近建议：按相似度从全部命令里挑最接近的（参照 CLI 指南）。"""
    import difflib

    # 全部可用命令清单（含动态缓存渠道命令，便于拼写错误时给出建议）
    from app import cache_store

    known = [
        "/exit", "/quit", "/clear", "/help", "/live",
        "/cache_search", "/cache_refresh",
        "/llm", "/research", "/cron", "/course",
    ] + [f"/cache_{ch}" for ch in cache_store.CHANNELS]
    matches = difflib.get_close_matches(cmd, known, n=1, cutoff=0.45)
    return matches[0] if matches else ""


# ===== CLI 显示层：清屏 + 彩色前缀（要求） =====
# 说明：颜色/清屏仅作用于 CLI 终端显示层；Web/API 走 /api/commands 返回原始文本，
# 不受影响。内容层仍保留 [信息]/[错误] 等文本标记，显示层做前缀着色映射。

# ANSI 颜色码（终端支持 VT 时生效，不支持则保持无前缀色文本）
_COLOR = {
    "INFO": "\x1b[34m",   # 蓝色：程序提示
    "ERROR": "\x1b[31m",  # 红色：错误
    "WARN": "\x1b[33m",   # 黄色：警告
    "GEO": "\x1b[32m",    # 绿色：CLI 返回结果
}
_RESET = "\x1b[0m"

# 旧文本前缀 → (新标签, 颜色) 映射（依次匹配，首个命中生效）
_PREFIX_MAP = (
    ("[错误]", "ERROR"),
    ("[注意]", "WARN"),
    ("[信息]", "INFO"),
    ("[已缓存]", "GEO"),
    ("[未生成]", "GEO"),
    ("[已登录]", "GEO"),
)


def _enable_ansi() -> None:
    """显式启用 Windows 控制台 ANSI/VT 支持（Win10+ conhost 默认开启，显式开启更稳）。

    原理：调用 kernel32.SetConsoleMode 设置 ENABLE_VIRTUAL_TERMINAL_PROCESSING，
    让 print 输出的 \x1b[...m 颜色序列被正确渲染；老终端不支持时静默忽略
    （退化为无颜色文本，不影响功能）。
    """
    import os

    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:  # noqa: BLE001 终端不支持 ANSI 时退化为无色文本
        pass


def _paint(text: str) -> str:
    """把 CLI 输出文本前缀着色：信息→蓝[INFO]、错误→红[ERROR]、
    警告→黄[WARN]、结果/状态行→绿[GEO]。

    规则：逐行处理——行首命中 _PREFIX_MAP 的前缀则换成彩色新标签；
    无前缀的非空行视为"CLI 返回结果"，加绿色 [GEO] 前缀；若该行含 `(!)`
    （教务成绩不及格标记，见 session_connector._humanize_session_response），
    整行以红色渲染并保留 [GEO] 前缀，突出不合格成绩；空行原样保留。
    仅用于终端 print，不改变内容层文本。
    """
    if not text:
        return text
    lines = []
    for line in text.split("\n"):
        mapped = None
        for old, label in _PREFIX_MAP:
            if line.startswith(old):
                mapped = f"{_COLOR[label]}[{label}]{_RESET}" + line[len(old):]
                break
        if mapped is not None:
            lines.append(mapped)
        elif line.strip():
            if "(!)" in line:
                # 不及格成绩标记：整行红色高亮（保留文本，Web 端仍可见 (!)）
                lines.append(f"{_COLOR['ERROR']}[GEO]{_RESET} " + line)
            else:
                lines.append(f"{_COLOR['GEO']}[GEO]{_RESET} " + line)
        else:
            lines.append(line)
    return "\n".join(lines)


def _clear_screen() -> None:
    """清空终端历史（要求：新输入后清理旧输出，保留面板干净）。"""
    import os

    try:
        if os.name == "nt":
            os.system("cls")
        else:
            print("\x1b[2J\x1b[H", end="", flush=True)
    except Exception:  # noqa: BLE001 清屏失败不影响使用
        pass



def _first_run_guide() -> None:
    """首次访问强制性引导：第一次进入 chat 时展示，需按回车确认后开始。

    通过 data/.cli_welcome 标志文件判断是否首次（data/ 不入库）。
    引导内容覆盖：两种用法、登录态（session-login）、配置（/configure）。
    """
    from pathlib import Path

    flag = Path("data/.cli_welcome")
    if flag.exists():
        return  # 已看过引导
    print("=" * 56)
    print("  【首次使用引导 · 30 秒上手】")
    print("-" * 56)
    print("  ① 直接输入问题        → LLM 回答（自动调用官网/门户/贴吧/知乎/B站等）")
    print("  ② /cache_search       → 不调 LLM 看缓存渠道；/cache_ifmweb 看门户功能")
    print("     /back 回看历史；/live 实时查询；/course 查课表")
    print("  ③ /live_nav <关键词>  → 官网机构导航：查学院/办公室官网入口（如 /live_nav 自动化）")
    print("  ④ /research <主题>    → 综合调研（会调用 LLM）")
    print("-" * 56)
    print("  门户/教务等个人数据功能需先登录（只登录一次，之后自动复用）：")
    print("    在 chat 内输入 /login（或退出后在终端执行 geopractor session-login）→ 浏览器登录地大认证门户")
    print("  配置模型：/configure（当前会话）或 chat 外 geopractor configure")
    print("=" * 56)
    try:
        input("（按回车键开始使用…）")
    except EOFError:  # 管道/非交互场景不阻塞
        pass
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("seen", encoding="utf-8")
    except OSError:  # data/ 不可写时不影响使用
        pass


def chat() -> None:
    """交互式对话：自然语言走 LLM；/xxx 走命令系统（缓存/定时/调研等）。

    交互历史机制（要求）：
        - 历史只"隐藏"不删除：每轮交互（输入+输出）都追加进 _history 栈，
          清屏后仍可通过 /back 逐轮回看、/forward 回到最新；
        - /back 不再承担缓存层级回退（该职责移至 /.. 与 /返回，见 commands.py）。
    """
    _enable_ansi()  # 启用终端颜色（不支持时静默退化，不影响功能）
    settings = get_settings()
    _warn_data_dir(settings)  # 防 DATA_DIR 系统变量污染（发现）
    if not settings.is_configured:
        # 未配置时的 3 步引导（对标 AI Onboarding：让用户尽快跑到"第一次可用"）
        print("=" * 56)
        print("  [配置] 尚未配置模型服务，按下面 3 步即可开始：")
        print("  " + "-" * 52)
        print("  ① geopractor configure   交互式填写 Base URL / 模型 / 密钥")
        print("  ② geopractor status      确认「密钥：已配置」")
        print("  ③ geopractor chat        重新进入对话")
        print("  " + "-" * 52)
        print("  说明：密钥加密存本机；也可编辑 .env（参考 .env.example）")
        print("=" * 56)
        return
    agent = _make_agent(settings)
    # 命令系统：构建上下文 + 启动定时任务（退出时停止）
    from app.commands import CmdContext
    import app.cron as cron

    ctx = CmdContext(agent=agent, settings=settings)

    # ---- 交互历史栈（隐藏不销毁，供 /back 回看）----
    # 每轮：{"input": 用户输入行, "outputs": [程序输出原文列表]}；索引从 0 开始
    _history: list[dict] = []
    _view_idx = 0  # 当前查看位置：len(_history)=最新；<len=回看历史某轮

    def _render_round(idx: int) -> None:
        """渲染并打印历史第 idx 轮内容（直接 print，不回写历史栈）。

        回看展示：分隔线 + 该轮用户输入 + 当时全部输出（着色），便于用户
        追溯上一轮交互；若 idx == len(_history) 表示已回到最新一屏。
        """
        if idx >= len(_history):
            _clear_screen()
            print("[信息] 已回到最新交互（当前屏幕）")
            return
        round_ = _history[idx]
        _clear_screen()
        print(f"— 回看历史 · 第 {idx + 1} 轮 —（/back 向前一层，/forward 向后一层，/new 回到最新）")
        print("你 > " + round_["input"])
        for text in round_["outputs"]:
            print(_paint(text))

    # CLI 显示层：命令输出经 _paint 着色（信息蓝/错误红/警告黄/结果绿）；
    # 同时把原文追加进当前轮历史（供 /back 回看）；Web/API 端用独立收集器
    _cur_outputs: list[str] = []

    def _out_capture(text: str) -> None:
        """CLI 输出钩子：记录原文到当前交互轮 + 着色打印。"""
        _cur_outputs.append(text)
        print(_paint(text))

    ctx.out = _out_capture
    cron.start()
    # 欢迎语：能力概览 + 示例问题（参照同行产品"上手即用"与 Aha Moment 引导）
    # 注意：不使用 emoji（Windows GBK 控制台无法输出，会 UnicodeEncodeError）
    print("=" * 56)
    print("  == 行至大地·Geopractor 已就绪 ==")
    print(f"  模型：{settings.llm_model}")
    print("-" * 56)
    print("  试试问：本周学校有什么通知？/ 地大宿舍条件怎么样？")
    print("  直接输入文字 → 自然语言问 LLM（自动调用各渠道工具）")
    print("  /cache_search  → 不调 LLM 直查缓存渠道（如信息门户/贴吧/知乎…）")
    print("  /cache_ifmweb  → 看信息门户功能（如勤工助学）并直达办理网址")
    print("  /live_nav 自动化 → 官网机构导航：查学院/办公室官网入口（/live_nav 不带词=全部）")
    print("  /back          → 回看历史（/forward 回到最新）；/course → 查课表；/live → 实时查询")
    print("  /login         → 浏览器登录门户/教务（之后自动复用会话）")
    print("  /research      → LLM 综合调研；/configure → 配置模型；/llm 切 LLM")
    print("  输入 /help 看完整命令，/help <主题> 看示例；/exit 退出")
    print("=" * 56)
    # 首次访问强制性引导（对标 AI Onboarding）：仅第一次进入 chat 展示，需按回车确认
    _first_run_guide()
    try:
        while True:
            line = input("你 > ").strip()
            if not line:
                continue
            # 新输入后清空屏幕（隐藏历史；历史仍保留在 _history 栈，/back 可回看）
            _clear_screen()
            # 历史回看命令：不写入历史栈（回看本身不算一轮交互）
            # 游标语义：_view_idx == len(_history) 表示停在最新屏；小于则正在回看该轮
            #  调整：/forward 改为"向后一层"（逐层向最新走），
            # 直接回最新改由 /new 承担；/back 仍为"向前一层"（向历史深处走）。
            if line == "/back":
                if not _history:
                    print("[信息] 暂无历史交互（这是第一轮）")
                elif _view_idx == 0:
                    # 已回退到最早一轮：保持显示并提示
                    _render_round(0)
                    print("[信息] 已是最早一轮历史")
                else:
                    _view_idx -= 1
                    _render_round(_view_idx)
                continue
            if line == "/forward":
                # 向后一层：向最新方向前进一轮（已在最新则清屏提示）
                if _view_idx >= len(_history):
                    _render_round(len(_history))
                else:
                    _view_idx += 1
                    _render_round(_view_idx)
                continue
            if line == "/new":
                # 直接回到最新楼层（等价清屏回到当前交互）
                _view_idx = len(_history)
                _render_round(len(_history))
                continue
            # 新一轮交互开始：重置输出收集器（游标先指到最新，回看状态解除）
            _cur_outputs = []
            if line.startswith("/"):
                # ---- 命令系统分发 ----
                cmd, _, arg = line.partition(" ")
                cmd = cmd.lower()
                if cmd in ("/exit", "/quit"):
                    break
                if cmd == "/clear":
                    # 隐藏当前屏幕 + 清空 LLM 会话上下文；交互历史栈保留（可 /back 回看）
                    agent._memory.clear()  # noqa: SLF001 会话清理（内存对象内部使用）
                    print("[信息] 屏幕已隐藏、LLM 会话上下文已清空；交互历史保留，输入 /back 可回看。")
                    _history.append({"input": line, "outputs": list(_cur_outputs)})
                    _view_idx = len(_history)
                    continue
                # 统一命令分发（/cache_* /live_* /help /llm /research /cron /course 等）
                from app.commands import dispatch_command

                if dispatch_command(ctx, cmd, arg):
                    _history.append({"input": line, "outputs": list(_cur_outputs)})
                    _view_idx = len(_history)
                    continue
                # 未知命令：给出相近命令建议（参照 CLI 指南"上下文感知建议"）
                hint = _suggest_command(cmd)
                if hint and hint != cmd:
                    print(f"未知命令：{cmd}。你是不是想输入：{hint}？")
                else:
                    print(f"未知命令：{cmd}（输入 /help 查看全部命令）")
                _history.append({"input": line, "outputs": list(_cur_outputs)})
                _view_idx = len(_history)
                continue
            # ---- 自然语言 → LLM ----
            try:
                reply = agent.chat(line)
            except LLMError as exc:
                reply = f"[错误] {exc}"
            print("Geopractor > " + _paint(reply))
            _history.append({"input": line, "outputs": [reply]})
            _view_idx = len(_history)
    except (KeyboardInterrupt, EOFError):
        print("\n再见！")
    finally:
        cron.stop()


def session_login() -> None:
    """打开浏览器登录教务系统，登录态持久保存（agent 自动复用）。

    说明：正方教务会话短效（约 20~60 分钟），本命令用 Playwright 持久化 profile
    保存登录态；之后 agent 每次请求会自动复用并保活，无需手动导出 Cookie。
    """
    from connectors.pw_session import login_jwgl

    ok = login_jwgl()
    if not ok:
        raise SystemExit(1)


# ===== 命令注册 =====
# 统一注册命令到 Typer；当 typer 不可用时这些调用不执行（app 为 None）
if app is not None:
    app.command(name="configure")(configure)
    app.command(name="chat")(chat)
    app.command(name="index")(index)
    app.command(name="status")(status)
    app.command(name="serve")(serve)
    app.command(name="session-login")(session_login)


def main() -> None:
    """统一入口：优先使用 typer；不可用时回退到手动分发。"""
    if app is not None:
        app()  # type: ignore[misc]
    else:
        _manual_dispatch()


def _manual_dispatch() -> None:
    """typer 不可用时的兜底命令分发（保持功能可用）。"""
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    handlers = {
        "configure": configure,
        "chat": chat,
        "index": index,
        "status": status,
        "serve": serve,
        "session-login": session_login,
    }
    handler = handlers.get(cmd)
    if handler:
        handler()
    else:
        print("用法：geopractor <configure|chat|index|status|serve|session-login>")


if __name__ == "__main__":
    main()
