# -*- coding: utf-8 -*-
"""B站社区内容连接器（L2 公共搜索接口渠道）。

设计说明（对应渠道规划一期）：
    - 使用 B站公开搜索接口（无需密钥，视频搜索类），返回标题/简介/播放量/链接；
    - 通过 ToolSpec 注册为 Agent 白名单工具，LLM 用 function calling 调用；
    - 符合"来源+摘要"引用策略：仅摘要式整理，附视频链接。
"""

from __future__ import annotations

import json
import urllib.parse

from urllib.request import Request, urlopen

from connectors.base import tool_error, tool_info

# B站搜索接口（公开视频搜索，无需登录态）
BILI_SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
TIMEOUT = 12


def bilibili_search(query: str, count: int = 8) -> str:
    """调用 B站视频搜索，返回结构化摘要文本（供 LLM 整理回答）。

    参数：
        query: 检索关键词（如"中国地质大学（武汉）"）
        count: 返回条数（默认 8）
    返回：
        多行文本，每条含标题/简介/播放量/链接；失败返回以 [错误] 开头提示。
    """
    params = urllib.parse.urlencode(
        {
            "search_type": "video",
            "keyword": query,
            "page": 1,
            "page_size": max(1, min(20, count)),
        }
    )
    url = f"{BILI_SEARCH_URL}?{params}"
    req = Request(
        url=url,
        method="GET",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126",
            "Referer": "https://search.bilibili.com/",
        },
    )
    try:
        with urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 网络/超时/HTTP 错误统一处理
        return tool_error("bilibili_search", f"检索失败：{exc}")

    if body.get("code") != 0:
        return tool_error("bilibili_search", f"接口返回错误：code={body.get('code')} message={body.get('message')}")

    results = body.get("data", {}).get("result", []) or []
    if not results:
        return tool_info("bilibili_search", f"未找到与「{query}」相关的 B站视频")

    lines = []
    for idx, item in enumerate(results[:count], start=1):
        title = (item.get("title", "") or "").replace("<em class=\"keyword\">", "").replace("</em>", "")
        desc = (item.get("description", "") or "").replace("\n", " ").strip()[:100]
        bvid = item.get("bvid", "")
        play = item.get("play", 0)
        author = item.get("author", "")
        link = f"https://www.bilibili.com/video/{bvid}" if bvid else item.get("arcurl", "")
        lines.append(f"{idx}. {title}\n   UP主：{author} | 播放：{play}\n   简介：{desc}\n   链接：{link}")
    return "\n\n".join(lines)


def to_tool_spec():
    """把 B站检索封装为 ToolSpec（供工具注册表注册）。

    结构化参数说明（对应待办方向 A 路线一）：
        - 声明 query（必填）+ count（可选），让 LLM 精确控制检索关键词与返回条数；
        - fn 直接引用 bilibili_search，参数名与 parameters 的 key 一一对应，
          ToolRegistry.run_tool_call 会以具名参数（**kwargs）方式调用；
        - count 为可选：LLM 不传时沿用函数默认值 8，无需特殊处理。
    """
    from app.agent.tools import ToolSpec

    return ToolSpec(
        name="bilibili_search",
        description="在B站检索相关视频（如校园话题、校园 vlog），返回标题、UP主、播放量与链接",
        fn=bilibili_search,
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词（如「中国地质大学（武汉）」）"},
                "count": {"type": "integer", "description": "返回条数（1-20，默认 8）"},
            },
            "required": ["query"],
        },
    )
