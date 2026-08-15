# -*- coding: utf-8 -*-
"""连接器模块单元测试：占位符替换、注册逻辑（不发起真实网络请求）。"""

import os
from types import SimpleNamespace

from app.agent.tools import ToolRegistry
from connectors.http_connector import HttpConnector, _PLACEHOLDER_RE, register_connectors


def test_resolve_placeholder(tmp_path, monkeypatch):
    """{{VAR}} 占位符应从环境变量取值；缺失变量给出提示不抛异常。"""
    monkeypatch.setenv("CAMPUS_TOKEN", "demo-token")
    connector = HttpConnector(
        name="demo",
        description="demo",
        url="https://example.edu.cn/api/courses?token={{CAMPUS_TOKEN}}&x={{MISSING_VAR}}",
    )
    resolved = connector._resolve(connector.url)
    assert "demo-token" in resolved
    assert "缺少环境变量 MISSING_VAR" in resolved


def test_register_connectors(tmp_path, monkeypatch):
    """连接器应能注册进工具注册表，未配置时跳过且不报错。"""
    config_path = tmp_path / "connectors.yaml"
    config_path.write_text(
        "connectors:\n"
        "  - name: course_api\n"
        "    description: 查询课程表\n"
        "    url: https://example.edu.cn/api/courses\n"
        "    method: GET\n",
        encoding="utf-8",
    )
    registry = ToolRegistry()
    # 未配置默认路径：应返回 0 且不抛异常
    assert register_connectors(registry, config_path=tmp_path / "not_exist.yaml") == 0
    # 配置指定路径：应注册 1 个连接器
    assert register_connectors(registry, config_path=config_path) == 1
    assert registry.get("course_api") is not None
    assert registry.call("course_api", "查询我的课表").startswith("[错误]")
