# -*- coding: utf-8 -*-
"""命令系统单元测试：缓存命令/LLM/调研/定时任务。"""

import json

from app import commands as cmd_mod
from app.commands import CmdContext, dispatch_cache
from app import cache_store


class FakeAgent:
    """不调 LLM 的假 agent（测试 /llm、/research 用）。"""

    def __init__(self):
        self.calls = []

    def chat(self, question: str) -> str:
        self.calls.append(question)
        return f"FAKE_REPLY:{question[:20]}"


class FakeSettings:
    is_configured = True


def _make_ctx(agent=None) -> tuple[CmdContext, list[str]]:
    out: list[str] = []
    ctx = CmdContext(agent=agent or FakeAgent(), settings=FakeSettings(), out=out.append)
    return ctx, out


def test_cache_search_lists_channels(monkeypatch):
    """/cache_search 应列出全部渠道与命令入口。"""
    ctx, out = _make_ctx()
    monkeypatch.setattr(cache_store, "list_cached", lambda: [
        {"channel": "ifmweb", "name": "信息门户", "desc": "d", "cached": False,
         "updated": None, "error": None, "section_count": 0, "command": "/cache_ifmweb"},
    ])
    dispatch_cache(ctx, "search")
    text = "\n".join(out)
    assert "信息门户" in text
    assert "/cache_ifmweb" in text


def test_cache_channel_list_sections(monkeypatch):
    """/cache_<渠道> 应列出功能（含序号与直达命令）。"""
    ctx, out = _make_ctx()
    monkeypatch.setattr(cache_store, "CHANNELS", {"ifmweb": {"name": "信息门户", "desc": ""}})
    monkeypatch.setattr(
        cache_store, "get_or_refresh",
        lambda ch, force=False: {"sections": [
            {"key": "pwps", "name": "勤工助学", "url": "u", "desc": ""},
            {"key": "schroll", "name": "学籍查询", "url": "u2", "desc": ""},
        ]},
    )
    dispatch_cache(ctx, "ifmweb")
    text = "\n".join(out)
    assert "勤工助学" in text
    assert "/cache_ifmweb 1" in text
    assert "/cache_ifmweb_pwps" in text


def test_cache_channel_locate_and_open(monkeypatch):
    """定位功能应设置当前缓存路径并调用 webbrowser 打开网址。"""
    ctx, out = _make_ctx()
    opened = []
    monkeypatch.setattr(cmd_mod.webbrowser, "open", lambda u: opened.append(u))
    monkeypatch.setattr(cache_store, "CHANNELS", {"ifmweb": {"name": "信息门户", "desc": ""}})
    monkeypatch.setattr(
        cache_store, "get_or_refresh",
        lambda ch, force=False: {"sections": [
            {"key": "pwps", "name": "勤工助学", "url": "https://x/pwps", "desc": "部门：本科生院"},
        ]},
    )
    dispatch_cache(ctx, "ifmweb 勤工助学")
    text = "\n".join(out)
    assert "勤工助学" in text
    assert "https://x/pwps" in text
    assert opened == ["https://x/pwps"]
    assert ctx.current_cache_path == "/cache_ifmweb_pwps"


def test_llm_injects_cache_context(monkeypatch):
    """/llm 应携带当前缓存路径上下文。"""
    agent = FakeAgent()
    ctx, out = _make_ctx(agent)
    ctx.current_cache_path = "/cache_ifmweb_pwps"
    cmd_mod._cmd_llm(ctx, "勤工助学怎么申请")  # noqa: SLF001 测试内部处理器
    assert "勤工助学怎么申请" in agent.calls[0]
    assert "/cache_ifmweb_pwps" in agent.calls[0]
    assert "FAKE_REPLY" in "\n".join(out)


def test_research_builds_prompt(monkeypatch):
    """/research 应构造多来源调研提示词。"""
    agent = FakeAgent()
    ctx, out = _make_ctx(agent)
    cmd_mod._cmd_research(ctx, "地大宿舍条件")  # noqa: SLF001
    assert "地大宿舍条件" in agent.calls[0]
    assert "调研" in agent.calls[0]


def test_cron_add_list_remove(tmp_path, monkeypatch):
    """定时任务 add/list/remove 应可用且持久化。"""
    import app.cron as cron

    monkeypatch.setattr(cron, "CRON_FILE", tmp_path / "cron.json")
    monkeypatch.setattr(cron, "CHANNELS", {"ifmweb": {}})
    task_id = cron.add_task("ifmweb", 30)
    tasks = cron.list_tasks()
    assert len(tasks) == 1 and tasks[0]["channel"] == "ifmweb"
    assert cron.remove_task(task_id) is True
    assert cron.list_tasks() == []


def test_cron_add_unknown_channel(tmp_path, monkeypatch):
    """添加未知渠道任务应报错。"""
    import app.cron as cron

    monkeypatch.setattr(cron, "CRON_FILE", tmp_path / "cron.json")
    monkeypatch.setattr(cron, "CHANNELS", {"ifmweb": {}})
    try:
        cron.add_task("bad_ch", 10)
        assert False, "应抛出 ValueError"
    except ValueError:
        pass


def test_help_overview_and_topics(monkeypatch):
    """/help 应输出总览；/help <主题> 应输出详情与示例；未知主题给提示。"""
    ctx, out = _make_ctx()
    cmd_mod._cmd_help(ctx, "")  # 总览
    text = "\n".join(out)
    assert "/cache_search" in text
    assert "/help <主题>" in text
    # 分级帮助：/help cache 应含示例与直达命令
    out.clear()
    cmd_mod._cmd_help(ctx, "cache")
    text = "\n".join(out)
    assert "勤工助学" in text
    assert "/cache_ifmweb_pwps" in text
    assert "示例" in text
    # /help api 应列出联动端点
    out.clear()
    cmd_mod._cmd_help(ctx, "api")
    assert "/api/commands" in "\n".join(out)
    # 未知主题应提示可用主题
    out.clear()
    cmd_mod._cmd_help(ctx, "bad_topic")
    assert "没有「bad_topic」主题的帮助" in "\n".join(out)


def test_course_command(monkeypatch):
    """/course 应复用 cug_course 连接器查询课表；未配置时给可读提示。"""
    from connectors import session_connector

    ctx, out = _make_ctx()
    # 未配置连接器 → 可读提示
    monkeypatch.setattr(session_connector, "load_session_connectors_from_yaml", lambda: [])
    cmd_mod._cmd_course(ctx, "")
    assert "未配置教务课表连接器" in "\n".join(out)
    # 已配置 → 调用连接器并输出结果
    class FakeConn:
        name = "cug_course"

        def invoke(self, question):
            return f"FAKE_COURSE:{question}"

    monkeypatch.setattr(
        session_connector, "load_session_connectors_from_yaml", lambda: [FakeConn()]
    )
    out.clear()
    cmd_mod._cmd_course(ctx, "上学期")
    text = "\n".join(out)
    assert "FAKE_COURSE:上学期" in text


def test_live_list_and_execute(monkeypatch):
    """/live 应列出全部实时命令；/live_<命令> 应调用对应连接器；未知命令给提示。"""
    ctx, out = _make_ctx()
    # /live（无参数）列出全部实时命令
    cmd_mod.dispatch_live(ctx, "")
    text = "\n".join(out)
    assert "/live_news" in text
    assert "/live_grade" in text
    assert "/live_catalog" in text
    # /live_news 放假 → 调用 cug_news_search
    monkeypatch.setattr(
        "connectors.cug_news_connector.cug_news_search", lambda kw="": "FAKE_NEWS:" + kw
    )
    out.clear()
    cmd_mod.dispatch_live(ctx, "news 放假")
    assert "FAKE_NEWS:放假" in "\n".join(out)
    # 未知实时命令 → 可读提示
    out.clear()
    cmd_mod.dispatch_live(ctx, "bad_cmd xxx")
    assert "未知实时命令" in "\n".join(out)


def test_live_grade_missing_connector(monkeypatch):
    """/live_grade 未配置教务连接器时应给可读提示。"""
    from connectors import session_connector

    monkeypatch.setattr(session_connector, "load_session_connectors_from_yaml", lambda: [])
    ctx, out = _make_ctx()
    cmd_mod.dispatch_live(ctx, "grade 2025 12")
    assert "未配置教务连接器 cug_grade" in "\n".join(out)


def test_dispatch_command_registered_commands(monkeypatch):
    """统一分发：/help /llm 等注册命令应命中（回归：此前带斜杠查表落空误入未知命令）。"""
    ctx, out = _make_ctx()
    # /help 应命中并输出命令总览
    assert cmd_mod.dispatch_command(ctx, "/help") is True
    assert "/cache_search" in "\n".join(out)
    # /llm 应命中并携带参数（FakeAgent 记录问题）
    agent = FakeAgent()
    ctx2, out2 = _make_ctx(agent)
    assert cmd_mod.dispatch_command(ctx2, "/llm", "你好") is True
    assert "你好" in agent.calls[0]
    assert "FAKE_REPLY" in "\n".join(out2)
    # /cache_search 命中
    out.clear()
    assert cmd_mod.dispatch_command(ctx, "/cache_search") is True
    assert "当前缓存渠道" in "\n".join(out)
    # 未知命令返回 False
    assert cmd_mod.dispatch_command(ctx, "/bad_cmd") is False


def test_back_command_navigation(monkeypatch):
    """缓存层级回退改用 /.. 与 /返回（/back 已移至 CLI 层做历史回看，见 cli.chat）。"""
    ctx, out = _make_ctx()
    # 模拟已进入功能层（如 /cache_ifmweb 勤工助学）
    ctx.current_cache_path = "/cache_ifmweb_pwps"
    monkeypatch.setattr(cache_store, "CHANNELS", {"ifmweb": {"name": "信息门户", "desc": ""}})
    monkeypatch.setattr(
        cache_store, "get_or_refresh",
        lambda ch, force=False: {"sections": [{"key": "pwps", "name": "勤工助学", "url": "u", "desc": ""}]},
    )
    # 功能层 → 渠道列表层
    assert cmd_mod.dispatch_command(ctx, "/..") is True
    assert ctx.current_cache_path == "/cache_ifmweb"
    # 渠道列表层 → 渠道总览层
    out.clear()
    assert cmd_mod.dispatch_command(ctx, "/..") is True
    assert ctx.current_cache_path == ""
    assert "渠道总览" in "\n".join(out)
    # 最顶层再 /.. → 提示
    out.clear()
    assert cmd_mod.dispatch_command(ctx, "/..") is True
    assert "已在最顶层" in "\n".join(out)
    # 中文别名 /返回 同样生效
    ctx.current_cache_path = "/cache_ifmweb_pwps"
    assert cmd_mod.dispatch_command(ctx, "/返回") is True
    assert ctx.current_cache_path == "/cache_ifmweb"


def test_configure_registered():
    """/configure 应已注册（chat 内可配置 LLM）。"""
    assert "configure" in cmd_mod.COMMANDS


def test_schedule_and_office_hours_registered(monkeypatch):
    """/schedule（课表预设方案）与 /office_hours（办公时间判断）应已注册并可分发。"""
    assert "schedule" in cmd_mod.COMMANDS
    assert "office_hours" in cmd_mod.COMMANDS
    # /next：自习室课表图片逐张翻看（/live_room 下载多张后 /next 打开下一张）
    assert "next" in cmd_mod.COMMANDS
    # 查看分支：/schedule 输出当前方案（不写配置，纯展示）
    ctx, out = _make_ctx()
    assert cmd_mod.dispatch_command(ctx, "/schedule") is True
    assert any("时间编排配置" in line for line in out)
    # /office_hours 输出当前是否办公时间（不写配置，实时判断）
    ctx2, out2 = _make_ctx()
    assert cmd_mod.dispatch_command(ctx2, "/office_hours") is True
    assert any("当前" in line for line in out2)
    # /next 无图片时给出引导提示（不崩溃）
    cmd_mod._room_images[:] = []
    ctx3, out3 = _make_ctx()
    assert cmd_mod.dispatch_command(ctx3, "/next") is True
    assert any("live_room" in line for line in out3)


def test_cli_paint_prefix_colors():
    """CLI 显示层 _paint：信息→蓝[INFO]、错误→红[ERROR]、警告→黄[WARN]、结果行→绿[GEO]。"""
    from app import cli

    # 前缀映射：颜色码存在且标签替换正确
    info = cli._paint("[信息] 正在查询…")
    assert "[INFO]" in info and "\x1b[34m" in info and "[信息]" not in info
    err = cli._paint("[错误] 查询失败")
    assert "[ERROR]" in err and "\x1b[31m" in err
    warn = cli._paint("[注意] 网络异常")
    assert "[WARN]" in warn and "\x1b[33m" in warn
    # 无前缀的结果行 → 绿色 [GEO]
    res = cli._paint("1. 自动化专业介绍\n2. 转专业申请")
    assert "[GEO]" in res and res.count("[GEO]") == 2
    # 不及格成绩标记 (!) → 整行红色（要求不合格课程红色标记）
    fail = cli._paint("1. 课程=高等数学 成绩=45(!)")
    assert "\x1b[31m" in fail
    # 空行保留、空文本原样
    assert cli._paint("") == ""
    blank = cli._paint("[信息] a\n\nb")
    assert "\n\n" in blank


def test_login_registered():
    """/login 与别名 /session-login 应已注册（chat 内可完成门户/教务登录）。"""
    # 要求把 geopractor session-login 集成进 CLI chat：/login 为主名，
    # /session-login 为别名（与原顶层子命令同名，降低迁移成本）
    assert "login" in cmd_mod.COMMANDS
    assert "session-login" in cmd_mod.COMMANDS
    assert cmd_mod.COMMANDS["login"] is cmd_mod.COMMANDS["session-login"]
