# -*- coding: utf-8 -*-
"""HTTP 校园连接器：把用户配置的校园信息接口注册为 Agent 工具。

设计说明（对应"通过工具访问与整理中国地质大学相关信息"的定位）：
    - 项目不内置任何校园端点/凭据；用户在 data/connectors.yaml 中自行定义
      "查询接口"，通过 URL 占位符与 .env 变量注入自己的账号凭据；
    - 连接器注册进工具注册表后，LLM 即可通过 function calling 调用它，
      实现"提供 LLM API → LLM 调工具 → 访问校园信息"的完整闭环；
    - 红线（对应可行性报告 §5.3）：只支持用户配置的合法接口，不内置
      绕过认证/抓取他人数据的逻辑；请求由用户本机发起，失败返回可读错误。

配置文件格式（data/connectors.yaml）：
    connectors:
      - name: course_api          # 工具名（供 LLM 调用）
        description: 查询我的课程表（需要校园账号授权）
        url: https://example.edu.cn/api/courses?student_id={{STUDENT_ID}}
        method: GET                # 或 POST
        headers:
          Authorization: "Bearer {{CAMPUS_TOKEN}}"
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

try:
    import httpx
except ImportError:  # 未安装 httpx 时回退 urllib（标准库），保持可用性
    httpx = None  # type: ignore[assignment]

from app.rate_limit import get_rate_limiter
from connectors.base import tool_error

# 连接器配置文件路径（data/ 已被 .gitignore 排除，不会进仓库）
CONNECTORS_FILE = "data/connectors.yaml"

# URL/Header 中的占位符形如 {{VAR_NAME}}，取值顺序：环境变量 -> 已配置值
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# 默认请求间隔与抖动（秒）：所有 HTTP 连接器共用低频限速，防封禁
_DEFAULT_INTERVAL = 4.0
_DEFAULT_JITTER = 1.0


class HttpConnector:
    """单个 HTTP 查询连接器（对应工具注册表中的一个 ToolSpec）。"""

    def __init__(
        self, name: str, description: str, url: str,
        method: str = "GET", headers: dict | None = None,
        interval: float = _DEFAULT_INTERVAL, jitter: float = _DEFAULT_JITTER,
    ) -> None:
        self.name = name
        self.description = description
        self.url = url
        self.method = method.upper()
        self.headers = headers or {}
        # 每连接器独立限速器（防封禁：请求间隔+随机抖动）
        self._limiter = get_rate_limiter(name, interval=interval, jitter=jitter)

    def _resolve(self, raw: str) -> str:
        """把 {{VAR}} 占位符替换为环境变量值；缺失变量保留原样并给出提示。"""
        def repl(match: re.Match) -> str:
            key = match.group(1)
            value = os.environ.get(key, "")
            return value if value else f"[缺少环境变量 {key}]"
        return _PLACEHOLDER_RE.sub(repl, raw)

    def invoke(self, question: str) -> str:
        """执行查询：发起 HTTP 请求并返回文本结果（JSON 原样返回，供 LLM 解析）。

        参数：
            question: 用户请求内容（可拼接进 URL 查询参数，由配置决定是否使用）
        返回：
            响应文本；失败时返回以 [错误] 开头的提示（不让异常中断 Agent）。
        """
        url = self._resolve(self.url)
        headers = {k: self._resolve(v) for k, v in self.headers.items()}
        # 把用户问题作为 q 参数附加（仅当 URL 含 {question} 时替换）
        url = url.replace("{question}", question)
        timeout = 15
        try:
            # 请求前限速（每连接器独立限速器，防高频触发目标站点风控）
            with self._limiter:
                if httpx is not None:
                    resp = httpx.request(self.method, url, headers=headers, timeout=timeout)
                    body = resp.text
                else:
                    # 回退：使用标准库 urllib（仅支持 GET，Headers 基础场景）
                    from urllib.request import Request, urlopen

                    req = Request(url, headers=headers, method=self.method)
                    with urlopen(req, timeout=timeout) as r:
                        body = r.read().decode("utf-8", errors="replace")
            return body[:8000]  # 截断超长响应，避免撑爆上下文
        except Exception as exc:
            return tool_error(self.name, f"请求失败：{exc}")


def load_connectors_from_yaml(path: str | Path = CONNECTORS_FILE) -> list[HttpConnector]:
    """从 YAML 配置加载连接器列表（缺失文件返回空列表）。

    说明：未安装 PyYAML 时尝试用简单解析兜底；MVP 推荐安装 PyYAML。
    """
    config_path = Path(path)
    if not config_path.exists():
        return []
    try:
        import yaml  # PyYAML（项目可选依赖，用于解析连接器配置）
    except ImportError:
        # 兜底：极简 YAML 解析（仅支持本文件约定的扁平结构）
        return _parse_flat_yaml(config_path.read_text(encoding="utf-8"))
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    connectors = []
    for item in data.get("connectors", []) or []:
        rl = item.get("rate_limit", {}) or {}
        connectors.append(
            HttpConnector(
                name=item["name"],
                description=item.get("description", item["name"]),
                url=item["url"],
                method=item.get("method", "GET"),
                headers=item.get("headers", {}),
                interval=rl.get("interval", _DEFAULT_INTERVAL),
                jitter=rl.get("jitter", _DEFAULT_JITTER),
            )
        )
    return connectors


def _parse_flat_yaml(text: str) -> list[HttpConnector]:
    """极简 YAML 兜底解析：支持 "- name/description/url/method/headers" 扁平结构。

    说明：仅为未安装 PyYAML 时的降级路径；正式使用建议安装 PyYAML。
    """
    connectors: list[HttpConnector] = []
    current: dict = {}
    in_headers = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- name:"):
            if current:
                connectors.append(_build_from_dict(current))
            current = {"name": stripped.split(":", 1)[1].strip()}
            in_headers = False
        elif in_headers and ":" in stripped:
            k, v = stripped.split(":", 1)
            current.setdefault("headers", {})[k.strip()] = v.strip()
        elif stripped.startswith("headers:"):
            in_headers = True
        elif ":" in stripped and not in_headers:
            k, v = stripped.split(":", 1)
            current[k.strip()] = v.strip()
    if current:
        connectors.append(_build_from_dict(current))
    return connectors


def _build_from_dict(item: dict) -> HttpConnector:
    """把解析结果组装为 HttpConnector 实例。"""
    return HttpConnector(
        name=item.get("name", "connector"),
        description=item.get("description", item.get("name", "connector")),
        url=item.get("url", ""),
        method=item.get("method", "GET"),
        headers=item.get("headers", {}),
    )


def register_connectors(registry, config_path: str | Path = CONNECTORS_FILE) -> int:
    """把配置中的连接器注册进工具注册表（供 Agent 通过 function calling 调用）。

    参数：
        registry: app.agent.tools.ToolRegistry 实例
        config_path: 连接器配置文件路径
    返回：
        成功注册的连接器数量。
    """
    from app.agent.tools import ToolSpec

    count = 0
    for connector in load_connectors_from_yaml(config_path):
        registry.register(
            ToolSpec(
                name=connector.name,
                description=connector.description,
                fn=connector.invoke,
            )
        )
        count += 1
    return count
