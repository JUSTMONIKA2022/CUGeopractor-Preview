# -*- coding: utf-8 -*-
"""课表结构化存储与时间编排模块（两套预设方案重构）。

背景：课表按"结构化 + 可编排"理念设计——
agent 根据「第一周周一、校区课表预设」等设定，结合当前时间判断
"下一节课是什么时候"，并在 CLI 上显示。

 重构：由"等间隔线性推算"改为"两套显式预设方案"：
    ① 南望山校区：按日期**自动切换**夏/冬两套课表（夏季 5/1–9/30、冬季 10/1–次年 4/30），
       每套 10 节课（含 30 分钟大课间、午休、晚课前间隔等真实差异）+ 办公时间；
    ② 未来城校区：无夏冬区分，单套 12 节课 + 办公时间。
    两套方案**可切换**（/schedule campus 南望山|未来城）、**可修改**
    （/schedule set period <节次> <HH:MM-HH:MM>，写入 overrides 覆盖内置预设）。

设计（模块化、低技术债）：
    - 课表行数据：来自会话连接器的课表快照（data/cache/course_snapshot.json），
      本模块只读取、不负责抓取（单一职责，避免与连接器耦合）；
    - 时间编排配置：data/schedule_config.json（可经 /schedule 命令查看/修改）；
    - 节次时间一律**查表**（periods 数组，索引+1=节次），不再线性推算——
      因为真实时间表含大课间/午休等非等间隔差异，线性公式无法表达；
    - 全部计算用"分钟"为单位（从 0 点起），避免时区/夏令时陷阱；
    - 本模块不 import 连接器/CLI，供 commands.py 调用。
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

# 时间编排配置文件（data/ 不入库）
SCHEDULE_CONFIG_FILE = Path("data/schedule_config.json")

# ---------------------------------------------------------------------------
# 内置两套预设方案（校级标准时间表）
# 结构说明：
#   periods: 每节课 (开始, 结束) 时间；periods[0] 即第 1 节，periods[n-1] 即第 n 节
#   office:  办公时间，[(上午开始,上午结束), (下午开始,下午结束)]
#   seasons: 南望山按日期自动切换的夏/冬两套（每个 season 含 months 起止月日）；
#            未来城无季节区分（seasons=None，periods/office 直接挂在方案上）
# ---------------------------------------------------------------------------
PRESET_PLANS: dict[str, dict] = {
    "nanwangshan": {
        "name": "南望山校区",
        "seasons": {
            "summer": {
                # 夏季课表：当年 5 月 1 日 ~ 9 月 30 日（按"月日"判定，跨年自动循环）
                "name": "夏季课表",
                "months": ((5, 1), (9, 30)),
                # 上午 08:00 起 4 节（第2→3节之间 30 分钟大课间）→ 午休 → 下午 14:30 起
                # 4 节 → 傍晚间隔 → 晚上 19:30 起 2 节
                "periods": [
                    ("08:00", "08:45"), ("08:50", "09:35"),   # 第 1、2 节
                    ("10:05", "10:50"), ("10:55", "11:40"),   # 第 3、4 节
                    ("14:30", "15:15"), ("15:20", "16:05"),   # 第 5、6 节
                    ("16:35", "17:20"), ("17:25", "18:10"),   # 第 7、8 节
                    ("19:30", "20:15"), ("20:20", "21:05"),   # 第 9、10 节
                ],
                "office": (("08:00", "12:00"), ("14:30", "17:30")),
            },
            "winter": {
                # 冬季课表：当年 10 月 1 日 ~ 次年 4 月 30 日（跨年循环判定）
                "name": "冬季课表",
                "months": ((10, 1), (4, 30)),
                # 冬季下午比夏季提前 30 分钟（14:00 起），晚间提前到 19:00 起
                "periods": [
                    ("08:00", "08:45"), ("08:50", "09:35"),
                    ("10:05", "10:50"), ("10:55", "11:40"),
                    ("14:00", "14:45"), ("14:50", "15:35"),
                    ("16:05", "16:50"), ("16:55", "17:40"),
                    ("19:00", "19:45"), ("19:50", "20:35"),
                ],
                "office": (("08:00", "12:00"), ("14:00", "17:00")),
            },
        },
    },
    "weilaicheng": {
        "name": "未来城校区",
        "seasons": None,  # 不存在夏/冬季课表（确认），单套教学时间
        # 上午 08:30 起 4 节（节间 5 分钟）→ 午休 → 下午 14:00 起 4 节 →
        # 傍晚间隔 → 晚上 18:30 起 4 节（含第 11、12 节）
        "periods": [
            ("08:30", "09:15"), ("09:20", "10:05"),
            ("10:15", "11:00"), ("11:05", "11:50"),
            ("14:00", "14:45"), ("14:50", "15:35"),
            ("15:45", "16:30"), ("16:35", "17:20"),
            ("18:30", "19:15"), ("19:20", "20:05"),
            ("20:15", "21:00"), ("21:05", "21:50"),
        ],
        "office": (("08:30", "12:00"), ("14:00", "17:30")),
    },
}

# 默认编排配置：campus=当前生效校区；first_week_monday=第一周周一（编排必需）；
# overrides=用户对某校区某季节时间表的自定义覆盖（未覆盖的节次仍用内置预设）
DEFAULT_SCHEDULE = {
    # 当前生效校区方案（nanwangshan=南望山校区 / weilaicheng=未来城校区）
    "campus": "nanwangshan",
    # 本学期第一周周一日期（ISO 格式，开学周）
    "first_week_monday": "",
    # 用户自定义覆盖：{校区key: {季节key: {"periods": [...], "office": [...]}}}
    # 季节 key：南望山为 "summer"/"winter"，未来城为 "default"
    "overrides": {},
}

# 未来城校区无季节区分时的固定季节 key
DEFAULT_SEASON_KEY = "default"


def load_schedule_config() -> dict:
    """读取时间编排配置；不存在/损坏时返回默认值（并就地生成配置文件）。

    注意：旧版（线性推算时代）的 first_class_time/class_duration_min 等字段
    已废弃，读取时按"只保留新字段"处理，旧字段自然丢弃（不影响新逻辑）。
    """
    config = dict(DEFAULT_SCHEDULE)
    if SCHEDULE_CONFIG_FILE.exists():
        try:
            data = json.loads(SCHEDULE_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                config.update({k: v for k, v in data.items() if k in DEFAULT_SCHEDULE})
        except Exception:  # noqa: BLE001 损坏配置按默认处理
            pass
    return config


def save_schedule_config(config: dict) -> None:
    """把时间编排配置落盘（仅保留已知字段，防止脏数据）。"""
    clean = {k: config.get(k, DEFAULT_SCHEDULE[k]) for k in DEFAULT_SCHEDULE}
    SCHEDULE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCHEDULE_CONFIG_FILE.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def first_week_monday(config: dict) -> datetime.date | None:
    """解析第一周周一日期；未配置/格式错误返回 None（提示用户先配置）。"""
    raw = str(config.get("first_week_monday", "")).strip()
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return None


def parse_period(jc: str) -> tuple[int, int]:
    """解析节次字段为 (起始节, 结束节)：'3-4' → (3,4)；'3' → (3,3)。

    说明：正方课表 jc 字段形如 "3-4"（第 3~4 节连堂）；解析失败返回 (0,0)
    表示未知节次（调用方跳过，避免算错时间）。
    """
    import re
    m = re.search(r"(\d+)\s*[-~—～]\s*(\d+)", str(jc))
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d+)", str(jc))
    if m:
        return (int(m.group(1)), int(m.group(1)))
    return (0, 0)


def parse_weeks(zcd: str) -> tuple[int, int] | None:
    """解析周次字段为 (起始周, 结束周)：'1-16周' → (1,16)；'第3周' → (3,3)。

    说明：仅支持单段连续范围（MVP 不处理单双周交替）；解析失败返回 None，
    调用方按"全部周都有课"处理（保守不误删）。
    """
    import re
    m = re.search(r"(\d+)\s*[-~—～]\s*(\d+)", str(zcd))
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d+)", str(zcd))
    if m:
        return (int(m.group(1)), int(m.group(1)))
    return None


def current_week(first_monday: datetime.date, today: datetime.date | None = None) -> int:
    """计算今天是开学第几周（第一周周一为第 1 周）。

    返回：
        >=1 表示当前教学周；0 表示尚未开学；负数表示开学前第 |n| 周。
    """
    today = today or datetime.date.today()
    return (today - first_monday).days // 7 + 1


# ---------------------------------------------------------------------------
# 预设方案解析（季节自动切换 + 用户覆盖合并）
# ---------------------------------------------------------------------------

def _to_minutes(hhmm: str) -> int:
    """把 "HH:MM" 换算为距 0 点的分钟数（编排计算统一用分钟，避免时区陷阱）。"""
    hh, mm = str(hhmm).split(":")
    return int(hh) * 60 + int(mm)


def season_for_date(plan: dict, on_date: datetime.date | None = None) -> str:
    """按日期判定当前生效季节（南望山夏/冬自动切换；未来城返回 DEFAULT_SEASON_KEY）。

    判定规则（确认"按日期自动切换"）：
        南望山：当年 5/1 ~ 9/30 为夏季课表，10/1 ~ 次年 4/30 为冬季课表；
        未来城：无季节区分，固定返回 "default"。
    用"月日"比较实现跨年循环（不写死年份，每年自动适用）。
    """
    seasons = plan.get("seasons")
    if not seasons:
        return DEFAULT_SEASON_KEY
    on_date = on_date or datetime.date.today()
    md = (on_date.month, on_date.day)
    for key, season in seasons.items():
        (sm, sd), (em, ed) = season["months"]
        start_md, end_md = (sm, sd), (em, ed)
        # 处理跨年区间（如 10/1 ~ 次年 4/30：start > end 时按"跨年"比较）
        if start_md <= end_md:
            if start_md <= md <= end_md:
                return key
        else:
            if md >= start_md or md <= end_md:
                return key
    # 理论上不会到达（夏冬区间互补覆盖全年）；兜底取第一个季节
    return next(iter(seasons))


def get_active_plan(config: dict, on_date: datetime.date | None = None) -> dict:
    """返回指定日期当前生效的方案定义（合并了用户 overrides）。

    返回 dict：
        {"campus_key", "campus_name", "season_key", "season_name",
         "periods": [[开始,结束], ...], "office": [[上午], [下午]]}
    用途：/schedule 展示、class_time_range 查表、is_office_hours 判断，统一从
    这一个入口取"当天实际时间表"，保证课表/办公时间/展示三处行为一致。
    """
    campus_key = config.get("campus") or DEFAULT_SCHEDULE["campus"]
    plan = PRESET_PLANS.get(campus_key)
    if plan is None:  # 配置了未知校区 → 回退南望山（不崩溃）
        campus_key = DEFAULT_SCHEDULE["campus"]
        plan = PRESET_PLANS[campus_key]
    seasons = plan.get("seasons")
    if seasons:
        season_key = season_for_date(plan, on_date)
        season = seasons[season_key]
        season_name = season["name"]
    else:
        season_key = DEFAULT_SEASON_KEY
        season = plan
        season_name = "（无季节区分）"
    # 先取内置预设，再用用户 override 覆盖（同一校区同一季节）
    periods = [list(t) for t in season["periods"]]
    office = [list(t) for t in season["office"]]
    over = (config.get("overrides") or {}).get(campus_key, {}).get(season_key)
    if over:
        if over.get("periods"):
            periods = [list(t) for t in over["periods"]]
        if over.get("office"):
            office = [list(t) for t in over["office"]]
    return {
        "campus_key": campus_key,
        "campus_name": plan["name"],
        "season_key": season_key,
        "season_name": season_name,
        "periods": periods,
        "office": office,
    }


def class_time_range(period_start: int, period_end: int, config: dict, on_date: datetime.date | None = None) -> tuple[datetime.datetime, datetime.datetime] | None:
    """把节次换算为 (开始时间, 结束时间) 区间（按当日生效方案查表）。

    说明：开始时间 = 第 period_start 节表的开始时刻，结束时间 = 第 period_end
    节表的结束时刻（查表，含大课间/午休差异）；on_date 用于确定夏/冬季节，
    缺省为今天。节次越界（超出该方案总节数）视为未知返回 None。
    """
    plan = get_active_plan(config, on_date)
    periods = plan["periods"]
    if period_start <= 0 or period_end < period_start or period_end > len(periods):
        return None
    start = _to_minutes(periods[period_start - 1][0])
    end = _to_minutes(periods[period_end - 1][1])
    base = datetime.datetime.combine(on_date or datetime.date.today(), datetime.time())
    return (
        base + datetime.timedelta(minutes=start),
        base + datetime.timedelta(minutes=end),
    )


def is_office_hours(config: dict, now: datetime.datetime | None = None) -> bool:
    """判断当前时刻是否处于当前方案（含季节）的办公时间内。

    用途：/office_hours 命令与 Agent 工具「现在是办公时间吗」的判定依据；
    办公时间来自方案配置（南望山夏/冬、未来城各有自己的办公时段）。
    返回 True 表示"在办公时间内"，False 表示午休/下班/夜间等非办公时段。
    """
    now = now or datetime.datetime.now()
    plan = get_active_plan(config, now.date())
    now_min = now.hour * 60 + now.minute
    for start_s, end_s in plan["office"]:
        start = _to_minutes(start_s)
        end = _to_minutes(end_s)
        if start <= now_min < end:
            return True
    return False


# ---------------------------------------------------------------------------
# 下一节课编排
# ---------------------------------------------------------------------------

def _weekday_cn(xqj) -> str:
    """星期数字 → 中文（'周一'…'周日'）；未知返回原文。"""
    try:
        return f"周{('一二三四五六日')[int(xqj) - 1]}"
    except (ValueError, TypeError, IndexError):
        return str(xqj)


def next_course(rows: list[dict], config: dict, now: datetime.datetime | None = None) -> dict | None:
    """根据当前时间找出"下一节课"。

    判定逻辑（wakeup 式编排）：
        1. 由第一周周一算出当前教学周；未配置第一周周一 → 返回 {"error": ...}；
        2. 只考虑当前星期且周次覆盖当前周的课程；
        3. 选其中"开始时间 > now 且最早"的课为下一节课；
        4. 今天无后续课返回 {"none_today": True}（由调用方提示明天）。

    返回 dict：
        {"course": ..., "start": datetime, "end": datetime, "minutes_left": int}
        或 {"error": 提示文本} / {"none_today": True} / {"no_schedule": True}（今天无课）
    """
    now = now or datetime.datetime.now()
    first = first_week_monday(config)
    if first is None:
        return {"error": "未配置第一周周一（/schedule set first_week_monday YYYY-MM-DD），无法编排课表时间"}
    week = current_week(first, now.date())
    if week < 1:
        return {"error": f"当前（{now.date()}）早于第一周（{first}），学期尚未开始，暂无课表可编排"}
    if week > 30:
        return {"error": f"当前已到第 {week} 周，超出常规教学周，请确认第一周日期是否配置正确"}

    today_cn = _weekday_cn(now.isoweekday())
    upcoming: list[tuple[datetime.datetime, datetime.datetime, dict]] = []
    for row in rows:
        # 仅当前星期（xqj 与 today 一致）的课程
        if str(row.get("xqj", "")) != str(now.isoweekday()):
            continue
        # 周次范围校验：解析失败视为全部周有课
        weeks = parse_weeks(str(row.get("zcd", "")))
        if weeks is not None and not (weeks[0] <= week <= weeks[1]):
            continue
        ps, pe = parse_period(str(row.get("jc", "")))
        if ps <= 0:
            continue  # 未知节次不参与编排
        rng = class_time_range(ps, pe, config, on_date=now.date())
        if rng is None:
            continue
        start, end = rng
        # 已开始（含进行中）的课不算"下一节"：下一节必须是尚未开始的课
        # （修复：此前用 end <= now 判断，会把"进行中的课"误当成下一节，
        #  如第 1-2 节 08:00-09:40，在 09:00 查询时仍会被选中—— 测试发现）
        if start <= now:
            continue
        upcoming.append((start, end, row))
    if not upcoming:
        # 今天没有剩余课程（含今天本就没课）
        today_rows = [
            r for r in rows if str(r.get("xqj", "")) == str(now.isoweekday())
        ]
        return {"none_today": True, "has_today_course": bool(today_rows)}
    start, end, row = min(upcoming, key=lambda x: x[0])
    return {
        "course": row,
        "start": start,
        "end": end,
        "minutes_left": int((start - now).total_seconds() // 60),
    }


def humanize_next_course(result: dict, config: dict) -> str:
    """把 next_course 的结果转成 CLI 可读文本（含倒计时与上午/下午/晚上区分）。"""
    if "error" in result:
        return f"[注意] {result['error']}"
    if result.get("none_today"):
        if result.get("has_today_course"):
            return "[信息] 今天的课程已全部结束，明天见（/next_course 明天再查）。"
        return "[信息] 今天没有排课（可休息或自习）。"
    row = result["course"]
    start, end = result["start"], result["end"]
    minutes = result["minutes_left"]
    # 倒计时格式：>=60 显示 X 小时 Y 分；否则显示 Y 分钟
    if minutes >= 60:
        countdown = f"{minutes // 60} 小时 {minutes % 60} 分"
    else:
        countdown = f"{minutes} 分钟"
    # 上/下午/晚上标签（重构：含晚课方案后 12:00 以后不都是"下午"）
    if start.hour < 12:
        period_label = "上午"
    elif start.hour < 18:
        period_label = "下午"
    else:
        period_label = "晚上"
    # 节次区间显示：同一节则只显示一次
    ps, pe = parse_period(str(row.get("jc", "")))
    period_txt = f"第{ps}节" if ps == pe else f"第{ps}-{pe}节"
    return (
        f"[GEO] 下一节课：{row.get('kcmc', '（未命名课程）')}　"
        f"{_weekday_cn(row.get('xqj', ''))} {period_label} {period_txt} "
        f"（{start:%H:%M}–{end:%H:%M}）"
        f"　@{row.get('cdmc', '地点待定')}"
        f"　距开始还有 {countdown}"
    )


# ---------------------------------------------------------------------------
# /schedule 展示（当前方案 + 时间表 + 办公时间）
# ---------------------------------------------------------------------------

def _format_clock(hhmm: str) -> str:
    """把 "HH:MM" 统一输出为 HH:MM（保持显示一致）。"""
    hh, mm = str(hhmm).split(":")
    return f"{int(hh):02d}:{int(mm):02d}"


def humanize_schedule(config: dict, on_date: datetime.date | None = None) -> str:
    """生成 /schedule 查看文本：当前校区、生效季节、逐节时间表、办公时间、修改指引。

    说明：这是配置查看的单一出口（commands.py 直接复用），保证 CLI/Web/API
    三端展示一致；南望山会标注"按日期自动切换"及当前生效季节。
    """
    on_date = on_date or datetime.date.today()
    plan = get_active_plan(config, on_date)
    lines = [
        f"时间编排配置（当前：{plan['campus_name']}｜{plan['season_name']}）：",
    ]
    first = first_week_monday(config)
    lines.append(
        f"  first_week_monday  第一周周一：{config.get('first_week_monday') or '（未配置，无法编排）'}"
        + (f"（当前教学周：{current_week(first, on_date)}）" if first else "")
    )
    # 南望山标注季节自动切换说明（未来城无季节区分则不提示）
    if plan["campus_key"] == "nanwangshan":
        lines.append("  （南望山夏/冬季课表按日期自动切换：夏季 5/1–9/30，冬季 10/1–次年 4/30）")
    lines.append("  课表时间（查表，含大课间/午休差异）：")
    for i, (start, end) in enumerate(plan["periods"], 1):
        lines.append(f"    第 {i} 节  {_format_clock(start)}–{_format_clock(end)}")
    # 办公时间展示（上午/下午各一段）
    office_txt = "；".join(
        f"{_format_clock(s)}–{_format_clock(e)}" for s, e in plan["office"]
    )
    lines.append(f"  办公时间：{office_txt}")
    lines.append("切换：/schedule campus 南望山|未来城｜修改：/schedule set period <节次> <HH:MM-HH:MM> [夏|冬]")
    lines.append("示例：/schedule set period 3 10:05-10:50")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 办公时间（展示 + 判断命令 / Agent 工具， 确认）
# ---------------------------------------------------------------------------

def office_hours_report() -> str:
    """生成「当前是否办公时间」的可读报告（/office_hours 命令与 Agent 工具共用）。

    说明：一个入口两种用法——CLI 的 /office_hours 直接输出；Agent 工具
    is_office_hours 的 fn 也指向它，LLM 问「现在是办公时间吗」时调用，
    返回含当前时间、是否办公、办公时段的完整文本，避免重复实现。
    """
    config = load_schedule_config()
    now = datetime.datetime.now()
    plan = get_active_plan(config, now.date())
    in_hours = is_office_hours(config, now)
    office_txt = "；".join(
        f"{_format_clock(s)}–{_format_clock(e)}" for s, e in plan["office"]
    )
    status = "在办公时间内" if in_hours else "非办公时间（午休/下班/夜间）"
    return (
        f"[信息] 当前 {now:%Y-%m-%d %H:%M}：{status}。"
        f"（{plan['campus_name']}｜{plan['season_name']}办公时间：{office_txt}）"
    )


def to_office_hours_tool_spec():
    """把「当前是否办公时间」封装为 Agent 工具（供工具注册表注册）。

    说明：无参数工具——parameters 声明为空对象（properties 为空），
    run_tool_call 走结构化分支（required 空、kwargs 空）以 fn() 无参调用；
    返回文本已含判断结论与办公时段，LLM 直接转述即可。
    """
    from app.agent.tools import ToolSpec

    return ToolSpec(
        name="is_office_hours",
        description="判断当前时间是否处于办公时间（依据当前校区方案的办公时间表；用户问「现在是办公时间吗」「现在能去办事吗」时调用）",
        fn=office_hours_report,
        parameters={"type": "object", "properties": {}},
    )
