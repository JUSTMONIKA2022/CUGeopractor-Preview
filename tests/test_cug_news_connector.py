# -*- coding: utf-8 -*-
"""官网实时检索连接器单元测试：mock HTTP，验证解析、关键词过滤、TTL 缓存与错误处理。"""

import time
from unittest.mock import MagicMock

import pytest

import connectors.cug_news_connector as mod
from connectors.cug_news_connector import cug_news_search

# 模拟官网列表页 HTML（xblist 布局：日期块 + 链接块）
SAMPLE_HTML = """
<div class="xblist-date"><p>2026-06</p><h2>10</h2></div>
<div class="xblist-title xblist-title2">
  <a href="https://i.cug.edu.cn/web/#/km-news/view/1"><h2>关于2026年端午节放假安排的通知</h2><div></div></a>
</div>
<div class="xblist-date"><p>2026-05</p><h2>20</h2></div>
<div class="xblist-title xblist-title2">
  <a href="../info/11049/112773.htm"><h2>奖学金评定工作通知</h2><div>为做好本年度奖学金评定</div></a>
</div>
"""


def _resp(status: int = 200, text: str = SAMPLE_HTML, etag: str = '"abc"', last_mod: str = "Mon, 01 Jan 2024"):
    """构造模拟 httpx 响应对象。"""
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.headers = {"ETag": etag, "Last-Modified": last_mod}
    return r


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch):
    """每个测试前清空栏目缓存与限速/熔断状态，保证相互隔离。"""
    for c in mod._CACHES.values():
        c.items = []
        c.etag = None
        c.last_modified = None
        c.expire_at = 0.0
    # 关闭限速等待与熔断，加速测试
    monkeypatch.setattr(mod, "_limiter", _NoWait())
    yield


class _NoWait:
    """测试用限速器替身：不等待。"""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_parse_and_keyword_filter(monkeypatch):
    """应正确解析列表项并按关键词过滤标题/摘要。"""
    monkeypatch.setattr(mod.httpx, "get", lambda *a, **k: _resp())
    result = cug_news_search("奖学金")
    assert "奖学金评定工作通知" in result
    assert "端午节" not in result
    assert "info/11049/112773.htm" in result  # 相对链接已还原为绝对链接


def test_empty_keyword_returns_all(monkeypatch):
    """空关键词应返回全部栏目条目。"""
    monkeypatch.setattr(mod.httpx, "get", lambda *a, **k: _resp())
    result = cug_news_search("")
    assert "端午节" in result and "奖学金" in result


def test_no_match_hint(monkeypatch):
    """无匹配时应返回提示。"""
    monkeypatch.setattr(mod.httpx, "get", lambda *a, **k: _resp())
    result = cug_news_search("不存在的词xyz")
    assert "未找到" in result


def test_ttl_cache_avoids_second_request(monkeypatch):
    """缓存期内第二次查询应命中缓存，不再发起网络请求。"""
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return _resp()

    monkeypatch.setattr(mod.httpx, "get", fake_get)
    cug_news_search("放假", channel="通知公告")
    n_after_first = calls["n"]
    cug_news_search("放假", channel="通知公告")  # 缓存期内
    assert calls["n"] == n_after_first  # 未新增请求


def test_conditional_request_headers(monkeypatch):
    """缓存过期后应携带 If-None-Match/If-Modified-Since 做条件请求。"""
    seen = {}

    def fake_get(url, headers=None, **k):
        seen.update(headers or {})
        return _resp(status=304)  # 内容未变更

    monkeypatch.setattr(mod.httpx, "get", fake_get)
    # 先填充一次缓存（含 ETag/Last-Modified）
    monkeypatch.setattr(mod.httpx, "get", lambda *a, **k: _resp())
    cug_news_search("放假", channel="通知公告")
    # 手动令缓存过期
    mod._CACHES["通知公告"].expire_at = time.monotonic() - 1
    # 再次查询：应发起带条件头的请求并复用旧缓存（304）
    monkeypatch.setattr(mod.httpx, "get", fake_get)
    result = cug_news_search("放假", channel="通知公告")
    assert "If-None-Match" in seen and "If-Modified-Since" in seen
    assert "端午节" in result  # 复用了旧缓存内容


def test_http_failure_returns_error(monkeypatch):
    """抓取失败（非 200/304）应返回可读错误。"""
    # backoff_retry 会对 500 重试后抛出 RuntimeError，由 cug_news_search 捕获为 [错误]
    monkeypatch.setattr(mod, "backoff_retry", lambda fn, **k: fn())
    monkeypatch.setattr(mod.httpx, "get", lambda *a, **k: _resp(status=500, text="err"))
    result = cug_news_search("放假", channel="通知公告")
    assert result.startswith("[错误]")


# ===== 官网机构导航（cug_navigation， 新增） =====

# 模拟机构页 HTML（官网 <li class="yj"><a target="_blank" href="...">名称</a> 结构）
ORG_HTML = """
<div class="teacher teacher1"><ul class="teacher_ul">
  <li class="yj"><a target="_blank" href="http://au.cug.edu.cn/">人工智能与自动化学院</a></li>
  <li class="kh">/</li>
  <li class="yj"><a target="_blank" href="http://wyxy.cug.edu.cn/">外国语学院</a></li>
</ul></div>
"""
DEPT_HTML = """
<div class="teacher teacher2"><ul class="teacher_ul">
  <li class="yj"><a target="_blank" href="https://bksy.cug.edu.cn/">本科生院</a></li>
  <li class="yj"><a target="_blank" href="http://zzb.cug.edu.cn/">党委组织部</a></li>
</ul></div>
"""


def _org_resp():
    """按 URL 返回对应机构的模拟页面（学院/部门）。"""
    def fake(url, **kw):
        if "jxkyjg" in url:
            return _resp(text=ORG_HTML)
        return _resp(text=DEPT_HTML)
    return fake


def test_navigation_parses_and_groups(monkeypatch):
    """官网导航应解析学院/部门两组机构并输出名称与链接。"""
    monkeypatch.setattr(mod, "_org_cache", {})  # 清机构缓存，保证走网络
    monkeypatch.setattr(mod.httpx, "get", _org_resp())
    result = mod.cug_navigation("")
    assert "人工智能与自动化学院" in result and "http://au.cug.edu.cn/" in result
    assert "本科生院" in result and "https://bksy.cug.edu.cn/" in result
    assert "官网学院" in result and "官网部门" in result


def test_navigation_keyword_filter(monkeypatch):
    """关键词应过滤机构（"自动化"只命中学院组）。"""
    monkeypatch.setattr(mod, "_org_cache", {})
    monkeypatch.setattr(mod.httpx, "get", _org_resp())
    result = mod.cug_navigation("自动化")
    assert "人工智能与自动化学院" in result
    assert "本科生院" not in result


def test_navigation_no_match_hint(monkeypatch):
    """无匹配机构时应给出可读提示（含官网组织页入口）。"""
    monkeypatch.setattr(mod, "_org_cache", {})
    monkeypatch.setattr(mod.httpx, "get", _org_resp())
    result = mod.cug_navigation("不存在的机构")
    assert result.startswith("[错误]")
    assert "zzjg1" in result  # 提示可访问官网组织机构页
