# -*- coding: utf-8 -*-
"""密钥本机加密存储模块。

安全目标（对应可行性报告 §3.4）：
    - API Key 不落明文：优先从 .env 读取；若用户通过配置命令写入密钥，
      则以 Fernet 对称加密后保存到 data/secrets.enc，主密钥保存在
      data/master.key（本机文件，权限收紧为仅当前用户可读写）；
    - 仓库永不包含任何真实密钥：.gitignore 已排除 data/ 与 .env；
    - 日志中统一走脱敏函数，防止密钥出现在 stdout/日志文件。
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# 主密钥文件（本机生成，权限收紧）
MASTER_KEY_FILE = "master.key"
# 加密后的密钥存储文件
SECRETS_FILE = "secrets.enc"
# PBKDF2 盐文件（用于从用户口令派生主密钥的增强路径，MVP 默认使用随机主密钥文件）
SALT_FILE = "master.salt"


def _ensure_data_dir(data_dir: str | Path) -> Path:
    """确保数据目录存在（向量库/会话/密钥统一存放处），返回绝对路径。

    设计说明：目录统一收敛在 data/ 下，便于 .gitignore 与 Docker 卷挂载管理。
    """
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_or_create_master_key(data_dir: str | Path) -> bytes:
    """加载或创建本机主密钥（Fernet 32 字节 url-safe base64）。

    说明：主密钥文件本身是敏感文件，创建后设置 0600 权限（POSIX）；
    Windows 下依赖文件系统 ACL 保护，MVP 阶段在文档中说明该限制。
    """
    path = _ensure_data_dir(data_dir) / MASTER_KEY_FILE
    if path.exists():
        return path.read_bytes()
    key = Fernet.generate_key()
    path.write_bytes(key)
    # POSIX 下收紧权限为仅当前用户可读写（Windows 无此语义，忽略异常）
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def store_api_key(api_key: str, data_dir: str | Path = "data", scope: str = "default") -> None:
    """将 API Key 加密后写入本地存储。

    参数：
        api_key:  待保存的 API 密钥明文（仅内存中使用，落盘为密文）
        data_dir: 数据目录（默认 data/）
        scope:    命名空间（多方案配置：不同 LLM 方案各自独立
                  存密钥文件 secrets_<scope>.enc，互不覆盖；默认 "default"）
    """
    if not api_key.strip():
        raise ValueError("API Key 不能为空")
    key = _load_or_create_master_key(data_dir)
    fernet = Fernet(key)
    encrypted = fernet.encrypt(api_key.encode("utf-8"))
    (_ensure_data_dir(data_dir) / _secret_file(scope)).write_bytes(encrypted)


def read_api_key(data_dir: str | Path = "data", scope: str = "default") -> str:
    """读取加密存储的 API Key；若不存在或解密失败返回空字符串。

    说明：解密失败时主动抛出 RuntimeError 提示重新配置，避免静默吞错
    导致后续请求以空密钥发起。
    """
    path = _ensure_data_dir(data_dir) / _secret_file(scope)
    if not path.exists():
        return ""
    try:
        key = _load_or_create_master_key(data_dir)
        decrypted = Fernet(key).decrypt(path.read_bytes())
        return decrypted.decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("密钥文件解密失败，可能被篡改；请重新配置 API Key") from exc


def _secret_file(scope: str) -> str:
    """按命名空间返回密钥文件名：默认 scope 用历史文件名 secrets.enc（兼容旧数据），
    其余用 secrets_<scope>.enc（多方案各自独立）。"""
    return SECRETS_FILE if scope in ("default", "") else f"secrets_{scope}.enc"


def redact(text: str, secret: str) -> str:
    """脱敏函数：把文本中的密钥替换为 ***，用于日志与回显。

    参数：
        text:   待脱敏文本（如日志行）
        secret: 需要隐藏的密钥明文（为空时原样返回）
    """
    if secret and secret in text:
        return text.replace(secret, "***")
    return text
