# -*- coding: utf-8 -*-
"""信息门户只读连接器单元测试：mock HTTP/会话，验证解析与错误处理。"""

import json
from unittest.mock import MagicMock, patch

from connectors.portal_connector import (
    portal_finished_tasks,
    portal_my_processes,
    portal_pending_notices,
    portal_personal_info,
    portal_service_catalog,
    portal_service_items,
    portal_study_room_timetable,
    portal_todo_tasks,
)


def _mock_resp(status: int, body=None, text: str = ""):
    """构造模拟的 httpx.Response。

    说明：连接器部分代码读取 resp.text（原始 JSON 文本），部分读取 resp.json()；
    这里当提供 body 且未显式给 text 时，自动用 JSON 序列化填充 text，保证两条路径都可用。
    """
    r = MagicMock()
    r.status_code = status
    if text == "" and body is not None:
        text = json.dumps(body, ensure_ascii=False)
    r.text = text
    if body is not None:
        r.json.return_value = body
    return r


# ===== portal_my_processes（办事流程-我发起的） =====

def test_my_processes_success(monkeypatch):
    """正常返回流程列表时应解析标题/时间/状态等字段。

    说明：接口实际返回 cells 为 [{col, value}, ...] 结构（实测），
    连接器需按 col/value 提取后展示。
    """
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: "CASTGC=abc")
    body = {
        "data": {
            "data": [
                {
                    "cells": [
                        {"col": "fdSubject", "value": "示例学生(20231000000)WPS企业会员/权益包授权"},
                        {"col": "fdStartTime", "value": 1762338616000},
                        {"col": "fdStatus", "value": "结束"},
                        {"col": "fdAppName", "value": "数智地大"},
                        {"col": "fdModuleName", "value": "流程管理"},
                    ]
                },
                {
                    "cells": [
                        {"col": "fdSubject", "value": "请假申请"},
                        {"col": "fdStartTime", "value": 1762500000000},
                        {"col": "fdStatus", "value": "审批中"},
                        {"col": "fdHandlerName", "value": "张三"},
                        {"col": "fdAppName", "value": "数智地大"},
                        {"col": "fdModuleName", "value": "流程管理"},
                    ]
                },
            ]
        }
    }
    with patch("connectors.portal_connector.httpx.post", return_value=_mock_resp(200, body)) as mock_post:
        result = portal_my_processes(limit=10)
    assert "WPS企业会员" in result
    assert "请假申请" in result
    assert "审批中" in result
    # 请求应打到 lbpm 列表接口且带门户会话 cookie
    assert mock_post.call_args.args[0] == "https://i.cug.edu.cn/data/lbpm-approval/portlet/myCreated/list"


def test_my_processes_empty(monkeypatch):
    """无流程记录时应返回可读提示。"""
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: "CASTGC=abc")
    body = {"data": {"data": []}}
    with patch("connectors.portal_connector.httpx.post", return_value=_mock_resp(200, body)):
        result = portal_my_processes()
    assert "没有我发起的办事流程" in result


def test_my_processes_session_missing(monkeypatch):
    """门户会话缺失时应返回重新登录指引。"""
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: None)
    result = portal_my_processes()
    assert result.startswith("[错误]")
    assert "session-login" in result


# ===== portal_study_room_timetable（自习室课表） =====

def test_study_room_timetable_success(monkeypatch):
    """正常返回课表文档列表时应解析教学楼名，并逐条拉详情获取课表图片。

    说明（实测打通）：列表接口（sysModelingMain/data）只返回标题/记录ID，
    课表图片在记录详情接口（sysModelingMain/view）的附件里（data.mechanisms.attachment），
    本测试按 URL 分发 mock：data 返回文档列表、view 返回附件下载信息，
    断言连接器把 downloadUrl 汇总给下载函数（/live_room 图片下载链路）。
    """
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: "CASTGC=abc")
    list_body = {
        "data": {
            "content": [
                {"fd_doc_subject": "东教楼", "fd_id": "rec1"},
                {"fd_doc_subject": "北区综合楼", "fd_id": "rec2"},
            ]
        }
    }
    detail_body = {
        "data": {
            "dynamicProps": {"tb_zxskbcx_drkb_jxlmc": "东教楼"},
            "mechanisms": {
                "attachment": [
                    {"downloadUrl": "/data/sys-attach/download/abc123", "fullName": "东教楼课表.png"},
                ]
            },
        }
    }

    def fake_post(url, *a, **k):
        # 列表接口返回文档列表；详情接口返回附件下载信息（两接口同一会话 cookie）
        if url.endswith("/sysModelingMain/data"):
            return _mock_resp(200, list_body)
        if url.endswith("/sysModelingMain/view"):
            return _mock_resp(200, detail_body)
        return _mock_resp(404)

    fake_saved = ["data/exports/live_room/东教楼课表.png", "data/exports/live_room/东教楼课表_2.png"]
    with patch("connectors.portal_connector.httpx.post", side_effect=fake_post) as mock_post, \
         patch("connectors.portal_connector._download_room_files", return_value=fake_saved) as mock_dl:
        result = portal_study_room_timetable(limit=10)
    assert "东教楼" in result
    assert "北区综合楼" in result
    # 第一次请求打到 sys-modeling 列表接口（fdListViewId 标识）
    assert mock_post.call_args_list[0].args[0] == "https://i.cug.edu.cn/data/sys-modeling/sysModelingMain/data"
    # 随后对每条记录调详情接口（sysModelingMain/view）拿附件下载信息
    view_urls = [c.args[0] for c in mock_post.call_args_list if c.args[0].endswith("/sysModelingMain/view")]
    assert len(view_urls) == 2
    # 两条记录返回同一 downloadUrl → 按 URL 去重后只汇总 1 张图（修复：
    # 实测多条"东教楼"记录引用同一张课表图，此前会下载多份一模一样的文件）
    assert len(mock_dl.call_args.args[0]) == 1
    # 详情附件 downloadUrl 是相对路径，已拼门户域名后汇总给下载函数
    assert mock_dl.call_args.args[0][0]["url"] == "https://i.cug.edu.cn/data/sys-attach/download/abc123"
    assert "已下载 2 张课表图片" in result


def test_study_room_timetable_empty(monkeypatch):
    """无已发布课表文档时应提示后勤保障部更新节奏。"""
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: "CASTGC=abc")
    body = {"data": {"content": []}}
    with patch("connectors.portal_connector.httpx.post", return_value=_mock_resp(200, body)):
        result = portal_study_room_timetable()
    assert "没有已发布的自习室课表" in result


def test_study_room_timetable_http_error(monkeypatch):
    """会话失效（401/901）时应返回重新登录指引。"""
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: "CASTGC=abc")
    with patch("connectors.portal_connector.httpx.post", return_value=_mock_resp(901)):
        result = portal_study_room_timetable()
    assert result.startswith("[错误]")
    assert "session-login" in result


def test_download_room_files_dedup_by_content(monkeypatch):
    """不同 downloadUrl 但内容相同（同一张课表图被重复上传）应只保留一份。

    说明（实测根因）：列表 3 条"东教楼"记录详情返回 3 个**不同**
    附件 URL（不同附件 ID），但下载后内容 MD5 完全相同——URL/文件名去重都拦不住，
    必须按内容 MD5 判重。本测试 mock 两个不同 URL 返回相同字节，断言只保存 1 份。
    """
    import connectors.portal_connector as pmod

    monkeypatch.setattr("connectors.portal_connector._get_session_or_error", lambda: "CASTGC=abc")

    def fake_get(url, *a, **k):
        # 两个 URL 返回完全相同的内容（模拟管理员重复上传同一张图）
        r = MagicMock()
        r.status_code = 200
        r.content = b"\x89PNG-same-content"
        r.headers = {"content-type": "image/png"}
        return r

    with patch("connectors.portal_connector.httpx.get", side_effect=fake_get):
        saved = pmod._download_room_files([
            {"url": "https://i.cug.edu.cn/a.png", "name": "课表a"},
            {"url": "https://i.cug.edu.cn/b.png", "name": "课表b"},
        ])
    assert len(saved) == 1  # 内容相同只保留第一份
    # 清理测试产物（data/ 不入库，但保持干净）
    from pathlib import Path
    for p in saved:
        Path(p).unlink(missing_ok=True)


# ===== portal_service_items（南望厅服务事项，公开页） =====

SERVICE_HTML = """
<html><body>
<nav>首页 返回</nav>
<div class="item">
  <h3>本科生在校证明</h3>
  <p>本科生院 办理时间：工作日</p>
</div>
<div class="item">
  <h3>学籍档案材料办理</h3>
  <p>图书档案与文博部</p>
</div>
<div class="footer">© 中国地质大学（武汉） 服务热线：027-67885111</div>
</body></html>
"""


def test_service_items_success(monkeypatch):
    """公开页抓取应提取服务事项并可关键词过滤。"""
    with patch("connectors.portal_connector.httpx.get", return_value=_mock_resp(200, text=SERVICE_HTML)):
        result = portal_service_items()
    assert "本科生在校证明" in result
    assert "学籍档案材料办理" in result


def test_service_items_keyword_filter(monkeypatch):
    """关键词过滤只保留匹配项。"""
    with patch("connectors.portal_connector.httpx.get", return_value=_mock_resp(200, text=SERVICE_HTML)):
        result = portal_service_items(keyword="学籍")
    assert "学籍档案材料办理" in result
    assert "本科生在校证明" not in result


def test_service_items_no_match(monkeypatch):
    """无匹配关键词时应返回可读提示。"""
    with patch("connectors.portal_connector.httpx.get", return_value=_mock_resp(200, text=SERVICE_HTML)):
        result = portal_service_items(keyword="不存在的服务xyz")
    assert "未找到" in result


# ===== portal_service_catalog（网上厅服务目录） =====

CATALOG_BODY = {
    "data": [
        {
            "children": [
                {
                    "text": "教学管理",
                    "children": [
                        {
                            "text": "本科生在校证明",
                            "fdShortName": "本科生在校证明",
                            "fdTransact": "https://i.cug.edu.cn/web/#/current/sys-modeling/app/km-zz/add/abc",
                            "fdDept": {"fdName": "本科生院"},
                            "fdTele": "027-67885000",
                            "fdGuide": "<p>在线申请，工作日办理</p>",
                        },
                        {
                            "text": "自习室课表查询",
                            "fdTransact": "https://i.cug.edu.cn/web/#/current/sys-modeling/app/app-701",
                            "fdDept": {"fdName": "后勤保障部"},
                            "fdTele": "",
                            "fdGuide": "",
                        },
                    ],
                },
                {
                    "text": "综合事务",
                    "children": [
                        {
                            "text": "场地预约",
                            "fdTransact": "https://i.cug.edu.cn/web/#/current/sys-modeling/app/app-422-booking",
                            "fdDept": {"fdName": "本科生院"},
                            "fdTele": "",
                            "fdGuide": "",
                        },
                    ],
                },
            ]
        }
    ]
}


def test_service_catalog_overview(monkeypatch):
    """无关键词时应按分类返回服务名概览。"""
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: "X-AUTH-TOKEN=abc")
    with patch("connectors.portal_connector.httpx.post", return_value=_mock_resp(200, CATALOG_BODY)):
        result = portal_service_catalog()
    assert "网上厅服务目录总览" in result
    assert "教学管理" in result
    assert "本科生在校证明" in result
    assert "场地预约" in result


def test_service_catalog_keyword(monkeypatch):
    """带关键词应返回匹配服务的详细信息（部门/电话/入口/指南）。"""
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: "X-AUTH-TOKEN=abc")
    with patch("connectors.portal_connector.httpx.post", return_value=_mock_resp(200, CATALOG_BODY)):
        result = portal_service_catalog(keyword="在校证明")
    assert "本科生在校证明" in result
    assert "本科生院" in result
    assert "办理入口" in result
    assert "场地预约" not in result


def test_service_catalog_no_match(monkeypatch):
    """无匹配关键词时应提示未找到。"""
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: "X-AUTH-TOKEN=abc")
    with patch("connectors.portal_connector.httpx.post", return_value=_mock_resp(200, CATALOG_BODY)):
        result = portal_service_catalog(keyword="不存在服务xyz")
    assert "未找到" in result


def test_service_catalog_session_missing(monkeypatch):
    """门户会话缺失时应返回重新登录指引。"""
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: None)
    result = portal_service_catalog()
    assert result.startswith("[错误]")
    assert "session-login" in result


# ===== portal_todo_tasks / portal_finished_tasks（待办/已办） =====

def test_todo_tasks_empty(monkeypatch):
    """无待办任务时应返回可读提示。"""
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: "X-AUTH-TOKEN=abc")
    body = {"data": {"content": [], "totalSize": 0}}
    with patch("connectors.portal_connector.httpx.post", return_value=_mock_resp(200, body)):
        result = portal_todo_tasks()
    assert "没有待办" in result


def test_todo_tasks_success(monkeypatch):
    """有待办时应列出任务标题。"""
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: "X-AUTH-TOKEN=abc")
    body = {"data": {"content": [{"fdSubject": "请假申请待审批"}, {"fdSubject": "场地预约待审批"}]}}
    with patch("connectors.portal_connector.httpx.post", return_value=_mock_resp(200, body)):
        result = portal_todo_tasks()
    assert "请假申请待审批" in result
    assert "场地预约待审批" in result


def test_finished_tasks_success(monkeypatch):
    """已办事项应列出主题/状态/单号。"""
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: "X-AUTH-TOKEN=abc")
    body = {
        "data": {
            "data": [
                {"fdSubject": "WPS企业会员授权", "fdProcessStatus": "结束", "fdNumber": "SQ2026001"},
            ]
        }
    }
    with patch("connectors.portal_connector.httpx.post", return_value=_mock_resp(200, body)):
        result = portal_finished_tasks()
    assert "WPS企业会员授权" in result
    assert "SQ2026001" in result


def test_finished_tasks_session_error(monkeypatch):
    """会话失效（901）时应返回重新登录指引。"""
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: "X-AUTH-TOKEN=abc")
    with patch("connectors.portal_connector.httpx.post", return_value=_mock_resp(901)):
        result = portal_finished_tasks()
    assert result.startswith("[错误]")
    assert "session-login" in result


# ===== portal_personal_info / portal_pending_notices（个人中心/待阅通知） =====

def test_personal_info_success(monkeypatch):
    """个人中心信息应返回绑定手机号。"""
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: "X-AUTH-TOKEN=abc")
    body = {"data": {"fdMobileNo": "13800000000"}}
    with patch("connectors.portal_connector.httpx.post", return_value=_mock_resp(200, body)):
        result = portal_personal_info()
    assert "13800000000" in result


def test_pending_notices_success(monkeypatch):
    """待阅通知应列出标题/发布人/时间/跳转链接。"""
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: "X-AUTH-TOKEN=abc")
    body = {
        "data": [
            {
                "text": "关于选课的通知",
                "creator": {"fdName": "本科生院"},
                "fdModuleName": "教学管理",
                "fdAppName": "数智地大",
                "created": 1767599766044,
                "href": "/data/sys-notify/sysNotifyTodo/jump?fdId=abc",
            }
        ]
    }
    with patch("connectors.portal_connector.httpx.post", return_value=_mock_resp(200, body)):
        result = portal_pending_notices()
    assert "关于选课的通知" in result
    assert "本科生院" in result
    assert "jump" in result


def test_pending_notices_empty(monkeypatch):
    """无待阅通知时应返回可读提示。"""
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: "X-AUTH-TOKEN=abc")
    body = {"data": []}
    with patch("connectors.portal_connector.httpx.post", return_value=_mock_resp(200, body)):
        result = portal_pending_notices()
    assert "没有待阅通知" in result


def test_my_processes_includes_href(monkeypatch):
    """我发起的流程应附带详情页链接（agent 只给入口，详情由用户查看）。"""
    monkeypatch.setattr("connectors.portal_connector.get_portal_cookie", lambda: "CASTGC=abc")
    body = {
        "data": {
            "data": [
                {
                    "href": "/web/#/current/km-review/kmReviewMain/view/abc123",
                    "cells": [{"col": "fdSubject", "value": "请假申请"}],
                }
            ]
        }
    }
    with patch("connectors.portal_connector.httpx.post", return_value=_mock_resp(200, body)):
        result = portal_my_processes()
    assert "请假申请" in result
    assert "https://i.cug.edu.cn/web/#/current/km-review/kmReviewMain/view/abc123" in result
