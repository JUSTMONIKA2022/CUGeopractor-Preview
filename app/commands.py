# -*- coding: utf-8 -*-
"""CLI 命令系统（CLI 大改核心）。

设计：
    - 命令注册表 COMMANDS：命令名 -> handler(context, arg)；
    - 前缀分发：/cache_* 系列为动态命令（渠道/功能层级），统一走 cache 分发器；
    - 命令只读/展示/打开网址，不调 LLM（除 /llm、/research 显式使用 LLM）；
    - context 携带 agent/settings/当前缓存路径，供 /llm 注入上下文；
    - 定时任务（/cron）与综合调研（/research）为扩展能力。

命令一览：
    /cache_search                  列出全部缓存渠道
    /cache_refresh [channel]       刷新指定/全部渠道缓存
    /cache_<channel>               列出该渠道功能（sections）
    /cache_<channel> <关键词|序号>  定位功能：显示详情，带网址则自动打开浏览器
    /llm <问题>                    任意层级切换到 LLM 调用（携带当前缓存路径上下文）
    /research <主题>               综合调研（agent 多来源自主搜集）
    /cron list|add|remove          定时任务管理
    /help                          帮助
"""

from __future__ import annotations

import webbrowser
from dataclasses import dataclass, field

# 缓存存储层以模块方式访问（而非 from-import 绑定），便于测试 monkeypatch 注入
from app import cache_store
from app.agent.core import Agent
from app.config import Settings

# 命令注册表：命令名 -> 处理器（context, arg）
COMMANDS: dict[str, callable] = {}


def register(name: str, handler: callable) -> None:
    """注册命令（模块导入时调用）。"""
    COMMANDS[name] = handler


@dataclass
class CmdContext:
    """命令执行上下文（贯穿一次 chat 会话）。"""

    agent: Agent
    settings: Settings
    # 最近访问的缓存路径（如 /cache_ifmweb_pwps），供 /llm 注入上下文
    current_cache_path: str = field(default="")
    # 交互模式下的输出函数（测试可注入收集器）
    out: callable = field(default=print)


# ===== 工具函数 =====

def _channel_from(path: str) -> str | None:
    """从缓存路径中提取渠道名（/cache_ifmweb_pwps -> ifmweb）。"""
    rest = path
    for ch in cache_store.CHANNELS:
        if rest == ch or rest.startswith(ch + "_"):
            return ch
    return None


def _format_time(ts: int | None) -> str:
    """时间戳转可读时间。"""
    import datetime

    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ===== /cache_* 缓存命令 =====

def _cmd_cache_search(ctx: CmdContext, arg: str) -> None:
    """列出全部缓存渠道与状态。"""
    lines = ["当前缓存渠道（/cache_<渠道> 查看功能）："]
    for entry in cache_store.list_cached():
        # 状态标记用文本而非 emoji（Windows GBK 控制台无法输出 ✅/⏳，会 UnicodeEncodeError）
        status = "[已缓存]" if entry["cached"] else "[未生成]（首次访问自动生成）"
        updated = f"｜更新于 {_format_time(entry['updated'])}" if entry.get("updated") else ""
        err = f"｜[注意] {entry['error']}" if entry.get("error") else ""
        lines.append(
            f"  {entry['name']}（{entry['command']}）：{status}{updated}{err}\n"
            f"     {entry['desc']}"
        )
    lines.append("提示：/cache_refresh [渠道] 手动刷新；/llm <问题> 切换 LLM 查询。")
    ctx.out("\n".join(lines))


def _cmd_cache_refresh(ctx: CmdContext, arg: str) -> None:
    """刷新指定/全部渠道缓存。"""
    arg = arg.strip()
    targets = [arg] if arg else list(cache_store.CHANNELS)
    for ch in targets:
        if ch not in cache_store.CHANNELS:
            ctx.out(f"[错误] 未知渠道：{ch}")
            continue
        data = cache_store.refresh_channel(ch)
        err = f"（{data['error']}）" if data.get("error") else ""
        ctx.out(f"[信息] 已刷新「{cache_store.CHANNELS[ch]['name']}」：{len(data['sections'])} 项功能{err}")
    if not arg:
        ctx.out("[信息] 全部渠道刷新完成")


def _cmd_cache_channel(ctx: CmdContext, channel: str, arg: str = "") -> None:
    """渠道级命令：列出功能 / 定位具体功能并打开网址。"""
    if channel not in cache_store.CHANNELS:
        ctx.out(f"[错误] 未知渠道：{channel}（可用 /cache_search 查看）")
        return
    data = cache_store.get_or_refresh(channel)
    if data.get("error"):
        ctx.out(f"[警告] {data['error']}")
    sections = data.get("sections") or []
    if not sections:
        ctx.out(f"[信息] 渠道「{cache_store.CHANNELS[channel]['name']}」暂无可用功能")
        return

    # 无参数：列出全部功能
    arg = arg.strip()
    if not arg:
        # 记录当前层级（供 /.. 返回用）：渠道列表层
        ctx.current_cache_path = f"/cache_{channel}"
        lines = [f"「{cache_store.CHANNELS[channel]['name']}」当前缓存的功能（{len(sections)} 项）："]
        for idx, sec in enumerate(sections, 1):
            lines.append(f"  [{idx}] {sec['name']}（/cache_{channel} {idx} 或 /cache_{channel}_{sec['key']}）")
        lines.append("提示：输入序号/关键词定位功能并自动打开网址；/llm <问题> 切换 LLM。")
        ctx.out("\n".join(lines))
        return

    # 定位 section：支持序号 / 关键词（名称或 key）
    section = _locate_section(sections, arg)
    if section is None:
        ctx.out(f"[信息] 未在「{cache_store.CHANNELS[channel]['name']}」中找到「{arg}」，可尝试："
                + "、".join(s["name"] for s in sections[:10]))
        return
    ctx.current_cache_path = f"/cache_{channel}_{section['key']}"
    _show_section(ctx, section)


def _locate_section(sections: list[dict], arg: str) -> dict | None:
    """在 sections 中定位：优先序号，其次 key，最后名称包含匹配。"""
    if arg.isdigit():
        idx = int(arg)
        if 1 <= idx <= len(sections):
            return sections[idx - 1]
        return None
    for sec in sections:
        if sec.get("key") == arg:
            return sec
    for sec in sections:
        if arg in sec.get("name", ""):
            return sec
    return None


def _show_section(ctx: CmdContext, section: dict) -> None:
    """展示一个功能：输出详情；带网址则自动打开浏览器。"""
    name, url, desc = section.get("name"), section.get("url", ""), section.get("desc", "")
    items = section.get("items") or []
    lines = [f"【{name}】"]
    if desc:
        lines.append(f"  说明：{desc}")
    if url:
        lines.append(f"  网址：{url}")
        lines.append("[信息] 正在为你打开网址…（如需自己办，请在弹出的浏览器中操作）")
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001 打开失败不影响输出
            lines.append(f"[警告] 自动打开失败：{exc}，可手动复制网址")
    elif items:
        lines.append(f"  共 {len(items)} 条：")
        for it in items[:10]:
            line = f"    - {it.get('name')}"
            if it.get("url"):
                line += f"\n      链接：{it['url']}"
            lines.append(line)
    else:
        lines.append("  （该功能无外部链接，详情如上）")
    ctx.out("\n".join(lines))


def dispatch_cache(ctx: CmdContext, arg: str) -> bool:
    """分发 /cache_* 系列命令。返回 True=已处理。

    arg 形式：search / refresh / refresh <ch> / <channel> / <channel> <关键词> /
              <channel>_<key>（直达功能）
    """
    arg = arg.strip()
    # /cache_search
    if arg == "search":
        _cmd_cache_search(ctx, "")
        return True
    # /cache_refresh [channel]
    if arg == "refresh" or arg.startswith("refresh "):
        _cmd_cache_refresh(ctx, arg[len("refresh"):].strip())
        return True
    # /cache_<channel>[ _<key>| <关键词>]
    for ch in cache_store.CHANNELS:
        if arg == ch:
            _cmd_cache_channel(ctx, ch)
            return True
        if arg.startswith(ch + "_"):
            _cmd_cache_channel(ctx, ch, arg[len(ch) + 1:])
            return True
        if arg.startswith(ch + " "):
            _cmd_cache_channel(ctx, ch, arg[len(ch) + 1:])
            return True
    ctx.out(f"[错误] 无法解析缓存命令：/cache_{arg}（/cache_search 查看渠道）")
    return True


# ===== /llm 混合模式 =====

def _cmd_llm(ctx: CmdContext, arg: str) -> None:
    """切换/使用 LLM：/llm <问题>；携带当前缓存路径上下文。"""
    if not ctx.settings.is_configured:
        ctx.out("[错误] 尚未配置模型服务，请先运行 geopractor configure")
        return
    question = arg.strip()
    if not question:
        ctx.out("[信息] 用法：/llm <问题>（如 /llm 勤工助学怎么申请）")
        return
    if ctx.current_cache_path:
        # 注入当前缓存路径上下文，让 LLM 结合该层级继续服务
        question = f"{question}\n（附注：用户当前正在查看缓存路径 {ctx.current_cache_path}）"
    ctx.out("[信息] 正在调用 LLM…")
    try:
        reply = ctx.agent.chat(question)
    except Exception as exc:  # noqa: BLE001 网络/额度等异常给出可读提示
        ctx.out(f"[错误] LLM 调用失败：{type(exc).__name__}: {exc}")
        return
    ctx.out(f"Geopractor > {reply}")


# ===== /research 综合调研 =====

def _cmd_research(ctx: CmdContext, arg: str) -> None:
    """综合调研：agent 多来源自主搜集（官网/门户/贴吧/知乎/B站等交叉验证）。"""
    if not ctx.settings.is_configured:
        ctx.out("[错误] 尚未配置模型服务，请先运行 geopractor configure")
        return
    topic = arg.strip()
    if not topic:
        ctx.out("[信息] 用法：/research <主题>（如 /research 地大宿舍条件）")
        return
    ctx.out("[信息] 正在调用 LLM 进行多来源综合调研（会消耗模型 token），可能耗时较长…")
    prompt = (
        f"请对「{topic}」做一次综合调研：从学校官网、信息门户、百度贴吧、知乎、B站等"
        "渠道尽可能多地搜集相关信息，交叉验证不同来源的差异，输出一份结构化调研报告"
        "（按来源分类列出，每条附标题与链接；来源冲突之处如实说明并提示以官方为准；"
        "社区来源标注『该信息来自社区，仅供参考』）。"
    )
    try:
        reply = ctx.agent.chat(prompt)
    except Exception as exc:  # noqa: BLE001
        ctx.out(f"[错误] 调研失败：{type(exc).__name__}: {exc}")
        return
    ctx.out(f"【调研报告】\n{reply}")


# ===== /cron 定时任务 =====

def _cmd_cron(ctx: CmdContext, arg: str) -> None:
    """定时任务管理：/cron list|add <渠道> <分钟> [次数]|remove <id>|stop。

    用途引导（要求"缺乏使用引导"）：定时任务 = 让后台按周期
    自动刷新渠道缓存，这样读 /cache_* 时总能看到较新数据，无需每次实时请求。
    """
    import app.cron as cron

    parts = arg.strip().split()
    if not parts or parts[0] == "list":
        tasks = cron.list_tasks()
        if not tasks:
            # 无任务时的引导：说明用途 + 示例（对标"意义不明"痛点）
            ctx.out(
                "[信息] 暂无定时任务。\n"
                "  用途：后台按周期自动刷新渠道缓存，读 /cache_* 时总是较新数据，无需每次实时请求。\n"
                "  用法：/cron add <渠道> <间隔分钟> [执行次数]\n"
                "  示例：/cron add ifmweb 30      → 每 30 分钟刷新信息门户缓存（不限次数）\n"
                "        /cron add tieba 60 5     → 每 60 分钟刷新贴吧，只执行 5 次\n"
                "  渠道：/cache_search 查看可用渠道"
            )
            return
        lines = ["定时任务列表（/cron remove <id> 删除）："]
        for t in tasks:
            status = f"｜上次 {_format_time(t['last_run'])}" if t["last_run"] else ""
            # 执行次数展示：已完成 / 第 N 次 / 不限次数
            if t["done"]:
                count = f"，已完成（共 {t['max_runs']} 次）"
            elif t["max_runs"] is not None:
                count = f"，已执行 {t['execute_count']}/{t['max_runs']} 次"
            else:
                count = f"，已执行 {t['execute_count']} 次（不限次数）"
            lines.append(f"  {t['id']}：渠道 {t['channel']}，每 {t['interval_min']} 分钟刷新{count}{status}")
        ctx.out("\n".join(lines))
        return
    action = parts[0]
    if action == "add" and len(parts) >= 3:
        # 可选第 4 个参数：执行次数（需求）
        max_runs = None
        if len(parts) >= 4:
            try:
                max_runs = int(parts[3])
            except ValueError:
                ctx.out(f"[错误] 执行次数必须是整数：{parts[3]}")
                return
        try:
            task_id = cron.add_task(parts[1], float(parts[2]), max_runs=max_runs)
        except ValueError as exc:
            ctx.out(f"[错误] {exc}")
            return
        times = f"，最多执行 {max_runs} 次" if max_runs is not None else "，不限次数"
        ctx.out(f"[信息] 已添加定时任务 {task_id}：每 {parts[2]} 分钟刷新「{parts[1]}」缓存{times}")
        return
    if action == "remove" and len(parts) >= 2:
        ok = cron.remove_task(parts[1])
        ctx.out(f"[信息] 任务 {parts[1]} 已删除" if ok else f"[错误] 未找到任务 {parts[1]}")
        return
    if action == "stop":
        cron.stop()
        ctx.out("[信息] 已停止定时任务调度（退出 CLI 后自动停止）")
        return
    ctx.out("[信息] 用法：/cron list｜/cron add <渠道> <间隔分钟> [执行次数]｜/cron remove <id>｜/cron stop")


# ===== /configure 会话内配置（多方案， 需求） =====

def _cmd_configure(ctx: CmdContext, arg: str) -> None:
    """在 chat 内管理 LLM 多方案：/configure [list|add|use|remove|show]。

    设计（多方案储存与切换）：
        - 方案非敏感字段（Base URL/模型）存 data/llm_profiles.json；
        - 密钥按方案名（scope）命名空间加密存储（secrets_<方案名>.enc），不落明文；
        - 切换方案后调用 agent.set_llm 热更新当前会话，立即生效（无需重启）。
    """
    from app.config import (
        delete_llm_profile,
        load_llm_profiles,
        save_llm_profile,
        switch_llm_profile,
    )
    from app.secrets import read_api_key, store_api_key
    from app.cli import _build_llm

    data_dir = ctx.settings.data_dir
    parts = arg.strip().split()

    def _show_list() -> None:
        """输出方案列表（当前方案标记 *）。"""
        data = load_llm_profiles(data_dir)
        lines = ["LLM 方案列表（当前：*）："]
        if data["profiles"]:
            for name, p in data["profiles"].items():
                mark = "*" if name == data["current"] else " "
                lines.append(f"  {mark} {name}：{p['model']}（{p['base_url']}）")
        # 无已存方案时展示 default（.env）占位，避免列表空白
        if not data["profiles"]:
            lines.append("  （暂无已存方案；default 使用 .env 配置）")
        lines.append("用法：/configure add <名字> 新增；/configure use <名字> 切换；/configure remove <名字> 删除")
        ctx.out("\n".join(lines))

    action = parts[0] if parts else ""
    if not action or action == "list":
        _show_list()
        return
    if action == "add":
        # 交互式录入方案（名字可带参数，如 /configure add deepseek）
        name = parts[1].strip() if len(parts) >= 2 else input("方案名（如 deepseek/qwen/local）: ").strip()
        if not name:
            ctx.out("[错误] 方案名不能为空")
            return
        try:
            base_url = input(f"方案 {name} 的 Base URL: ").strip()
            model = input(f"方案 {name} 的模型名称: ").strip()
            if not base_url or not model:
                ctx.out("[错误] Base URL 与模型名称必填")
                return
            key = input("API 密钥（可留空）: ").strip()
        except EOFError:  # 非交互场景（如 Web/测试）直接提示用法
            ctx.out("[错误] 交互式录入需要终端输入；用法：/configure add <名字>")
            return
        save_llm_profile(data_dir, name, base_url, model)
        if key:
            store_api_key(key, data_dir, scope=name)  # 密钥按方案命名空间加密存储
        # 新增即设为当前方案，热更新当前会话 LLM
        ctx.agent.set_llm(_build_llm(ctx.settings))
        ctx.out(f"[信息] 方案「{name}」已保存并设为当前（密钥已加密存储；当前会话已热切换）。")
        return
    if action == "use":
        name = parts[1].strip() if len(parts) >= 2 else ""
        if not switch_llm_profile(data_dir, name):
            ctx.out(f"[错误] 未找到方案：{name}（/configure list 查看全部）")
            return
        # 热切换：重建 LLM 客户端并注入当前会话 agent（无需重启 CLI）
        ctx.agent.set_llm(_build_llm(ctx.settings))
        ctx.out(f"[信息] 已切换到方案「{name}」，当前会话立即生效。")
        return
    if action == "remove":
        name = parts[1].strip() if len(parts) >= 2 else ""
        if not delete_llm_profile(data_dir, name):
            ctx.out(f"[错误] 未找到方案：{name}（/configure list 查看全部）")
            return
        ctx.out(f"[信息] 方案「{name}」已删除（当前方案已回退到 default/.env）。")
        return
    if action == "show":
        data = load_llm_profiles(data_dir)
        name = parts[1].strip() if len(parts) >= 2 else data["current"]
        if name == "default":
            # default 方案 = .env 配置（非存储方案）
            ctx.out(
                f"[信息] 当前方案：default（.env）\n"
                f"  Base URL：{ctx.settings.llm_base_url or '（未配置）'}\n"
                f"  模型：{ctx.settings.llm_model or '（未配置）'}\n"
                f"  密钥：{'已配置' if (ctx.settings.llm_api_key or read_api_key(data_dir)) else '未配置'}"
            )
            return
        p = data["profiles"].get(name)
        if not p:
            ctx.out(f"[错误] 未找到方案：{name}")
            return
        # 密钥仅显示是否已配置（脱敏），不输出明文
        has_key = bool(read_api_key(data_dir, scope=p.get("scope", name)))
        ctx.out(
            f"[信息] 方案「{name}」\n"
            f"  Base URL：{p['base_url']}\n"
            f"  模型：{p['model']}\n"
            f"  密钥：{'已配置（加密存储）' if has_key else '未配置'}"
        )
        return
    ctx.out("[信息] 用法：/configure（查看方案）｜/configure add <名字>｜/configure use <名字>｜/configure remove <名字>｜/configure show [名字]")


# ===== /login 会话登录（chat 内触发 session-login 流程） =====

def _cmd_login(ctx: CmdContext, arg: str) -> None:
    """在 chat 内完成门户/教务登录（等价于顶层 geopractor session-login）。

    用 Playwright 打开可见浏览器，引导用户登录统一认证门户并进入教务；
    登录态持久保存（data/browser_profile/jwgl），之后 /course、/live_* 教务类
    命令与 agent 都会自动复用该登录态——无需退出 chat 单独执行 session-login。

    注意：本命令会阻塞等待用户在浏览器中完成登录（最长约 10 分钟），
    登录完成后关闭/继续浏览器流程，命令自动返回。
    """
    from connectors.pw_session import login_jwgl

    ctx.out("[信息] 将在浏览器中打开统一认证门户登录页（等价于 geopractor session-login）…")
    ctx.out("[信息] 请在弹出的浏览器中完成登录（账号密码 + 滑块验证码）；登录完成后程序自动继续。")
    try:
        ok = login_jwgl()
    except Exception as exc:  # noqa: BLE001 登录流程异常给出可读提示
        ctx.out(f"[错误] 登录流程异常：{type(exc).__name__}: {exc}")
        return
    if not ok:
        ctx.out("[错误] 登录未完成（超时或失败），可重试 /login。")
        return
    # 登录成功：连接器每次查询均惰性重新加载（_session_invoke//course 均每次
    # load_session_connectors_from_yaml），新登录态在下次查询时自动生效，无需热刷新
    ctx.out("[已登录] 会话已持久保存；/course、/live_* 教务命令与 agent 将自动复用该登录态。")


# ===== /course 教务课表直达（不调 LLM） =====

def _cmd_course(ctx: CmdContext, arg: str) -> None:
    """查询教务课表（复用已配置的 cug_course 会话连接器，不调 LLM）。

    用法：/course [学期]，学期支持"2025-2026-2"/"2025 12"/"上学期"/"下学期"；
    省略学期时按连接器默认学期查询。学期参数由连接器内部自动替换 body 的 xnm/xqm
    （新增：解决"agent 查不了过往课表"的痛点）。
    /course next 子命令：查看下一节课（基于课表快照 + 时间编排配置，见 /schedule）。
    """
    if arg.strip().lower() == "next":
        _cmd_next_course(ctx)
        return
    from connectors.session_connector import load_session_connectors_from_yaml

    connectors = {c.name: c for c in load_session_connectors_from_yaml()}
    conn = connectors.get("cug_course")
    if conn is None:
        ctx.out("[错误] 未配置教务课表连接器（请检查 data/session_connectors.yaml 是否有 cug_course）")
        return
    question = arg.strip()
    ctx.out("[信息] 正在查询课表" + (f"（学期：{question}）" if question else "（默认学期）") + "…")
    try:
        reply = conn.invoke(question)
    except Exception as exc:  # noqa: BLE001 查询异常给出可读提示
        ctx.out(f"[错误] 课表查询失败：{type(exc).__name__}: {exc}")
        return
    ctx.out(f"【教务课表】\n{reply}")


# ===== 下一节课 / 时间编排配置（需求：结构化课表 + 时间编排） =====

def _load_course_rows() -> list[dict]:
    """读取课表快照的课程行；无快照/损坏返回空列表。"""
    from connectors.session_connector import COURSE_SNAPSHOT_FILE
    import json as _json

    if not COURSE_SNAPSHOT_FILE.exists():
        return []
    try:
        data = _json.loads(COURSE_SNAPSHOT_FILE.read_text(encoding="utf-8"))
        return data.get("rows") or []
    except Exception:  # noqa: BLE001 快照损坏按无课表处理
        return []


def _cmd_next_course(ctx: CmdContext, arg: str = "") -> None:
    """下一节课：基于课表快照 + 时间编排配置推算（wakeup 式），输出倒计时。"""
    from app import course_schedule as sched

    rows = _load_course_rows()
    if not rows:
        ctx.out(
            "[注意] 暂无课表数据。请先查询一次课表（/course 或 /live_course），"
            "系统会缓存结构化课表，之后即可用 /next_course 判断下一节课。"
        )
        return
    config = sched.load_schedule_config()
    result = sched.next_course(rows, config)
    ctx.out(sched.humanize_next_course(result, config))


# 校区名/别名 → 预设方案 key（/schedule campus 切换用；支持中文名与英文 key）
_CAMPUS_ALIASES = {
    "南望山": "nanwangshan", "南望山校区": "nanwangshan",
    "nanwangshan": "nanwangshan", "nan": "nanwangshan",
    "未来城": "weilaicheng", "未来城校区": "weilaicheng",
    "weilaicheng": "weilaicheng", "weilai": "weilaicheng",
}


def _resolve_campus(name: str) -> str | None:
    """把校区名/别名解析为方案 key；未知返回 None。"""
    return _CAMPUS_ALIASES.get(str(name).strip().lower())


def _parse_season(arg: str) -> tuple[str | None, bool]:
    """解析季节参数（/schedule set period / reset 的 [夏|冬]）。

    返回 (season_key, ok)：
        - 空参数 → (None, True)：由调用方按"当前生效季节"处理；
        - "夏"/"summer" → ("summer", True)；"冬"/"winter" → ("winter", True)；
        - 非法参数 → (None, False)：调用方报错提示。
    """
    a = str(arg).strip().lower()
    if not a:
        return None, True
    if a in ("夏", "summer"):
        return "summer", True
    if a in ("冬", "winter"):
        return "winter", True
    return None, False


def _cmd_schedule(ctx: CmdContext, arg: str) -> None:
    """时间编排配置（重构为两套预设方案：南望山夏/冬自动切换 + 未来城）。

    用法：
        /schedule                    查看当前方案（校区/季节/逐节时间表/办公时间）
        /schedule campus <校区>       切换预设方案（南望山 / 未来城）
        /schedule set first_week_monday YYYY-MM-DD   设置第一周周一（编排必需）
        /schedule set period <节次> <HH:MM-HH:MM> [夏|冬]  修改某节课时间
        /schedule reset [夏|冬]       恢复当前方案的默认时间表（不填=全部）
    说明：南望山夏/冬按日期自动切换（夏 5/1–9/30、冬 10/1–次年 4/30）；
    修改某节课时缺省作用于"当前生效季节"（未来城无季节区分）。
    """
    import re as _re

    from app import course_schedule as sched

    parts = arg.strip().split()
    action = parts[0] if parts else ""
    if action == "campus":
        # 切换预设方案（南望山/未来城），写入配置立即生效
        if len(parts) < 2:
            ctx.out("[错误] 用法：/schedule campus 南望山|未来城")
            return
        key = _resolve_campus(parts[1])
        if key is None:
            ctx.out(f"[错误] 未知校区：{parts[1]}（可用：南望山、未来城）")
            return
        config = sched.load_schedule_config()
        config["campus"] = key
        sched.save_schedule_config(config)
        plan = sched.get_active_plan(config)
        ctx.out(f"[信息] 已切换到「{plan['campus_name']}」（当前生效：{plan['season_name']}）。输入 /schedule 查看时间表")
        return
    if action == "set":
        if len(parts) < 2:
            ctx.out("[错误] 用法：/schedule set <键> <值>（first_week_monday | period）")
            return
        key = parts[1]
        if key == "first_week_monday":
            # 第一周周一（编排必需项，保留原有设置方式）
            if len(parts) < 3:
                ctx.out("[错误] 用法：/schedule set first_week_monday YYYY-MM-DD")
                return
            config = sched.load_schedule_config()
            config["first_week_monday"] = parts[2]
            sched.save_schedule_config(config)
            ctx.out(f"[信息] 已设置 first_week_monday={parts[2]}（/schedule 查看全部）")
            return
        if key == "period":
            # 修改某节课时间：/schedule set period <节次> <HH:MM-HH:MM> [夏|冬]
            if len(parts) < 4:
                ctx.out("[错误] 用法：/schedule set period <节次> <HH:MM-HH:MM> [夏|冬]（如：set period 3 10:05-10:50）")
                return
            try:
                period = int(parts[2])
            except ValueError:
                ctx.out("[错误] 节次必须是数字（如 3 表示第 3 节）")
                return
            # 时间区间兼容半角/全角连字符（"10:05-10:50" / "10:05–10:50"）
            m = _re.match(r"^(\d{1,2}:\d{2})\s*[-~—–]\s*(\d{1,2}:\d{2})$", parts[3])
            if not m:
                ctx.out("[错误] 时间格式应为 HH:MM-HH:MM（如 10:05-10:50）")
                return
            config = sched.load_schedule_config()
            plan = sched.get_active_plan(config)  # 取当前生效方案（含校区 key）
            campus = plan["campus_key"]
            season_key, ok = _parse_season(parts[4] if len(parts) >= 5 else "")
            if not ok:
                ctx.out("[错误] 季节参数只能为：夏 / 冬（南望山区分季节，未来城不区分）")
                return
            if season_key is None:
                # 未指定季节：南望山按"当前自动生效季节"，未来城固定 default
                season_key = plan["season_key"]
            # 取该校区该季节的内置预设，校验节次越界并作为覆盖的基础表
            seasons = sched.PRESET_PLANS.get(campus, {}).get("seasons") or {}
            base = seasons.get(season_key) if seasons else sched.PRESET_PLANS[campus]
            total = len(base["periods"])
            if period < 1 or period > total:
                ctx.out(f"[错误] 节次超出范围：{plan['campus_name']} 该季节共 {total} 节")
                return
            # 写入 overrides（保留既有自定义节次，只改目标节）
            overrides = config.setdefault("overrides", {})
            camp_over = overrides.setdefault(campus, {})
            season_over = camp_over.setdefault(season_key, {})
            periods = list(season_over.get("periods") or [list(t) for t in base["periods"]])
            periods[period - 1] = [m.group(1), m.group(2)]
            season_over["periods"] = periods
            sched.save_schedule_config(config)
            season_txt = sched.PRESET_PLANS[campus]["seasons"][season_key]["name"] if seasons else "教学时间"
            ctx.out(f"[信息] 已设置{plan['campus_name']}「{season_txt}」第 {period} 节为 {m.group(1)}–{m.group(2)}（/schedule 查看）")
            return
        ctx.out(f"[错误] 未知配置项：{key}（可用：first_week_monday、period）")
        return
    if action == "reset":
        # 恢复当前方案默认时间表：/schedule reset [夏|冬]（不填=全部恢复）
        config = sched.load_schedule_config()
        plan = sched.get_active_plan(config)
        campus = plan["campus_key"]
        season_key, ok = _parse_season(parts[1] if len(parts) >= 2 else "")
        if not ok:
            ctx.out("[错误] 季节参数只能为：夏 / 冬")
            return
        overrides = config.get("overrides") or {}
        if season_key is None:
            overrides.pop(campus, None)  # 清空该校区全部季节的自定义
            ctx.out(f"[信息] 已恢复「{plan['campus_name']}」全部默认时间表")
        else:
            camp_over = overrides.get(campus) or {}
            camp_over.pop(season_key, None)
            if camp_over:
                overrides[campus] = camp_over
            else:
                overrides.pop(campus, None)
            ctx.out(f"[信息] 已恢复「{plan['campus_name']}」{season_key} 默认时间表")
        config["overrides"] = overrides
        sched.save_schedule_config(config)
        return
    # 查看当前编排配置（含当前生效方案、季节、逐节时间表、办公时间）
    config = sched.load_schedule_config()
    ctx.out(sched.humanize_schedule(config))


def _cmd_office_hours(ctx: CmdContext, arg: str = "") -> None:
    """当前是否办公时间（依据当前校区方案的办公时间表；不调 LLM）。"""
    from app import course_schedule as sched

    ctx.out(sched.office_hours_report())


# ===== /live_* 实时命令（不调 LLM、不走缓存，直接调连接器实时查询） =====

def _session_invoke(name: str) -> callable:
    """返回调用教务会话连接器（name）的可调用对象（惰性加载，缺失给可读提示）。

    实时命令与 /course 共用同一会话连接器机制；问题文本会经连接器内部的
    parse_semester 自动带上学期参数（如 /live_grade 2025-2026-2）。
    """

    def call(question: str = "") -> str:
        from connectors.session_connector import load_session_connectors_from_yaml

        conns = {c.name: c for c in load_session_connectors_from_yaml()}
        conn = conns.get(name)
        if conn is None:
            return f"[错误] 未配置教务连接器 {name}（data/session_connectors.yaml 缺失）"
        return conn.invoke(question)

    return call


def _college_live(arg: str) -> str:
    """/live_college 命令的包装：第一个词为学院名，其余为过滤关键词。

    说明：_live_registry 的 kw 型命令把整个参数作为单参传给 fn；学院检索需要
    (学院名, 关键词) 两个参数，这里拆分开再调 cug_college_search。
    """
    from connectors.college_connector import cug_college_search

    parts = str(arg).strip().split(maxsplit=1)
    college = parts[0] if parts else ""
    keyword = parts[1] if len(parts) > 1 else ""
    return cug_college_search(college, keyword)


def _live_registry() -> dict:
    """实时命令注册表（惰性构建，避免模块顶层引入全部连接器，保持导入轻量）。"""
    from connectors.bilibili_connector import bilibili_search
    from connectors.cug_news_connector import cug_navigation, cug_news_search
    from connectors.portal_connector import (
        portal_finished_tasks,
        portal_my_processes,
        portal_pending_notices,
        portal_personal_info,
        portal_service_catalog,
        portal_service_items,
        portal_study_room_timetable,
        portal_todo_tasks,
    )
    from connectors.tieba_connector import tieba_search
    from connectors.xiaohongshu_connector import xhs_search
    from connectors.zhihu_connector import zhihu_global_search, zhihu_search

    return {
        # 公开/社区渠道实时查询（kw=关键词型：参数直接传给连接器）
        "news": {"desc": "官网通知公告/学术动态/地大要闻（如 /live_news 放假）", "fn": cug_news_search, "kw": True},
        "nav": {"desc": "官网机构导航·学院/办公室（如 /live_nav 自动化）", "fn": cug_navigation, "kw": True},
        "college": {"desc": "学院网站检索（如 /live_college 自动化 实习）", "fn": _college_live, "kw": True},
        "zhihu": {"desc": "知乎站内搜索（如 /live_zhihu 地大宿舍）", "fn": zhihu_search, "kw": True},
        "zhihu_global": {"desc": "知乎全网搜索（如 /live_zhihu_global 地大宿舍）", "fn": zhihu_global_search, "kw": True},
        "bilibili": {"desc": "B站搜索（如 /live_bilibili 地大）", "fn": bilibili_search, "kw": True},
        "tieba": {"desc": "贴吧帖子（依赖本地服务，如 /live_tieba 自动化）", "fn": tieba_search, "kw": True},
        "xhs": {"desc": "小红书（需 Cookie，如 /live_xhs 地大）", "fn": xhs_search, "kw": True},
        # 信息门户只读实时查询
        "catalog": {"desc": "门户网上厅服务目录（如 /live_catalog 勤工助学）", "fn": portal_service_catalog, "kw": True},
        "service": {"desc": "南望厅服务事项（如 /live_service 证明）", "fn": portal_service_items, "kw": True},
        "process": {"desc": "我发起的办事流程（可带数量，如 /live_process 5）", "fn": portal_my_processes, "limit": True},
        "room": {"desc": "自习室课表（如 /live_room）", "fn": portal_study_room_timetable, "limit": True},
        "todo": {"desc": "我的待办（如 /live_todo）", "fn": portal_todo_tasks, "limit": True},
        "finished": {"desc": "我的已办（如 /live_finished）", "fn": portal_finished_tasks, "limit": True},
        "notices": {"desc": "我的待阅通知（如 /live_notices）", "fn": portal_pending_notices, "limit": True},
        "profile": {"desc": "门户账户信息（如 /live_profile）", "fn": portal_personal_info},
        # 教务实时（复用会话连接器，支持按学期查询）
        "course": {"desc": "我的课表（可带学期，如 /live_course 2025-2026-2）", "fn": _session_invoke("cug_course"), "kw": True},
        "grade": {"desc": "我的成绩（可带学期，如 /live_grade 2025-2026-2）", "fn": _session_invoke("cug_grade"), "kw": True},
        "exam": {"desc": "考试安排（可带学期，如 /live_exam 上学期）", "fn": _session_invoke("cug_exam"), "kw": True},
        "student": {"desc": "学籍信息（如 /live_student）", "fn": _session_invoke("cug_student_info"), "kw": True},
        "plan": {"desc": "培养方案（如 /live_plan）", "fn": _session_invoke("cug_training_plan"), "kw": True},
    }


# /live_room 下载的自习室课表图片路径与当前查看序号（/next 逐张翻看；CLI 会话级状态，
# 不跨会话持久化）。每次 /live_room 成功下载后重置列表，/next 递增序号打开下一张。
_room_images: list[str] = []
_room_image_idx: int = 0


def _cmd_next_image(ctx: CmdContext, arg: str = "") -> None:
    """查看下一张自习室课表图片（/live_room 下载多张时逐张翻看）。

    说明：需求——自习室课表图片可能有多张，/live_room 只打开第一张；
    输入 /next 依次打开后续图片，到最后一张时给出提示（不再循环）。
    """
    global _room_image_idx
    if not _room_images:
        ctx.out("[注意] 暂无自习室课表图片。请先运行 /live_room 下载课表图片（列表会记录图片文件）。")
        return
    if _room_image_idx + 1 >= len(_room_images):
        ctx.out(f"[信息] 已是最后一张（共 {len(_room_images)} 张）。")
        return
    _room_image_idx += 1
    path = _room_images[_room_image_idx]
    try:
        import os
        os.startfile(path)  # type: ignore[attr-defined] Windows 用默认程序打开图片
    except (OSError, AttributeError):
        ctx.out(f"[错误] 无法打开图片：{path}（请到 data/exports/live_room/ 手动查看）")
        return
    ctx.out(f"[信息] 已打开第 {_room_image_idx + 1}/{len(_room_images)} 张：{path}（/next 查看下一张）")


def _cmd_live(ctx: CmdContext, name: str, arg: str, spec: dict) -> None:
    """通用实时命令处理器：按注册表调用连接器函数并输出结果。"""
    arg = arg.strip()
    # 参数解析：kw=关键词型（直接传词）；limit=数量型（可传数字，默认 10）；无参型
    if spec.get("kw"):
        # 关键词型命令无参数时传空串而非空 tuple：连接器函数的 keyword 是位置参数，
        # 传空 tuple 会抛 missing keyword TypeError（反馈 /live_news 报错）
        params: tuple = (arg,)  # 无参即 ("",)，连接器空关键词会给出友好提示/默认结果
        if not arg:
            ctx.out("[注意] 未提供关键词，将返回默认/全部结果；建议：/live_news <关键词>")
    elif spec.get("limit"):
        n = int(arg) if arg.isdigit() else 10
        params = (n,)
    else:
        params = ()
    ctx.out(f"[信息] 实时查询「{spec['desc']}」…")
    try:
        reply = spec["fn"](*params)
    except Exception as exc:  # noqa: BLE001 实时查询失败给出可读提示
        ctx.out(f"[错误] 查询失败：{type(exc).__name__}: {exc}")
        return
    # 培养方案：结果同时导出为本地文件并打开，方便查看/存档。
    #  要求"下载原文件而非 txt"——现同时导出：
    #   - 可读 txt（humanize 文本，方便阅读）
    #   - JSON 原文（连接器保存的原始响应，即服务端返回的"原文件"）
    #  实测打通课程明细：/live_plan 输出概要 + 97 门课程完整清单，
    # 而非之前只有 1 条概要（fetch_training_plan_full，见 session_connector.py）。
    if name == "plan":
        from connectors.session_connector import (
            _last_plan_detail_raw,
            fetch_training_plan_full,
        )

        reply = fetch_training_plan_full()
        # 原文件优先取课程明细原始响应（97 门课程的真身），次选概要响应
        raw = _last_plan_detail_raw or _session_last_raw("cug_training_plan")
        _export_session_result(ctx, "培养方案", reply, raw=raw)
        # 官方 PDF 引导（实测：教务 dc 导出接口学生账号无访问权限、
        # 帆软报表被学校过滤器拦截，官方 PDF 需在浏览器打开培养方案页手动打印/另存为）
        reply += (
            "\n\n[信息] 官方 PDF：教务系统对学生无直接下载接口。如需官方版式，"
            "请在浏览器打开培养方案页面后「打印 → 另存为 PDF」：\n"
            "  https://jwgl.cug.edu.cn/jwglxt/jxzxjhgl/jxzxjhck_cxJxzxjhckIndex.html?gnmkdm=N153540\n"
            "  （浏览器需已登录教务；未登录会自动跳到统一认证登录页）"
        )
    if name == "room":
        # 同步自习室课表图片列表（/next 逐张翻看）：取连接器最近一次下载的图片路径
        from connectors import portal_connector

        _room_images[:] = list(getattr(portal_connector, "_last_room_files", []))
        global _room_image_idx
        _room_image_idx = 0
        if len(_room_images) > 1:
            ctx.out(f"[信息] 已下载 {len(_room_images)} 张课表图片，输入 /next 可查看下一张。")
    ctx.out(reply)


def _session_last_raw(name: str) -> str:
    """读取指定会话连接器最近一次请求的原始响应（导出原文件用）；无则空串。"""
    from connectors.session_connector import load_session_connectors_from_yaml

    for conn in load_session_connectors_from_yaml():
        if conn.name == name:
            return getattr(conn, "last_raw", "") or ""
    return ""


def _export_session_result(ctx: CmdContext, label: str, text: str, raw: str = "") -> None:
    """把命令结果导出为本地文件（data/exports/），并尝试用系统默认程序打开。

    说明：教务类数据（如培养方案 97 门课程）输出较长，落盘便于用户查看/存档；
    有 raw（服务端原始响应）时额外保存同名 .json（即"原文件"），无则仅文本导出。
    data/ 目录不入库。
    """
    from pathlib import Path

    export_dir = Path("data/exports")
    export_dir.mkdir(parents=True, exist_ok=True)
    # 可读文本版（.txt）
    path = export_dir / f"{label}.txt"
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 导出失败不影响命令输出
        ctx.out(f"[注意] 导出 {label} 失败：{exc}")
        return
    saved = [str(path)]
    # 原文件版（.json）：服务端返回的原始响应
    if raw:
        raw_path = export_dir / f"{label}.json"
        try:
            raw_path.write_text(raw, encoding="utf-8")
            saved.append(str(raw_path))
        except OSError:  # noqa: BLE001 原文件导出失败不阻断
            pass
    ctx.out(f"[信息] {label}已导出：{saved}（正在打开可读版…）")
    try:
        import os

        os.startfile(str(path))  # type: ignore[attr-defined] Windows 用默认程序打开
    except (OSError, AttributeError):
        pass  # 打开失败不影响（用户可自行打开文件）


def list_live() -> str:
    """列出全部实时命令（供 /live 无参数时展示）。"""
    lines = ["实时命令（不调 LLM，直接实时查询数据源；/live_<命令> [参数]）："]
    for name, spec in _live_registry().items():
        lines.append(f"  /live_{name}  {spec['desc']}")
    lines.append("示例：/live_catalog 勤工助学；/live_grade 2025-2026-2；/live_news 放假")
    return "\n".join(lines)


def dispatch_live(ctx: CmdContext, arg: str) -> bool:
    """分发 /live 系列命令：/live（列出全部）| /live_<命令> [参数]。"""
    arg = arg.strip()
    if not arg:
        ctx.out(list_live())
        return True
    name, _, rest = arg.partition(" ")
    spec = _live_registry().get(name)
    if spec is None:
        ctx.out(f"[错误] 未知实时命令：/live_{name}（输入 /live 查看全部）")
        return True
    _cmd_live(ctx, name, rest, spec)
    return True


def _cmd_back(ctx: CmdContext) -> None:
    """返回上一层缓存导航：/cache_<渠道>_<功能> → /cache_<渠道> → /cache_search。"""
    path = ctx.current_cache_path
    if path.startswith("/cache_"):
        rest = path[len("/cache_"):]  # "ifmweb" 或 "ifmweb_pwps"
        channel, _, key = rest.partition("_")
        if key:
            # 功能层 → 渠道列表层
            ctx.current_cache_path = f"/cache_{channel}"
            ctx.out(f"[信息] 已返回「{cache_store.CHANNELS.get(channel, {}).get('name', channel)}」功能列表…")
            dispatch_cache(ctx, channel)
        else:
            # 渠道列表层 → 渠道总览层
            ctx.current_cache_path = ""
            ctx.out("[信息] 已返回缓存渠道总览…")
            dispatch_cache(ctx, "search")
    else:
        ctx.out("[信息] 已在最顶层（当前无缓存层级可返回）")


def dispatch_command(ctx: CmdContext, cmd: str, arg: str = "") -> bool:
    """统一命令分发入口（CLI chat 循环与 Web /api/commands 共用）。

    处理：/cache_*、/live_*、通用注册命令（/help /llm /research /cron /course 等）、
    /..（返回上一层缓存导航；/back 已移至 CLI 层做历史回看）。返回 True=已处理；False=未知命令。
    注意：COMMANDS 注册表 key 不带斜杠（如 "help"），此处统一 lstrip("/") 再查表
    （修复：此前 CLI/API 用带斜杠的 cmd 查表永远落空，导致 /help、
    /llm、/research 等全部误入"未知命令"分支——该 bug 因测试只直调函数、
    未覆盖分发路径而未暴露，本次重构为单一分发源并补端到端测试）。
    """
    cmd = cmd.strip().lower()
    arg = arg.strip()
    # 返回上一层缓存导航：/cache_ifmweb_pwps → /cache_ifmweb → /cache_search
    #  要求：/back 改为 CLI 层"回到上一次交互历史"（见 cli.chat），
    # 缓存层级回退保留给 /.. 与 /返回（避免 /back 语义分裂；Web/API 仍可用）
    if cmd in ("/..", "/返回"):
        _cmd_back(ctx)
        return True
    # 缓存命令（不调 LLM）
    if cmd == "/cache_search":
        dispatch_cache(ctx, "search")
        return True
    if cmd == "/cache_refresh":
        dispatch_cache(ctx, "refresh " + arg)
        return True
    if cmd.startswith("/cache_"):
        dispatch_cache(ctx, cmd[len("/cache_"):] + (f" {arg}" if arg else ""))
        return True
    # 实时命令（不调 LLM）
    if cmd == "/live" or cmd.startswith("/live_"):
        dispatch_live(ctx, cmd[len("/live_"):] + (f" {arg}" if arg else ""))
        return True
    # 通用注册命令（key 不带斜杠）
    handler = COMMANDS.get(cmd.lstrip("/"))
    if handler is not None:
        handler(ctx, arg)
        return True
    return False


# ===== /help =====

# 分级帮助主题：/help <主题> 查看详情与示例（参照 CLI 指南：总览 + 分级 + 示例）
_HELP_TOPICS: dict[str, str] = {
    "cache": (
        "/cache 缓存命令（不调 LLM，秒级返回）：\n"
        "  /cache_search                     列出全部渠道与缓存状态\n"
        "  /cache_<渠道>                     列出该渠道功能（如 /cache_ifmweb）\n"
        "  /cache_<渠道> <关键词|序号>        定位功能并自动打开办理网址\n"
        "  /cache_<渠道>_<key>               直达功能（如 /cache_ifmweb_pwps）\n"
        "  /cache_refresh [渠道]             强制刷新缓存（不填渠道=全部）\n\n"
        "示例：想了解信息门户能否办理勤工助学\n"
        "  /cache_search       → 看到「信息门户 /cache_ifmweb」\n"
        "  /cache_ifmweb       → 看到「勤工助学 /cache_ifmweb 1」\n"
        "  /cache_ifmweb 1     → 显示详情并自动打开办理网址（写操作由你自己办）"
    ),
    "llm": (
        "/llm <问题>：任意层级切换到 LLM 调用，自动携带当前缓存路径上下文。\n"
        "  直接输入文字（不带 /）同样走 LLM，且会按需自动调用各渠道工具。\n\n"
        "示例：/cache_ifmweb 之后输入\n"
        "  /llm 勤工助学怎么申请\n"
        "  → LLM 会结合你在看的 /cache_ifmweb_pwps 上下文回答"
    ),
    "research": (
        "/research <主题>：综合调研（**会调用 LLM**，消耗模型 token，需已配置模型）。\n"
        "  机制：LLM 收到主题后自主决定调用多个渠道工具（官网实时检索、信息门户\n"
        "  服务目录、贴吧、知乎、B站等），逐渠道搜集 → 交叉验证来源差异 → 输出\n"
        "  结构化调研报告（按来源分类、附标题与链接；来源冲突如实说明并提示以\n"
        "  官方为准；社区来源标注『该信息来自社区，仅供参考』）。\n"
        "  与 /live_* 的区别：/live 是固定命令直查某渠道；/research 由 LLM 灵活\n"
        "  组合多个渠道并生成综合分析。\n\n"
        "示例：/research 地大宿舍条件"
    ),
    "cron": (
        "/cron 定时任务：后台按周期刷新渠道缓存（配置持久化到 data/cache/cron.json）。\n"
        "  用途：自动保持 /cache_* 数据较新，无需每次实时请求。\n"
        "  /cron list                   查看当前任务（含已执行次数）\n"
        "  /cron add <渠道> <分钟> [次数]  添加任务（如 /cron add ifmweb 30；\n"
        "                                 可选次数，如 /cron add tieba 60 5 只执行 5 次）\n"
        "  /cron remove <id>            删除任务\n"
        "  /cron stop                   停止调度（退出 CLI 时自动停止）"
    ),
    "course": (
        "/course [学期]：直达查询教务课表（不调 LLM，复用登录会话）。\n"
        "  学期支持：\"2025-2026-2\"（=2025-2026学年第2学期）、\"上学期\"、\"下学期\"；省略则默认学期。\n"
        "  同理，直接问 LLM\"查询上学期课表/成绩\"也会自动带上学期参数。\n"
        "  课表检查：每次查询课表都会自动与上次缓存课表对比，换课/调课会明确提示。\n\n"
        "示例：/course 2025-2026-2    → 查询 2025-2026 学年第 2 学期课表\n"
        "      /next_course            → 判断下一节课（结构化课表 + 时间编排）\n"
        "      /schedule               → 课表预设方案配置（南望山夏/冬自动切换、未来城，可改单节时间）\n"
        "      /office_hours           → 当前是否办公时间（依据当前校区方案的办公时间表）"
    ),
    "live": (
        "/live_<命令> [参数]：实时查询（不调 LLM、不走缓存，直接调数据源）。\n"
        "  公开/社区：news 官网｜zhihu 知乎｜zhihu_global 全网｜bilibili B站｜tieba 贴吧｜xhs 小红书\n"
        "  门户只读：catalog 服务目录｜service 南望厅｜process 流程｜room 自习室｜todo 待办｜finished 已办｜notices 通知｜profile 账户\n"
        "  教务：course 课表｜grade 成绩｜exam 考试｜student 学籍｜plan 培养方案（可带学期）\n\n"
        "示例：/live_catalog 勤工助学；/live_grade 2025-2026-2；/live_news 放假"
    ),
    "api": (
        "API 联动（供其他程序/软件接入，完整协议见 docs/api-protocol.md）：\n"
        "  GET  /api/cache                     渠道缓存状态列表\n"
        "  GET  /api/cache/{channel}?refresh=  读取/强制刷新某渠道\n"
        "  POST /api/commands                 执行命令（{\"command\":\"/cache_search\"}）\n"
        "  POST /api/research                 综合调研（{\"message\":\"...\"}）\n"
        "鉴权：配置 GEOPRACTOR_API_TOKEN 后需在请求头带 Bearer Token。"
    ),
}


def _cmd_help(ctx: CmdContext, arg: str) -> None:
    """命令帮助（/help 总览；/help <主题> 详情与示例）。"""
    arg = arg.strip().lower()
    if arg:
        # 分级帮助：/help cache /help llm /help research /help cron /help api
        topic = _HELP_TOPICS.get(arg)
        if topic:
            ctx.out(topic)
            return
        ctx.out(f"[信息] 没有「{arg}」主题的帮助，可用：{'、'.join(_HELP_TOPICS)}")
        return
    # 总览（一屏可读完 + 引导分级帮助）
    ctx.out(
        "Geopractor CLI —— 行至大地·校园 Agent\n"
        "用法：直接输入文字 = 自然语言问 LLM；/命令 = 不调 LLM 直查缓存/管理\n\n"
        "【命令速查】（/help <主题> 看详情与示例）\n"
        "  /cache_search             列出全部缓存渠道与状态\n"
        "  /cache_<渠道>             查看某渠道功能（如 /cache_ifmweb）\n"
        "  /cache_<渠道> <关键词|序号>  定位功能并自动打开办理网址\n"
        "  /back                     回到上一次交互历史（/forward 向后一层，/new 回到最新）\n"
        "  /..                       返回上一层缓存导航（/cache_<渠道>_<功能> → <渠道> → 总览）\n"
        "  /cache_refresh [渠道]     手动刷新缓存\n"
        "  /llm <问题>               切到 LLM 查询（携带当前缓存路径上下文）\n"
        "  /research <主题>          综合调研（LLM 多来源交叉验证，需模型）\n"
        "  /live_<命令> [参数]        实时查询（不调 LLM，如 /live_catalog 勤工助学）\n"
        "  /course [学期]            直达查询教务课表（如 /course 上学期）\n"
        "  /next_course              下一节课（基于结构化课表 + 时间编排推算）\n"
        "  /next                     查看下一张自习室课表图片（/live_room 下载多张时翻看）\n"
        "  /schedule                 课表预设方案配置（南望山夏/冬自动切换、未来城，可改单节时间）\n"
        "  /office_hours             当前是否办公时间（依据当前校区方案办公时间表）\n"
        "  /live_nav <关键词>        官网机构导航：查学院/办公室官网（如 /live_nav 自动化）\n"
        "  /live_college <学院> [关键词]  学院网站检索：抓学院官网栏目（如 /live_college 自动化 实习）\n"
        "  /cron list|add|remove     定时任务管理（支持执行次数）\n"
        "  /configure                管理 LLM 多方案（/configure list/add/use）\n"
        "  /login                    浏览器登录门户/教务（等价 session-login）\n"
        "  /clear /exit              清空会话 / 退出\n\n"
        "【渠道】官网(ofcweb) 信息门户(ifmweb) 贴吧(tieba) 知乎(zhihu) B站(bilibili) 教务(jwgl)\n"
        "【提示】/help cache 看缓存命令示例；/help live 看实时命令列表；/help api 看程序联动接口"
    )


# ===== 注册 =====
register("cache_search", lambda ctx, arg: dispatch_cache(ctx, "search"))
register("cache_refresh", lambda ctx, arg: dispatch_cache(ctx, "refresh " + arg.strip()))
register("llm", _cmd_llm)
register("research", _cmd_research)
register("cron", _cmd_cron)
register("course", _cmd_course)
register("next_course", _cmd_next_course)
register("next", _cmd_next_image)
register("schedule", _cmd_schedule)
register("office_hours", _cmd_office_hours)
register("configure", _cmd_configure)
register("login", _cmd_login)
# 别名：与原顶层子命令 geopractor session-login 同名，降低用户迁移成本
register("session-login", _cmd_login)
register("help", _cmd_help)
