# -*- coding: utf-8 -*-
"""学院网站检索连接器单元测试：mock HTTP，验证学院匹配、栏目识别、条目解析与关键词过滤。"""

from unittest.mock import MagicMock

import pytest

import connectors.college_connector as mod
from connectors.college_connector import cug_college_search, match_colleges

# 模拟学院首页 HTML：含导航噪音 + 内容栏目块（通知公告/学院新闻/学术动态）
HOME_HTML = """
<html><body>
<a href="/">首页</a> <a href="http://www.cug.edu.cn/">学校主页</a>
<div class="col"><a href="tzgg.htm">通知公告</a></div>
<div class="col"><a href="index/xyxw.htm">学院新闻</a></div>
<div class="col"><a href="xshd.htm">学术动态</a></div>
<a href="info/1035/1001.htm">某篇具体文章</a>
</body></html>
"""

# 模拟栏目列表页 HTML：info 条目（含标题尾部带日期的情况）
LIST_HTML = """
<html><body>
<ul>
<li><a href="info/1035/9076.htm">多媒体设备采购结果公告</a> 2026-07-08</li>
<li><a href="info/1035/9036.htm">博士研究生招生复试公告</a></li>
<li><a href="info/1035/8886.htm">研招校园开放日活动方案2026-07-01</a></li>
<li><a href="http://wenming.cug.edu.cn/info/1008/110737.htm">学院动态外链新闻</a></li>
</ul>
</body></html>
"""


def _resp(status: int = 200, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = text
    return r


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """每个测试前清空缓存与熔断，隔离用例。"""
    mod._caches.clear()
    mod._breaker._fail_count = 0
    mod._breaker._open_until = 0.0
    monkeypatch.setattr(mod, "_limiter", _NoWait())
    yield


class _NoWait:
    """测试用限速器替身：不等待。"""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_home(url, *a, **k):
    """按 URL 分发：学院根路径返回首页 HTML，其余（栏目页）返回列表页 HTML。"""
    if url.rstrip("/").endswith("cug.edu.cn"):
        return _resp(200, HOME_HTML)
    return _resp(200, LIST_HTML)


def test_match_colleges():
    """学院名称模糊匹配：简称/全称/多命中/无命中。"""
    assert [c["name"] for c in match_colleges("自动化")] == ["人工智能与自动化学院"]
    assert [c["name"] for c in match_colleges("计算机")] == ["计算机学院"]
    assert len(match_colleges("地球")) >= 2  # 地球与行星科学学院等
    assert match_colleges("不存在的学院") == []


def test_find_columns(monkeypatch):
    """首页栏目识别：提取内容栏目、排除导航噪音与 info 文章页。"""
    cols = mod._find_columns(HOME_HTML, "http://au.cug.edu.cn/")
    assert any("通知公告" in n for n in cols.values())
    assert any("学院新闻" in n for n in cols.values())
    # 导航噪音（首页/学校主页）与 info 文章页不应成为栏目
    assert not any(("首页" in n or "学校主页" in n) for n in cols.values())
    assert not any("info/" in u for u in cols)


def test_parse_items():
    """栏目列表页条目解析：提取 info 链接、清洗标题尾部日期、站外链接保留。"""
    items = mod._parse_items(LIST_HTML, "http://au.cug.edu.cn/index/tzgg.htm")
    assert len(items) == 4
    titles = [it["title"] for it in items]
    assert "多媒体设备采购结果公告" in titles
    # 标题尾部日期应被清洗
    assert any(t == "研招校园开放日活动方案" for t in titles)
    # 相对链接拼成绝对地址
    assert any("info/1035/9076.htm" in it["link"] for it in items)


def test_search_keyword_filter(monkeypatch):
    """关键词过滤：只返回标题含关键词的条目。"""
    def fake_get(url, *a, **k):
        # 首页 → HOME_HTML；栏目页 → LIST_HTML
        if "info/" in url or "tzgg.htm" in url or "xyxw.htm" in url or "xshd.htm" in url:
            return _resp(200, LIST_HTML)
        return _resp(200, HOME_HTML)

    monkeypatch.setattr(mod.httpx, "get", fake_get)
    result = cug_college_search("自动化", "公告")
    assert "多媒体设备采购结果公告" in result
    assert "博士研究生招生复试公告" in result
    # 不相关条目（研招校园开放日/外链新闻）被过滤
    assert "研招校园开放日" not in result


def test_search_no_keyword_returns_all(monkeypatch):
    """空关键词返回全部栏目条目。"""
    monkeypatch.setattr(mod.httpx, "get", _fake_home)
    result = cug_college_search("自动化")
    assert "多媒体设备采购结果公告" in result
    assert "学院新闻" in result or "通知公告" in result
    assert "来源" in result


def test_search_no_match_hint(monkeypatch):
    """学院名无匹配时应提示（而非报错）。"""
    result = cug_college_search("不存在的学院")
    assert "未找到" in result


def test_to_tool_spec_registers():
    """工具封装：应声明 college 必填 + keyword 可选的结构化参数。"""
    spec = mod.to_tool_spec()
    assert spec.name == "cug_college_search"
    props = spec.parameters["properties"]
    assert "college" in props and "keyword" in props
    assert spec.parameters["required"] == ["college"]
