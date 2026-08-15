# -*- coding: utf-8 -*-
"""信息门户只读连接器（L1 官方个人·门户服务层）。

背景（浏览器实测，见 docs/portal-capability-assessment.md）：
    - 信息门户（i.cug.edu.cn，金智数智地大 SPA）的网上厅聚合了约 100 项服务，
      数据接口统一在 `https://i.cug.edu.cn/data/` 域（JSON/form POST + 门户会话 cookie 鉴权）；
    - 其中部分服务为**只读查询**，适合 Agent 接入；其余为申请/审批型写操作（不接入）；
    - 本连接器实现已实测可用的 3 个只读服务：
        1) portal_my_processes        办事流程-我发起的（lbpm 审批引擎列表接口）
        2) portal_study_room_timetable 自习室课表查询（sys-modeling 数据接口，返回图片文档列表）
        3) portal_service_items       南望厅服务事项（公开静态页抓取，无需登录）

设计边界：
    - 只读：不提交任何表单、不发起申请/审批（写操作红线不变）；
    - 会话：门户会话为长效 CAS（CASTGC），经 Playwright 持久会话自动获取（复用
      `geopractor session-login` 的登录态），失败时提示重新登录；
    - 限速/熔断/TLS1.2：与其它连接器一致，低频防封禁。
"""

from __future__ import annotations

import json
import ssl
from urllib.parse import quote

import httpx

from app.rate_limit import CircuitBreaker, backoff_retry, get_rate_limiter
from connectors.base import tool_error, tool_info
from connectors.pw_session import get_portal_cookie

# 门户数据接口基址（金智 /data/ 域）
PORTAL_DATA_BASE = "https://i.cug.edu.cn/data"
# 门户首页（Referer 与 cookie 获取用）
PORTAL_HOME = "https://i.cug.edu.cn/web/"
# 南望厅一站式师生服务大厅（公开静态页，无需登录）
SERVICE_HALL_URL = "https://service.cug.edu.cn/fwsx.htm"

# 自习室课表应用的 fdListViewId（浏览器实测；若门户改版需重新抓取，
# 可用网上厅打开该服务后从 sysModelingMain/data 请求中提取）
_STUDY_ROOM_LIST_VIEW_ID = "1ic2k7jbqwu2w1jrtw2dtitibl5m01932nw0"
# 自习室课表记录详情接口参数（Playwright 抓包实测）：
#   sysModelingMain/view 需 fdViewId + fdFormId/fdXFormId；fdId 为列表行主键。
#   详情响应 attachment[] 含课表图片 downloadUrl（/data/sys-attach/download/<id>）。
#   若门户改版导致这些 ID 变化，打开网上厅「自习室课表查询」后从
#   sysModelingMain/index 与 sysModelingMain/view 请求参数中重新提取。
_STUDY_ROOM_VIEW_ID = "1ic288l29wtuw3st4w3ib7o1e3sr44lv3ew0"
_STUDY_ROOM_FORM_ID = "1ic288kjiwtuw3sr6w3radsld2jh79tn18w0"

# 限速与熔断（独立命名空间，防封禁核心）
_limiter = get_rate_limiter("portal", interval=5.0, jitter=1.5)
_breaker = CircuitBreaker()

# 强制 TLS1.2（与教务连接器一致：该校网关对 TLS1.3 握手存在间歇性重置）
_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.minimum_version = ssl.TLSVersion.TLSv1_2
_SSL_CONTEXT.maximum_version = ssl.TLSVersion.TLSv1_2

# 门户 SPA 的浏览器风格请求头（模拟真实前端调用，避免被风控）
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"


def _portal_headers(cookie: str, content_type: str = "application/x-www-form-urlencoded") -> dict:
    """组装门户数据接口的浏览器风格请求头（含门户会话 cookie）。"""
    return {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": content_type,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://i.cug.edu.cn",
        "Referer": PORTAL_HOME,
        "Cookie": cookie,
    }


def _post_data(url: str, cookie: str, data: str, content_type: str) -> httpx.Response:
    """在限速+退避下 POST 门户数据接口（统一错误上抛由调用方处理）。"""
    def do_request() -> httpx.Response:
        with _limiter:
            return httpx.post(
                url,
                headers=_portal_headers(cookie, content_type),
                data=data,
                timeout=15,
                verify=_SSL_CONTEXT,
            )

    return backoff_retry(do_request, retries=2, base_delay=1.0)


def _get_session_or_error() -> str | None:
    """取门户会话 cookie；失败时返回 None（调用方给出登录指引）。"""
    return get_portal_cookie()


def _session_error(source: str) -> str:
    """门户会话不可用时的统一可读提示。"""
    return (
        tool_error(source, "未取得有效门户会话。\n")
        + "  信息门户会话已失效。请运行 geopractor session-login 在浏览器中登录一次，"
        "之后 agent 将自动复用该登录态。"
    )


# ===== 1. 办事流程-我发起的（只读） =====

def portal_my_processes(limit: int = 10) -> str:
    """查询我发起的办事流程（只读列表：标题/发起时间/状态/来源模块）。

    接口（浏览器实测）：POST /data/lbpm-approval/portlet/myCreated/list，空参数即返回
    当前用户发起的流程列表（含流程状态、审批进度等），带门户会话 cookie 可只读调用。
    """
    if not _breaker.allow():
        return tool_error("portal_my_processes", "连接器处于熔断冷却中，请稍后再试")
    cookie = _get_session_or_error()
    if not cookie:
        return _session_error("portal_my_processes")
    try:
        resp = _post_data(
            f"{PORTAL_DATA_BASE}/lbpm-approval/portlet/myCreated/list",
            cookie,
            data="{}",
            content_type="application/json",
        )
    except Exception as exc:  # noqa: BLE001 网络/超时
        _breaker.record_failure()
        return tool_error("portal_my_processes", f"请求失败：{exc}")
    if resp.status_code in (301, 302, 401, 403, 901):
        _breaker.record_failure()
        return _session_error("portal_my_processes")
    if resp.status_code != 200:
        _breaker.record_failure()
        return tool_error("portal_my_processes", f"返回 HTTP {resp.status_code}")
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        _breaker.record_failure()
        return tool_error("portal_my_processes", "响应不是合法 JSON")

    rows = (body.get("data") or {}).get("data") or []
    if not rows:
        _breaker.record_success()
        return tool_info("portal_my_processes", "没有我发起的办事流程记录")
    _breaker.record_success()
    # 接口返回 data.data 为「每条流程一条记录」的列表，每条含 cells（[{col,value},...]）
    # 与 href（流程详情链接）；需逐行转换后再取值（诊断确认）。
    lines = []
    for row in rows[:limit]:
        cells = row.get("cells") or []
        rec = {c.get("col"): c.get("value") for c in cells}
        subject = rec.get("fdSubject") or "（无标题）"
        status = rec.get("fdStatus") or "未知"
        handler = rec.get("fdHandlerName") or "—"
        module = rec.get("fdModuleName") or ""
        app = rec.get("fdAppName") or ""
        ts = rec.get("fdStartTime") or 0
        time_str = f"{int(ts) / 1000:.0f}" if ts else "—"
        line = f"- {subject}\n  发起时间：{time_str}｜状态：{status}｜处理人：{handler}｜来源：{app}/{module}"
        # 附上流程详情页链接（门户内页，供用户在浏览器中查看审批记录等；
        # 写操作/详情查看由用户自己完成，agent 只给入口）
        href = row.get("href") or ""
        if href:
            line += f"\n  详情：https://i.cug.edu.cn{href if href.startswith('/') else '/' + href}"
        lines.append(line)
    head = f"我发起的办事流程（共 {len(rows)} 条，显示前 {min(limit, len(rows))} 条）：\n"
    return head + "\n".join(lines)


# ===== 2. 自习室课表查询（只读，返回图片文档列表） =====


def _fetch_room_detail(cookie: str, fd_id: str) -> dict:
    """调自习室课表记录详情接口（sysModelingMain/view），返回图片下载信息。

    背景（Playwright 抓包实测）：列表接口只返回文档标题/记录ID，
    课表**图片**存在记录详情里——详情接口响应 attachment[] 数组每项含
    downloadUrl（/data/sys-attach/download/<id>）与文件名。本函数按记录主键
    拉详情并提取图片下载 URL，供 CLI 下载到本地打开（需求 /live_room）。

    返回：{"subject": 教学楼名, "images": [{"name": 文件名, "url": 下载URL}, ...]}
    失败/无图片返回空 images（调用方回退提示），不报错。
    """
    detail_body = json.dumps(
        {
            "fdId": fd_id,
            "fdMode": 1,
            "fdViewId": _STUDY_ROOM_VIEW_ID,
            "fdFormId": _STUDY_ROOM_FORM_ID,
            "fdXFormId": _STUDY_ROOM_FORM_ID,
            "mechanisms": {"load": "*"},
        },
        ensure_ascii=False,
    )
    try:
        resp = _post_data(
            f"{PORTAL_DATA_BASE}/sys-modeling/sysModelingMain/view",
            cookie,
            data=detail_body,
            content_type="application/json",
        )
    except Exception:  # noqa: BLE001 单条详情失败不影响其他记录
        return {"subject": "", "images": []}
    if resp.status_code != 200:
        return {"subject": "", "images": []}
    try:
        data = (resp.json().get("data") or {})
    except Exception:  # noqa: BLE001
        return {"subject": "", "images": []}
    props = data.get("dynamicProps") or {}
    # 教学楼名优先取业务字段 tb_zxskbcx_drkb_jxlmc，其次文档标题
    subject = props.get("tb_zxskbcx_drkb_jxlmc") or props.get("fd_doc_subject") or ""
    images = []
    # 附件位置实测（）：httpx 直连时在 data.mechanisms.attachment；
    # 浏览器 SPA 抓包时曾在 data 顶层——两种位置都兼容，避免改版/参数差异失效
    attachments = (data.get("mechanisms") or {}).get("attachment") or data.get("attachment") or []
    for att in attachments:
        url = att.get("downloadUrl") or ""
        if not url:
            continue
        # downloadUrl 为相对路径（/data/sys-attach/...），拼门户域名成绝对地址
        images.append({
            "name": att.get("fullName") or att.get("fdFileName") or "",
            "url": f"https://i.cug.edu.cn{url}" if url.startswith("/") else url,
        })
    return {"subject": subject, "images": images}


# 最近一次 /live_room 下载的课表图片本地路径（供 CLI /next 逐张查看；data/ 不入库）
_last_room_files: list[str] = []


def _md5_hex(data: bytes) -> str:
    """计算内容 MD5（课表图片内容判重用：同一张图被重复上传时 URL/文件名可能都不同）。"""
    import hashlib

    return hashlib.md5(data).hexdigest()


def _download_room_files(files: list[dict]) -> list[str]:
    """把课表图片/PDF 下载到 data/exports/live_room/，返回本地路径列表。

    说明：登录态下用门户会话 cookie 下载（图片接口需要鉴权）；下载失败
    的单个文件跳过并记录，不阻断其它文件；目录 data/ 不入库（.gitignore）。
    扩展名按实际内容嗅探（附件接口返回 image/png 等），避免 URL 无后缀误判。
     修复：按下载地址（downloadUrl）去重——列表多条记录可能引用
    同一张课表图（实测两条"东教楼"记录返回同一附件），避免下载出多份一模一样的文件。
    下载完成后把路径记录到 _last_room_files，供 CLI /next 逐张查看。
    """
    from pathlib import Path

    cookie = _get_session_or_error()
    if not cookie:
        return []
    export_dir = Path("data/exports/live_room")
    export_dir.mkdir(parents=True, exist_ok=True)
    # 下载前清空目录：避免历史残留（旧的多份重复文件）与新下载混在一起误导
    # （实测：管理员曾把同一张课表图重复上传多份，旧文件会残留）
    for old in export_dir.iterdir():
        if old.is_file():
            try:
                old.unlink()
            except OSError:  # noqa: BLE001 单个旧文件删不掉不阻断
                pass
    saved: list[str] = []
    # 按下载地址去重（同一张图只下载一次）；文件名仍去重（同名加序号防覆盖）
    seen_urls: set[str] = set()
    seen_names: set[str] = set()
    # 内容 MD5 去重（终极保障， 实测根因）：列表 3 条"东教楼"记录的
    # 附件 URL 各不相同但内容是同一张图（管理员重复上传），URL/文件名去重都拦不住，
    # 必须按下载内容 MD5 判重——相同内容只保留第一份
    seen_hashes: set[str] = set()
    for i, f in enumerate(files[:10], 1):  # 上限 10 张，防刷屏
        url = f.get("url", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            resp = httpx.get(url, headers=_portal_headers(cookie), timeout=20, verify=_SSL_CONTEXT)
        except Exception:  # noqa: BLE001 单个文件下载失败跳过
            continue
        if resp.status_code != 200 or not resp.content:
            continue
        # 内容判重：同一张图（无论 URL/文件名）只保留第一份
        md5 = _md5_hex(resp.content)
        if md5 in seen_hashes:
            continue
        seen_hashes.add(md5)
        # 扩展名优先按 Content-Type 推断（image/png -> png），兜底 .bin
        ctype = (resp.headers.get("content-type") or "").lower()
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
               "image/bmp": "bmp", "application/pdf": "pdf"}.get(ctype.split(";")[0], "bin")
        name = (f.get("name") or f"live_room_{i}").rsplit(".", 1)[0] or f"live_room_{i}"
        path = export_dir / f"{name}.{ext}"
        if str(path) in seen_names:
            path = export_dir / f"{name}_{i}.{ext}"
        seen_names.add(str(path))
        try:
            path.write_bytes(resp.content)
        except OSError:  # noqa: BLE001 磁盘写入失败跳过
            continue
        saved.append(str(path))
    # 记录本次下载路径（供 CLI /next 查看下一张；/live_room 会话级状态）
    global _last_room_files
    _last_room_files = saved
    return saved


def portal_study_room_timetable(limit: int = 10) -> str:
    """查询自习室课表（只读，返回课表文档列表并下载图片）。

    背景：后勤保障部每日 17 时前以**图片形式**上传次日课表及自习室安排，
    门户内对应应用（app-701-zxskbcx）的数据接口为
    POST /data/sys-modeling/sysModelingMain/data（fdListViewId 标识列表），
    带门户会话 cookie 可只读拉取文档列表（含教学楼名等）。
     起：列表后逐条调 sysModelingMain/view 详情，把课表**图片**
    下载到 data/exports/live_room/ 并尝试打开第一张（需求 /live_room 可见图片）。
    """
    if not _breaker.allow():
        return tool_error("portal_study_room_timetable", "连接器处于熔断冷却中，请稍后再试")
    cookie = _get_session_or_error()
    if not cookie:
        return _session_error("portal_study_room_timetable")
    # 组装查询 JSON body（诊断确认：该接口只接受 application/json，
    # 传 form 编码会返回 HTTP 415；conditions/sorts 为嵌套 JSON 对象）
    body_data = json.dumps(
        {
            "fdListViewId": _STUDY_ROOM_LIST_VIEW_ID,
            "fdMode": 1,
            "pageSize": limit,
            "conditions": {"$and": []},
            "sorts": {"fd_create_time": "desc"},
            "params": {},
        },
        ensure_ascii=False,
    )
    try:
        resp = _post_data(
            f"{PORTAL_DATA_BASE}/sys-modeling/sysModelingMain/data",
            cookie,
            data=body_data,
            content_type="application/json",
        )
    except Exception as exc:  # noqa: BLE001
        _breaker.record_failure()
        return tool_error("portal_study_room_timetable", f"请求失败：{exc}")
    if resp.status_code in (301, 302, 401, 403, 901):
        _breaker.record_failure()
        return _session_error("portal_study_room_timetable")
    if resp.status_code != 200:
        _breaker.record_failure()
        return tool_error("portal_study_room_timetable", f"返回 HTTP {resp.status_code}")
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        _breaker.record_failure()
        return tool_error("portal_study_room_timetable", "响应不是合法 JSON")

    content = (body.get("data") or {}).get("content") or []
    if not content:
        _breaker.record_success()
        return tool_info("portal_study_room_timetable", "当前没有已发布的自习室课表文档（后勤保障部每日 17 时前更新）")
    _breaker.record_success()
    lines = []
    # 汇总各记录详情返回的图片下载项（实测：图片在详情接口里）
    all_images: list[dict] = []
    seen_image_urls: set[str] = set()
    for row in content[:limit]:
        # 文档标题（如教学楼名）+ 记录主键（详情接口依赖它拿图片）
        subject = row.get("fd_doc_subject") or row.get("fd_subject") or "（未命名文档）"
        doc_id = row.get("fd_id") or ""
        lines.append(f"- {subject}" + (f"（记录ID：{doc_id}）" if doc_id else ""))
        # 逐条拉详情：拿到课表图片 downloadUrl（失败返回空，不影响列表）
        if doc_id:
            detail = _fetch_room_detail(cookie, doc_id)
            for img in detail.get("images") or []:
                # 按 downloadUrl 去重：列表多条记录可能引用同一张课表图（实测两条
                # "东教楼"记录返回同一附件），避免 /live_room 下载出多份一模一样的图片
                if not img.get("url") or img["url"] in seen_image_urls:
                    continue
                seen_image_urls.add(img["url"])
                img.setdefault("name", f"{subject}_{len(all_images) + 1}")
                all_images.append(img)
    head = (
        f"自习室课表文档列表（共 {len(content)} 条，显示前 {min(limit, len(content))} 条）：\n"
    )
    body = head + "\n".join(lines)
    # 有可下载图片：下载到本地并提示（仅提示路径，不重复输出图片内容）
    if all_images:
        saved = _download_room_files(all_images)
        if saved:
            body += (
                f"\n[信息] 已下载 {len(saved)} 张课表图片到：\n  "
                + "\n  ".join(saved)
                + "\n  （正在尝试用系统默认程序打开第一张…）"
            )
            try:
                import os
                os.startfile(saved[0])  # type: ignore[attr-defined] Windows 打开图片
            except (OSError, AttributeError):
                pass  # 打开失败不影响（用户可自行打开文件）
        else:
            body += "\n（课表图片下载失败，请在门户应用内查看具体图片）"
    else:
        body += "\n（课表为图片文档，详情未取到可下载图片，请在门户应用内查看）"
    return body


# ===== 3. 南望厅服务事项（公开静态页抓取，无需登录） =====

def portal_service_items(keyword: str = "") -> str:
    """查询一站式师生服务大厅（南望厅）的服务事项（公开信息，无需登录）。

    数据源：service.cug.edu.cn/fwsx.htm 为**公开静态页**，按部门分类列出服务事项
    及咨询电话（027-67885111）；此处抓取页面后按关键词过滤，返回事项清单。
    """
    if not _breaker.allow():
        return tool_error("portal_service_items", "连接器处于熔断冷却中，请稍后再试")
    try:
        with _limiter:
            resp = httpx.get(
                SERVICE_HALL_URL,
                headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
                timeout=15,
                verify=_SSL_CONTEXT,
            )
    except Exception as exc:  # noqa: BLE001
        _breaker.record_failure()
        return tool_error("portal_service_items", f"请求失败：{exc}")
    if resp.status_code != 200:
        _breaker.record_failure()
        return tool_error("portal_service_items", f"返回 HTTP {resp.status_code}")

    # 静态页解析：提取"部门 → 服务事项"结构（按常见 HTML 结构尝试多个模式）
    html = resp.text
    items = _parse_service_items(html, keyword)
    _breaker.record_success()
    if not items:
        return tool_info("portal_service_items", f"南望厅服务事项中未找到与「{keyword}」相关的内容（页面共 {len(html)} 字符）")
    head = f"南望厅服务事项（匹配「{keyword}」，{len(items)} 条）：\n" if keyword else f"南望厅服务事项（共 {len(items)} 条）：\n"
    return head + "\n".join(items)


def _parse_service_items(html: str, keyword: str = "") -> list[str]:
    """从南望厅静态页 HTML 中提取服务事项文本（按部门/条目切分，关键词过滤）。

    说明：该页为 JSP 静态渲染（fwsx2.jsp），结构可能随站点改版变化；此处用
    宽松规则：去掉脚本/样式/标签后按行清洗，保留含服务关键词的行。
    """
    import re as _re

    # 去掉 script/style 与标签，保留文本行
    text = _re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = _re.sub(r"(?s)<[^>]+>", "\n", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # 过滤明显的导航/页脚噪音，保留 2~30 字的候选条目
    skip_words = ("首页", "返回", "登录", "注册", "服务热线", "©", "更多", "咨询", "电话")
    out = []
    for ln in lines:
        if not (1 < len(ln) <= 40):
            continue
        if ln.startswith(skip_words):
            continue
        if keyword and keyword not in ln:
            continue
        if ln not in out:
            out.append(ln)
    return out[:30]


# ===== 4. 网上厅服务目录（全量服务查询，让 Agent 知晓门户全部服务） =====

def _clean_html(html: str, limit: int = 200) -> str:
    """去掉 HTML 标签取纯文本（用于 fdGuide 服务指南的摘要展示）。"""
    import re as _re

    text = _re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    text = _re.sub(r"(?s)<[^>]+>", " ", text)
    return _re.sub(r"\s+", " ", text).strip()[:limit]


def fetch_service_catalog() -> list[tuple[str, str, str, str, str, str]]:
    """拉取网上厅服务目录，返回结构化列表 [(分类, 服务名, 部门, 电话, 办理入口URL, 指南)]。

    供 portal_service_catalog 工具与 CLI 缓存（cache_store）复用；
    会话缺失/请求失败返回空列表（由调用方决定提示或降级）。

    数据源：POST /data/ext-general/extGeneralApplication/getCateServiceAppData
    （浏览器实测 ：空参即返回服务目录树，叶子含 fdShortName/fdDept/
    fdTele/fdTransact/fdGuide）。
    """
    cookie = get_portal_cookie()
    if not cookie:
        return []
    try:
        resp = _post_data(
            f"{PORTAL_DATA_BASE}/ext-general/extGeneralApplication/getCateServiceAppData",
            cookie,
            data="{}",
            content_type="application/json",
        )
    except Exception:  # noqa: BLE001 网络/超时按不可用降级
        return []
    if resp.status_code not in (200,):
        return []
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return []

    # 递归拍平服务目录树：记录 (分类, 服务名, 部门, 电话, 办理入口, 指南)
    services: list[tuple[str, str, str, str, str, str]] = []

    def _walk(nodes, category: str) -> None:
        for node in nodes or []:
            name = node.get("text") or node.get("fdShortName") or ""
            transact = node.get("fdTransact") or ""
            children = node.get("children") or []
            if name and transact:
                dept = (node.get("fdDept") or {}).get("fdName", "")
                guide = _clean_html(node.get("fdGuide") or "")
                services.append((category, name, dept, node.get("fdTele") or "", transact, guide))
            # 递归进入子层（分类层的 children 是具体服务）
            _walk(children, name or category)

    data = body.get("data") or []
    for top in data:
        for grp in (top.get("children") or []):
            _walk([grp], grp.get("text") or grp.get("fdShortName") or "")
    return services


def portal_service_catalog(keyword: str = "") -> str:
    """查询信息门户网上厅的**服务目录**（全量服务，无需逐条硬编码）。

    用途：让 Agent 知晓门户提供哪些服务（名称/分类/责任部门/咨询电话/办理入口/
    服务指南），便于引导用户去门户办事或后续调用对应只读接口。纯目录查询，只读。

    实现：复用 fetch_service_catalog()（结构化目录数据），此处负责文本格式化。
    """
    if not _breaker.allow():
        return tool_error("portal_service_catalog", "连接器处于熔断冷却中，请稍后再试")
    services = fetch_service_catalog()
    if not services:
        # 区分"会话缺失"与"目录为空"（fetch 失败可能因未登录）
        if not get_portal_cookie():
            return _session_error("portal_service_catalog")
        _breaker.record_success()
        return tool_info("portal_service_catalog", "服务目录为空（可能门户改版或接口异常）")

    _breaker.record_success()
    # 关键词过滤（按服务名匹配）
    matched = [s for s in services if not keyword or keyword in s[1]]
    if keyword and not matched:
        return tool_info("portal_service_catalog", f"网上厅服务目录中未找到与「{keyword}」相关的服务（共 {len(services)} 项）")

    # 有关键词：输出匹配服务的详细信息（部门/电话/入口/指南摘要）
    if keyword:
        head = f"网上厅服务目录（匹配「{keyword}」，{len(matched)} 项）：\n"
        parts = []
        for cat, name, dept, tele, transact, guide in matched[:15]:
            line = f"- [{cat}] {name}\n  责任部门：{dept or '—'}｜咨询：{tele or '—'}\n  办理入口：{transact}"
            if guide:
                line += f"\n  说明：{guide}"
            parts.append(line)
        return head + "\n".join(parts)

    # 无关键词：按分类分组的服务名概览（控制输出长度，避免撑爆上下文）
    by_cat: dict[str, list[str]] = {}
    for cat, name, *_ in services:
        by_cat.setdefault(cat, []).append(name)
    head = f"网上厅服务目录总览（共 {len(services)} 项服务）：\n"
    parts = []
    for cat, names in by_cat.items():
        parts.append(f"【{cat}】（{len(names)} 项）\n  " + "、".join(names[:20]))
    return head + "\n".join(parts)


# ===== 5. 办事流程-我的待办 / 我的已办（只读） =====

def _post_portal_json(source: str, path: str, data: str = "{}") -> str:
    """通用门户 JSON POST（供待办/已办复用）：返回原始 JSON 文本或 [错误] 提示。"""
    if not _breaker.allow():
        return tool_error(source, "连接器处于熔断冷却中，请稍后再试")
    cookie = _get_session_or_error()
    if not cookie:
        return _session_error(source)
    try:
        resp = _post_data(f"{PORTAL_DATA_BASE}{path}", cookie, data=data, content_type="application/json")
    except Exception as exc:  # noqa: BLE001
        _breaker.record_failure()
        return tool_error(source, f"请求失败：{exc}")
    if resp.status_code in (301, 302, 401, 403, 901):
        _breaker.record_failure()
        return _session_error(source)
    if resp.status_code != 200:
        _breaker.record_failure()
        return tool_error(source, f"返回 HTTP {resp.status_code}")
    _breaker.record_success()
    return resp.text


def portal_todo_tasks(limit: int = 10) -> str:
    """查询我的待办任务（信息门户办事流程：待我审批/办理的事项）。

    接口（浏览器实测）：POST /data/sys-person/sysPersonTask/getSelfTasksPage。
    """
    raw = _post_portal_json("portal_todo_tasks", "/sys-person/sysPersonTask/getSelfTasksPage")
    if raw.startswith("[错误]") or raw.startswith("[信息]"):
        return raw
    try:
        body = json.loads(raw)
    except Exception:  # noqa: BLE001
        return tool_error("portal_todo_tasks", "响应不是合法 JSON")
    content = (body.get("data") or {}).get("content") or []
    if not content:
        return tool_info("portal_todo_tasks", "当前没有待办任务")
    lines = []
    for item in content[:limit]:
        subject = item.get("fdSubject") or "（无标题）"
        lines.append(f"- {subject}")
    return f"我的待办任务（共 {len(content)} 条，显示前 {min(limit, len(content))} 条）：\n" + "\n".join(lines)


def portal_finished_tasks(limit: int = 10) -> str:
    """查询我的已办事项（信息门户办事流程：已完成/结束的流程）。

    接口（浏览器实测）：POST /data/km-review/portlet/process/finished，
    返回主题/流程状态/申请单编号/申请人/当前处理人/当前节点。
    """
    raw = _post_portal_json("portal_finished_tasks", "/km-review/portlet/process/finished")
    if raw.startswith("[错误]") or raw.startswith("[信息]"):
        return raw
    try:
        body = json.loads(raw)
    except Exception:  # noqa: BLE001
        return tool_error("portal_finished_tasks", "响应不是合法 JSON")
    rows = (body.get("data") or {}).get("data") or []
    if not rows:
        return tool_info("portal_finished_tasks", "当前没有已办事项")
    lines = []
    for row in rows[:limit]:
        subject = row.get("fdSubject") or "（无标题）"
        status = row.get("fdProcessStatus") or "—"
        number = row.get("fdNumber") or "—"
        lines.append(f"- {subject}\n  状态：{status}｜单号：{number}")
    return f"我的已办事项（共 {len(rows)} 条，显示前 {min(limit, len(rows))} 条）：\n" + "\n".join(lines)


# ===== 6. 个人中心信息 / 待阅通知（只读） =====

def portal_personal_info() -> str:
    """查询我的门户账户信息（目前返回绑定手机号；门户个人中心更多字段未开放接口）。

    接口（浏览器实测）：POST /data/sys-person/lcode/grzxgrxxjm/getCurPerson。
    """
    raw = _post_portal_json("portal_personal_info", "/sys-person/lcode/grzxgrxxjm/getCurPerson")
    if raw.startswith("[错误]") or raw.startswith("[信息]"):
        return raw
    try:
        body = json.loads(raw)
    except Exception:  # noqa: BLE001
        return tool_error("portal_personal_info", "响应不是合法 JSON")
    data = body.get("data") or {}
    mobile = data.get("fdMobileNo") or "—"
    return f"我的门户账户信息：\n- 绑定手机号：{mobile}"


def portal_pending_notices(limit: int = 10) -> str:
    """查询我的待阅通知（信息门户：待阅文件/通知/调查问卷等）。

    接口（浏览器实测）：POST /data/sys-notify/portlet/todo/list，
    返回通知列表（标题/发布人/来源模块/时间/跳转链接）。
    注意：列表中的「置为已办/稍后处理」是写操作，agent 不调用，仅提示。
    """
    raw = _post_portal_json("portal_pending_notices", "/sys-notify/portlet/todo/list")
    if raw.startswith("[错误]") or raw.startswith("[信息]"):
        return raw
    try:
        body = json.loads(raw)
    except Exception:  # noqa: BLE001
        return tool_error("portal_pending_notices", "响应不是合法 JSON")
    items = body.get("data") or []
    if not items:
        return tool_info("portal_pending_notices", "当前没有待阅通知")
    lines = []
    for item in items[:limit]:
        text = (item.get("text") or "").strip() or "（无标题）"
        creator = (item.get("creator") or {}).get("fdName") or "—"
        module = item.get("fdModuleName") or ""
        app = item.get("fdAppName") or ""
        ts = item.get("created") or 0
        time_str = f"{int(ts) / 1000:.0f}" if ts else "—"
        href = item.get("href") or ""
        line = f"- {text}\n  发布人：{creator}｜来源：{app}/{module}｜时间：{time_str}"
        if href:
            line += f"\n  打开：https://i.cug.edu.cn{href if href.startswith('/') else '/' + href}"
        lines.append(line)
    return f"我的待阅通知（共 {len(items)} 条，显示前 {min(limit, len(items))} 条）：\n" + "\n".join(lines)


# ===== 工具注册 =====

def to_tool_specs():
    """返回本连接器全部工具规格（供注册表批量注册）。"""
    from app.agent.tools import ToolSpec

    return [
        ToolSpec(
            name="portal_my_processes",
            description=(
                "查询我发起的办事流程（信息门户：标题/发起时间/状态/处理人/来源模块），"
                "需要已登录信息门户（geopractor session-login）"
            ),
            fn=portal_my_processes,
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "最多返回条数（默认 10）"}},
                "required": [],
            },
        ),
        ToolSpec(
            name="portal_study_room_timetable",
            description=(
                "查询自习室课表（信息门户网上厅：后勤保障部每日 17 时前以图片形式上传的"
                "课表及自习室安排文档列表），需要已登录信息门户"
            ),
            fn=portal_study_room_timetable,
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "最多返回条数（默认 10）"}},
                "required": [],
            },
        ),
        ToolSpec(
            name="portal_service_items",
            description=(
                "查询一站式师生服务大厅（南望厅）的服务事项清单（公开信息，无需登录），"
                "可按关键词过滤（如「证明」「宿舍」）"
            ),
            fn=portal_service_items,
            parameters={
                "type": "object",
                "properties": {"keyword": {"type": "string", "description": "关键词过滤（可选）"}},
                "required": [],
            },
        ),
        ToolSpec(
            name="portal_service_catalog",
            description=(
                "查询信息门户网上厅的完整服务目录（按分类列出全部服务；或用关键词查询"
                "具体服务的责任部门/咨询电话/办理入口/服务指南）。适合回答『学校有XX服务吗』"
                "『XX怎么办』，需要已登录信息门户"
            ),
            fn=portal_service_catalog,
            parameters={
                "type": "object",
                "properties": {"keyword": {"type": "string", "description": "关键词过滤（可选，如「证明」「选课」）"}},
                "required": [],
            },
        ),
        ToolSpec(
            name="portal_todo_tasks",
            description=(
                "查询我的待办任务（信息门户办事流程：待我审批/办理的事项），"
                "需要已登录信息门户"
            ),
            fn=portal_todo_tasks,
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "最多返回条数（默认 10）"}},
                "required": [],
            },
        ),
        ToolSpec(
            name="portal_finished_tasks",
            description=(
                "查询我的已办事项（信息门户办事流程：已完成/结束的流程，含状态与申请单号），"
                "需要已登录信息门户"
            ),
            fn=portal_finished_tasks,
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "最多返回条数（默认 10）"}},
                "required": [],
            },
        ),
        ToolSpec(
            name="portal_personal_info",
            description=(
                "查询我的门户账户信息（绑定手机号等），需要已登录信息门户"
            ),
            fn=portal_personal_info,
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        ToolSpec(
            name="portal_pending_notices",
            description=(
                "查询我的待阅通知（信息门户：待阅文件/通知/调查问卷等，含标题/发布人/时间/"
                "跳转链接；查看与办理由用户自己在门户完成），需要已登录信息门户"
            ),
            fn=portal_pending_notices,
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "最多返回条数（默认 10）"}},
                "required": [],
            },
        ),
    ]
