# -*- coding: utf-8 -*-
"""社区连接器（知乎/B站）单元测试：mock HTTP 响应验证结构化输出与错误处理。"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from connectors.bilibili_connector import bilibili_search
from connectors.zhihu_connector import zhihu_search, zhihu_global_search


def _mock_response(body: dict):
    """构造模拟的 HTTP 响应上下文管理器。"""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(body, ensure_ascii=False).encode("utf-8")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = lambda s, *a: False
    return mock_resp


# ===== 知乎连接器 =====

def test_zhihu_search_missing_key(monkeypatch, tmp_path):
    """未配置密钥时应返回可读错误提示（在临时目录运行，避免读取本机 .env）。"""
    monkeypatch.delenv("ZHIHU_ACCESS_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)  # 切换到无 .env 的临时目录，确保密钥读取失败
    result = zhihu_search("中国地质大学")
    assert result.startswith("[错误]") and "ZHIHU_ACCESS_SECRET" in result


def test_zhihu_search_success(monkeypatch):
    """正常响应应返回标题/摘要/链接结构化文本。"""
    monkeypatch.setenv("ZHIHU_ACCESS_SECRET", "demo-secret")
    body = {
        "Data": {
            "Items": [
                {
                    "Title": "中国地质大学（武汉）怎么样？",
                    "ContentText": "宿舍条件不错，专业很强",
                    "Url": "https://www.zhihu.com/question/123",
                }
            ]
        }
    }
    with patch("connectors.zhihu_connector.urlopen", return_value=_mock_response(body)):
        result = zhihu_search("中国地质大学")
    assert "中国地质大学（武汉）怎么样？" in result
    assert "https://www.zhihu.com/question/123" in result
    assert "宿舍条件不错" in result


def test_zhihu_search_empty(monkeypatch):
    """无结果时应返回"未找到"提示。"""
    monkeypatch.setenv("ZHIHU_ACCESS_SECRET", "demo-secret")
    body = {"Data": {"Items": []}}
    with patch("connectors.zhihu_connector.urlopen", return_value=_mock_response(body)):
        result = zhihu_search("不存在的话题xyz")
    assert "未找到" in result


# ===== B站连接器 =====

def test_bilibili_search_success():
    """正常响应应返回标题/UP主/播放量/链接。"""
    body = {
        "code": 0,
        "data": {
            "result": [
                {
                    "title": "<em class=\"keyword\">中国地质大学</em>（武汉）航拍",
                    "description": "校园风景航拍",
                    "bvid": "BV1xx411c7mD",
                    "play": 12000,
                    "author": "地大校园君",
                }
            ]
        }
    }
    with patch("connectors.bilibili_connector.urlopen", return_value=_mock_response(body)):
        result = bilibili_search("中国地质大学")
    assert "中国地质大学（武汉）航拍" in result
    assert "地大校园君" in result
    assert "BV1xx411c7mD" in result
    assert "12000" in result


def test_bilibili_search_api_error():
    """B站接口非 0 code 应返回可读错误。"""
    body = {"code": -400, "message": "请求错误"}
    with patch("connectors.bilibili_connector.urlopen", return_value=_mock_response(body)):
        result = bilibili_search("任意关键词")
    assert "接口返回错误" in result
    assert "-400" in result


def test_bilibili_search_empty():
    """无结果时应返回"未找到"提示。"""
    body = {"code": 0, "data": {"result": []}}
    with patch("connectors.bilibili_connector.urlopen", return_value=_mock_response(body)):
        result = bilibili_search("不存在的话题xyz")
    assert "未找到" in result


# ===== 知乎全网搜索连接器 =====

def test_zhihu_global_search_success(monkeypatch):
    """正常响应应返回标题/摘要（截断）/链接；超长 ContentText 应被截断。"""
    monkeypatch.setenv("ZHIHU_ACCESS_SECRET", "demo-secret")
    long_text = "地" * 600  # 超过 GLOBAL_TEXT_LIMIT(500) 的长文本，验证截断
    body = {
        "Code": 0,
        "Message": "success",
        "Data": {
            "Items": [
                {
                    "Title": "中国地质大学(武汉)本科招生网",
                    "ContentText": long_text,
                    "Url": "https://zhaosheng.cug.edu.cn/",
                }
            ]
        },
    }
    with patch("connectors.zhihu_connector.urlopen", return_value=_mock_response(body)):
        result = zhihu_global_search("中国地质大学（武汉）")
    assert "中国地质大学(武汉)本科招生网" in result
    assert "https://zhaosheng.cug.edu.cn/" in result
    # 截断验证：摘要长度应不超过 500 字符
    import re
    snippet = re.search(r"摘要：(\S+)", result).group(1)
    assert len(snippet) <= 500, "全网搜索的长正文必须截断以控制 token 规模"


def test_zhihu_global_search_empty(monkeypatch):
    """无结果时应返回"未找到"提示。"""
    monkeypatch.setenv("ZHIHU_ACCESS_SECRET", "demo-secret")
    body = {"Code": 0, "Message": "success", "Data": {"Items": []}}
    with patch("connectors.zhihu_connector.urlopen", return_value=_mock_response(body)):
        result = zhihu_global_search("不存在的话题xyz")
    assert "未找到" in result


def test_zhihu_global_search_api_error(monkeypatch):
    """接口 Code 非 0 时应返回可读错误（含 Code/Message）。"""
    monkeypatch.setenv("ZHIHU_ACCESS_SECRET", "demo-secret")
    body = {"Code": 400, "Message": "参数错误"}
    with patch("connectors.zhihu_connector.urlopen", return_value=_mock_response(body)):
        result = zhihu_global_search("任意关键词")
    assert "接口返回错误" in result
    assert "400" in result


def test_zhihu_global_search_missing_key(monkeypatch, tmp_path):
    """未配置密钥时应返回可读错误提示（含 ZHIHU_ACCESS_SECRET 指引）。"""
    monkeypatch.delenv("ZHIHU_ACCESS_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)  # 切换到无 .env 的临时目录，确保密钥读取失败
    result = zhihu_global_search("中国地质大学")
    assert result.startswith("[错误]") and "ZHIHU_ACCESS_SECRET" in result
