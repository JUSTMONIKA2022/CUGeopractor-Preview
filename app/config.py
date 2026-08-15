# -*- coding: utf-8 -*-
"""配置模块：统一加载 .env / 环境变量 / 默认值。

设计说明：
    - 使用 pydantic-settings 从 .env（占位模板见 .env.example）与系统环境变量读取配置；
    - 所有敏感字段（API Key）不在此模块输出到日志；
    - 三形态（Web UI / CLI / Docker）共用同一配置入口，避免功能分叉。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

# 多方案配置文件名（存于 data/ 下，不入库；密钥另存于 secrets 加密存储）
PROFILES_FILE_NAME = "llm_profiles.json"
# 默认方案名（表示"使用 .env 配置"，非真实存储的方案）
_DEFAULT_PROFILE = "default"


class Settings(BaseSettings):
    """全局配置类。

    字段命名与 .env.example 保持一致；默认值保证"开箱即用"的最小体验：
    未配置 LLM 时，CLI/Web 会提示用户先完成配置。
    """

    # pydantic-settings 配置：读取项目根目录 .env；忽略未声明的多余字段
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ===== LLM 配置（OpenAI 兼容协议，用户自配）=====
    llm_base_url: str = ""      # 模型服务 Base URL（DeepSeek/Qwen/本地 Ollama 等）
    llm_model: str = ""         # 模型名称（如 deepseek-chat）
    llm_api_key: str = ""       # API 密钥（优先读取；为空时回退到 secrets 加密存储）
    llm_timeout: int = 60       # 请求超时（秒）

    # ===== 知识库向量化（Embedding）=====
    # 默认：使用 ChromaDB 内置本地 ONNX 小模型（免密钥、数据不出本机、首次自动下载权重），
    # 开箱即用；如需更强效果或远程向量化，配置以下三项后改用 OpenAI 兼容 /embeddings 接口。
    embedding_base_url: str = ""   # 外部 embedding API 地址（配置后启用；留空用内置模型）
    embedding_api_key: str = ""    # 外部 embedding API 密钥（仅外部模式使用）
    embedding_model: str = ""      # 外部 embedding 模型名（如 text-embedding-3-small）

    # ===== 本地服务配置 =====
    host: str = "127.0.0.1"     # Web UI 监听地址（安全要求：默认仅本机，勿轻易对外开放）
    port: int = 8080            # Web UI 端口
    # Agent API 可选鉴权 Token（默认空=不校验；设置后 /api/chat 与 /api/agent/invoke
    # 需携带 Authorization: Bearer <token>。建议仅在需要对外暴露 8080 时配置）
    api_token: str = Field(default="", validation_alias="GEOPRACTOR_API_TOKEN")

    # ===== 数据目录 =====
    # 说明：只认带 GEOPRACTOR_ 前缀的环境变量，彻底避免被系统里常见的同名变量
    # （如 DATA_DIR 可能被其他软件占用，pydantic-settings 中系统环境变量优先级
    # 高于 .env）污染；默认值即项目内 data/，与旧 .env 无前缀写法等效。
    knowledge_dir: str = Field(
        default="data/knowledge",  # 用户自备知识库文档目录
        validation_alias="GEOPRACTOR_KNOWLEDGE_DIR",
    )
    data_dir: str = Field(
        default="data",  # 向量库/会话/密钥存储目录（本地，不入库）
        validation_alias="GEOPRACTOR_DATA_DIR",
    )

    @property
    def is_configured(self) -> bool:
        """是否已具备调用 LLM 的最小配置（base_url 与 model 必填）。"""
        return bool(self.llm_base_url.strip() and self.llm_model.strip())


@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例（lru_cache 保证进程内只解析一次 .env）。

    返回：
        Settings 实例；调用方可通过其属性读取全部配置。
    """
    return Settings()


def update_env_file(updates: dict[str, str], env_path: str | Path = ".env") -> None:
    """按 key 合并更新 .env 文件（保留其他已有配置）。

    背景：CLI 的 configure 与 Web 的 /api/config 都曾用"整文件覆盖写"，会把
    .env 中已有的校园凭据（CUG_USERNAME / JWGL_COOKIE / XHS_COOKIE 等）全部清掉，
    导致用户配置一次 LLM 后所有连接器失效。本函数改为：
        1) 读取现有 .env 的 key-value（保留注释与空行）；
        2) 仅更新 updates 中出现的 key（已有则替换值，无则追加）；
        3) 写回原文件。
    说明：仅更新目标 key，不触碰其他配置，避免误删凭据。

    参数：
        updates:  要写入的 {KEY: VALUE} 映射
        env_path: .env 文件路径（默认项目根）
    """
    path = Path(env_path)
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    # 收集现有 key -> 行号（用于替换；保留注释/空行原样）
    index: dict[str, int] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            index[key] = i

    # 合并更新：已有 key 就地替换，新 key 追加到末尾
    for key, value in updates.items():
        new_line = f"{key}={value}"
        if key in index:
            lines[index[key]] = new_line
        else:
            lines.append(new_line)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ===== 多方案配置（需求：/configure 支持多方案储存与切换）=====
# 方案文件 data/llm_profiles.json 结构：
#   {"current": "方案名", "profiles": {"方案名": {"base_url": ..., "model": ..., "scope": "方案名"}}}
# 密钥不落明文：按 scope 命名空间存入 secrets 加密文件（secrets_<方案名>.enc）。


def _profiles_path(data_dir: str | Path) -> Path:
    """方案配置文件路径（data/llm_profiles.json）。"""
    return Path(data_dir) / PROFILES_FILE_NAME


def load_llm_profiles(data_dir: str | Path) -> dict:
    """读取全部 LLM 方案配置；文件不存在/损坏时返回空结构。"""
    path = _profiles_path(data_dir)
    if not path.exists():
        return {"current": _DEFAULT_PROFILE, "profiles": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 损坏配置按空处理，不阻断使用
        return {"current": _DEFAULT_PROFILE, "profiles": {}}
    if not isinstance(data, dict):
        return {"current": _DEFAULT_PROFILE, "profiles": {}}
    data.setdefault("current", _DEFAULT_PROFILE)
    data.setdefault("profiles", {})
    return data


def _save_llm_profiles(data_dir: str | Path, data: dict) -> None:
    """方案配置原子落盘。"""
    path = _profiles_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def save_llm_profile(data_dir: str | Path, name: str, base_url: str, model: str) -> None:
    """新增或更新一个 LLM 方案，并设为当前方案。

    参数：
        data_dir: 数据目录
        name:     方案名（建议短名，如 deepseek / qwen / local）
        base_url: 模型服务 Base URL
        model:    模型名称
    说明：密钥由调用方以 scope=name 存入 secrets 加密存储；此处只存非敏感字段。
    """
    if not name.strip() or name == _DEFAULT_PROFILE:
        raise ValueError(f"方案名不能为空且不能为 {_DEFAULT_PROFILE}（保留名）")
    data = load_llm_profiles(data_dir)
    data["profiles"][name.strip()] = {
        "base_url": base_url.strip(),
        "model": model.strip(),
        "scope": name.strip(),
    }
    data["current"] = name.strip()
    _save_llm_profiles(data_dir, data)


def delete_llm_profile(data_dir: str | Path, name: str) -> bool:
    """删除方案；若删的是当前方案则回退到 default（.env）。返回是否找到。"""
    data = load_llm_profiles(data_dir)
    if name not in data["profiles"]:
        return False
    del data["profiles"][name]
    if data["current"] == name:
        data["current"] = _DEFAULT_PROFILE
    _save_llm_profiles(data_dir, data)
    return True


def switch_llm_profile(data_dir: str | Path, name: str) -> bool:
    """切换当前方案（name 须为已保存方案）；返回是否成功。"""
    data = load_llm_profiles(data_dir)
    if name not in data["profiles"]:
        return False
    data["current"] = name
    _save_llm_profiles(data_dir, data)
    return True


def active_profile_name(data_dir: str | Path) -> str:
    """当前活动方案名（无已存方案时为 default，表示使用 .env 配置）。"""
    return load_llm_profiles(data_dir).get("current", _DEFAULT_PROFILE)
