# -*- coding: utf-8 -*-
"""缓存存储层单元测试：落盘/读取/TTL/刷新/渠道列表。"""

import json
import time

from app import cache_store
from app.cache_store import (
    CACHE_TTL,
    load_cached,
    refresh_channel,
    save_cached,
    get_or_refresh,
    list_cached,
)


def test_save_and_load(tmp_path, monkeypatch):
    """保存后可读取到相同内容。"""
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path)
    data = {"channel": "ifmweb", "name": "信息门户", "updated": int(time.time()), "sections": []}
    save_cached("ifmweb", data)
    assert load_cached("ifmweb") == data


def test_load_expired_returns_none(tmp_path, monkeypatch):
    """超过 TTL 的缓存视为不存在。"""
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path)
    old = {"updated": int(time.time()) - CACHE_TTL - 10, "sections": []}
    save_cached("tieba", old)
    assert load_cached("tieba") is None


def test_refresh_unknown_channel(tmp_path, monkeypatch):
    """未知渠道刷新应记录 error 且不抛异常。"""
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path)
    data = refresh_channel("unknown_xyz")
    assert data["error"] and "未知渠道" in data["error"]


def test_get_or_refresh_generates(tmp_path, monkeypatch):
    """首次访问应触发生成并落盘。"""
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        cache_store, "_BUILDERS",
        {"ifmweb": lambda: [{"key": "pwps", "name": "勤工助学", "url": "https://x", "items": []}]},
    )
    data = get_or_refresh("ifmweb")
    assert data["sections"][0]["name"] == "勤工助学"
    assert (tmp_path / "ifmweb.json").exists()


def test_get_or_refresh_force(tmp_path, monkeypatch):
    """force=True 应绕过缓存重新生成。"""
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path)
    calls = {"n": 0}

    def builder():
        calls["n"] += 1
        return [{"key": "a", "name": f"服务{calls['n']}", "url": "", "items": []}]

    monkeypatch.setattr(cache_store, "_BUILDERS", {"ifmweb": builder})
    get_or_refresh("ifmweb")
    get_or_refresh("ifmweb")          # 命中缓存，不重新生成
    get_or_refresh("ifmweb", force=True)  # 强制刷新
    assert calls["n"] == 2


def test_list_cached(tmp_path, monkeypatch):
    """list_cached 应覆盖全部渠道并标记缓存状态。"""
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path)
    entries = {e["channel"]: e for e in list_cached()}
    assert set(entries) == set(cache_store.CHANNELS)
    assert entries["ifmweb"]["cached"] is False
    # 生成一个后应标记已缓存
    save_cached("ifmweb", {"updated": int(time.time()), "sections": []})
    entries = {e["channel"]: e for e in list_cached()}
    assert entries["ifmweb"]["cached"] is True


def test_build_ifmweb_reuses_fetch(monkeypatch):
    """门户缓存生成应复用 fetch_service_catalog 的结构化数据。"""
    from connectors import portal_connector

    monkeypatch.setattr(
        portal_connector, "fetch_service_catalog",
        lambda: [("教学管理", "勤工助学", "本科生院", "027-67885010", "https://i.cug.edu.cn/x", "指南")],
    )
    sections = cache_store._build_ifmweb()  # noqa: SLF001 测试内部适配器
    assert sections[0]["name"] == "勤工助学"
    assert sections[0]["url"] == "https://i.cug.edu.cn/x"
    assert sections[0]["key"] == "pwps"  # 常用服务应有语义 key
