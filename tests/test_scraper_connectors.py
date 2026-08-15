# -*- coding: utf-8 -*-
"""贴吧/小红书连接器单元测试：mock HTTP 响应，验证解析、过滤与错误处理。"""

import json
from unittest.mock import MagicMock, patch

from connectors.tieba_connector import (
    tieba_search,
    _warmup_and_fetch,
    _render_posts,
    _external_search,
    _forum_search,
)
from connectors.xiaohongshu_connector import xhs_search


def _mock_get(html: str, status: int = 200):
    """构造模拟的 GET 响应上下文管理器。"""
    mock_resp = MagicMock()
    mock_resp.text = html
    mock_resp.status_code = status
    return mock_resp


def _mock_post(body: dict, status: int = 200):
    """构造模拟的 POST 响应上下文管理器。"""
    mock_resp = MagicMock()
    mock_resp.text = json.dumps(body, ensure_ascii=False)
    mock_resp.status_code = status
    return mock_resp


# ===== 贴吧连接器 =====

TIEBA_HTML = """
<li class="j_thread_list" data-field='{"id": 12345, "title": "中国地质大学武汉吧 宿舍条件"}'></li>
<li class="j_thread_list" data-field='{"id": 67890, "title": "食堂好吃吗"}'></li>
<li class="j_thread_list" data-field='{"id": 11111, "title": "无关帖子"}'></li>
"""


def test_tieba_search_filters_by_keyword(monkeypatch):
    """抓取结果应按关键词过滤标题。"""
    # 隔离真实 .env：未配置外部服务（否则 _external_search 会真实请求本地服务，测试不稳定）
    monkeypatch.setattr("connectors.tieba_connector._env_or_dotenv", lambda k: "")
    monkeypatch.setattr("connectors.tieba_connector._fetch_html", lambda url, params=None: TIEBA_HTML)
    result = tieba_search("宿舍")
    assert "中国地质大学武汉吧 宿舍条件" in result
    assert "食堂好吃吗" not in result


# ===== 贴吧外部服务模式（TIEBA_API_BASE，BYO）=====

def test_tieba_external_search_success(monkeypatch):
    """配置了 TIEBA_API_BASE 时，外部服务返回的 threadList 应被解析。"""
    body = {
        "forum": {},
        "threadList": [
            {"tid": "10884865883", "title": "求助能不能带台式机进宿舍啊"},
            {"tid": "10880887789", "title": "求助地大武汉26级测控女生宿舍分配问题"},
            {"tid": "999", "title": "无关帖"},
        ],
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = body
    monkeypatch.setattr("connectors.tieba_connector._env_or_dotenv", lambda k: "https://cf.eztb.org" if k == "TIEBA_API_BASE" else "")
    with patch("connectors.tieba_connector.httpx.get", return_value=mock_resp) as mock_get:
        posts = _external_search("宿舍", "中国地质大学武汉吧")
    assert ("求助能不能带台式机进宿舍啊", "https://tieba.baidu.com/p/10884865883") in posts
    # 请求应把吧名去掉"吧"后缀传给服务
    args, kwargs = mock_get.call_args
    assert kwargs["params"]["fname"] == "中国地质大学武汉"


def test_tieba_external_search_not_configured(monkeypatch):
    """未配置 TIEBA_API_BASE 时应返回空列表（由上层降级）。"""
    monkeypatch.setattr("connectors.tieba_connector._env_or_dotenv", lambda k: "")
    assert _external_search("宿舍", "中国地质大学武汉吧") == []


def test_tieba_external_search_http_error(monkeypatch):
    """外部服务返回非 200 时应返回空列表（降级）。"""
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    monkeypatch.setattr("connectors.tieba_connector._env_or_dotenv", lambda k: "https://cf.eztb.org" if k == "TIEBA_API_BASE" else "")
    with patch("connectors.tieba_connector.httpx.get", return_value=mock_resp):
        assert _external_search("宿舍", "中国地质大学武汉吧") == []


def test_tieba_search_external_mode_first(monkeypatch):
    """配置 TIEBA_API_BASE 后 tieba_search 应首选真·吧内搜索（/forum/search，SSE）。

     改造：/forum/thread 只返回最新帖列表（配合本地标题过滤只能命中近期帖），
    新增 /forum/search（服务端扫描吧内历史帖）为首选；本测试验证搜索路径被调用、
    且不再抓网页（_fetch_html）也不走列表接口（/forum/thread）。
    """
    sse_text = (
        'data: {"type":"threads","count":100}\n\n'
        'data: {"type":"progress"}\n\n'
        'data: {"type":"match","posts":[{"tid":"123","threadTitle":"宿舍条件怎么样","content":"x"}]}\n\n'
        'data: {"type":"done","stats":{"threadsScanned":100,"totalMatches":1}}\n\n'
    )
    mock_resp = _mock_get(sse_text)
    monkeypatch.setattr(
        "connectors.tieba_connector._env_or_dotenv",
        lambda k: "https://cf.eztb.org" if k == "TIEBA_API_BASE" else "",
    )
    with patch("connectors.tieba_connector.httpx.get", return_value=mock_resp) as mock_get:
        with patch("connectors.tieba_connector._fetch_html") as mock_fetch:
            result = tieba_search("宿舍")
    assert "宿舍条件怎么样" in result
    # 请求应打到 /forum/search（吧内搜索），而非 /forum/thread（最新列表）
    url = mock_get.call_args[0][0]
    assert url.endswith("/forum/search")
    mock_fetch.assert_not_called()  # 搜索成功时不再抓网页


def test_forum_search_parses_sse(monkeypatch):
    """_forum_search 应解析 SSE 流中的 match 事件，并按 tid 去重。"""
    sse_text = (
        'data: {"type":"threads","count":100}\n\n'
        'data: {"type":"progress"}\n\n'
        'data: {"type":"match","posts":[{"tid":"10934788116","threadTitle":"自动化专业怎么样","content":"..."}]}\n\n'
        # 同一 tid 再次命中（不同楼层）：应去重，只保留一次
        'data: {"type":"match","posts":[{"tid":"10934788116","threadTitle":"自动化专业怎么样","content":"重复楼层"}]}\n\n'
        # threadTitle 为空：退回 content 首行做标题
        'data: {"type":"match","posts":[{"tid":"999","threadTitle":"","content":"第一条内容\\n第二条"}]}\n\n'
        'data: {"type":"done","stats":{"threadsScanned":100,"totalMatches":3}}\n\n'
    )
    mock_resp = _mock_get(sse_text)
    monkeypatch.setattr(
        "connectors.tieba_connector._env_or_dotenv",
        lambda k: "https://cf.eztb.org" if k == "TIEBA_API_BASE" else "",
    )
    with patch("connectors.tieba_connector.httpx.get", return_value=mock_resp):
        posts = _forum_search("自动化", "中国地质大学武汉吧")
    # 去重后仅 2 条：tid 10934788116 出现一次 + tid 999
    assert ("自动化专业怎么样", "https://tieba.baidu.com/p/10934788116") in posts
    assert ("第一条内容", "https://tieba.baidu.com/p/999") in posts
    assert len(posts) == 2, f"同一帖子被多次命中时应去重，实际：{posts}"


def test_forum_search_splits_keywords(monkeypatch):
    """多词关键词应拆分为逗号分隔传给 /forum/search。

     实测：keywords 为逗号分隔的多词列表，整短语精确匹配会 0 结果
    （"自动化宿舍" 命中 0），逗号分隔可 OR 匹配（"自动化,宿舍" 命中 50 条）。
    """
    sse_text = 'data: {"type":"done","stats":{"threadsScanned":1,"totalMatches":0}}\n\n'
    mock_resp = _mock_get(sse_text)
    monkeypatch.setattr(
        "connectors.tieba_connector._env_or_dotenv",
        lambda k: "https://cf.eztb.org" if k == "TIEBA_API_BASE" else "",
    )
    with patch("connectors.tieba_connector.httpx.get", return_value=mock_resp) as mock_get:
        _forum_search("自动化 宿舍", "中国地质大学武汉吧")
    params = mock_get.call_args.kwargs["params"]
    assert params["keywords"] == "自动化,宿舍"


def test_tieba_search_empty(monkeypatch):
    """无匹配帖子时应返回提示。"""
    monkeypatch.setattr("connectors.tieba_connector._env_or_dotenv", lambda k: "")
    monkeypatch.setattr("connectors.tieba_connector._fetch_html", lambda url, params=None: "")
    # HTTP 与 Playwright 渲染都无结果（避免真实启动浏览器）
    monkeypatch.setattr("connectors.tieba_connector._render_posts", lambda url, params=None: [])
    result = tieba_search("不存在的话题xyz")
    assert "未找到" in result


def test_tieba_search_no_match_not_anti_spam(monkeypatch):
    """数据源正常返回帖子但无关键词匹配时，不应误报为反爬。

    回归场景：能力边界实测发现——外部服务正常返回帖子列表，只是列表里没有
    匹配关键词的帖子，旧实现却提示"（可能被反爬限制）"误导用户；本测试
    确保"无匹配"与"被反爬"两种提示被正确区分。
    """
    monkeypatch.setattr(
        "connectors.tieba_connector._env_or_dotenv",
        lambda k: "https://cf.eztb.org" if k == "TIEBA_API_BASE" else "",
    )
    body = {
        "threadList": [
            {"tid": "123", "title": "选课求助贴"},
            {"tid": "456", "title": "请假制度求助"},
        ],
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = body
    with patch("connectors.tieba_connector.httpx.get", return_value=mock_resp):
        result = tieba_search("食堂")  # 列表中无此关键词
    assert "未找到" in result
    assert "反爬" not in result, "正常列表无匹配不应误报为反爬"
    assert "换个关键词" in result


def test_tieba_search_failure(monkeypatch):
    """抓取失败（反爬/网络错误）应返回可读错误。"""
    def raise_error(url, params=None):
        raise RuntimeError("触发反爬 HTTP 403")
    monkeypatch.setattr("connectors.tieba_connector._env_or_dotenv", lambda k: "")
    monkeypatch.setattr("connectors.tieba_connector._fetch_html", raise_error)
    # 渲染 fallback 也无结果，保留原始错误信息
    monkeypatch.setattr("connectors.tieba_connector._render_posts", lambda url, params=None: [])
    result = tieba_search("任意关键词")
    assert result.startswith("[错误]")


def test_tieba_user_cookie_mode_uses_session():
    """用户会话模式：配置了 TIEBA_COOKIE 时应直接携带 Cookie 抓目标页，不预热首页。

    说明：TIEBA_COOKIE 是用户浏览器登录贴吧后 F12 导出的真实会话（仅存本机），
    连接器复用该会话访问公开帖子——这是"用户本人真实会话"模式，代码无绕过逻辑。
    """
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = TIEBA_HTML
    client.get.return_value = resp

    with patch("connectors.tieba_connector._env_or_dotenv", return_value="bduss=test; STOKEN=abc"):
        result = _warmup_and_fetch(client, "https://tieba.baidu.com/f", {"kw": "中国地质大学武汉吧"})

    # 只请求目标页 1 次（未预热首页），且携带用户 Cookie
    assert client.get.call_count == 1
    call_kwargs = client.get.call_args.kwargs
    assert call_kwargs["headers"]["Cookie"] == "bduss=test; STOKEN=abc"
    assert result == TIEBA_HTML


def test_tieba_anonymous_mode_warms_up():
    """匿名模式：未配置 TIEBA_COOKIE 时应先预热首页再抓目标页（原有行为）。"""
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = TIEBA_HTML
    client.get.return_value = resp

    with patch("connectors.tieba_connector._env_or_dotenv", return_value=""):
        _warmup_and_fetch(client, "https://tieba.baidu.com/f", {"kw": "中国地质大学武汉吧"})

    # 预热首页 + 目标页共 2 次请求，目标页请求不带用户 Cookie
    assert client.get.call_count == 2
    last_kwargs = client.get.call_args.kwargs
    assert "Cookie" not in last_kwargs["headers"]


# ===== 小红书连接器 =====

def test_xhs_search_missing_cookie(monkeypatch):
    """未配置 Cookie 时应返回降级提示（高风险渠道不默认开启）。"""
    monkeypatch.delenv("XHS_COOKIE", raising=False)
    monkeypatch.setattr("connectors.xiaohongshu_connector._env_or_dotenv", lambda k: "")
    result = xhs_search("中国地质大学")
    assert "XHS_COOKIE" in result


def test_xhs_search_success(monkeypatch):
    """正常响应应返回标题/摘要/链接。"""
    # 按 key 区分：XHS_COOKIE 有值、XHS_API_BASE 未配置（走 Cookie 直连模式）
    monkeypatch.setattr(
        "connectors.xiaohongshu_connector._env_or_dotenv",
        lambda k: "web_session=abc" if k == "XHS_COOKIE" else "",
    )
    body = {
        "data": {
            "items": [
                {
                    "id": "abc123",
                    "note_card": {
                        "display_title": "中国地质大学（武汉）校园探秘",
                        "desc": "分享地大的校园生活与宿舍环境",
                    },
                }
            ]
        }
    }
    monkeypatch.setattr("connectors.xiaohongshu_connector.backoff_retry", lambda fn, **k: json.dumps(body, ensure_ascii=False))
    result = xhs_search("中国地质大学")
    assert "中国地质大学（武汉）校园探秘" in result
    assert "abc123" in result


def test_xhs_search_empty(monkeypatch):
    """无结果时应返回提示。"""
    monkeypatch.setattr(
        "connectors.xiaohongshu_connector._env_or_dotenv",
        lambda k: "web_session=abc" if k == "XHS_COOKIE" else "",
    )
    body = {"data": {"items": []}}
    monkeypatch.setattr("connectors.xiaohongshu_connector.backoff_retry", lambda fn, **k: json.dumps(body, ensure_ascii=False))
    result = xhs_search("不存在的话题xyz")
    assert "未找到" in result


def test_xhs_search_external_service_mode(monkeypatch):
    """外部服务模式（BYO）：配置 XHS_API_BASE 时应调用用户自配服务并解析约定 JSON。

    说明：本项目不包含签名/逆向代码，仅以通用 HTTP 客户端调用用户自部署的
    xhs 数据服务（见 docs/xhs-service-guide.md），此处 mock 该服务返回。
    """
    monkeypatch.setattr(
        "connectors.xiaohongshu_connector._env_or_dotenv",
        lambda k: "http://127.0.0.1:5100" if k == "XHS_API_BASE" else "",
    )
    body = {
        "code": 0,
        "data": {
            "items": [
                {
                    "title": "地大宿舍攻略",
                    "desc": "南望山 vs 未来城宿舍对比",
                    "url": "https://www.xiaohongshu.com/explore/abc123",
                }
            ]
        },
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = body
    with patch("connectors.xiaohongshu_connector.httpx.get", return_value=mock_resp) as mock_get:
        result = xhs_search("地大宿舍")
    assert "地大宿舍攻略" in result
    assert "https://www.xiaohongshu.com/explore/abc123" in result
    # 请求应打到用户自配的服务地址
    assert mock_get.call_args.args[0] == "http://127.0.0.1:5100/search"


def test_xhs_search_external_service_error(monkeypatch):
    """外部服务返回非 0 code 时应透传错误信息。"""
    monkeypatch.setattr(
        "connectors.xiaohongshu_connector._env_or_dotenv",
        lambda k: "http://127.0.0.1:5100" if k == "XHS_API_BASE" else "",
    )
    body = {"code": 1001, "message": "账号被风控"}
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = body
    with patch("connectors.xiaohongshu_connector.httpx.get", return_value=mock_resp):
        result = xhs_search("任意关键词")
    assert "账号被风控" in result


def test_xhs_search_detects_300011(monkeypatch):
    """直连模式应识别账号级风控 300011，而不是误报为"未找到"。

    回归场景：此前 300011 响应（data={}）被当成"未找到"，用户无法定位
    是账号被风控；本次小号实测暴露该缺陷，修复为明确提示。
    """
    monkeypatch.setattr(
        "connectors.xiaohongshu_connector._env_or_dotenv",
        lambda k: "web_session=abc" if k == "XHS_COOKIE" else "",
    )
    body = {"code": 300011, "success": False, "msg": "当前账号存在异常，请切换账号后重试", "data": {}}
    monkeypatch.setattr(
        "connectors.xiaohongshu_connector.backoff_retry",
        lambda fn, **k: json.dumps(body, ensure_ascii=False),
    )
    result = xhs_search("中国地质大学")
    assert "300011" in result
    assert "账号风控" in result
    assert "未找到" not in result


# ===== 贴吧 Playwright 渲染提取（贴吧网页版改版 CSR 后的数据源）=====

class _FakePWPage:
    """模拟 Playwright Page：记录 goto 参数，返回预设的提取结果。"""

    def __init__(self, items):
        self._items = items
        self.url_called = None
        self.params_called = None

    def goto(self, url, params=None, **kwargs):
        self.url_called = url
        self.params_called = params

    def wait_for_timeout(self, *args):
        pass

    def eval_on_selector_all(self, selector, js):
        return self._items


class _FakePWContext:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page

    def add_cookies(self, cookies):
        pass


class _FakePWBrowser:
    def __init__(self, page):
        self._page = page
        self.closed = False

    def new_context(self, **kwargs):
        return _FakePWContext(self._page)

    def close(self):
        self.closed = True


class _FakePW:
    """模拟 playwright.sync_api 的入口对象。"""

    def __init__(self, items):
        self._browser = _FakePWBrowser(_FakePWPage(items))
        self.launched = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def chromium(self):
        return self

    def launch(self, **kwargs):
        self.launched = kwargs
        return self._browser


def test_tieba_render_posts_extracts(monkeypatch):
    """Playwright 渲染应提取帖子标题并规范化为 /p/{tid} 干净链接。

    说明：贴吧网页版已改版为 CSR（JS 渲染），纯 HTTP 拿不到帖子；
    _render_posts 用真实浏览器渲染后按 DOM 提取。此处用 fake Playwright
    验证 Python 侧的处理逻辑（链接规范化/标题清洗/浏览器关闭）。
    """
    pytest = __import__("pytest")
    playwright_sync = pytest.importorskip("playwright.sync_api")  # 环境无 Playwright 时跳过
    items = [
        {"href": "https://tieba.baidu.com/p/9067184456?fr=frs", "title": "再次提醒各位24级新生关注志愿录取规则"},
        {"href": "https://tieba.baidu.com/p/8673247008?fr=frs", "title": "吧务珍重申明"},
        {"href": "https://tieba.baidu.com/p/10925894346?fr=frs", "title": "求助考研资料"},
    ]
    fake = _FakePW(items)
    monkeypatch.setattr("connectors.tieba_connector._env_or_dotenv", lambda k: "BDUSS=test")
    monkeypatch.setattr(playwright_sync, "sync_playwright", lambda: fake)

    results = _render_posts("https://tieba.baidu.com/f", {"kw": "中国地质大学武汉吧"})

    assert len(results) == 3
    assert results[0] == ("再次提醒各位24级新生关注志愿录取规则", "https://tieba.baidu.com/p/9067184456")
    # 链接统一为干净的 /p/{tid} 形式（去除 ?fr=frs 查询参数）
    assert all("?" not in link for _, link in results)
    # goto 的 URL 应拼接了 kw 查询参数（Playwright goto 不支持 params，需自行拼串）
    assert "kw=" in fake._browser._page.url_called
    # 浏览器应正常关闭（资源释放）
    assert fake._browser.closed
