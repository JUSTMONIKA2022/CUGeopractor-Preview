# -*- coding: utf-8 -*-
"""中国地质大学（武汉）官网实时检索连接器（L0 官方公开渠道）。

设计说明（对应渠道规划二期 L0，策略：实时检索而非 RAG 入库）：
    - 目标栏目：通知公告 / 学术动态 / 地大要闻（官网公开列表页，服务端渲染可爬）；
    - 为何实时检索：公告通知时效性强、增量爆炸，全量入 RAG 会导致向量库膨胀、
      旧公告干扰检索、需频繁重建索引。实时检索"现查现取"，仅返回摘要+链接，零维护；
    - 高性能设计（调研结论落地）：
        1) TTL 内存缓存：同一栏目在缓存期内重复查询不打网络（默认 300s）；
        2) ETag/Last-Modified 条件请求：缓存过期后带 If-None-Match/If-Modified-Since，
           服务器未变更返回 304 直接复用旧缓存，省带宽；
        3) 全局限速器 + 随机抖动 + 指数退避 + 熔断：低频防封禁（复用 app.rate_limit）；
    - 合规红线：仅取官网公开列表页的标题/日期/摘要/链接；不抓正文详情（详情页分散在
      OA/信息门户等需登录系统）；不批量、不破解。
"""

from __future__ import annotations

import re
import threading
import time

import httpx

from app.rate_limit import CircuitBreaker, backoff_retry, get_rate_limiter
from connectors.base import tool_error, tool_info

# 官网基地址
BASE = "https://www.cug.edu.cn/"
# 支持检索的栏目（栏目名 -> 列表页相对路径）
CHANNELS = {
    "通知公告": "index/tzgg.htm",
    "学术动态": "index/xsdt.htm",
    "地大要闻": "index/ddyw.htm",
}
# 默认检索栏目（None 表示全部）
DEFAULT_CHANNEL = "通知公告"
# 请求间隔（秒）：低频防封禁
INTERVAL = 4.0
JITTER = 1.5
# 单次返回结果上限
MAX_RESULTS = 8
# 缓存有效期（秒）：期内重复查询直接命中缓存
CACHE_TTL = 300.0

# 浏览器请求头（官网无强反爬，标准头即可）
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_limiter = get_rate_limiter("cug_news", interval=INTERVAL, jitter=JITTER)
_breaker = CircuitBreaker()


class _ChannelCache:
    """单栏目缓存：保存解析结果 + 条件请求凭证（ETag/Last-Modified）+ 过期时间。"""

    def __init__(self) -> None:
        self.items: list[dict] = []          # 已解析的列表项
        self.etag: str | None = None         # 上次响应 ETag
        self.last_modified: str | None = None  # 上次响应 Last-Modified
        self.expire_at: float = 0.0          # 缓存过期时间戳（monotonic）


# 栏目缓存注册表（线程安全）
_CACHES: dict[str, _ChannelCache] = {name: _ChannelCache() for name in CHANNELS}
_CACHE_LOCK = threading.Lock()


def _parse_list(html: str, page_url: str) -> list[dict]:
    """解析官网栏目列表页，返回 [{date, title, desc, link}, ...]。

    列表项结构（官网 xblist 布局）：
        <div class="xblist-date"><p>2025-08</p><h2>28</h2></div>
        <div class="xblist-title xblist-title2">
            <a href="../info/xxx.htm 或 外链"><h2>标题</h2><div>摘要</div></a>
        </div>
    做法：分别抽取日期块与链接块，按出现顺序一一配对。
    """
    # 链接块：<a href="..."><h2>标题</h2><div>摘要</div>（href 可为相对 info/ 或绝对外链）
    # 注意：摘要 </div> 后不一定紧跟 </a>（可能还有日期/更多嵌套），故不以 </a> 收尾
    link_pat = re.compile(
        r'<a href="([^"]+)"[^>]*>\s*<h2>(.*?)</h2>\s*<div>(.*?)</div>',
        re.S,
    )
    # 日期块：<p>YYYY-MM</p><h2>DD</h2>
    date_pat = re.compile(r"<p>(\d{4}-\d{2})</p>\s*<h2>(\d{2})</h2>")

    links = link_pat.findall(html)
    dates = date_pat.findall(html)

    items: list[dict] = []
    for i, (href, title, desc) in enumerate(links):
        # 清洗标题/摘要中的 HTML 标签与多余空白
        title = re.sub(r"<[^>]+>", "", title).strip()
        desc = re.sub(r"<[^>]+>", "", desc).strip()
        desc = re.sub(r"\s+", " ", desc)[:100]
        if not title:
            continue
        # 还原绝对链接（相对路径基于当前列表页解析）
        link = str(httpx.URL(page_url).join(href))
        date = f"{dates[i][0]}-{dates[i][1]}" if i < len(dates) else ""
        items.append({"date": date, "title": title, "desc": desc, "link": link})
    return items


def _fetch_channel(channel: str) -> list[dict]:
    """抓取并解析单个栏目列表页（带 TTL 缓存 + ETag/Last-Modified 条件请求）。

    流程：
        1) 缓存未过期 -> 直接返回缓存；
        2) 缓存过期 -> 带 If-None-Match/If-Modified-Since 发起条件请求；
        3) 304 -> 复用旧缓存并续期；200 -> 解析新内容并更新缓存与凭证。
    """
    cache = _CACHES[channel]
    with _CACHE_LOCK:
        # 缓存命中且未过期：直接返回，不打网络
        if cache.items and time.monotonic() < cache.expire_at:
            return cache.items
        etag, last_mod = cache.etag, cache.last_modified

    url = str(httpx.URL(BASE).join(CHANNELS[channel]))

    def do_request() -> httpx.Response:
        with _limiter:
            headers = dict(_HEADERS)
            # 条件请求凭证：让服务器在内容未变时返回 304
            if etag:
                headers["If-None-Match"] = etag
            if last_mod:
                headers["If-Modified-Since"] = last_mod
            return httpx.get(url, headers=headers, timeout=15, follow_redirects=True)

    resp = backoff_retry(do_request, retries=2, base_delay=1.0)

    with _CACHE_LOCK:
        if resp.status_code == 304 and cache.items:
            # 内容未变更：复用旧缓存，仅续期
            cache.expire_at = time.monotonic() + CACHE_TTL
            return cache.items
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        items = _parse_list(resp.text, url)
        # 更新缓存与条件请求凭证，设置过期时间
        cache.items = items
        cache.etag = resp.headers.get("ETag")
        cache.last_modified = resp.headers.get("Last-Modified")
        cache.expire_at = time.monotonic() + CACHE_TTL
        return items


def cug_news_search(keyword: str = "", channel: str | None = None) -> str:
    """检索官网公开栏目（通知公告/学术动态/地大要闻），返回摘要+链接。

    参数：
        keyword: 检索关键词（匹配标题或摘要，如"放假""奖学金""学术报告"）
                 （默认空串：/live_news 无参或 LLM 缺参时返回全部栏目最新内容）
        channel: 指定栏目名（可选；不填则检索全部栏目）
    返回：
        多行文本，每条含日期/标题/摘要/链接；失败返回以 [错误] 开头提示。
    """
    if not _breaker.allow():
        return tool_error("cug_news_search", "连接器处于熔断冷却中，请稍后再试")

    # 确定要检索的栏目集合
    if channel and channel in CHANNELS:
        targets = [channel]
    else:
        targets = list(CHANNELS)

    all_items: list[dict] = []
    last_err: Exception | None = None
    for ch in targets:
        try:
            all_items.extend(_fetch_channel(ch))
        except Exception as exc:  # noqa: BLE001 单栏目失败不阻断其他栏目
            last_err = exc

    if not all_items and last_err is not None:
        _breaker.record_failure()
        return tool_error("cug_news_search", f"检索失败：{last_err}")

    # 关键词过滤（标题或摘要包含关键词，不区分大小写）
    kw = keyword.strip().lower()
    if kw:
        filtered = [
            it for it in all_items
            if kw in it["title"].lower() or kw in it["desc"].lower()
        ]
    else:
        filtered = all_items

    # 去重（按链接）并限量
    seen: set[str] = set()
    results: list[dict] = []
    for it in filtered:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        results.append(it)
        if len(results) >= MAX_RESULTS:
            break

    _breaker.record_success()
    if not results:
        return tool_info("cug_news_search", f"官网公开栏目未找到与「{keyword}」相关的公告（仅检索通知公告/学术动态/地大要闻列表页）")

    lines = []
    for i, it in enumerate(results, 1):
        date = f"[{it['date']}] " if it["date"] else ""
        lines.append(f"{i}. {date}{it['title']}\n   摘要：{it['desc']}\n   链接：{it['link']}")
    return "\n\n".join(lines)


# ===== 官网机构导航（需求：官网渠道简陋，无学院/办公室导航） =====
# 官网「组织机构」页（zzjg1.htm）分两类：
#   - 教学科研机构 jxkyjg.htm：学院 + 部分重点实验室/研究院；
#   - 管理服务机构 glfwjg.htm：各职能部门/办公室。
# 机构链接集中在 <li class="yj"><a target="_blank" href="http://xxx.cug.edu.cn/">名称</a>
# （实测页面结构；若官网改版需重新核对）。
ORG_PAGES = {
    "学院": "https://www.cug.edu.cn/zzjg1/jxkyjg.htm",
    "部门": "https://www.cug.edu.cn/zzjg1/glfwjg.htm",
}
# 导航噪声标题（官网公共菜单，非机构）
_ORG_SKIP = {
    "首页", "English", "地大要闻", "领导活动", "科技创新", "学术动态", "通知公告",
    "专题聚焦", "媒体地大", "文明网", "信息门户", "学校主页", "返回首页",
}
# 机构导航缓存：页面 URL -> (过期时间, [(name, url), ...])
_org_cache: dict[str, tuple[float, list[dict]]] = {}
_org_lock = threading.Lock()


def _fetch_org_group(name: str) -> list[dict]:
    """抓取一类机构导航（学院/部门），TTL 缓存；失败返回空列表。"""
    url = ORG_PAGES[name]
    now = time.monotonic()
    with _org_lock:
        cached = _org_cache.get(url)
        if cached and now - cached[0] < CACHE_TTL:
            return cached[1]
    items: list[dict] = []
    try:
        with _limiter:
            resp = httpx.get(url, headers=_HEADERS, timeout=15)
    except Exception:  # noqa: BLE001 网络异常按空处理（调用方降级提示）
        return []
    if resp.status_code != 200:
        return []
    # 提取机构链接：<a target="_blank" href="http://xxx.cug.edu.cn/">机构名</a>
    for m in re.finditer(r'<a target="_blank" href="(http[^"]+)"[^>]*>\s*([^<]{2,40})\s*</a>', resp.text):
        link, title = m.group(1), m.group(2).strip()
        if not title or title in _ORG_SKIP or "cug.edu.cn" not in link:
            continue
        items.append({"name": title, "url": link})
    # 去重（同 URL 保留首个）
    seen: set[str] = set()
    unique = [it for it in items if not (it["url"] in seen or seen.add(it["url"]))]
    with _org_lock:
        _org_cache[url] = (time.monotonic(), unique)
    return unique


def cug_navigation(keyword: str = "") -> str:
    """官网机构导航：列出学院/职能部门及官网链接（实时抓取，TTL 缓存）。

    背景（需求）：官网渠道原本只有公告检索，缺少"导航到学院、
    办公室"的能力。本工具从官网「组织机构」页抓取学院（教学科研机构）与
    职能部门（管理服务机构）清单，让 agent 能引导用户直达某学院/部门官网。

    参数：
        keyword: 过滤关键词（匹配机构名，如"自动化""教务处"）；空串返回全部
    返回：
        按「学院 / 部门」分组的机构导航（名称 + 官网链接）；失败返回 [错误] 提示。
    """
    if not _breaker.allow():
        return tool_error("cug_navigation", "连接器处于熔断冷却中，请稍后再试")
    kw = keyword.strip().lower()
    out = []
    failed = []
    for group in ("学院", "部门"):
        items = _fetch_org_group(group)
        if not items:
            failed.append(group)
            continue
        # 关键词过滤（名称包含关键词，不区分大小写）
        filtered = [it for it in items if not kw or kw in it["name"].lower()]
        if filtered:
            lines = [f"▎官网{group}（{len(filtered)} 个）："]
            for i, it in enumerate(filtered, 1):
                lines.append(f"  {i}. {it['name']}\n     官网：{it['url']}")
            out.append("\n".join(lines))
    if not out:
        _breaker.record_failure()
        msg = " / ".join(failed) if failed else f"未找到与「{keyword}」匹配的学院/部门"
        return tool_error("cug_navigation", f"官网机构导航获取失败（{msg}）。可访问 https://www.cug.edu.cn/zzjg1.htm 查看")
    _breaker.record_success()
    head = f"官网机构导航（中国地质大学武汉，关键词「{keyword or '全部'}」）：\n"
    return head + "\n\n".join(out)


def to_tool_spec():
    """把官网实时检索封装为 ToolSpec（供工具注册表注册）。

    结构化参数说明（对应待办方向 A 路线一，解决 channel 无法暴露的问题）：
        - 声明 keyword（必填）+ channel（可选，enum 限定三个栏目），
          让 LLM 既能按关键词检索，也能指定只在某栏目内检索（不传则全栏目）；
        - fn 直接引用 cug_news_search，参数名与 parameters 的 key 一一对应，
          ToolRegistry.run_tool_call 会以具名参数（**kwargs）方式调用；
        - channel 为可选且用 enum 约束：LLM 只会传合法栏目名，非法值兜底为全栏目。
    """
    from app.agent.tools import ToolSpec

    return ToolSpec(
        name="cug_news_search",
        description="实时检索中国地质大学（武汉）官网公开栏目（通知公告/学术动态/地大要闻），返回日期、标题、摘要与链接",
        fn=cug_news_search,
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "检索关键词（匹配标题或摘要，如「放假」「奖学金」）"},
                "channel": {
                    "type": "string",
                    "enum": list(CHANNELS),
                    "description": "限定检索的栏目（不传则检索全部栏目）",
                },
            },
            "required": ["keyword"],
        },
    )


def to_navigation_tool_spec():
    """把官网机构导航封装为 ToolSpec（供工具注册表注册，LLM 可调用）。

    与 cug_news_search 互补：检索管"官网有什么公告"，导航管"官网有哪些
    学院/办公室、入口在哪"（需求补齐官网渠道导航能力）。
    """
    from app.agent.tools import ToolSpec

    return ToolSpec(
        name="cug_navigation",
        description="查询中国地质大学（武汉）官网机构导航：列出学院与职能部门及其官网链接（如想访问某学院/办公室官网时调用）",
        fn=cug_navigation,
        parameters={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "过滤关键词（匹配机构名，如「自动化」「教务处」「研究生院」；空串返回全部）",
                },
            },
            "required": ["keyword"],
        },
    )
