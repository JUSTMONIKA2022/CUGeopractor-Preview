# -*- coding: utf-8 -*-
"""配置模块单元测试：验证默认值与 .env 覆盖行为。"""

from app.config import Settings, update_env_file


def test_default_values(monkeypatch, tmp_path):
    """默认配置应满足安全要求：本机监听、空 LLM 配置、标准数据目录。"""
    monkeypatch.chdir(tmp_path)  # 隔离项目根 .env（避免读到真实 LLM 配置）
    s = Settings()
    assert s.host == "127.0.0.1", "默认监听地址必须是本机回环，禁止对外暴露"
    assert s.llm_base_url == ""
    assert s.llm_model == ""
    assert not s.is_configured, "未配置 base_url/model 时 is_configured 应为 False"


def test_env_override(monkeypatch, tmp_path):
    """.env 或环境变量应能覆盖默认值（模拟用户配置 base_url/model）。"""
    monkeypatch.chdir(tmp_path)
    s = Settings(llm_base_url="https://example.com/v1", llm_model="demo-model")
    assert s.is_configured is True


def test_update_env_file_merges_not_overwrites(tmp_path):
    """update_env_file 应保留既有 key（如校园凭据），只更新目标 key。

    回归场景：此前 CLI/Web 配置用整文件覆盖写 .env，会把已配置的
    CUG_USERNAME / JWGL_COOKIE 等凭据清掉；本测试确保合并更新语义。
    """
    env_path = tmp_path / ".env"
    # 预置"已有凭据"
    env_path.write_text("CUG_USERNAME=20231000000\nJWGL_COOKIE=abc=1; def=2\n", encoding="utf-8")
    # 合并更新 LLM 配置
    update_env_file({"LLM_BASE_URL": "https://api.example.com/v1", "LLM_MODEL": "demo"}, env_path)

    content = env_path.read_text(encoding="utf-8")
    assert "CUG_USERNAME=20231000000" in content, "既有的校园凭据不能被覆盖删除"
    assert "JWGL_COOKIE=abc=1; def=2" in content
    assert "LLM_BASE_URL=https://api.example.com/v1" in content
    assert "LLM_MODEL=demo" in content

    # 再次更新已有 key 应替换而非追加重复
    update_env_file({"LLM_MODEL": "new-model"}, env_path)
    content2 = env_path.read_text(encoding="utf-8")
    assert content2.count("LLM_MODEL=") == 1
    assert "LLM_MODEL=new-model" in content2
