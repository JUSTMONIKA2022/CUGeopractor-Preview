# -*- coding: utf-8 -*-
"""百度贴吧抓取连接器（L3 社区公开内容渠道）。

设计说明（对应渠道规划二期）：
    - 目标贴吧：中国地质大学武汉吧（可配置）
    - 抓取方式：网页抓取（匿名，不走登录），仅取公开帖子列表
    - 反爬缓解（关键）：
        1) 完整浏览器请求头（User-Agent/Referer/Accept/Sec-Fetch 系列），模拟真实浏览器指纹；
        2) Cookie 预热：先访问贴吧首页拿到 BAIDUID 等会话 Cookie，再带 Cookie 抓列表页，
           避免"裸请求"被反爬直接 403；
        3) 备用端点：列表页被拦时退回移动端页（tieba.baidu.com/mo/q/m），提高可用性；
        4) 全局限速器（间隔≥5s + 随机抖动）、指数退避、熔断，低频防封禁。
    - 使用说明：被贴吧风控拦截时如实返回状态提示，不执行页面内 JS 挑战
      （不内置绕过平台安全措施的逻辑）。
    - 用户会话模式：可配置 TIEBA_COOKIE（浏览器登录贴吧后 F12 导出的会话
      Cookie，仅存本机），连接器将优先复用该真实会话访问，规避匿名动态风控。
    - 网络环境说明：贴吧对出口 IP 有动态风控（间歇性），VPN/代理等共享出口
      更易触发"百度安全验证"；拦截时提示用户自行选择是否更换网络后重试。
"""

from __future__ import annotations

import json
import re

import httpx

from app.rate_limit import CircuitBreaker, backoff_retry, get_rate_limiter
from connectors.base import tool_error, tool_info
from connectors.session_connector import _env_or_dotenv

# 贴吧抓取基地址
TIEBA_BASE = "https://tieba.baidu.com"
# 贴吧首页（用于 Cookie 预热）
TIEBA_HOME = f"{TIEBA_BASE}/index.html"
# 目标贴吧（默认"中国地质大学武汉吧"，可配置）
DEFAULT_KW = "中国地质大学武汉吧"
# 请求间隔（秒）：低频防封禁
INTERVAL = 5.0
JITTER = 1.5
# 单次抓取的帖子数上限（要求多返回一些：8→15）
MAX_POSTS = 15

# 完整的浏览器请求头（模拟真实 Chrome，降低被反爬识别概率）
# 说明：仅设 User-Agent 不足以通过贴吧反爬，需补齐 Referer/Sec-Fetch 等指纹头
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

_limiter = get_rate_limiter("tieba", interval=INTERVAL, jitter=JITTER)
_breaker = CircuitBreaker()


def _warmup_and_fetch(client: httpx.Client, url: str, params: dict | None) -> str:
    """抓取贴吧目标页（单次会话内），支持两种模式。

    模式一（用户会话模式，优先）：用户配置了 TIEBA_COOKIE（从已登录贴吧的
    浏览器 F12 导出，仅存本机 .env/环境变量），直接携带该 Cookie 请求目标页，
    不再匿名预热。本质是"复用用户本人已通过贴吧验证的真实会话"访问公开帖子，
    规避匿名抓取被动态风控拦截；代码中不包含任何绕过验证的逻辑。

    模式二（匿名模式，默认）：先 GET 贴吧首页拿 BAIDUID 等 Cookie，再带
    Cookie 抓目标列表页；对 403/429 抛出可重试异常，其余非 200 抛 HTTP 错误。
    """
    user_cookie = _env_or_dotenv("TIEBA_COOKIE")
    if user_cookie:
        # 用户会话模式：复用用户已通过验证的真实会话（浏览器 F12 导出）
        headers = dict(_BROWSER_HEADERS)
        headers["Cookie"] = user_cookie
        resp = client.get(url, params=params, headers=headers, timeout=15, follow_redirects=True)
    else:
        # 第 1 步：匿名 Cookie 预热（取 BAIDUID 等），失败不致命，继续尝试抓目标页
        try:
            client.get(TIEBA_HOME, headers=_BROWSER_HEADERS, timeout=15)
        except Exception:  # noqa: BLE001 预热失败不阻断，继续抓目标页
            pass
        # 第 2 步：带会话 Cookie 抓目标列表页
        resp = client.get(url, params=params, headers=_BROWSER_HEADERS, timeout=15, follow_redirects=True)

    if resp.status_code in (403, 429):
        # 识别"百度安全验证"JS 挑战页：纯 HTTP 库无法执行 JS 绕过（本工具不内置
        # 绕过平台安全措施的逻辑）。贴吧对出口 IP 有动态风控（间歇性，VPN/代理等
        # 共享出口更易触发），此处如实返回状态提示，由用户自行决定是否更换网络重试。
        if "百度安全验证" in resp.text:
            hint = ""
            if user_cookie:
                # 用户会话模式下 403 大概率是登录态过期，给出针对性提示
                hint = "（已配置 TIEBA_COOKIE：可能是会话已过期，请重新在浏览器导出）"
            raise RuntimeError(
                "贴吧风控拦截（百度安全验证，IP 级动态风控、间歇性）："
                f"请稍后重试或更换网络环境{hint}"
            )
        raise RuntimeError(f"触发反爬 HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return resp.text


def _make_ssl_context() -> "ssl.SSLContext":
    """构造强制 TLS1.2 的 SSL 上下文。

    根因（实测诊断）：贴吧部分 CDN 节点对 TLS1.3 握手直接丢包，导致 httpx
    默认协商 TLS1.3 时 ConnectTimeout（握手超时）；强制降到 TLS1.2 后握手成功。
    裸 ssl 库默认协商 1.2 故一直能通——据此定位为 TLS 版本兼容问题。
    """
    import ssl as _ssl

    ctx = _ssl.create_default_context()
    ctx.minimum_version = _ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = _ssl.TLSVersion.TLSv1_2
    return ctx


def _fetch_html(url: str, params: dict | None = None) -> str:
    """在限速+退避下，用会话客户端预热 Cookie 并抓取 HTML。"""
    def do_request() -> str:
        with _limiter:
            # 每次抓取新建会话客户端，保证 Cookie 干净且连接复用；
            # verify 用强制 TLS1.2 上下文（规避贴吧节点对 TLS1.3 握手丢包）
            with httpx.Client(verify=_make_ssl_context()) as client:
                return _warmup_and_fetch(client, url, params)

    return backoff_retry(do_request, retries=2, base_delay=1.0)


def _parse_posts(html: str) -> list[tuple[str, str]]:
    """从贴吧 HTML 解析帖子列表，返回 [(标题, 链接), ...]。

    兼容两种结构：
        - PC 端列表页：<li class="j_thread_list" data-field='{json}'>
        - 通用链接：<a href="/p/数字" title="标题">
    """
    posts = re.findall(
        r'<li[^>]*class="j_thread_list[^"]*"[^>]*data-field=\'([^\']+)\'',
        html,
        re.S,
    )
    results: list[tuple[str, str]] = []
    if posts:
        import json as _json

        for raw in posts:
            try:
                meta = _json.loads(raw.replace("&quot;", '"'))
                title = meta.get("title", "")
                thread_id = meta.get("id", "")
                if title and thread_id:
                    results.append((title.strip(), f"{TIEBA_BASE}/p/{thread_id}"))
            except Exception:  # noqa: BLE001 单条解析失败跳过，不影响整体
                continue
        return results

    # 备用结构：直接抓 <a> 链接（/p/数字 + title）
    for href, title in re.findall(r'href="(/p/\d+)"[^>]*title="([^"]+)"', html):
        results.append((title.strip(), TIEBA_BASE + href))
    return results


def _render_posts(url: str, params: dict | None) -> list[tuple[str, str]]:
    """用 Playwright 真实浏览器渲染贴吧列表页，提取帖子标题与链接。

    背景：贴吧网页版已改版为 CSR 单页应用（`<div id="app">` + JS 异步加载），
    纯 HTTP 请求（即使带用户 Cookie）只能拿到空壳，拿不到帖子。本函数用
    真实浏览器执行 JS 渲染后再按 DOM 结构提取（复用用户 TIEBA_COOKIE）。

    提取策略（实测 DOM 结构）：
        - 帖子链接：`a[href*="/p/"]`，href 形如 https://tieba.baidu.com/p/{tid}?fr=frs；
        - 标题：优先取子元素 `.thread-title` 的文本并剔除 `.thread-tag`（置顶/精 等
          标签词），无该结构时退回链接元素的整体文本（清洗空白后取首个非空行）。

    返回：[(标题, 链接), ...]；Playwright 不可用/渲染失败返回空列表（不抛异常）。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        # 未安装 Playwright 时提示性降级：返回空，由上层走"未找到"分支
        return []

    user_cookie = _env_or_dotenv("TIEBA_COOKIE")
    # 提取脚本：剔除标题内标签词，返回 [{href, title}]
    # 注意：Playwright 的 eval_on_selector_all 要求字符串以箭头函数/函数开头
    # （不能带前导缩进或换行，否则会被当作函数体导致语法错误）
    extract_js = (
        "els => els.map(e => {"
        "const t = e.querySelector('.thread-title');"
        "let title = '';"
        "if (t) {"
        "const clone = t.cloneNode(true);"
        "clone.querySelectorAll('.thread-tag').forEach(x => x.remove());"
        "title = clone.innerText.trim();"
        "} else { title = e.innerText.trim(); }"
        "return {href: e.href, title: title};"
        "}).filter(x => x.title.length > 0)"
    )
    try:
        with sync_playwright() as pw:
            try:
                # 优先复用系统 Chrome（项目本机已验证可启动），失败退回自带 chromium
                browser = pw.chromium.launch(channel="chrome", headless=True)
            except Exception:  # noqa: BLE001
                browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                    )
                )
                if user_cookie:
                    # 字符串 Cookie 丢失 domain 信息，统一挂 .baidu.com（tieba 为其子域）
                    context.add_cookies(
                        [
                            {
                                "name": item.split("=", 1)[0].strip(),
                                "value": item.split("=", 1)[1].strip(),
                                "domain": ".baidu.com",
                                "path": "/",
                            }
                            for item in user_cookie.split(";")
                            if "=" in item
                        ]
                    )
                page = context.new_page()
                # 注意：Playwright 的 goto 不支持 params 参数，需自行拼接查询串
                from urllib.parse import urlencode as _urlencode

                full_url = f"{url}?{_urlencode(params)}" if params else url
                page.goto(full_url, timeout=30000, wait_until="domcontentloaded")
                # 等待 SPA 渲染出帖子列表（含网络请求与前端渲染时间）
                page.wait_for_timeout(6000)
                items = page.eval_on_selector_all('a[href*="/p/"]', extract_js)
            finally:
                browser.close()

        results: list[tuple[str, str]] = []
        for item in items:
            title = re.sub(r"\s+", " ", item.get("title", "")).strip()
            href = item.get("href", "")
            m = re.search(r"/p/(\d+)", href)
            link = f"{TIEBA_BASE}/p/{m.group(1)}" if m else href
            if title:
                results.append((title, link))
        return results
    except Exception:  # noqa: BLE001 渲染失败不致命，交由上层处理
        return []


def _external_search(keyword: str, kw: str) -> list[tuple[str, str]]:
    """外部贴吧数据服务模式（BYO）：调用用户自配服务的 /forum/thread 接口。

    背景：贴吧网页版为 CSR + 动态风控，纯 HTTP/Playwright 渲染均可能被拦。
    用户可自部署 Tieba-API-SCF（github.com/Dilettante258/Tieba-API-SCF，Docker
    一行启动，服务端内置 BDUSS）并把地址配置为 TIEBA_API_BASE；本项目仅作
    通用 HTTP 客户端调用其公开 GET 接口，**不含任何签名/逆向/绕过代码**。

    接口约定（Tieba-API-SCF v3）：
        GET {TIEBA_API_BASE}/forum/thread?fname=<贴吧名>&page=1&rn=<条数>
        成功返回：{"forum":..., "threadList": [{"tid":..., "title": "标题", ...}]}

    返回：
        [(标题, 帖子链接), ...]；未配置服务或调用失败返回空列表（由上层降级）。
    """
    base = _env_or_dotenv("TIEBA_API_BASE")
    if not base:
        return []
    try:
        with _limiter:
            resp = httpx.get(
                f"{base.rstrip('/')}/forum/thread",
                params={"fname": kw.rstrip("吧"), "page": 1, "rn": MAX_POSTS * 2},
                timeout=15,
            )
        if resp.status_code != 200:
            return []
        body = resp.json()
    except Exception:  # noqa: BLE001 网络/超时/JSON 解析失败均按不可用降级
        return []

    items = body.get("threadList", []) or []
    results: list[tuple[str, str]] = []
    for item in items:
        title = (item.get("title") or "").strip()
        tid = item.get("tid") or item.get("id") or ""
        link = f"{TIEBA_BASE}/p/{tid}" if tid else ""
        if title:
            results.append((title, link))
    return results


def _first_line(text: str, limit: int = 40) -> str:
    """取文本首行并截断（SSE 命中内容可能很长，只取首行做兜底标题）。"""
    first = (text or "").strip().splitlines()
    return (first[0] if first else "")[:limit]


def _split_fallback(word: str) -> str | None:
    """无分隔连续词的回退拆词：优先"前 3 字 + 剩余"成两词（逗号连接）。

    背景（实测）：/forum/search 的 keywords 为逗号分隔多词列表，
    整短语精确匹配为 0（"自动化宿舍" 命中 0），而"自动化,宿舍"可命中 50 条。
    用户/LLM 输入常为无分隔的连续词（如"自动化宿舍"），re.split 无法拆分，
    这里按中文常见词组习惯启发式拆分：前 3 字 + 剩余（两段均 ≥2 字才拆），
    拆成逗号分隔的多词交给服务端 OR 匹配。

    返回："前段,后段" 字符串；无法合理拆分返回 None（保持整词）。
    """
    n = len(word)
    for cut in (3, 2):
        if n - cut >= 2:  # 两段均至少 2 字
            return f"{word[:cut]},{word[cut:]}"
    return None


def _forum_search(keyword: str, kw: str) -> list[tuple[str, str]]:
    """真·吧内搜索（Tieba-API-SCF /forum/search，SSE 流式返回）。

    背景（实测暴露）：
        - `/forum/thread` 只返回**最新帖子列表**，配合本地 `keyword in title` 过滤
          只能命中近期帖子；用户手动在贴吧搜"自动化"能搜出大量历史帖（全库搜索）。
        - Tieba-API-SCF v3 提供 `/forum/search`：服务端内置 BDUSS，按关键词扫描
          吧内帖子（count 张）并以 **SSE**（text/event-stream）流式回传：
              data: {"type":"threads","count":100}
              data: {"type":"progress"}
              data: {"type":"match","posts":[{"tid":..., "threadTitle":..., "content":..., ...}]}
              data: {"type":"done","stats":{...}}
          本函数消费 SSE 流，提取所有 match 事件中的命中帖子。

    返回：
        [(标题, 帖子链接), ...]；服务未配置/调用失败返回空列表（由上层降级到列表过滤）。
    """
    base = _env_or_dotenv("TIEBA_API_BASE")
    if not base:
        return []
    try:
        with _limiter:
            # 关键词参数：/forum/search 的 keywords 为**逗号分隔**的多词列表——
            # 实测整短语精确匹配失败（"自动化宿舍" 0 条），逗号分隔可 OR 匹配
            # 多个词（"自动化,宿舍" 50 条），对齐贴吧客户端模糊搜索体验。
            # 拆分顺序：① 已有分隔符（空格/逗号/顿号/分号）直接拆分；
            # ② 无分隔的连续长词（≥4 字）启发式拆词（前 3 字+剩余）。
            words = [p for p in re.split(r"[\s,，、;；]+", keyword) if p]
            if not words:
                words = [keyword]
            if len(words) == 1 and len(keyword) >= 4:
                fallback = _split_fallback(keyword)
                if fallback:
                    words = fallback.split(",")
            resp = httpx.get(
                f"{base.rstrip('/')}/forum/search",
                # count=扫描帖子数（1~300，越大越能覆盖历史帖但耗时越长）；
                # depth=first 只取首楼层（关键词通常出现在标题/首楼）；sort=1 最新回复优先
                params={
                    "fname": kw.rstrip("吧"),
                    "keywords": ",".join(words),
                    "count": "100",
                    "depth": "first",
                    "sort": "1",
                },
                timeout=120,  # SSE 长任务：允许较长时间（100 帖扫描约 5~30 秒）
            )
        if resp.status_code != 200:
            return []
    except Exception:  # noqa: BLE001 网络/超时均按不可用降级
        return []

    results: list[tuple[str, str]] = []
    seen: set[str] = set()  # 按 tid 去重：同一帖子可能被多个 match 事件命中（不同楼层/内容）
    for line in resp.text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            evt = json.loads(line[len("data:"):].strip())
        except Exception:  # noqa: BLE001 单条事件解析失败跳过
            continue
        if evt.get("type") != "match":
            continue
        # match 事件：posts 为该条命中的帖子（可能多条）；threadTitle 可能为空，
        # 空时退回 content 首行做标题，保证每条都有可读标题
        for item in evt.get("posts", []) or []:
            title = (item.get("threadTitle") or "").strip() or _first_line(item.get("content") or "")
            tid = item.get("tid") or ""
            if title and tid and tid not in seen:
                seen.add(tid)
                results.append((title, f"{TIEBA_BASE}/p/{tid}"))
    return results


def tieba_search(keyword: str, kw: str = DEFAULT_KW) -> str:
    """抓取贴吧公开帖子（按贴吧+关键词过滤），返回结构化摘要。

    检索策略（改造，对齐用户手动搜索体验）：
        1. **首选：真·吧内搜索** `/forum/search`（Tieba-API-SCF，SSE 流式）——
           服务端内置 BDUSS 扫描吧内帖子（count=100），可命中**历史帖**，
           与用户手动在贴吧搜索一致；结果已按关键词匹配，直接使用；
        2. **降级：列表页标题过滤**（外部服务列表 / 网页 / 移动端 / 渲染）——
           /forum/search 不可用（服务未启动/超时）时退回"最新帖列表 + 本地过滤"，
           仅能覆盖近期帖子。

    参数：
        keyword: 检索关键词（如"宿舍""自动化"）
        kw:      目标贴吧名（默认"中国地质大学武汉吧"）
    返回：
        多行文本，每条含标题/链接；失败返回以 [错误] 开头提示。
    """
    if not _breaker.allow():
        return tool_error("tieba_search", "连接器处于熔断冷却中，请稍后再试")

    # 首选：真·吧内搜索（/forum/search，可搜历史帖）。结果已按关键词匹配，无需再过滤
    posts = _forum_search(keyword, kw)
    if posts:
        filtered = posts[:MAX_POSTS]
        _breaker.record_success()
        lines = [f"{i}. {title}\n   链接：{link}" for i, (title, link) in enumerate(filtered, 1)]
        return "\n\n".join(lines)

    # ---- 降级链：列表页（只能覆盖近期帖子）+ 本地关键词过滤 ----
    last_err = None
    posts = _external_search(keyword, kw)
    if not posts:
        try:
            html = _fetch_html(f"{TIEBA_BASE}/f", params={"kw": kw, "ie": "utf-8"})
            posts = _parse_posts(html)
        except Exception as exc:  # noqa: BLE001
            posts = []
            last_err = exc
        else:
            last_err = None

    # 备用：PC 端被反爬拦截/无结果时，退回移动端页（反爬相对宽松）
    if not posts:
        try:
            html = _fetch_html(f"{TIEBA_BASE}/mo/q/m", params={"word": kw})
            posts = _parse_posts(html)
            last_err = None
        except Exception as exc:  # noqa: BLE001
            last_err = exc

    # 备用2：贴吧网页版已改版为 CSR（JS 渲染），纯 HTTP 拿不到帖子时，
    # 用 Playwright 真实浏览器渲染后按 DOM 提取（复用用户 TIEBA_COOKIE）
    if not posts:
        posts = _render_posts(f"{TIEBA_BASE}/f", {"kw": kw, "ie": "utf-8"})
        if posts:
            last_err = None

    if last_err is not None and not posts:
        _breaker.record_failure()
        return tool_error("tieba_search", f"抓取失败：{last_err}")

    # 列表结果需本地过滤（列表型结果只含近期帖，关键词命中率有限）
    filtered = [(t, l) for (t, l) in posts if not keyword or keyword in t][:MAX_POSTS]

    if not filtered:
        _breaker.record_success()
        if posts:
            # 数据源正常返回了帖子列表，只是没有匹配关键词的帖子：
            # 如实说明"无匹配"，不要把正常无结果误报为反爬（能力边界实测发现的误导点）
            return tool_info(
                "tieba_search",
                f"贴吧「{kw}」未找到与「{keyword}」相关的公开帖子（当前帖子列表中无匹配，可换个关键词再试）",
            )
        # 数据源为空（网页抓取/渲染均未拿到任何帖子）：保留"可能被反爬限制"提示
        return tool_info(
            "tieba_search",
            f"贴吧「{kw}」未找到与「{keyword}」相关的公开帖子（可能被反爬限制）",
        )

    _breaker.record_success()
    lines = [f"{i}. {title}\n   链接：{link}" for i, (title, link) in enumerate(filtered, 1)]
    return "\n\n".join(lines)


def to_tool_spec():
    """把贴吧抓取封装为 ToolSpec（供工具注册表注册）。

    结构化参数说明（对应待办方向 A 路线一，解决 kw 无法暴露的问题）：
        - 声明 keyword（必填）+ kw（可选），让 LLM 既能检索关键词，也能指定贴吧名
          （默认"中国地质大学武汉吧"），彻底释放贴吧工具能力；
        - fn 直接引用 tieba_search，参数名与 parameters 的 key 一一对应，
          ToolRegistry.run_tool_call 会以具名参数（**kwargs）方式调用；
        - kw 为可选：LLM 不传时沿用函数默认值 DEFAULT_KW，无需特殊处理。
    """
    from app.agent.tools import ToolSpec

    return ToolSpec(
        name="tieba_search",
        description="抓取百度贴吧（默认中国地质大学武汉吧，可指定其他贴吧）的公开帖子，返回标题与链接",
        fn=tieba_search,
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "检索关键词（如「宿舍」「食堂」）"},
                "kw": {"type": "string", "description": "目标贴吧名（默认「中国地质大学武汉吧」）"},
            },
            "required": ["keyword"],
        },
    )
