# -*- coding: utf-8 -*-
"""课表结构化存储与时间编排模块测试（app/course_schedule.py）。

覆盖点（两套预设方案重构后）：
    - 节次/周次解析、教学周计算（既有逻辑保留）；
    - 节次时间改为**查表**：南望山夏/冬按日期自动切换，未来城无季节区分；
    - /schedule 方案切换（campus）、单节时间覆盖（overrides）、办公时间判断。
"""

from __future__ import annotations

import datetime

from app import course_schedule as sched

# 示例课表行（正方教务 kbList 字段：kcmc 课程名 / xqj 星期 / jc 节次 / zcd 周次 / cdmc 地点 / xm 教师）
ROWS = [
    {"kcmc": "高等数学", "xqj": "1", "jc": "1-2", "zcd": "1-16周", "cdmc": "教一楼101", "xm": "张三"},
    {"kcmc": "大学英语", "xqj": "1", "jc": "3-4", "zcd": "1-16周", "cdmc": "教一楼202", "xm": "李四"},
    {"kcmc": "程序设计", "xqj": "3", "jc": "5-6", "zcd": "1-16周", "cdmc": "实验楼301", "xm": "王五"},
]

# 编排配置（新结构）：campus=当前方案、first_week_monday=第一周周一、overrides=用户覆盖
# 第一周周一 2026-02-23（周一）；测试时间 2026-03-02 为周一
CONFIG = {
    "campus": "nanwangshan",
    "first_week_monday": "2026-02-23",
    "overrides": {},
}

# 固定日期：2026-03-02 属冬季（10/1–次年4/30）；2026-07-15 属夏季（5/1–9/30）
WINTER_MONDAY = datetime.date(2026, 3, 2)
SUMMER_DATE = datetime.date(2026, 7, 15)


def test_parse_period():
    """节次解析：连堂/单节/未知。"""
    assert sched.parse_period("3-4") == (3, 4)
    assert sched.parse_period("3") == (3, 3)
    assert sched.parse_period("第3节") == (3, 3)
    assert sched.parse_period("") == (0, 0)


def test_parse_weeks():
    """周次解析：范围/单周/未知。"""
    assert sched.parse_weeks("1-16周") == (1, 16)
    assert sched.parse_weeks("第3周") == (3, 3)
    assert sched.parse_weeks("") is None


def test_current_week():
    """教学周计算：第一周周一为第 1 周。"""
    first = datetime.date(2026, 2, 23)
    assert sched.current_week(first, datetime.date(2026, 2, 23)) == 1
    assert sched.current_week(first, datetime.date(2026, 3, 2)) == 2
    assert sched.current_week(first, datetime.date(2026, 2, 16)) == 0  # 开学前


def test_season_for_date():
    """南望山夏/冬按日期自动切换：5/1–9/30 夏，10/1–次年4/30 冬（跨年循环）。"""
    plan = sched.PRESET_PLANS["nanwangshan"]
    assert sched.season_for_date(plan, datetime.date(2026, 5, 1)) == "summer"
    assert sched.season_for_date(plan, datetime.date(2026, 9, 30)) == "summer"
    assert sched.season_for_date(plan, datetime.date(2026, 10, 1)) == "winter"
    assert sched.season_for_date(plan, datetime.date(2026, 4, 30)) == "winter"
    # 跨年（2027 年 1 月）仍属冬季
    assert sched.season_for_date(plan, datetime.date(2027, 1, 15)) == "winter"
    # 未来城无季节区分，固定 default
    assert sched.season_for_date(sched.PRESET_PLANS["weilaicheng"]) == sched.DEFAULT_SEASON_KEY


def test_class_time_range_summer():
    """夏季查表：第1-2节 08:00–09:35；第3-4节 10:05–11:40（含30分钟大课间）。"""
    rng = sched.class_time_range(1, 2, CONFIG, on_date=SUMMER_DATE)
    assert rng is not None
    assert (rng[0].hour, rng[0].minute) == (8, 0)
    # 第 2 节结束 09:35（查表，不再是线性推算的 09:40）
    assert (rng[1].hour, rng[1].minute) == (9, 35)
    rng34 = sched.class_time_range(3, 4, CONFIG, on_date=SUMMER_DATE)
    assert rng34 is not None
    assert (rng34[0].hour, rng34[0].minute) == (10, 5)
    assert (rng34[1].hour, rng34[1].minute) == (11, 40)
    # 越界节次返回 None（南望山共 10 节）
    assert sched.class_time_range(0, 1, CONFIG, on_date=SUMMER_DATE) is None
    assert sched.class_time_range(1, 11, CONFIG, on_date=SUMMER_DATE) is None


def test_class_time_range_winter():
    """冬季查表：下午比夏季提前 30 分钟（第5-6节 14:00–15:35）。"""
    rng = sched.class_time_range(5, 6, CONFIG, on_date=WINTER_MONDAY)
    assert rng is not None
    assert (rng[0].hour, rng[0].minute) == (14, 0)
    assert (rng[1].hour, rng[1].minute) == (15, 35)


def test_class_time_range_future_city():
    """未来城：无季节区分，12 节课；第11-12节 20:15–21:50。"""
    config = dict(CONFIG, campus="weilaicheng")
    rng = sched.class_time_range(11, 12, config, on_date=SUMMER_DATE)
    assert rng is not None
    assert (rng[0].hour, rng[0].minute) == (20, 15)
    assert (rng[1].hour, rng[1].minute) == (21, 50)
    # 未来城 12 节是上限
    assert sched.class_time_range(1, 13, config, on_date=SUMMER_DATE) is None


def test_next_course_weekday_morning():
    """周一上午 09:00：第1-2节已开始（08:00-09:35），下一节应为第3-4节 10:05 的大学英语。"""
    now = datetime.datetime(2026, 3, 2, 9, 0)  # 周一（2026-03-02 是周一，属冬季表，上午时间与夏季一致）
    result = sched.next_course(ROWS, CONFIG, now=now)
    assert "error" not in result and "none_today" not in result
    assert result["course"]["kcmc"] == "大学英语"
    assert result["minutes_left"] == 65  # 10:05 - 09:00


def test_next_course_after_today():
    """周一晚上 22:00：今天课程已全部结束。"""
    now = datetime.datetime(2026, 3, 2, 22, 0)
    result = sched.next_course(ROWS, CONFIG, now=now)
    assert result.get("none_today") is True
    assert result.get("has_today_course") is True  # 今天确实有课但已结束


def test_next_course_no_first_monday():
    """未配置第一周周一：返回错误提示。"""
    config = dict(CONFIG)
    config["first_week_monday"] = ""
    now = datetime.datetime(2026, 3, 2, 9, 0)
    result = sched.next_course(ROWS, config, now=now)
    assert "error" in result


def test_humanize_next_course():
    """下一节课可读文本：含课程名/时间区间/倒计时（09:00 → 10:05 = 65 分钟，显示为 1 小时 5 分）。"""
    now = datetime.datetime(2026, 3, 2, 9, 0)
    result = sched.next_course(ROWS, CONFIG, now=now)
    text = sched.humanize_next_course(result, CONFIG)
    assert "大学英语" in text and "10:05" in text and "1 小时 5 分" in text


def test_override_period():
    """用户覆盖某节课时间后，查表应使用覆盖值（南望山夏季第 3 节改为 10:10–10:55）。"""
    # 构造与 /schedule set period 3 10:10-10:55 相同结果的 overrides：
    # 完整复制夏季默认表，仅修改第 3 节，写入 overrides.nanwangshan.summer.periods
    base = sched.PRESET_PLANS["nanwangshan"]["seasons"]["summer"]
    periods = [list(t) for t in base["periods"]]
    periods[2] = ["10:10", "10:55"]
    config = {
        "campus": "nanwangshan",
        "first_week_monday": "2026-02-23",
        "overrides": {"nanwangshan": {"summer": {"periods": periods}}},
    }
    rng = sched.class_time_range(3, 3, config, on_date=SUMMER_DATE)
    assert rng is not None
    assert (rng[0].hour, rng[0].minute) == (10, 10)
    assert (rng[1].hour, rng[1].minute) == (10, 55)


def test_is_office_hours():
    """办公时间判断：夏季上午 09:00 在办公时间；午休/晚间非办公。"""
    # 夏季办公：上午 08:00–12:00，下午 14:30–17:30
    assert sched.is_office_hours(CONFIG, datetime.datetime(2026, 7, 15, 9, 0)) is True
    assert sched.is_office_hours(CONFIG, datetime.datetime(2026, 7, 15, 12, 30)) is False  # 午休
    assert sched.is_office_hours(CONFIG, datetime.datetime(2026, 7, 15, 15, 0)) is True
    assert sched.is_office_hours(CONFIG, datetime.datetime(2026, 7, 15, 20, 0)) is False  # 晚间
    # 冬季下午 14:00 开始办公（比夏季提前 30 分钟）
    assert sched.is_office_hours(CONFIG, datetime.datetime(2026, 12, 1, 14, 15)) is True


def test_humanize_schedule_contains_plan_info():
    """/schedule 查看文本应含校区、季节、节次时间与办公时间。"""
    text = sched.humanize_schedule(CONFIG, on_date=SUMMER_DATE)
    assert "南望山校区" in text and "夏季课表" in text
    assert "第 1 节" in text and "08:00–08:45" in text
    assert "办公时间" in text and "14:30–17:30" in text  # 夏季下午办公时间


def test_humanize_schedule_future_city():
    """未来城查看文本：无季节区分、办公时间 08:30–12:00 / 14:00–17:30。"""
    config = dict(CONFIG, campus="weilaicheng")
    text = sched.humanize_schedule(config, on_date=SUMMER_DATE)
    assert "未来城校区" in text
    assert "第 12 节" in text  # 12 节课
    assert "08:30–12:00" in text
