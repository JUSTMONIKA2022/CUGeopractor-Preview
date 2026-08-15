# -*- coding: utf-8 -*-
"""学院网站实时检索连接器（需求）。

背景：官网机构导航（cug_navigation）只给学院官网**链接**；要求"抓取学院网站"。
本连接器抓取学院网站各栏目的**列表页**（标题/链接），支持关键词过滤。

设计（对齐官网 cug_news_search 策略：实时检索、不抓正文、限速防封禁）：
    - 学院列表：内置 40 个学院/研究机构（名称 + 首页 URL，来自官网组织机构页实测）；
    - 抓取流程：匹配学院 → 抓首页提取全部栏目链接 → 串行抓各栏目列表页 → 关键词过滤；
    - 栏目识别：首页 `<a href="xxx.htm">栏目名</a>`，栏目名含"通知/公告/新闻/动态/活动/
      工作/教学/学生/招生/学术/党建/培养"等词即视为内容栏目（用户选择"全部栏目"）；
    - 条目解析：栏目页 `info/<栏目ID>/<文章ID>.htm` 链接（地大统一 CMS 结构，
       实测 dxy/au 两个学院一致）；标题清洗标签与尾部日期；
    - 合规红线：只取公开列表页的标题/链接，不抓正文详情、不批量（低频限速）。
"""

from __future__ import annotations

import re
import threading
import time

import httpx

from app.rate_limit import CircuitBreaker, backoff_retry, get_rate_limiter
from connectors.base import tool_error, tool_info

# 内置学院列表（名称 + 首页 URL；来源：官网「组织机构」页  实测）
COLLEGES: list[dict] = [
    {"name": "地球与行星科学学院", "url": "http://dxy.cug.edu.cn/"},
    {"name": "资源学院", "url": "http://zyxy.cug.edu.cn/"},
    {"name": "紧缺战略矿产资源协同创新中心", "url": "https://xtcx.cug.edu.cn/"},
    {"name": "材料与化学学院", "url": "http://chxy.cug.edu.cn/"},
    {"name": "纳米矿物材料及应用教育部工程研究中心", "url": "https://ngm.cug.edu.cn/"},
    {"name": "环境学院", "url": "http://ses.cug.edu.cn/"},
    {"name": "地下水质与健康教育部重点实验室", "url": "https://gwaterlab.cug.edu.cn/"},
    {"name": "工程学院", "url": "http://gcxy.cug.edu.cn/"},
    {"name": "地球物理与空间信息学院", "url": "http://dkxy.cug.edu.cn/"},
    {"name": "海洋学院", "url": "http://cmst.cug.edu.cn/"},
    {"name": "机械与电子信息学院", "url": "http://jidian.cug.edu.cn/"},
    {"name": "人工智能与自动化学院", "url": "http://au.cug.edu.cn/"},
    {"name": "经济管理学院", "url": "http://jgxy.cug.edu.cn/"},
    {"name": "外国语学院", "url": "http://wyxy.cug.edu.cn/"},
    {"name": "地理与信息工程学院", "url": "http://xgxy.cug.edu.cn/"},
    {"name": "数学与物理学院", "url": "http://slxy.cug.edu.cn/"},
    {"name": "珠宝学院", "url": "http://zbxy.cug.edu.cn/"},
    {"name": "公共管理学院", "url": "http://ggxy.cug.edu.cn/"},
    {"name": "教育研究院", "url": "https://jyyjy.cug.edu.cn/index.htm"},
    {"name": "计算机学院", "url": "http://cs.cug.edu.cn/"},
    {"name": "体育学院", "url": "http://ty.cug.edu.cn/"},
    {"name": "艺术与传媒学院", "url": "http://sac.cug.edu.cn/"},
    {"name": "马克思主义学院", "url": "http://mkszyxy.cug.edu.cn/"},
    {"name": "李四光学院", "url": "http://lsgxy.cug.edu.cn/"},
    {"name": "未来技术学院", "url": "https://sft.cug.edu.cn/"},
    {"name": "先进技术研究院", "url": "https://xjjsyjy.cug.edu.cn/"},
    {"name": "地质探测与评估教育部重点实验室", "url": "https://jslab.cug.edu.cn/index.htm"},
    {"name": "自然资源调查研究院", "url": "https://ddy.cug.edu.cn/"},
    {"name": "地质过程与成矿预测全国重点实验室", "url": "https://gpmr.cug.edu.cn/"},
    {"name": "地质微生物与环境全国重点实验室", "url": "https://gmec.cug.edu.cn/"},
    {"name": "深层地热富集机理与高效开发全国重点实验室", "url": "https://energy.cug.edu.cn/"},
    {"name": "国家地理信息系统工程技术研究中心", "url": "https://gis.cug.edu.cn/"},
    {"name": "湖北巴东地质灾害国家野外科学观测研究站", "url": "https://tgrc.cug.edu.cn/"},
    {"name": "国际教育学院", "url": "http://iec.cug.edu.cn/"},
    {"name": "远程与继续教育学院", "url": "http://yjxy.cug.edu.cn/"},
    {"name": "国家卓越工程师学院", "url": "https://zhuoy.cug.edu.cn/index.htm"},
    {"name": "安哈尔特智能工程与可持续发展学院", "url": "https://cug-anhalt.cug.edu.cn/"},
    {"name": "内蒙古研究院", "url": "https://imit.cug.edu.cn/"},
    {"name": "工程创新训练中心", "url": "https://gxzx.cug.edu.cn/"},
]

# 栏目名识别：首页栏目块链接文字含以下词即视为内容栏目（用户选择"全部栏目"）
_COLUMN_KEYWORDS = (
    "通知", "公告", "新闻", "动态", "活动", "工作", "教学", "学生",
    "招生", "就业", "研究", "学术", "党建", "培养", "信息", "服务",
)
# 导航噪音：首页里常见的非内容链接文字（排除）
_NAV_NOISE = (
    "首页", "返回", "更多", "more", "english", "联系我们", "学校主页",
    "信息门户", "登录", "注册", "设为首页", "收藏", "友情链接", "网站地图",
    "搜索", "wap", "手机", "微博", "微信", "邮箱", "旧版",
)
# 单栏目最多返回条数（防刷屏）
MAX_PER_COLUMN = 8
# 栏目抓取上限（学院网站栏目通常 6~10 个；超过时按出现顺序截断，避免单次调用过慢）
MAX_COLUMNS = 12
# 缓存有效期（秒）：同一学院栏目/条目在缓存期内不重复抓取
CACHE_TTL = 300.0
# 请求间隔（秒）：低频防封禁（栏目串行抓取，间隔小些保证总时长可控）
INTERVAL = 1.2
JITTER = 0.6

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_limiter = get_rate_limiter("cug_college", interval=INTERVAL, jitter=JITTER)
_breaker = CircuitBreaker()


class _CollegeCache:
    """单个学院的栏目链接/条目缓存（TTL 过期，线程安全）。"""

    def __init__(self) -> None:
        self.columns: dict[str, str] = {}   # 栏目URL -> 栏目名
        self.items: dict[str, list[dict]] = {}  # 栏目URL -> 条目列表
        self.expire_at: float = 0.0


_cache_lock = threading.Lock()
_caches: dict[str, _CollegeCache] = {}


def match_colleges(query: str) -> list[dict]:
    """按名称模糊匹配学院（query 含于名称，或名称含 query，取交集更精确）。

    返回匹配的学院列表（可能多个，由调用方提示用户明确）。
    """
    q = str(query).strip()
    if not q:
        return []
    hits = []
    for c in COLLEGES:
        name = c["name"]
        if q in name or name in q:
            hits.append(c)
    return hits


def _fetch_html(url: str) -> str:
    """限速 + 退避抓取页面 HTML；失败抛异常（调用方统一处理）。"""
    def do_fetch() -> str:
        with _limiter:
            resp = httpx.get(url, headers=_HEADERS, timeout=20, follow_redirects=True)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            return resp.text
    return backoff_retry(do_fetch, retries=2, base_delay=1.0)


def _find_columns(html: str, home_url: str) -> dict[str, str]:
    """从学院首页提取全部内容栏目链接：{绝对URL: 栏目名}（去重、排除导航噪音）。

    识别规则（实测 dxy/au 两个学院一致）：
        - 首页栏目块形如 <a href="tzgg.htm">通知公告</a> 或 <a href="djgz/tzgg.htm">通知公告</a>；
        - 栏目名是 2~12 字中文，含"通知/公告/新闻/动态/活动/工作/教学/学生"等词；
        - 排除：info 文章页链接、站外链接（非 *.cug.edu.cn）、导航噪音文字。
    """
    pat = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>\s*([^<]{2,14})\s*</a>', re.S)
    cols: dict[str, str] = {}
    for href, text in pat.findall(html):
        name = re.sub(r"\s+", "", text).strip()
        if not name or len(name) > 12:
            continue
        if any(n in name for n in _NAV_NOISE):
            continue
        low = href.lower()
        # 排除 info 文章页与无 .htm 后缀的链接
        if "info/" in low and low.endswith(".htm"):
            continue
        if not low.endswith(".htm"):
            continue
        # 排除站外绝对链接（非 cug.edu.cn 域）
        if low.startswith(("http://", "https://")) and ".cug.edu.cn" not in low:
            continue
        # 栏目名需含内容关键词（"全部栏目"策略：通知/新闻/动态/活动/工作等）
        if not any(k in name for k in _COLUMN_KEYWORDS):
            continue
        full = str(httpx.URL(home_url).join(href))
        cols.setdefault(full, name)
    return cols


def _parse_items(html: str, page_url: str) -> list[dict]:
    """从栏目列表页提取条目：{title, link}（地大统一 CMS info/<id>/<artid>.htm）。

    清洗：去除标题内 HTML 标签、压缩空白、去掉标题尾部的日期（如 "标题"）。
    去重按链接。仅取列表页标题/链接（用户选择"仅列表标题"），不抓正文。
    """
    pat = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
    out: list[dict] = []
    seen: set[str] = set()
    for href, title in pat.findall(html):
        low = href.lower()
        if "info/" not in low or not low.endswith(".htm"):
            continue
        t = re.sub(r"<[^>]+>", "", title).strip()
        t = re.sub(r"\s+", " ", t)
        # 去掉标题尾部的日期（部分学院列表把日期拼在标题后，如 "标题 "）
        t = re.sub(r"(20\d{2}[-.]\d{1,2}[-.]\d{1,2})\s*$", "", t).strip()
        if not t:
            continue
        link = str(httpx.URL(page_url).join(href))
        if link in seen:
            continue
        seen.add(link)
        out.append({"title": t, "link": link})
    return out


def _get_cached(url: str) -> _CollegeCache:
    """按学院首页 URL 取缓存对象（不存在则创建）。"""
    with _cache_lock:
        if url not in _caches:
            _caches[url] = _CollegeCache()
        return _caches[url]


def cug_college_search(college: str, keyword: str = "") -> str:
    """检索学院网站各栏目（实时抓取首页栏目 + 列表页，关键词过滤标题）。

    参数：
        college: 学院名（支持简称，如"自动化"；模糊匹配，多命中时提示明确）
        keyword: 过滤关键词（匹配条目标题；空串=返回该学院全部栏目最新条目）
    返回：按栏目分组的多行文本（标题+链接），失败/无匹配返回以 [错误] 开头的提示。
    """
    if not _breaker.allow():
        return tool_error("cug_college_search", "连接器处于熔断冷却中，请稍后再试")
    hits = match_colleges(college)
    if not hits:
        return tool_error("cug_college_search", f"未找到「{college}」对应的学院（/live_nav 可查看全部学院列表）")
    if len(hits) > 1:
        names = "、".join(h["name"] for h in hits[:8])
        return tool_info("cug_college_search", f"「{college}」匹配到多个学院，请明确：{names}")
    coll = hits[0]
    home_url = coll["url"]
    name = coll["name"]
    kw = str(keyword).strip()

    # 1) 首页 → 栏目链接（缓存期内复用）
    cache = _get_cached(home_url)
    now = time.monotonic()
    if cache.expire_at < now or not cache.columns:
        try:
            html = _fetch_html(home_url)
        except Exception as exc:  # noqa: BLE001
            _breaker.record_failure()
            return tool_error("cug_college_search", f"抓取 {name} 首页失败：{exc}")
        cache.columns = _find_columns(html, home_url)
        cache.items = {}
        cache.expire_at = now + CACHE_TTL
        _breaker.record_success()
    columns = cache.columns
    if not columns:
        return tool_info("cug_college_search", f"未能从 {name} 首页识别到内容栏目（站点结构可能特殊，可访问 {home_url} 查看）")

    # 2) 串行抓各栏目列表页（全局限速防封禁；缓存期内不重复抓）
    lines: list[str] = []
    total = 0
    for col_url, col_name in list(columns.items())[:MAX_COLUMNS]:
        items = cache.items.get(col_url)
        if items is None:
            try:
                col_html = _fetch_html(col_url)
                items = _parse_items(col_html, col_url)
            except Exception:  # noqa: BLE001 单个栏目失败不阻断其它栏目
                items = []
            cache.items[col_url] = items
        # 关键词过滤 + 截断
        matched = [it for it in items if not kw or kw in it["title"]]
        if not matched:
            continue
        lines.append(f"\n▎{col_name}")
        for i, it in enumerate(matched[:MAX_PER_COLUMN], 1):
            lines.append(f"  {i}. {it['title']}\n     {it['link']}")
        total += min(len(matched), MAX_PER_COLUMN)
    if not lines:
        hint = f"「{kw}」" if kw else ""
        return tool_info("cug_college_search", f"{name} 各栏目未找到{hint}相关条目")
    head = f"学院网站检索「{name}」（{home_url}）" + (f"，关键词「{kw}」" if kw else "") + f"：共 {total} 条"
    return head + "\n" + "\n".join(lines) + "\n\n来源：学院官网公开栏目列表（仅标题/链接，详情请点链接查看）"


def to_tool_spec():
    """把学院网站检索封装为 ToolSpec（供工具注册表注册，LLM 可调用）。

    结构化参数：college 必填（学院名/简称）、keyword 可选（过滤关键词）。
    """
    from app.agent.tools import ToolSpec

    return ToolSpec(
        name="cug_college_search",
        description="检索中国地质大学各学院官网的内容栏目（通知公告/学院新闻/学术动态等），返回条目标题与链接（如想知道某学院的最新通知/新闻时调用）",
        fn=cug_college_search,
        parameters={
            "type": "object",
            "properties": {
                "college": {"type": "string", "description": "学院名或简称（如「自动化」「计算机」「环境学院」）"},
                "keyword": {"type": "string", "description": "过滤关键词（匹配条目标题，如「招生」「实习」；可省略）"},
            },
            "required": ["college"],
        },
    )
