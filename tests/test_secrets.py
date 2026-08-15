# -*- coding: utf-8 -*-
"""密钥模块单元测试：验证加密存储往返、空值拒绝与脱敏。"""

import pytest

from app.secrets import read_api_key, redact, store_api_key


def test_store_and_read(tmp_path):
    """写入的密钥应能被原样读回（加密存储往返）。"""
    store_api_key("sk-demo-secret-123", data_dir=tmp_path)
    assert read_api_key(data_dir=tmp_path) == "sk-demo-secret-123"


def test_store_rejects_empty(tmp_path):
    """空密钥应被拒绝，避免产生无效密文。"""
    with pytest.raises(ValueError):
        store_api_key("   ", data_dir=tmp_path)


def test_redact():
    """脱敏函数应隐藏密钥明文。"""
    assert redact("请求失败，key=sk-abc", "sk-abc") == "请求失败，key=***"
    assert redact("无敏感信息", "sk-xyz") == "无敏感信息"
