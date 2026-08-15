# -*- coding: utf-8 -*-
"""本地 Web 全链路冒烟测试：验证服务可拉起、工具注册无误（无凭据、不依赖外网）。

覆盖（对应待办优化方向 D）：
    - create_app 工厂可正常创建（静态页/API 路由挂载不报错）；
    - GET /api/health 返回 200 与连接器清单；
    - GET /api/agent/tools 返回工具列表（核心工具已注册）；
    - POST /api/chat 未配置模型时返回可读 400 提示（不抛异常）；
    - 根路径静态页可访问。
"""

import importlib.util
from pathlib import Path

import pytest

# fastapi.testclient 依赖 httpx；不存在则整体跳过（环境未装 Web 依赖）
if importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("httpx") is None:
    pytest.skip("未安装 fastapi/httpx，跳过 Web 冒烟测试", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402

from app.agent.tools import create_default_registry  # noqa: E402
from app.config import Settings  # noqa: E402
from app.rag.retriever import Retriever  # noqa: E402
from app.rag.store import VectorStore  # noqa: E402
from app.web.server import create_app  # noqa: E402


def _make_settings(tmp_path: Path) -> Settings:
    """构造指向临时目录的无凭据配置（避免读写真实 data/）。"""
    return Settings(
        llm_base_url="",
        llm_model="",
        host="127.0.0.1",
        port=8080,
        data_dir=str(tmp_path / "data"),
        knowledge_dir=str(tmp_path / "knowledge"),
    )


@pytest.fixture()
def client(tmp_path):
    """创建指向临时目录的 Web 应用与测试客户端。"""
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_health_ok(client):
    """GET /api/health 应返回 200 与结构字段（未配置模型也正常）。"""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["configured"] is False  # 冒烟测试无凭据，视为未配置
    assert isinstance(body["connectors"], list)


def test_tools_list_registered(client):
    """GET /api/agent/tools 应包含核心工具（知识检索 + 社区渠道）。"""
    resp = client.get("/api/agent/tools")
    assert resp.status_code == 200
    names = [t["name"] for t in resp.json()["tools"]]
    # 核心工具必须存在（功能注册无误）
    assert "knowledge_search" in names
    for expected in ("cug_news_search", "zhihu_search", "bilibili_search", "tieba_search"):
        assert expected in names, f"缺少工具 {expected}"


def test_chat_unconfigured_hint(client):
    """未配置模型服务时 POST /api/chat 应返回可读 400 提示（不抛异常）。"""
    resp = client.post("/api/chat", json={"message": "你好"})
    assert resp.status_code == 400
    assert "尚未配置" in resp.json()["reply"]


def test_agent_invoke_unconfigured(client):
    """未配置模型服务时 POST /api/agent/invoke 应返回可读 400。"""
    resp = client.post("/api/agent/invoke", json={"message": "你好", "session_id": "smoke"})
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


# ===== /api/commands 模型门槛分级（放宽，对齐 CLI） =====

def test_commands_pure_unconfigured(client):
    """未配置模型时，纯命令（/help、/schedule）应可执行，返回 200 与文本输出。

    对齐 CLI：/cache_*、/live_*、/schedule、/office_hours、/next、/next_course
    等纯命令不依赖 LLM，无需配置模型即可调用。
    """
    # /help 必然有输出（帮助文本）
    resp = client.post("/api/commands", json={"command": "/help"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["output"].strip() != ""
    # /schedule 纯本地查表（课表预设方案），不触发 LLM
    resp = client.post("/api/commands", json={"command": "/schedule"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert ("南望山" in body["output"]) or ("未来城" in body["output"])


def test_commands_llm_unconfigured_400(client):
    """未配置模型时，依赖 LLM 的命令（/llm、/research）应返回可读 400。"""
    resp = client.post("/api/commands", json={"command": "/llm 你好"})
    assert resp.status_code == 400
    assert "尚未配置" in resp.json()["error"]
    resp = client.post("/api/commands", json={"command": "/research 地大宿舍"})
    assert resp.status_code == 400
    assert "尚未配置" in resp.json()["error"]


def test_static_page_served(client):
    """根路径应提供静态页面（index.html 存在时 200）。"""
    resp = client.get("/")
    # 静态目录存在时返回页面；若目录为空则 404 也不应 500
    assert resp.status_code in (200, 404)


def test_registry_has_session_connectors(tmp_path):
    """工具注册表应能加载会话连接器（配置缺失时 0，不抛异常）。"""
    registry = create_default_registry(Retriever(VectorStore(data_dir=str(tmp_path / "data"))))
    from connectors.session_connector import register_session_connectors

    count = register_session_connectors(registry, config_path=tmp_path / "none.yaml")
    assert count == 0  # 无配置文件 → 连接器默认关闭（安全设计）


# ===== Agent API 可选鉴权（CUGEOPRACTOR_API_TOKEN）=====

def test_api_token_default_pass(tmp_path):
    """未配置 token 时 API 无需鉴权（默认放行，保持开箱即用）。"""
    app = create_app(_make_settings(tmp_path))
    with TestClient(app) as c:
        resp = c.post("/api/chat", json={"message": "你好"})
        assert resp.status_code == 400  # 未配置模型提示（而非 401）
        assert "尚未配置" in resp.json()["reply"]


def test_api_token_configured_requires_bearer(tmp_path):
    """配置 token 后，无/错误 token 的请求应 401。"""
    settings = _make_settings(tmp_path)
    settings.api_token = "secret-token-123"
    app = create_app(settings)
    with TestClient(app) as c:
        # 无 Authorization 头 → 401
        resp = c.post("/api/chat", json={"message": "你好"})
        assert resp.status_code == 401
        # 错误 token → 401
        resp = c.post(
            "/api/chat", json={"message": "你好"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401
        # invoke 同样受保护
        resp = c.post("/api/agent/invoke", json={"message": "你好"})
        assert resp.status_code == 401


def test_api_token_configured_valid_token(tmp_path):
    """配置 token 后，携带正确 Bearer Token 应放行（走到业务逻辑）。"""
    settings = _make_settings(tmp_path)
    settings.api_token = "secret-token-123"
    app = create_app(settings)
    with TestClient(app) as c:
        resp = c.post(
            "/api/chat", json={"message": "你好"},
            headers={"Authorization": "Bearer secret-token-123"},
        )
        # 鉴权通过后进入业务逻辑：未配置模型 → 400（而非 401）
        assert resp.status_code == 400
        assert "尚未配置" in resp.json()["reply"]
