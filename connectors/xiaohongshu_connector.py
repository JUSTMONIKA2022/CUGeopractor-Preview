# -*- coding: utf-8 -*-
"""小红书内容连接器（L3 高风险渠道，用户自配模式）。

设计说明（对应渠道规划二期/后期评估）：
    - 小红书强反爬（签名算法+登录态），匿名抓取极不稳定；
    - 本连接器采用"用户自配"模式，按以下优先级降级：
        1) 外部服务模式（推荐，BYO）：用户自行部署 xhs 数据服务（如
           github.com/ReaJason/xhs 的签名服务 + 薄封装），配置 XHS_API_BASE，
           本连接器仅以通用 HTTP 客户端调用其 /search 接口——代码不含任何
           签名/逆向实现，也不携带第三方项目代码；
        2) 用户自带 Cookie 模式：用户在自己浏览器登录后导出会话 Cookie
           （XHS_COOKIE，仅存本机），连接器直连小红书 Web 搜索接口；
        3) 均未配置时给出明确指引。
    - 使用说明：仅检索公开内容；不破解签名算法、不内置绕过平台安全措施的逻辑。
"""

from __future__ import annotations

import json
import time
from urllib.parse import quote

import httpx

from app.rate_limit import CircuitBreaker, backoff_retry, get_rate_limiter
from connectors.base import tool_error, tool_info
from connectors.session_connector import _env_or_dotenv

# 小红书搜索接口（Web 端，需登录态；仅"用户 Cookie 直连模式"使用）
XHS_SEARCH_URL = "https://edith.xiaohongshu.com/api/sns/web/v1/search/notes"
# 请求间隔（秒）：低频
INTERVAL = 8.0
JITTER = 2.0

_limiter = get_rate_limiter("xiaohongshu", interval=INTERVAL, jitter=JITTER)
_breaker = CircuitBreaker()


def _external_search(keyword: str, count: int = 8) -> str:
    """调用用户自配的外部 xhs 数据服务（BYO 模式），返回结构化摘要文本。

    接口约定（本连接器定义，用户侧薄封装按其实现，见 docs/xhs-service-guide.md）：
        GET {XHS_API_BASE}/search?keyword=xxx&count=n
        成功返回：{"code": 0, "data": {"items": [{"title","desc","url"}, ...]}}
        失败返回：{"code": <非0>, "message": "错误说明"}

    说明：本函数只是通用 HTTP 客户端——请求由用户自部署的服务处理，
    项目代码不包含签名生成/逆向等任何实现。
    """
    base = _env_or_dotenv("XHS_API_BASE")
    if not base:
        return ""
    try:
        resp = httpx.get(
            f"{base.rstrip('/')}/search",
            params={"keyword": keyword, "count": max(1, min(20, count))},
            timeout=15,
        )
        if resp.status_code != 200:
            return tool_error("xiaohongshu_search", f"外部小红书服务返回 HTTP {resp.status_code}")
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 网络/超时/JSON 解析错误统一处理
        return tool_error("xiaohongshu_search", f"外部小红书服务调用失败：{exc}")

    if body.get("code") != 0:
        return tool_error("xiaohongshu_search", f"外部小红书服务返回错误：{body.get('message', body.get('code'))}")

    items = body.get("data", {}).get("items", []) or []
    if not items:
        return tool_info("xiaohongshu_search", f"未找到与「{keyword}」相关的小红书内容")

    lines = []
    for idx, item in enumerate(items[:count], start=1):
        title = (item.get("title") or "").strip()
        desc = (item.get("desc") or "").replace("\n", " ").strip()[:100]
        url = item.get("url") or ""
        lines.append(f"{idx}. {title}\n   摘要：{desc}\n   链接：{url}")
    return "\n\n".join(lines)


def xhs_search(keyword: str = "") -> str:
    """在小红书检索公开内容（用户自配模式），返回结构化摘要。

    数据获取优先级（用户自配，风险自担）：
        1) 外部服务模式：配置 XHS_API_BASE（用户自行部署的 xhs 数据服务）；
        2) 用户 Cookie 模式：配置 XHS_COOKIE（浏览器登录后导出，仅存本机）。
    均未配置时返回配置指引；账号级风控（code=300011）时提示更换小号。

    参数：
        keyword: 检索关键词（如"中国地质大学（武汉） 宿舍"）
                 （默认空串：/live_xhs 无参或 LLM 缺参时不再抛 TypeError）
    返回：
        多行文本，每条含标题/摘要/链接；失败返回以 [错误] 开头提示。
    """
    if not _breaker.allow():
        return tool_error("xiaohongshu_search", "连接器处于熔断冷却中，请稍后再试")

    # 优先外部服务模式（BYO）：用户自配的 xhs 服务已处理签名/登录态
    external = _external_search(keyword)
    if external:
        return external

    # 其次用户自带 Cookie 模式
    cookie = _env_or_dotenv("XHS_COOKIE")
    if not cookie:
        return (
            tool_error("xiaohongshu_search", "小红书数据获取需用户自配（二选一）：\n")
            + "  ① 外部服务模式：自行部署 xhs 数据服务后设置环境变量 XHS_API_BASE"
            "（见 docs/xhs-service-guide.md）；\n"
            "  ② Cookie 模式：浏览器登录后 F12 导出 Cookie 设置 XHS_COOKIE"
            "（仅本机使用，不入库）。\n"
            "  提示：若账号被风控（code=300011），需更换小号。"
        )

    def do_request() -> str:
        with _limiter:
            resp = httpx.post(
                XHS_SEARCH_URL,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126",
                    "Content-Type": "application/json;charset=UTF-8",
                    "Cookie": cookie,
                    "Origin": "https://www.xiaohongshu.com",
                    "Referer": "https://www.xiaohongshu.com/",
                },
                json={"keyword": keyword, "page": 1, "page_size": 8, "search_id": "", "sort": "general", "note_type": 0},
                timeout=15,
            )
        if resp.status_code in (401, 403, 429):
            raise RuntimeError(f"触发反爬/登录失效 HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return resp.text

    try:
        body = backoff_retry(do_request, retries=1, base_delay=1.0)
        data = json.loads(body)
    except Exception as exc:  # noqa: BLE001
        _breaker.record_failure()
        return tool_error("xiaohongshu_search", f"抓取失败：{exc}")

    # 识别账号级风控（300011）：此前曾把该错误误报为"未找到"，导致用户无法定位根因
    if data.get("code") == 300011 or data.get("success") is False:
        _breaker.record_failure()
        msg = data.get("msg") or "当前账号存在异常"
        return (
            tool_error("xiaohongshu_search", f"账号风控（code=300011）：{msg}\n")
            + "  当前 Cookie 关联的账号已被平台标记，需更换更干净的小号。"
            "（本机环境（IP/设备指纹）登录的新号也可能被关联风控，建议换设备/移动网络）"
        )

    items = data.get("data", {}).get("items", []) or []
    if not items:
        _breaker.record_success()
        return tool_info("xiaohongshu_search", f"未找到与「{keyword}」相关的公开内容（可能需更新 Cookie 或被限流）")

    _breaker.record_success()
    lines = []
    for idx, item in enumerate(items[:8], start=1):
        note = item.get("note_card", {}) or {}
        title = note.get("display_title", "") or note.get("desc", "")[:40]
        desc = (note.get("desc", "") or "").replace("\n", " ").strip()[:100]
        note_id = item.get("id", "") or note.get("note_id", "")
        link = f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else ""
        lines.append(f"{idx}. {title}\n   摘要：{desc}\n   链接：{link}")
    return "\n\n".join(lines)


def to_tool_spec():
    """把小红书检索封装为 ToolSpec（供工具注册表注册）。

    结构化参数说明（对应待办方向 A 路线一）：
        - 声明 keyword（必填），fn 直接引用 xhs_search，
          参数名与 parameters 的 key 一一对应，ToolRegistry.run_tool_call
          会以具名参数（**kwargs）方式调用；
        - 小红书接口仅支持关键词检索（无额外的可暴露参数），故只声明一个必填参数。
    """
    from app.agent.tools import ToolSpec

    return ToolSpec(
        name="xiaohongshu_search",
        description="在小红书检索公开内容（需用户自带登录 Cookie），返回标题、摘要与链接",
        fn=xhs_search,
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "检索关键词（如「中国地质大学（武汉） 宿舍」）"},
            },
            "required": ["keyword"],
        },
    )
