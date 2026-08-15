# -*- coding: utf-8 -*-
"""Playwright 持久会话模块单元测试（mock playwright，不启动真实浏览器）。

登录链路说明（浏览器实测）：
    - 教务无独立登录入口，必须经信息门户进入；
    - 流程：门户搜索框输入「教务管理」→ 下拉项出现 → 点击 → 新标签页打开教务。
"""

import pytest

from types import SimpleNamespace

import connectors.pw_session as pw


class _FakePWPage:
    """伪页面：模拟门户登录态判定与「搜索→点击→新标签页」流程。"""

    def __init__(self, url: str = pw.PORTAL_HOME_URL, logged_in: bool = True,
                 popup: "_FakePWPage | None" = None) -> None:
        self.url = url
        self._logged_in = logged_in
        self._popup = popup

    def goto(self, url, **kwargs):  # noqa: ARG002
        self.url = url

    def wait_for_timeout(self, ms):  # noqa: ARG002
        return None

    def wait_for_selector(self, selector, **kwargs):  # noqa: ARG002
        # 搜索流程应能找到选择器
        return None

    def fill(self, selector, value):  # noqa: ARG002
        return None

    def click(self, selector, **kwargs):  # noqa: ARG002
        return None

    def expect_popup(self):
        return _FakePopupContext(self._popup)

    def wait_for_load_state(self, state, **kwargs):  # noqa: ARG002
        return None


class _FakePopupContext:
    """伪 expect_popup 上下文：模拟 with 语句。"""

    def __init__(self, popup) -> None:
        self._popup = popup or _FakePWPage(url=pw.JWGL_HOME_URL, logged_in=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):  # noqa: ARG002
        return None

    @property
    def value(self):
        return self._popup


class _FakePWContext:
    """伪浏览器上下文：记录 pages 与 cookies。"""

    def __init__(self, pages, cookies) -> None:
        self.pages = pages
        self._cookies = cookies

    def cookies(self, url: str) -> list[dict]:  # noqa: ARG002
        return self._cookies

    def new_page(self):
        return self.pages[0]

    def close(self):
        return None


class _FakePlaywright:
    """伪 playwright 入口。"""

    def __init__(self, contexts) -> None:
        self._contexts = list(contexts)
        self.chromium = SimpleNamespace(
            launch_persistent_context=lambda user_data_dir, **kw: self._contexts.pop(0)
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):  # noqa: ARG002
        return None


def _patch_playwright(monkeypatch, contexts):
    """把 connectors.pw_session 中的 sync_playwright 替换为伪实现。"""
    monkeypatch.setattr(
        "connectors.pw_session.sync_playwright",
        lambda: _FakePlaywright(contexts),
    )


def test_get_session_cookie_logged_in(monkeypatch, tmp_path):
    """门户已登录时经搜索进入教务并返回 Cookie 串。"""
    ctx = _FakePWContext(
        pages=[_FakePWPage(pw.PORTAL_HOME_URL, logged_in=True)],  # 门户已登录
        cookies=[
            {"name": "JSESSIONID", "value": "ABC123"},
            {"name": "route", "value": "xyz"},
        ],
    )
    _patch_playwright(monkeypatch, [ctx])
    pw._cookie_cache.clear()
    result = pw.get_session_cookie(tmp_path / "profile")
    assert result == "JSESSIONID=ABC123; route=xyz"


def test_get_session_cookie_portal_logged_out(monkeypatch, tmp_path):
    """门户会话失效（跳回统一认证登录页）应返回 None。"""
    ctx = _FakePWContext(
        pages=[_FakePWPage("https://sfrz.cug.edu.cn/authserver/login", logged_in=False)],
        cookies=[],
    )
    _patch_playwright(monkeypatch, [ctx])
    pw._cookie_cache.clear()
    assert pw.get_session_cookie(tmp_path / "profile") is None


def test_get_session_cookie_playwright_missing(monkeypatch, tmp_path):
    """未安装 Playwright 时应返回 None（由上层降级提示）。"""
    monkeypatch.setattr("connectors.pw_session.sync_playwright", None)
    pw._cookie_cache.clear()
    assert pw.get_session_cookie(tmp_path / "profile") is None


def test_cookie_cache_hit(monkeypatch, tmp_path):
    """TTL 内二次调用应命中缓存，不再启动浏览器。"""
    calls = []

    class _CountingPlaywright(_FakePlaywright):
        def __enter__(self):
            calls.append(1)
            return self

    ctx = _FakePWContext(
        pages=[_FakePWPage(pw.PORTAL_HOME_URL, logged_in=True)],
        cookies=[{"name": "JSESSIONID", "value": "ABC123"}],
    )
    monkeypatch.setattr(
        "connectors.pw_session.sync_playwright",
        lambda: _CountingPlaywright([ctx]),
    )
    pw._cookie_cache.clear()
    pw.get_session_cookie(tmp_path / "profile")
    pw.get_session_cookie(tmp_path / "profile")
    assert len(calls) == 1  # 第二次命中缓存，未再启动浏览器


def test_login_jwgl_no_playwright(monkeypatch, capsys, tmp_path):
    """未安装 Playwright 时 login_jwgl 返回 False 并给出安装提示。"""
    monkeypatch.setattr("connectors.pw_session.sync_playwright", None)
    assert pw.login_jwgl(tmp_path / "profile") is False
    assert "未安装 Playwright" in capsys.readouterr().out


def test_login_jwgl_portal_flow(monkeypatch, tmp_path):
    """login_jwgl 完整链路：门户登录 → 搜索进入教务 → 返回 True。"""
    jwgl_page = _FakePWPage(pw.JWGL_HOME_URL, logged_in=True)

    # 用可变 page 模拟登录切换：首轮检查未登录 → 轮询再次检查时已登录
    class _MutablePage(_FakePWPage):
        def __init__(self) -> None:
            super().__init__(pw.PORTAL_LOGIN_URL, logged_in=False)
            self._called = 0

        @property
        def url(self) -> str:  # 首次登录页，之后门户
            return self._url

        @url.setter
        def url(self, value: str) -> None:
            self._url = value

        def wait_for_timeout(self, ms):
            self._called += 1
            if self._called >= 2:
                self._url = pw.PORTAL_HOME_URL  # 模拟用户已完成登录

    page = _MutablePage()
    page.expect_popup = lambda: _FakePopupContext(jwgl_page)
    page.wait_for_selector = lambda s, **kw: None
    page.fill = lambda s, v: None
    page.click = lambda s, **kw: None

    ctx = _FakePWContext(pages=[page], cookies=[])
    _patch_playwright(monkeypatch, [ctx])
    assert pw.login_jwgl(tmp_path / "profile", timeout=10.0) is True


def test_login_jwgl_goto_network_error_recovered(monkeypatch, tmp_path):
    """登录页导航抛 ERR_CONNECTION_CLOSED 但页面已部分加载时应容错继续。

    实测 /login 报错：Page.goto: net::ERR_CONNECTION_CLOSED——学校登录页
    在 load 事件触发前连接被关闭；修复为 domcontentloaded + 容错后，只要页面
    URL 已进入 authserver 登录页，就不中止流程，交给轮询继续等待用户登录。
    """
    jwgl_page = _FakePWPage(pw.JWGL_HOME_URL, logged_in=True)

    class _RecoverPage(_FakePWPage):
        def __init__(self) -> None:
            super().__init__(pw.PORTAL_LOGIN_URL, logged_in=False)
            self._called = 0

        @property
        def url(self) -> str:
            return self._url

        @url.setter
        def url(self, value: str) -> None:
            self._url = value

        def goto(self, url, **kwargs):  # noqa: ARG002
            # 模拟学校登录页 load 事件前连接被关闭；页面已部分加载到登录页
            self._url = pw.PORTAL_LOGIN_URL
            raise Exception("Error: Page.goto: net::ERR_CONNECTION_CLOSED")

        def wait_for_timeout(self, ms):
            self._called += 1
            if self._called >= 3:  # 容错等待(1) + 轮询两轮后模拟用户完成登录
                self._url = pw.PORTAL_HOME_URL

    page = _RecoverPage()
    page.expect_popup = lambda: _FakePopupContext(jwgl_page)
    page.wait_for_selector = lambda s, **kw: None
    page.fill = lambda s, v: None
    page.click = lambda s, **kw: None

    ctx = _FakePWContext(pages=[page], cookies=[])
    _patch_playwright(monkeypatch, [ctx])
    assert pw.login_jwgl(tmp_path / "profile", timeout=10.0) is True


def test_login_jwgl_goto_network_error_blank(monkeypatch, capsys, tmp_path):
    """登录页导航失败且页面停在空白页时应明确报错返回 False。"""
    class _BlankPage(_FakePWPage):
        def goto(self, url, **kwargs):  # noqa: ARG002
            # 导航彻底失败：页面仍停在 about:blank，无法继续登录
            raise Exception("net::ERR_CONNECTION_CLOSED")

    ctx = _FakePWContext(pages=[_BlankPage("about:blank", logged_in=False)], cookies=[])
    _patch_playwright(monkeypatch, [ctx])
    assert pw.login_jwgl(tmp_path / "profile", timeout=10.0) is False
    assert "无法打开统一认证登录页" in capsys.readouterr().out
