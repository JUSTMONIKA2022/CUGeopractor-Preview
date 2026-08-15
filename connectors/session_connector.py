# -*- coding: utf-8 -*-
"""会话型连接器：复用"用户浏览器登录态"访问校园本人数据接口。

设计说明（对应 L1 评估报告 §4）：
    - 前提：用户在浏览器登录信息门户后，把会话 Cookie 配置到本机
      （通过 .env / 环境变量占位 {{SESSION_COOKIE}}），项目不接触、不存储密码；
    - 只允许访问"白名单接口"（用户显式配置的本人数据接口），
      白名单校验在发起请求前强制执行，防止会话被用于访问其它地址；
    - 所有请求走全局限速器（防封禁）+ 指数退避重试 + 轻量熔断；
    - 会话失效/风控拦截（403/401/302）返回可读提示，建议用户重新登录；
    - 红线：不实现自动模拟登录、不破解验证码、不绕过认证、不批量抓取他人数据。

配置文件（用户本机 data/session_connectors.yaml，不入库）：
    session_connectors:
      - name: cug_course
        description: 查询我的课程表（需登录信息门户）
        url: https://xyfw.cug.edu.cn/.../course-list
        method: GET
        cookie: "{{SESSION_COOKIE}}"
        headers:
          User-Agent: "Mozilla/5.0 ..."
          Referer: https://xyfw.cug.edu.cn/
        allowed_prefix: https://xyfw.cug.edu.cn
        rate_limit:
          interval: 5.0
          jitter: 1.5
"""

from __future__ import annotations

import datetime
import json
import os
import re
import ssl
from pathlib import Path

import httpx

from app.rate_limit import CircuitBreaker, backoff_retry, get_rate_limiter
# Playwright 持久会话取 Cookie（可选依赖；未安装时返回 None 由 invoke 降级提示）
from connectors.base import tool_error
from connectors.pw_session import get_session_cookie

# 配置文件默认路径（data/ 已被 .gitignore 排除，用户配置不入库）
SESSION_CONNECTORS_FILE = "data/session_connectors.yaml"

# 占位符形如 {{VAR}}，从环境变量读取；缺失时回退读取 .env（Cookie 等敏感信息不进配置文件明文）
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# 强制 TLS1.2 的 SSL 上下文（全局限速器同层）
# 原因：实测 jwgl.cug.edu.cn（教务）网关对 TLS1.3 握手存在间歇性重置（UNEXPECTED_EOF），
#       强制降到 TLS1.2 后链路稳定（裸 ssl 库默认协商 1.2 一直可通，据此定位）。
_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.minimum_version = ssl.TLSVersion.TLSv1_2
_SSL_CONTEXT.maximum_version = ssl.TLSVersion.TLSv1_2


def _env_or_dotenv(key: str) -> str:
    """取环境变量；缺失时回退读取项目 .env（支持 {{VAR}} 占位符配置化）。"""
    value = os.environ.get(key, "")
    if value:
        return value
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return ""


# 正方教务学期编码：xqm=3 表示第一学期（秋季学期），xqm=12 表示第二学期（春季学期）
_SEMESTER_NUM_MAP = {1: "3", 2: "12", 3: "3", 12: "12"}
# 中文学期词 → 学期号
_SEMESTER_WORD_MAP = {"一": 1, "二": 2, "上": 1, "下": 2, "1": 1, "2": 2}


def _current_semester() -> tuple[int, str]:
    """计算当前正方学期 (xnm, xqm)：9 月开学起为新学年第一学期。"""
    now = datetime.datetime.now()
    if now.month >= 9:
        return now.year, "3"
    return now.year - 1, "12"


def parse_semester(question: str) -> dict[str, str] | None:
    """从问题文本中解析正方教务学期参数，返回 {"xnm":..., "xqm":...}；未识别返回 None。

    支持格式（新增，解决"agent 查不了过往课表/成绩"的根因——连接器
    body 里 xnm/xqm 原本硬编码固定学期，无法按用户指定学期查询）：
        - "2025-2026-2" / "2025-2026 第二学期"    学年 + 学期号（1/2 → 3/12）
        - "2025 12" / "2025 3"                    学年 + 正方编码显式
        - "上学期" / "下学期" / "上一学期" / "下一学期"  相对当前学期
        - "2025年第二学期"                         年份 + 中文学期
    其余文本返回 None（调用方保持默认参数）。
    """
    text = question.strip()
    if not text:
        return None
    cur_xnm, cur_xqm = _current_semester()

    # 1) 相对学期："上学期 / 下学期 / 上一学期 / 下一学期"
    if "上学期" in text or "上一学期" in text:
        # 当前秋(3) → 上学期为当年春(12)；当前春(12) → 上学期为去年秋(3)
        xqm = "12" if cur_xqm == "3" else "3"
        xnm = str(cur_xnm - 1) if xqm == "3" else str(cur_xnm)
        return {"xnm": xnm, "xqm": xqm}
    if "下学期" in text or "下一学期" in text:
        xqm = "3" if cur_xqm == "12" else "12"
        xnm = str(cur_xnm + 1) if xqm == "12" else str(cur_xnm)
        return {"xnm": xnm, "xqm": xqm}

    # 2) "2025-2026-2" 或 "2025-2026 第二学期"（含分隔符的学年区间，学期号可紧跟其后）
    m = re.search(r"(20\d{2})\s*[-－—~～]\s*(20\d{2})\s*[-－—]?\s*([一二1-2]?)\s*(?:学期)?", text)
    if m:
        xnm = m.group(1)
        num = int(m.group(3)) if m.group(3) else 1
        return {"xnm": xnm, "xqm": _SEMESTER_NUM_MAP.get(num, "3")}

    # 3) "2025 12" / "2025 3" / "2025年第二学期"：年份 + 学期（数字或中文）
    m = re.search(r"(20\d{2})\D{0,4}([一二12])?\s*(?:学期)?$", text)
    if m and m.group(2):
        num = int(_SEMESTER_WORD_MAP.get(m.group(2), 1))
        return {"xnm": m.group(1), "xqm": _SEMESTER_NUM_MAP.get(num, "3")}
    m = re.search(r"(20\d{2})\s+(1[02]|[123])\b", text)
    if m:
        num = int(m.group(2))
        return {"xnm": m.group(1), "xqm": _SEMESTER_NUM_MAP.get(num, "3")}

    return None


def _humanize_session_response(text: str, name: str = "", limit: int = 4000) -> str:
    """把教务会话接口的原始响应转为可读文本（两次迭代）。

    背景：反馈 `/live_grade` 等直接返回接口原始 JSON/网页文本不可读；
    后续实测发现成绩接口的 `xm` 字段实为**学生姓名**而非教师（误映射"教师"），
    且课表接口返回完整 JSON 元数据（kbList 空时甩大段 JSON）。本函数：

        1. 解析 JSON，抽取数据行数组（rows/items/data/**kbList**，正方教务常见结构）；
        2. **按连接器名选择字段映射**（成绩只显示课程/成绩/学分/绩点；课表含
           教师/星期/节次/周次/地点；考试含时间/地点/座位号）——避免成绩把
           学生姓名误标为"教师"；
        3. 成绩（cj/zpcj）数值 < 50 时追加 `(!)` 标记，供 CLI 显示层标红；
        4. 课表无数据（kbList 空）时给出友好提示而非甩 JSON 元数据；
        5. 非 JSON / 解析失败原样截断返回，不影响原链路。

    返回：可读的逐条文本；非结构化数据返回截断原文。
    """
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001 非 JSON（HTML/错误页等）原样截断返回
        return text[:limit]

    rows: list | None = None
    if isinstance(data, list) and data:
        rows = data
    elif isinstance(data, dict):
        for key in ("rows", "items", "data", "kbList"):
            value = data.get(key)
            if isinstance(value, list) and value:
                rows = value
                break
    if not rows:
        # 课表接口：解析成功但无课表数据（未排课/学期未开放）→ 友好提示，不甩 JSON
        if name == "cug_course" and isinstance(data, dict):
            return "本学期暂无课表数据（可能尚未排课或学期未开放）。"
        # 单对象响应兜底（修复 /live_student 空返回：学籍信息接口
        # 返回单个对象而非列表，此前 rows 提取不到导致输出为空）。把整个对象
        # 当作一行显示其字段；无内容则回退原文。
        # 注意：data 内已含列表字段（如 {"rows": [], "items": [...]}）时不兜底，
        # 否则会把包裹结构整个当一行输出（测试发现误伤空列表响应）。
        has_list = any(isinstance(v, list) for v in data.values())
        if isinstance(data, dict) and data and not has_list:
            rows = [data]
        else:
            return text[:limit]

    # 按连接器选择字段映射：成绩接口的 xm 是学生姓名（实测），
    # 故成绩/默认映射不包含 xm；课表接口 xm 才是教师
    field_labels = {
        "cug_grade": {"kcmc": "课程", "cj": "成绩", "xf": "学分", "jd": "绩点", "zpcj": "总评"},
        "cug_exam": {"kcmc": "课程", "kssj": "时间", "cdmc": "地点", "zwh": "座位号", "kslxmc": "考试类型"},
        "cug_student_info": {
            "xm": "姓名", "xh": "学号", "jg_id": "学院", "zyh_id": "专业",
            "njdm_id": "年级", "xbm": "性别", "mzm": "民族", "csrq": "出生日期",
            "sjhm": "手机号", "pyccdm": "培养层次", "xqmc": "校区", "bj": "班级",
        },
    }.get(name) or {
        "kcmc": "课程", "cj": "成绩", "xf": "学分", "jd": "绩点",
        "kssj": "时间", "kslxmc": "考试类型", "cdmc": "地点",
        "xm": "教师", "zcd": "周次", "jc": "节次", "zwh": "座位号",
        "xnm": "学年", "xqm": "学期",
    }

    # 课表字段：星期数字（xqj 1~7）→ 中文（周一~周日）
    weekday_map = {str(i): f"周{('一二三四五六日')[i - 1]}" for i in range(1, 8)}

    def _label_value(key: str, value: str) -> str:
        """按字段加工显示值：星期数字转中文；成绩<50 追加 (!) 标红标记。

        学期（xqm）说明：正方教务用 xqm=12 表示第二学期、xqm=3 表示第一学期
        （反馈"学期=12"难懂），这里映射回人类可读的"第 N 学期"。
        """
        if key == "xqj" and str(value) in weekday_map:
            return weekday_map[str(value)]
        if key == "xqm":
            return {"12": "第2学期", "3": "第1学期"}.get(str(value), str(value))
        if key in ("cj", "zpcj"):
            try:
                if float(value) < 50:  # 总评不及格：加 (!) 供 CLI 显示层标红
                    return f"{value}(!)"
            except (TypeError, ValueError):
                pass
        return str(value)

    lines = [f"共 {len(rows)} 条"]
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        parts = [
            f"{label}={_label_value(key, row[key])}"
            for key, label in field_labels.items()
            if row.get(key) not in (None, "")
        ]
        if parts:
            lines.append(f"{i}. " + "  ".join(parts))
        elif any(row.values()):
            # 行内没有任何映射字段命中：输出该行全部非空字段（key=value），
            # 保证未知结构（如培养方案列表仅 1 条、字段与课表/成绩不同）也能看到内容，
            # 而非只显示"共 1 条"却无实质信息（反馈"txt 只有共1条"）。
            others = [f"{k}={v}" for k, v in row.items() if v not in (None, "")]
            lines.append(f"{i}. " + "  ".join(others) if others else f"{i}. （无内容）")
    out = "\n".join(lines)
    return out if out.strip() else text[:limit]


# 课表快照缓存路径（data/ 不入库）：存上次实时/LLM 查询到的课表行，
# 供"课表检查机制"对比——防止换课后用户仍按旧课表上课（需求）
COURSE_SNAPSHOT_FILE = Path("data/cache/course_snapshot.json")


def _course_rows(text: str) -> list[dict]:
    """从课表接口响应中提取课程行列表（kbList，正方教务课表常见结构）。

    解析失败/无课表数据返回空列表（调用方据此跳过对比，避免误报"全部取消"）。
    """
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001 非 JSON（错误页/HTML）不参与对比
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("kbList", "rows", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def _course_key(row: dict) -> tuple[str, str, str]:
    """课程定位主键：课程名 + 星期 + 节次（用于判断"同一门课"）。

    说明：正方课表一学期内同一门课在相同星期/节次是唯一的，以这三字段
    定位课程可忽略教师/教室等变动字段的干扰。
    """
    return (str(row.get("kcmc", "")), str(row.get("xqj", "")), str(row.get("jc", "")))


def _course_compare_rows(new_rows: list[dict], old_rows: list[dict]) -> str:
    """对比新旧课表，返回人类可读的差异描述（空串=无变化）。

    检查项（防换课场景）：
        1. 新增课程：旧表没有的（课程+星期+节次）；
        2. 已取消课程：旧表有而新表没有的；
        3. 变动课程：同一门课的时间（星期/节次）或地点（cdmc）发生变化。
    对比不因教师/教室等次要字段变化而误报，仅提示"排课有调整，请重新核对"。
    """
    weekday = {str(i): f"周{('一二三四五六日')[i - 1]}" for i in range(1, 8)}
    old_map = {_course_key(r): r for r in old_rows}
    new_map = {_course_key(r): r for r in new_rows}
    notes: list[str] = []
    # 新增课程：新表有、旧表无
    for key, row in new_map.items():
        if key not in old_map:
            wd = weekday.get(row.get("xqj", ""), str(row.get("xqj", "")))
            notes.append(f"新增：{row.get('kcmc', '?')}（{wd} 第{row.get('jc', '?')}节，{row.get('cdmc', '地点待定')}）")
    # 已取消课程：旧表有、新表无
    for key, row in old_map.items():
        if key not in new_map:
            wd = weekday.get(row.get("xqj", ""), str(row.get("xqj", "")))
            notes.append(f"已取消：{row.get('kcmc', '?')}（{wd} 第{row.get('jc', '?')}节）")
    # 变动课程：同主键但时间（星期/节次）或地点变化
    for key, new_row in new_map.items():
        old_row = old_map.get(key)
        if old_row is None:
            continue
        changed: list[str] = []
        if new_row.get("cdmc") not in (None, "") and new_row.get("cdmc") != old_row.get("cdmc"):
            changed.append(f"地点：{old_row.get('cdmc', '?')}→{new_row.get('cdmc', '?')}")
        if not changed and old_row.get("cdmc") in (None, "") and new_row.get("cdmc") not in (None, ""):
            changed.append(f"地点：待定→{new_row.get('cdmc', '?')}")
        if changed:
            wd = weekday.get(new_row.get("xqj", ""), str(new_row.get("xqj", "")))
            notes.append(f"变动：{new_row.get('kcmc', '?')}（{wd} 第{new_row.get('jc', '?')}节）" + "；".join(changed))
    if not notes:
        return ""
    return "课表检查结果（本次查询 vs 上次缓存课表）：\n" + "\n".join("  - " + n for n in notes) + "\n（若为换课/调课，请以上述差异为准重新安排）"


def _compare_and_cache_course(text: str) -> str:
    """课表检查机制：与上次缓存课表对比并输出差异描述，同时写回新快照。

    执行策略（需求"每次实时/LLM 查询课表自动对比缓存"）：
        - 本次解析失败/无课表数据：不覆盖旧快照、不输出差异（避免误报全部取消）；
        - 首次查询（无旧快照）：只建立快照，输出"已建立课表基线"提示；
        - 后续查询：对比后输出差异描述，并把本次课表写回快照缓存。
    返回：差异描述文本（追加到查询结果后）；无差异返回空串。
    """
    rows = _course_rows(text)
    if not rows:
        return ""  # 本次无课表数据：不对比、不覆盖（保持上次快照）
    old: list[dict] = []
    if COURSE_SNAPSHOT_FILE.exists():
        try:
            data = json.loads(COURSE_SNAPSHOT_FILE.read_text(encoding="utf-8"))
            old = data.get("rows") or []
        except Exception:  # noqa: BLE001 快照损坏按无旧快照处理
            old = []
    # 首次查询：建立课表基线（便于后续对比）
    if not old:
        COURSE_SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        COURSE_SNAPSHOT_FILE.write_text(
            json.dumps({"updated": datetime.datetime.now().isoformat(), "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return "[信息] 课表检查：已建立本次课表基线（下次查询将自动对比是否有换课/调课）。"
    # 有旧快照：对比并写回新课表
    diff = _course_compare_rows(rows, old)
    COURSE_SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    COURSE_SNAPSHOT_FILE.write_text(
        json.dumps({"updated": datetime.datetime.now().isoformat(), "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not diff:
        return "[信息] 课表检查：本次查询与上次缓存课表一致（无换课/调课）。"
    return diff


class SessionConnector:
    """单个校园系统会话连接器（携带登录态访问本人数据接口）。"""

    def __init__(
        self,
        name: str,
        description: str,
        url: str,
        method: str = "GET",
        cookie: str = "",
        headers: dict | None = None,
        allowed_prefix: str = "",
        interval: float = 5.0,
        jitter: float = 1.5,
        body: str = "",
        pw_profile: str = "",
        semester_params: list[str] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.url = url
        self.method = method.upper()
        self.cookie = cookie
        self.headers = headers or {}
        # POST 请求体模板（可含 {question} 占位，如正方课表接口的表单参数）
        self.body = body
        # Playwright 持久会话 profile 目录（可选）：配置后自动复用用户浏览器登录态，
        # 无需手动导出 Cookie（正方教务会话短效，浏览器访问同时起到"保活"作用）
        self.pw_profile = pw_profile
        # 学期参数白名单（如 ["xnm","xqm"]）：声明后，用户问题中含学期信息（"上学期"/
        # "2025-2026-2" 等）时，invoke 会自动把 body 里对应参数替换为解析出的学期。
        # 解决"agent 无法按用户指定学期查询课表/成绩/考试"的痛点（新增）。
        self.semester_params = semester_params or []
        # 白名单前缀：请求 URL 必须以此开头（防止会话被用于访问其它地址）
        self.allowed_prefix = allowed_prefix or self.url[: len(self.url) // 2]
        # 限速与熔断（防封禁核心）
        self._limiter = get_rate_limiter(name, interval=interval, jitter=jitter)
        self._breaker = CircuitBreaker()
        # 最近一次成功请求的原始响应（供导出"原文件"用：培养方案等接口的 JSON
        # 原文才是服务端返回的真身，humanize 文本只是展示层—— 需求）
        self.last_raw: str = ""

    def _resolve_cookie(self) -> str:
        """解析会话 Cookie：优先 Playwright 持久会话自动获取；否则用环境变量占位符。

        返回空字符串表示未取得有效会话（调用方给出可读提示）。
        """
        if self.pw_profile:
            cookie_value = get_session_cookie(self.pw_profile) or ""
            return cookie_value
        cookie_value = self._resolve(self.cookie)
        if cookie_value.startswith("[缺少环境变量"):
            return ""
        return cookie_value

    def _resolve(self, raw: str) -> str:
        """把 {{VAR}} 占位符替换为环境变量/.env 值；缺失时保留并提示。"""
        def repl(match: re.Match) -> str:
            key = match.group(1)
            value = _env_or_dotenv(key)
            return value if value else f"[缺少环境变量 {key}]"
        return _PLACEHOLDER_RE.sub(repl, raw)

    def _check_allowlist(self, url: str) -> bool:
        """白名单校验：URL 必须以 allowed_prefix 开头（否则拒绝访问）。"""
        return url.startswith(self.allowed_prefix)

    def invoke(self, question: str) -> str:
        """执行一次会话查询，返回文本结果。

        参数：
            question: 用户请求内容（可拼入 URL 的 {question} 占位）
        返回：
            响应文本（截断防撑爆上下文）；失败/被拒返回以 [错误] 开头提示。
        """
        if not self._breaker.allow():
            return tool_error(self.name, "处于熔断冷却中，请稍后再试")

        url = self._resolve(self.url).replace("{question}", question)
        if not self._check_allowlist(url):
            return tool_error(self.name, f"拒绝访问白名单以外的地址：{url}")
        # 解析会话 Cookie：Playwright 持久会话自动获取，或环境变量占位符（未配置时视为未登录）
        cookie_value = self._resolve_cookie()
        if not cookie_value:
            if self.pw_profile:
                return (
                    tool_error(self.name, "未取得有效教务会话。\n")
                    + "  教务系统会话已失效（正方会话短效）。请运行 cugeopractor session-login "
                    "在浏览器中重新登录一次，之后 agent 将自动复用该登录态。"
                )
            return tool_error(self.name, "未配置会话（请登录教务系统并设置 SESSION_COOKIE/JWGL_COOKIE 环境变量）")

        # 组装浏览器风格请求头 + 会话 Cookie
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Cookie": cookie_value,
        }
        for k, v in self.headers.items():
            headers[k] = self._resolve(v)
        # POST 请求体：模板中的 {question} 替换为用户问题
        body_text = self._resolve(self.body) if self.body else ""
        # 学期参数化：用户问题含学期信息时替换 body 中对应的 xnm/xqm（仅白名单内参数）
        if body_text and self.semester_params:
            semester = parse_semester(question)
            if semester:
                for key, val in semester.items():
                    if key in self.semester_params:
                        # 只替换形如 "xnm=xxx" 或 "&xnm=xxx" 的参数值
                        body_text = re.sub(
                            rf"(^|&){key}=[^&]*", rf"\g<1>{key}={val}", body_text
                        )
        data = body_text.replace("{question}", question) if body_text else None

        def do_request() -> str:
            # 每次请求前限速（防封禁）
            with self._limiter:
                resp = httpx.request(
                    self.method, url, headers=headers, data=data,
                    timeout=15, follow_redirects=False, verify=_SSL_CONTEXT,
                )
            return resp

        try:
            resp = backoff_retry(do_request, retries=2, base_delay=1.0)
        except Exception as exc:  # 网络/超时
            self._breaker.record_failure()
            return tool_error(self.name, f"请求失败：{exc}")

        # 会话失效/风控特征处理：301/302 跳转登录页、401/403 未授权
        if resp.status_code in (301, 302, 401, 403):
            self._breaker.record_failure()
            return (
                tool_error(self.name, f"请求被拦截（HTTP {resp.status_code}）。\n")
                + "  会话可能已失效或触发了访问控制，请重新登录信息门户后更新会话"
            )
        # 正方教务系统会话失效特征码：HTTP 901（空响应体）
        if resp.status_code == 901:
            self._breaker.record_failure()
            if self.pw_profile:
                # Playwright 会话模式下：清掉缓存并给出重新登录指引
                from connectors import pw_session

                pw_session._cookie_cache.clear()  # noqa: SLF001 强制下次重新取会话
                return (
                    tool_error(self.name, "教务会话已过期（HTTP 901）。\n")
                    + "  正方系统会话短效，请运行 cugeopractor session-login 重新登录一次，"
                    "之后 agent 将自动复用并保活该登录态。"
                )
            return (
                tool_error(self.name, "教务会话已过期（HTTP 901）。\n")
                + "  正方系统会话短效（约 20~60 分钟），请重新登录教务系统并更新 JWGL_COOKIE；"
                "或改用 Playwright 持久会话模式（配置 pw_profile 字段）免手动导出。"
            )
        if resp.status_code != 200:
            self._breaker.record_failure()
            return tool_error(self.name, f"返回 HTTP {resp.status_code}")
        self._breaker.record_success()
        # 保存原始响应，供 /live_plan 等导出"原文件"（JSON 原文）使用
        self.last_raw = resp.text
        # 响应可读化：JSON 结构化数据（成绩/课表/考试等）按连接器字段映射转中文标签
        # 逐条输出（成绩接口的 xm 是学生姓名，故按 name 区分映射，避免误标"教师"）；
        # 非 JSON 原样截断（防止撑爆 LLM 上下文）
        human = _humanize_session_response(resp.text, name=self.name)
        # 课表检查机制（需求）：每次实时/LLM 查询课表后自动与
        # 上次缓存课表对比，输出差异描述（换课/调课提醒），并写回新快照。
        # 仅课表接口参与；失败/空课表不覆盖快照，避免误报"全部取消"。
        if self.name == "cug_course":
            check = _compare_and_cache_course(resp.text)
            if check:
                human = f"{human}\n\n{check}"
        return human


def load_session_connectors_from_yaml(path: str | Path = SESSION_CONNECTORS_FILE) -> list[SessionConnector]:
    """从 YAML 配置加载会话连接器列表（缺失文件返回空列表）。"""
    config_path = Path(path)
    if not config_path.exists():
        return []
    try:
        import yaml
    except ImportError:
        return []
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    connectors: list[SessionConnector] = []
    for item in data.get("session_connectors", []) or []:
        rl = item.get("rate_limit", {}) or {}
        connectors.append(
            SessionConnector(
                name=item["name"],
                description=item.get("description", item["name"]),
                url=item["url"],
                method=item.get("method", "GET"),
                cookie=item.get("cookie", "{{SESSION_COOKIE}}"),
                headers=item.get("headers", {}),
                allowed_prefix=item.get("allowed_prefix", ""),
                interval=rl.get("interval", 5.0),
                jitter=rl.get("jitter", 1.5),
                body=item.get("body", ""),
                pw_profile=item.get("pw_profile", ""),
                semester_params=item.get("semester_params"),
            )
        )
    return connectors


# ===== 培养方案完整版（实测打通：概要 + 97 门课程明细） =====

# 最近一次培养方案课程明细的原始响应（供 /live_plan 导出"原文件"用）
_last_plan_detail_raw: str = ""


def fetch_training_plan_full() -> str:
    """获取培养方案完整内容（专业概要 + 课程明细），供 /live_plan 导出"原文件"。

    背景：培养方案列表接口只返回 1 条概要（专业/学制/计划人数，含 jxzxjhxx_id
    主键）；**课程明细**在另一个接口（jxzxjhkcxx_cxJxzxjhkcxxIndex.html），
    反馈"下载到的培养方案只有'共1条'"就是只拿到了概要。本函数：

        1) 调 cug_training_plan（概要）→ 从原始响应解析 jxzxjhxx_id；
        2) 用 jxzxjhxx_id 调课程明细接口 → 97 门课程（jqGrid 分页参数）；
        3) 合并输出：概要字段 + 按课程类别分组的课程清单（含学分/开课部门）。

    返回：多行可读文本；中途失败返回以 [错误] 开头的提示（不影响概要展示）。
    """
    import httpx as _httpx

    from app.rate_limit import get_rate_limiter

    conns = {c.name: c for c in load_session_connectors_from_yaml()}
    plan = conns.get("cug_training_plan")
    if plan is None:
        return tool_error("cug_training_plan", "未配置培养方案连接器（data/session_connectors.yaml 缺失 cug_training_plan）")
    # 1) 概要：先展示，并从原始响应解析课程明细主键 jxzxjhxx_id
    summary_text = plan.invoke("")
    jh_id = ""
    raw = getattr(plan, "last_raw", "") or ""
    try:
        data = json.loads(raw)
        items = data.get("items") or []
        if items:
            jh_id = str(items[0].get("jxzxjhxx_id", ""))
    except Exception:  # noqa: BLE001 概要响应非 JSON 时跳过明细
        pass
    if not jh_id:
        return summary_text + "\n[注意] 未能从概要解析培养方案主键 jxzxjhxx_id，无法获取课程明细。"
    # 2) 课程明细接口（实测参数：jqGrid 分页 + paramMap_jxzxjhkc 字段）
    cookie = plan._resolve_cookie()
    if not cookie:
        return summary_text + "\n[错误] 未取得教务会话，无法获取课程明细（请 session-login 重新登录）。"
    detail_url = (
        "https://jwgl.cug.edu.cn/jwglxt/jxzxjhgl/jxzxjhkcxx_cxJxzxjhkcxxIndex.html"
        "?doType=query&gnmkdm=N153540"
    )
    detail_body = (
        f"jxzxjhxx_id={jh_id}&jyxdxnm=&jyxdxqm=&yxxdxnm=&yxxdxqm=&shzt=&kch=&xdlx="
        "&_search=false&nd=1&queryModel.showCount=200&queryModel.currentPage=1"
        "&queryModel.sortName=&queryModel.sortOrder=asc&time=1"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126",
        "Accept": "application/json, text/plain, */*",
        "Cookie": cookie,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://jwgl.cug.edu.cn/jwglxt/jxzxjhgl/jxzxjhck_cxJxzxjhckIndex.html?gnmkdm=N153540",
    }
    limiter = get_rate_limiter("cug_training_plan_detail", interval=5.0, jitter=1.5)
    try:
        with limiter:
            resp = _httpx.post(detail_url, headers=headers, data=detail_body, timeout=20, verify=_SSL_CONTEXT)
    except Exception as exc:  # noqa: BLE001 网络/超时
        return summary_text + f"\n[错误] 课程明细请求失败：{exc}"
    if resp.status_code != 200:
        return summary_text + f"\n[错误] 课程明细返回 HTTP {resp.status_code}"
    # 保存课程明细原始响应，供 /live_plan 导出"原文件"（服务端返回的 JSON 真身）
    global _last_plan_detail_raw
    _last_plan_detail_raw = resp.text
    try:
        detail = resp.json()
    except Exception:  # noqa: BLE001
        return summary_text + "\n[错误] 课程明细响应不是合法 JSON"
    rows = detail.get("items") or []
    if not rows:
        return summary_text + "\n[信息] 课程明细为空（可能培养方案未录入课程）。"
    # 3) 按课程类别分组统计 + 逐条输出
    groups: dict[str, list[dict]] = {}
    for row in rows:
        cat = row.get("kclbmc") or "（未分类）"
        groups.setdefault(cat, []).append(row)
    lines = [f"\n\n【课程明细】共 {len(rows)} 门课程，按类别分组："]
    for cat, items in groups.items():
        xf_total = sum(float(i.get("xf") or 0) for i in items)
        lines.append(f"\n▎{cat}（{len(items)} 门，合计 {xf_total:.1f} 学分）")
        for i, row in enumerate(items, 1):
            xf = row.get("xf") or ""
            lines.append(
                f"  {i}. {row.get('kcmc', '?')}　{kch_label(row.get('kch'))}　"
                f"性质：{row.get('kcxzmc', '?')}　类型：{row.get('kclxmc', '?')}　"
                f"学分：{xf}　开课：{row.get('kkbmmc', '?')}　周次：{row.get('qsjsz', '?')}"
            )
    return summary_text + "\n".join(lines)


def kch_label(kch) -> str:
    """课程号加说明前缀（课程号为纯数字时标 '课号'，避免与序号混淆）。"""
    return f"课号 {kch}" if kch else ""



def register_session_connectors(registry, config_path: str | Path = SESSION_CONNECTORS_FILE) -> int:
    """把配置中的会话连接器注册进工具注册表（供 LLM function calling 调用）。

    参数：
        registry: app.agent.tools.ToolRegistry 实例
        config_path: 会话连接器配置文件路径
    返回：
        成功注册的数量；无配置文件返回 0（连接器默认关闭）。
    """
    from app.agent.tools import ToolSpec

    count = 0
    for connector in load_session_connectors_from_yaml(config_path):
        registry.register(
            ToolSpec(
                name=connector.name,
                description=connector.description + "（需要登录信息门户并配置会话）",
                fn=connector.invoke,
            )
        )
        count += 1
    return count
