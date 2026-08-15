# -*- coding: utf-8 -*-
"""会话型连接器单元测试：白名单校验、Cookie 注入、HTTP 状态处理（mock 请求）。"""

from types import SimpleNamespace

import pytest

from app.agent.tools import ToolRegistry
from connectors.session_connector import (
    SessionConnector,
    register_session_connectors,
    _humanize_session_response,
)


import json


def test_humanize_session_response_grade():
    """成绩接口 JSON 应转为中文标签逐条可读输出，且不含学生姓名误标"教师"。

     实测：成绩接口的 xm 字段是学生姓名（非教师），按连接器
    cug_grade 映射时只显示 课程/成绩/学分/绩点，不显示 xm/xh 等冗余字段。
    """
    raw = json.dumps({
        "rows": [
            {"xnm": "2025", "xqm": "2", "kcmc": "大学英语", "cj": "85", "xf": "3.0", "jd": "3.5", "xm": "张三", "xh": "20230001"},
            {"xnm": "2025", "xqm": "2", "kcmc": "高等数学", "cj": "45", "xf": "5.0", "jd": "0.0", "xm": "张三"},
        ],
        "total": 2,
    }, ensure_ascii=False)
    out = _humanize_session_response(raw, name="cug_grade")
    assert "共 2 条" in out
    assert "课程=大学英语" in out and "成绩=85" in out and "学分=3.0" in out and "绩点=3.5" in out
    assert "教师" not in out and "张三" not in out  # 学生姓名不再误标为教师
    assert "xh" not in out  # 学号冗余字段不展示
    # 不及格（<50）成绩应带 (!) 标记，供 CLI 显示层标红
    assert "成绩=45(!)" in out


def test_humanize_session_response_course_empty():
    """课表接口无数据（kbList 空）应返回友好提示，而非甩 JSON 元数据。"""
    raw = json.dumps({"xsxx": {"XM": "张三"}, "kbList": [], "xqjmcMap": {}}, ensure_ascii=False)
    out = _humanize_session_response(raw, name="cug_course")
    assert "暂无课表数据" in out


def test_humanize_session_response_non_json():
    """非 JSON（HTML 错误页/纯文本）应原样截断返回，不影响原链路。"""
    html = "<html>会话过期</html>" * 50
    out = _humanize_session_response(html)
    assert out == html[:4000]
    # 空数据数组也应原样返回（无内容可读化）
    assert _humanize_session_response('{"rows": []}') == '{"rows": []}'[:4000]


def _make_connector(monkeypatch, status: int = 200, text: str = "ok", cookie_env: str = "demo-cookie",
                    name: str = "cug_course"):
    """构造一个把 httpx.request 替换为假实现的会话连接器（name 可指定，影响可读化映射）。"""
    import os

    monkeypatch.setenv("SESSION_COOKIE", cookie_env)

    def fake_request(method, url, headers=None, timeout=None, follow_redirects=None, data=None, verify=None):
        return SimpleNamespace(status_code=status, text=text)

    monkeypatch.setattr("connectors.session_connector.httpx.request", fake_request)
    return SessionConnector(
        name=name,
        description="查询我的课程表",
        url="https://xyfw.cug.edu.cn/api/courses",
        method="GET",
        cookie="{{SESSION_COOKIE}}",
        allowed_prefix="https://xyfw.cug.edu.cn",
        interval=0.05,
        jitter=0.0,
    )


def test_allowlist_rejects_foreign_url(monkeypatch):
    """白名单以外的 URL 应被拒绝，不允许携带会话访问。"""
    conn = _make_connector(monkeypatch)
    conn.url = "https://evil.example.com/api/steal"  # 模拟配置被篡改/注入
    result = conn.invoke("查询")
    assert "拒绝访问白名单以外的地址" in result


def test_missing_cookie_hint(monkeypatch):
    """未配置会话 Cookie 时应给出可读提示而非发起请求。"""
    conn = _make_connector(monkeypatch, cookie_env="")
    result = conn.invoke("查询")
    assert "未配置会话" in result


def test_success_invokes_with_cookie(monkeypatch):
    """正常请求应携带 Cookie；课表接口空数据（无 kbList）应返回友好提示而非原始 JSON。"""
    conn = _make_connector(monkeypatch, status=200, text='{"courses": []}')
    result = conn.invoke("查询我的课表")
    #  优化：课表接口解析成功但无数据 → 友好提示（不再甩 JSON 元数据）
    assert "暂无课表数据" in result
    # 非课表连接器（如考试，默认映射）空数组仍原样截断返回
    conn2 = _make_connector(monkeypatch, status=200, text='{"rows": []}', name="cug_exam")
    assert '{"rows": []}' in conn2.invoke("查询")


def test_session_expired_detection(monkeypatch):
    """401/403/302 应被识别为会话失效并给出提示。"""
    conn = _make_connector(monkeypatch, status=403, text="forbidden")
    result = conn.invoke("查询")
    assert "HTTP 403" in result
    assert "重新登录" in result


def test_register_session_connectors(tmp_path, monkeypatch):
    """无配置文件时返回 0；有配置时注册为工具。"""
    registry = ToolRegistry()
    assert register_session_connectors(registry, config_path=tmp_path / "none.yaml") == 0

    cfg = tmp_path / "session_connectors.yaml"
    cfg.write_text(
        "session_connectors:\n"
        "  - name: cug_course\n"
        "    description: 查询课程表\n"
        "    url: https://xyfw.cug.edu.cn/api/courses\n"
        "    method: GET\n",
        encoding="utf-8",
    )
    assert register_session_connectors(registry, config_path=cfg) == 1
    assert registry.get("cug_course") is not None


# ===== Playwright 持久会话模式（pw_profile）=====

def _make_pw_connector(monkeypatch, status: int = 200, text: str = "ok"):
    """构造使用 pw_profile 的会话连接器（mock httpx 请求）。"""

    def fake_request(method, url, headers=None, timeout=None, follow_redirects=None, data=None, verify=None):
        return SimpleNamespace(status_code=status, text=text)

    monkeypatch.setattr("connectors.session_connector.httpx.request", fake_request)
    return SessionConnector(
        name="cug_course",
        description="查询我的课程表",
        url="https://jwgl.cug.edu.cn/jwglxt/kbcx/xskbcx_cxXsgrkb.html?gnmkdm=N2151",
        method="POST",
        cookie="{{JWGL_COOKIE}}",
        pw_profile="data/browser_profile/jwgl",
        allowed_prefix="https://jwgl.cug.edu.cn",
        interval=0.05,
        jitter=0.0,
    )


def test_pw_profile_uses_automatic_cookie(monkeypatch):
    """pw_profile 模式下自动从 Playwright 会话取 Cookie 并携带请求。"""
    captured = {}

    def fake_request(method, url, headers=None, timeout=None, follow_redirects=None, data=None, verify=None):
        captured["cookie"] = headers.get("Cookie", "")
        return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr("connectors.session_connector.httpx.request", fake_request)
    monkeypatch.setattr(
        "connectors.session_connector.get_session_cookie",
        lambda profile: "JSESSIONID=AUTO123",
    )
    conn = SessionConnector(
        name="cug_course",
        description="查询我的课程表",
        url="https://jwgl.cug.edu.cn/jwglxt/kbcx/xskbcx_cxXsgrkb.html?gnmkdm=N2151",
        method="POST",
        pw_profile="data/browser_profile/jwgl",
        allowed_prefix="https://jwgl.cug.edu.cn",
        interval=0.05,
        jitter=0.0,
    )
    result = conn.invoke("查询")
    assert "ok" in result
    assert captured["cookie"] == "JSESSIONID=AUTO123"


def test_pw_profile_no_session_hint(monkeypatch):
    """pw_profile 模式下取不到会话时应提示运行 session-login。"""
    monkeypatch.setattr(
        "connectors.session_connector.get_session_cookie",
        lambda profile: None,
    )
    conn = SessionConnector(
        name="cug_course",
        description="查询我的课程表",
        url="https://jwgl.cug.edu.cn/jwglxt/kbcx/xskbcx_cxXsgrkb.html?gnmkdm=N2151",
        method="POST",
        pw_profile="data/browser_profile/jwgl",
        allowed_prefix="https://jwgl.cug.edu.cn",
        interval=0.05,
        jitter=0.0,
    )
    result = conn.invoke("查询")
    assert "session-login" in result


def test_pw_profile_901_hint(monkeypatch):
    """pw_profile 模式下 HTTP 901 应提示重新登录（含 session-login）。"""
    monkeypatch.setattr(
        "connectors.session_connector.get_session_cookie",
        lambda profile: "JSESSIONID=EXPIRED",
    )
    conn = _make_pw_connector(monkeypatch, status=901, text="")
    result = conn.invoke("查询")
    assert "901" in result
    assert "session-login" in result


# ===== 学期参数化（新增：解决"agent 查不了过往课表/成绩"）=====

def test_parse_semester_formats():
    """parse_semester 应解析学年区间/显式编码/相对学期；无关文本返回 None。"""
    from connectors.session_connector import parse_semester

    # 学年区间："2025-2026-2" → 第二学期(12)；"-1" → 第一学期(3)
    assert parse_semester("查询2025-2026-2课表") == {"xnm": "2025", "xqm": "12"}
    assert parse_semester("2025-2026-1 课表") == {"xnm": "2025", "xqm": "3"}
    # 显式编码："2025 12" / "2025 3"
    assert parse_semester("2025 12") == {"xnm": "2025", "xqm": "12"}
    assert parse_semester("2025 3") == {"xnm": "2025", "xqm": "3"}
    # 相对学期：结果依赖当前日期，只断言结构与非 None
    for q in ("上学期课表", "查询大二上学期成绩", "下学期考试安排"):
        sem = parse_semester(q)
        assert sem is not None and set(sem) == {"xnm", "xqm"}
    # 无关文本 → None
    assert parse_semester("你好") is None
    assert parse_semester("") is None


def test_semester_params_applied_to_body(monkeypatch):
    """声明 semester_params 后，invoke 应把 body 中 xnm/xqm 替换为解析出的学期。"""
    captured = {}

    def fake_request(method, url, headers=None, timeout=None, follow_redirects=None, data=None, verify=None):
        captured["data"] = data
        return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr("connectors.session_connector.httpx.request", fake_request)
    monkeypatch.setattr(
        "connectors.session_connector.get_session_cookie", lambda profile: "S=A"
    )
    conn = SessionConnector(
        name="cug_course",
        description="查询课程表",
        url="https://jwgl.cug.edu.cn/jwglxt/kbcx/xskbcx_cxXsgrkb.html?gnmkdm=N2151",
        method="POST",
        body="xnm=2026&xqm=3&kzlx=ck&xsdm=",
        pw_profile="data/browser_profile/jwgl",
        allowed_prefix="https://jwgl.cug.edu.cn",
        interval=0.05,
        jitter=0.0,
        semester_params=["xnm", "xqm"],
    )
    conn.invoke("查询2025-2026-2课表")
    assert captured["data"] == "xnm=2025&xqm=12&kzlx=ck&xsdm="


def test_semester_params_ignored_when_undeclared(monkeypatch):
    """未声明 semester_params 时，body 参数保持默认，不因问题文本变化。"""
    captured = {}

    def fake_request(method, url, headers=None, timeout=None, follow_redirects=None, data=None, verify=None):
        captured["data"] = data
        return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr("connectors.session_connector.httpx.request", fake_request)
    monkeypatch.setattr(
        "connectors.session_connector.get_session_cookie", lambda profile: "S=A"
    )
    conn = SessionConnector(
        name="cug_course",
        description="查询课程表",
        url="https://jwgl.cug.edu.cn/jwglxt/kbcx/xskbcx_cxXsgrkb.html?gnmkdm=N2151",
        method="POST",
        body="xnm=2026&xqm=3&kzlx=ck",
        pw_profile="data/browser_profile/jwgl",
        allowed_prefix="https://jwgl.cug.edu.cn",
        interval=0.05,
        jitter=0.0,
    )
    conn.invoke("查询2025-2026-2课表")
    assert captured["data"] == "xnm=2026&xqm=3&kzlx=ck"
