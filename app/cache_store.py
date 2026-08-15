# -*- coding: utf-8 -*-
"""CLI 命令系统的缓存存储层：渠道缓存生成/落盘/读取/刷新。

设计（对应 CLI 大改-命令系统）：
    - 缓存渠道：学校官网、信息门户、百度贴吧、知乎、B站、教务系统；
    - 数据来源：复用现有连接器（实时接口）动态生成 → 落盘 data/cache/<channel>.json；
      之后命令读缓存（不调 LLM、不重复请求），支持手动 /cache_refresh 与定时任务刷新；
    - 缓存结构（统一 schema）：
        {
          "channel": "ifmweb",
          "name": "信息门户",
          "updated": 时间戳,
          "error": 生成失败时的说明（可选）,
          "sections": [
            {"key": "pwps", "name": "勤工助学", "url": "办理入口", "desc": "说明",
             "items": [{"name","url","desc"}, ...]}   # 列表型 section 用 items
          ]
        }
    - 层级命令约定：/cache_<channel> 列出 sections；/cache_<channel> <关键词|序号> 定位；
      带 url 的 section 支持 webbrowser 打开（"写操作交给用户、agent 给入口"决策）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# 缓存目录（data/ 已被 .gitignore 排除，缓存不入库）
CACHE_DIR = Path("data/cache")
# 缓存有效期（秒）：30 分钟
CACHE_TTL = 1800.0

# ===== 渠道元数据 =====
CHANNELS = {
    "ofcweb": {"name": "学校官网", "desc": "通知公告/学术动态/地大要闻（实时检索）"},
    "ifmweb": {"name": "信息门户", "desc": "网上厅服务目录（75 项服务）与已接入只读工具"},
    "tieba": {"name": "百度贴吧", "desc": "中国地质大学武汉吧公开帖子（本地服务）"},
    "zhihu": {"name": "知乎", "desc": "地大相关内容（站内搜索）"},
    "bilibili": {"name": "B站", "desc": "地大相关内容（公开搜索）"},
    "jwgl": {"name": "教务系统", "desc": "我的课表/成绩/考试/学籍/培养方案（需登录）"},
}

# 门户常用服务的语义 key（用户示例 /cache_ifmweb_pwps 风格）；
# 未覆盖的服务用序号 key（svc_1, svc_2, ...），两者均可定位
_SLUG_MAP = {
    "勤工助学": "pwps",
    "学籍查询": "schroll",
    "自习室课表": "room",
    "成绩办理": "score",
    "在校证明": "certificate",
    "选课": "course_select",
    "转专业": "major_change",
    "宿舍调整": "dorm",
}

# 贴吧/知乎/B站 的预置检索主题（生成缓存时各查一组关键词）
TIEBA_TOPICS = ["宿舍", "食堂", "选课", "考研", "新生", "转专业"]
ZHIHU_TOPICS = ["考研", "宿舍", "食堂", "就业", "转专业", "科研"]
BILIBILI_TOPICS = ["校园", "宿舍", "考研", "食堂", "新生"]


def cache_path(channel: str) -> Path:
    """缓存文件路径。"""
    return CACHE_DIR / f"{channel}.json"


def load_cached(channel: str) -> dict | None:
    """读取渠道缓存；无缓存/已过期返回 None（由上层决定刷新）。"""
    path = cache_path(channel)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 损坏的缓存按不存在处理
        return None
    if time.time() - float(data.get("updated", 0)) > CACHE_TTL:
        return None
    return data


def save_cached(channel: str, data: dict) -> None:
    """把渠道缓存落盘（原子写：先写临时文件再替换，避免写一半损坏）。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = cache_path(channel)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def refresh_channel(channel: str) -> dict:
    """强制刷新某渠道缓存（调适配器生成 + 落盘），返回缓存数据。"""
    builder = _BUILDERS.get(channel)
    data = {
        "channel": channel,
        "name": CHANNELS.get(channel, {}).get("name", channel),
        "updated": int(time.time()),
        "error": None,
        "sections": [],
    }
    if builder is None:
        data["error"] = f"未知渠道：{channel}"
        return data
    try:
        sections = builder()
        data["sections"] = sections or []
        if not sections:
            data["error"] = "该渠道当前无可用内容（可能未登录或接口异常）"
    except Exception as exc:  # noqa: BLE001 生成失败记录错误，不中断 CLI
        data["error"] = f"生成缓存失败：{type(exc).__name__}: {exc}"
    save_cached(channel, data)
    return data


def get_or_refresh(channel: str, force: bool = False) -> dict:
    """取缓存；无缓存/过期/强制刷新时重新生成。"""
    if not force:
        cached = load_cached(channel)
        if cached is not None:
            return cached
    return refresh_channel(channel)


def list_cached() -> list[dict]:
    """列出全部渠道的缓存状态（供 /cache_search 展示）。"""
    result = []
    for channel, meta in CHANNELS.items():
        entry = {
            "channel": channel,
            "name": meta["name"],
            "desc": meta["desc"],
            "cached": False,
            "updated": None,
            "error": None,
            "section_count": 0,
            "command": f"/cache_{channel}",
        }
        path = cache_path(channel)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                entry["cached"] = True
                entry["updated"] = data.get("updated")
                entry["error"] = data.get("error")
                entry["section_count"] = len(data.get("sections") or [])
            except Exception:  # noqa: BLE001
                pass
        result.append(entry)
    return result


# ===== 渠道适配器 =====

def _section(key: str, name: str, url: str = "", desc: str = "", items: list | None = None) -> dict:
    """构造统一 section 结构。"""
    return {"key": key, "name": name, "url": url, "desc": desc, "items": items or []}


def _build_ofcweb() -> list[dict]:
    """官网缓存：三栏目各取最近新闻（标题/日期/链接）。"""
    from connectors.cug_news_connector import cug_news_search

    sections = []
    channels = [("通知公告", "notice"), ("学术动态", "academic"), ("地大要闻", "news")]
    for label, key in channels:
        raw = cug_news_search(label, 8)
        items = []
        for line in raw.splitlines():
            # 解析 cug_news_search 输出："1. [日期] 标题\n 链接：..."
            if line.strip().startswith(("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.")):
                title = line.strip().split(" ", 1)[-1]
                items.append({"name": title, "url": "", "desc": ""})
            elif line.strip().startswith("链接：") and items:
                items[-1]["url"] = line.strip().replace("链接：", "")
        sections.append(_section(key, label, desc=f"栏目「{label}」最近 8 条", items=items[:8]))
    return sections


def _build_ifmweb() -> list[dict]:
    """信息门户缓存：网上厅服务目录（75 项，含办理入口 URL）。"""
    from connectors.portal_connector import fetch_service_catalog

    services = fetch_service_catalog()  # [(分类, 服务名, 部门, 电话, 入口URL, 指南)]
    sections = []
    for idx, (cat, name, dept, tele, url, guide) in enumerate(services, 1):
        key = _SLUG_MAP.get(name, f"svc_{idx}")
        desc = f"分类：{cat}｜部门：{dept or '—'}｜咨询：{tele or '—'}"
        if guide:
            desc += f"｜{guide}"
        sections.append(_section(key, name, url=url, desc=desc))
    return sections


def _build_tieba() -> list[dict]:
    """贴吧缓存：预置主题各查一组公开帖子。"""
    from connectors.tieba_connector import tieba_search

    sections = []
    for topic in TIEBA_TOPICS:
        raw = tieba_search(topic)
        items = []
        for line in raw.splitlines():
            if line.strip() and not line.strip().startswith(("[", "链接：")):
                title = line.strip().lstrip("0123456789. ").strip()
                if title:
                    items.append({"name": title, "url": "", "desc": ""})
            elif line.strip().startswith("链接：") and items:
                items[-1]["url"] = line.strip().replace("链接：", "")
        sections.append(_section(f"topic_{len(sections) + 1}", f"主题「{topic}」", items=items[:12]))
    return sections


def _build_zhihu() -> list[dict]:
    """知乎缓存：预置主题站内搜索。"""
    from connectors.zhihu_connector import zhihu_search

    sections = []
    for topic in ZHIHU_TOPICS:
        raw = zhihu_search(f"中国地质大学（武汉） {topic}")
        items = []
        for line in raw.splitlines():
            if line.strip().startswith(("1.", "2.", "3.", "4.", "5.")):
                items.append({"name": line.strip().split(" ", 1)[-1], "url": "", "desc": ""})
            elif line.strip().startswith("链接：") and items:
                items[-1]["url"] = line.strip().replace("链接：", "")
        sections.append(_section(f"topic_{len(sections) + 1}", f"主题「{topic}」", items=items[:8]))
    return sections


def _build_bilibili() -> list[dict]:
    """B站缓存：预置主题公开搜索。"""
    from connectors.bilibili_connector import bilibili_search

    sections = []
    for topic in BILIBILI_TOPICS:
        raw = bilibili_search(f"中国地质大学（武汉） {topic}")
        items = []
        for line in raw.splitlines():
            if line.strip().startswith(("1.", "2.", "3.", "4.", "5.")):
                items.append({"name": line.strip().split(" ", 1)[-1], "url": "", "desc": ""})
            elif line.strip().startswith("链接：") and items:
                items[-1]["url"] = line.strip().replace("链接：", "")
        sections.append(_section(f"topic_{len(sections) + 1}", f"主题「{topic}」", items=items[:8]))
    return sections


def _build_jwgl() -> list[dict]:
    """教务缓存：我的课表/成绩/考试/学籍/培养方案（会话型，需登录）。"""
    from connectors.session_connector import load_session_connectors_from_yaml

    connectors = {c.name: c for c in load_session_connectors_from_yaml()}
    plan = [
        ("cug_course", "课表", "course"),
        ("cug_grade", "成绩", "score"),
        ("cug_exam", "考试", "exam"),
        ("cug_student_info", "学籍", "schroll"),
        ("cug_training_plan", "培养方案", "plan"),
    ]
    sections = []
    for name, label, key in plan:
        conn = connectors.get(name)
        if not conn:
            continue
        raw = conn.invoke(f"查询{label}")  # 真实调用（带登录态），结果摘要入缓存
        desc = raw.strip().replace("\n", "；")[:200]
        sections.append(_section(key, label, desc=desc or f"{label}暂无数据"))
    return sections


# 渠道适配器注册表
_BUILDERS = {
    "ofcweb": _build_ofcweb,
    "ifmweb": _build_ifmweb,
    "tieba": _build_tieba,
    "zhihu": _build_zhihu,
    "bilibili": _build_bilibili,
    "jwgl": _build_jwgl,
}
