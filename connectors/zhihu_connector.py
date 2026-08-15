# -*- coding: utf-8 -*-
"""知乎社区内容连接器（L2 官方 OpenAPI 渠道）。

设计说明（对应渠道规划一期）：
    - 复用 research/zhihu-search.py 已验证可用的调用方式（urllib 标准库 + Bearer 鉴权）；
    - 密钥从环境变量/.env 读取（ZHIHU_ACCESS_SECRET），项目不预置、不入库；
    - 通过 ToolSpec 注册为 Agent 白名单工具，LLM 用 function calling 调用；
    - 返回结构化摘要（标题 + 摘要 + 链接），符合"来源+摘要"引用策略。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from urllib.parse import urlencode
from urllib.request import Request, urlopen

from connectors.base import tool_error, tool_info

# 知乎 OpenAPI 基地址与接口（与 research/search_helper.py 保持一致）
ZHIHU_BASE = "https://developer.zhihu.com"
ZHIHU_SEARCH_PATH = "/api/v1/content/zhihu_search"
# 知乎全网搜索接口（官方 OpenAPI，专为 LLM 设计；SearchDB=all 表示全网检索，
# 见 research/search_helper.py 已验证用法）
ZHIHU_GLOBAL_SEARCH_PATH = "/api/v1/content/global_search"
# 全网搜索返回的 ContentText 可能是整页长文本，截断以控制 token 规模
GLOBAL_TEXT_LIMIT = 500
# 请求超时（秒）
TIMEOUT = 10


def _read_secret() -> str:
    """读取知乎访问密钥：优先环境变量，其次项目 .env（不入库）。"""
    secret = os.environ.get("ZHIHU_ACCESS_SECRET", "").strip()
    if secret:
        return secret
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ZHIHU_ACCESS_SECRET="):
                return line.split("=", 1)[1].strip()
    return ""


def _call_content_api(path: str, params: dict) -> dict:
    """调用知乎 OpenAPI 内容检索接口，返回解析后的 JSON 字典。

    实现要点（与 research/search_helper.py 保持一致）：
        - 复用 _read_secret 读取密钥（环境变量/.env 回退，不入库）；
        - Bearer 鉴权 + X-Request-Timestamp 时间戳头（服务端校验要求）；
        - 网络/超时/HTTP 错误统一抛异常，由上层函数转成可读 [错误] 提示，
          避免异常直接中断 Agent 主循环。
    """
    secret = _read_secret()
    if not secret:
        raise RuntimeError("未配置知乎密钥（请在 .env 设置 ZHIHU_ACCESS_SECRET）")
    url = f"{ZHIHU_BASE}{path}?{urlencode(params)}"
    req = Request(
        url=url,
        method="GET",
        headers={
            "Authorization": f"Bearer {secret}",
            "X-Request-Timestamp": str(int(time.time())),
        },
    )
    with urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def zhihu_search(query: str, count: int = 10) -> str:
    """调用知乎站内检索，返回结构化摘要文本（供 LLM 整理回答）。

    参数：
        query: 检索关键词（如"中国地质大学（武汉） 宿舍"）
        count: 返回条数（默认 10，上限 10）
    返回：
        多行文本，每条含标题/摘要/链接；失败返回以 [错误] 开头提示。
    """
    try:
        body = _call_content_api(
            ZHIHU_SEARCH_PATH,
            {"Query": query, "Count": max(1, min(10, count))},
        )
    except Exception as exc:  # noqa: BLE001 网络/超时/HTTP 错误统一处理
        return tool_error("zhihu_search", f"检索失败：{exc}")

    items = body.get("Data", {}).get("Items", []) or []
    if not items:
        return tool_info("zhihu_search", f"未找到与「{query}」相关的知乎内容")

    lines = []
    for idx, item in enumerate(items[:count], start=1):
        title = item.get("Title", "无标题")
        content = (item.get("ContentText", "") or "").replace("\n", " ").strip()[:150]
        url = item.get("Url", "")
        lines.append(f"{idx}. {title}\n   摘要：{content}\n   链接：{url}")
    return "\n\n".join(lines)


def zhihu_global_search(query: str, count: int = 10) -> str:
    """调用知乎全网搜索（官方 OpenAPI），返回结构化摘要文本（供 LLM 整理回答）。

    与 zhihu_search（仅知乎站内）的区别：
        - global_search 检索全网公开内容（含官网/权威来源），信源更广；
        - 返回结构实测为 {"Code","Message","Data":{"Items":[...]}}，
          条目含 Title/ContentText/Url，ContentText 可能是整页长文本，故截断；
        - SearchDB=all 为已验证的全网检索模式（见 research/search_helper.py）。

    参数：
        query: 检索关键词（如"中国地质大学（武汉） 考研"）
        count: 返回条数（默认 10，上限 20）
    返回：
        多行文本，每条含标题/摘要（截断）/链接；失败返回以 [错误] 开头提示。
    """
    try:
        body = _call_content_api(
            ZHIHU_GLOBAL_SEARCH_PATH,
            {"Query": query, "Count": max(1, min(20, count)), "SearchDB": "all"},
        )
    except Exception as exc:  # noqa: BLE001 网络/超时/HTTP 错误统一处理
        return tool_error("zhihu_global_search", f"检索失败：{exc}")

    if body.get("Code") not in (0, None):
        return tool_error("zhihu_global_search", f"接口返回错误：Code={body.get('Code')} Message={body.get('Message')}")

    items = body.get("Data", {}).get("Items", []) or []
    if not items:
        return tool_info("zhihu_global_search", f"未找到与「{query}」相关的全网内容")

    lines = []
    for idx, item in enumerate(items[:count], start=1):
        title = item.get("Title", "无标题")
        content = (item.get("ContentText", "") or "").replace("\n", " ").strip()[:GLOBAL_TEXT_LIMIT]
        url = item.get("Url", "")
        # 链接可能缺失（如聚合摘要条目），此时只输出标题与摘要，不输出空链接
        line = f"{idx}. {title}\n   摘要：{content}"
        if url:
            line += f"\n   链接：{url}"
        lines.append(line)
    return "\n\n".join(lines)


def to_tool_spec():
    """把知乎检索封装为 ToolSpec（供工具注册表注册）。

    结构化参数说明（对应待办方向 A 路线一）：
        - 声明 query（必填）+ count（可选），让 LLM 精确控制检索关键词与返回条数；
        - fn 直接引用 zhihu_search，参数名与 parameters 的 key 一一对应，
          ToolRegistry.run_tool_call 会以具名参数（**kwargs）方式调用；
        - count 为可选：LLM 不传时沿用函数默认值 10，无需特殊处理。
    """
    from app.agent.tools import ToolSpec

    return ToolSpec(
        name="zhihu_search",
        description="在知乎检索相关内容（如校园话题），返回标题、摘要与链接",
        fn=zhihu_search,
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词（如「中国地质大学（武汉） 宿舍」）"},
                "count": {"type": "integer", "description": "返回条数（1-10，默认 10）"},
            },
            "required": ["query"],
        },
    )


def to_global_tool_spec():
    """把知乎全网搜索封装为 ToolSpec（供工具注册表注册）。

    结构化参数说明（对应待办方向 A 路线一）：
        - 与 to_tool_spec 同构：声明 query（必填）+ count（可选），
          fn 直接引用 zhihu_global_search，具名参数调用；
        - count 可选：LLM 不传时沿用函数默认值 10。
    """
    from app.agent.tools import ToolSpec

    return ToolSpec(
        name="zhihu_global_search",
        description="在知乎全网（含官网与权威来源）检索相关内容，返回标题、摘要与链接",
        fn=zhihu_global_search,
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词（如「中国地质大学（武汉） 考研」）"},
                "count": {"type": "integer", "description": "返回条数（1-20，默认 10）"},
            },
            "required": ["query"],
        },
    )
