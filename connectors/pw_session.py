# -*- coding: utf-8 -*-
"""Playwright 持久会话管理（教务系统 L1 渠道）。

背景：
    - 正方教务系统**没有独立登录入口**，必须从信息门户（i.cug.edu.cn）进入
      （直接访问 jwgl URL 会显示"该页面无效，请从地大主页新版信息门户登陆"）；
    - 正方教务会话为服务端超时（约 20~60 分钟），手动导出 Cookie 频繁失效。
本模块用 Playwright **持久化浏览器 profile** 保存用户真实登录态：
    - `login_jwgl`：打开可见浏览器 → 统一认证门户登录页，用户手动登录一次
      （账号密码 + 滑块验证码）→ 自动在门户搜索「教务管理」进入教务系统，
      登录态持久保存在本机 profile（data/browser_profile/jwgl，已被 .gitignore 排除）；
    - `get_session_cookie`：每次请求前以 headless 复用同一 profile，
      经门户搜索进入教务系统（同时刷新服务端会话活跃时间 → 自动保活），
      返回当前有效 Cookie；门户会话失效则返回 None，调用方提示用户重新登录。

门户 → 教务跳转细节（浏览器实测）：
    - 门户为 Vue SPA，搜索框 `input[placeholder="请输入服务或应用"]`；
    - 输入「教务管理」后约 2~3.5 秒下拉出现 `li.lui-pro-search-item:has-text("教务管理")`；
    - 点击后 `window.open('https://jwgl.cug.edu.cn/sso/driotlogin')` **新标签页**打开，
      认证完全走 cookie（无 ticket 参数），落地 `index_initMenu.html?jsdm=xs&_t=...&echarts=1`。

设计边界：
    - 登录完全由用户手动完成（本项目不实现自动模拟登录/破解验证码）；
    - 本模块只负责"复用用户真实登录态"，不绕过任何平台验证；
    - Playwright 为可选依赖（pyproject `render` 组），未安装时返回 None 由上层降级提示。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# 持久化 profile 根目录（data/ 已被 .gitignore 排除，登录态不入库）
PROFILE_ROOT = Path("data/browser_profile")
# 统一认证门户登录页（service 指向信息门户；用户在这里手动登录）
PORTAL_LOGIN_URL = (
    "https://sfrz.cug.edu.cn/authserver/login"
    "?service=https%3A%2F%2Fi.cug.edu.cn%2Fweb%2F%3FCASLOGIN%3DCASLOGIN"
)
# 信息门户首页（Vue SPA）
PORTAL_HOME_URL = "https://i.cug.edu.cn/web/"
# 教务系统落地页（学生身份；由门户跳转产生，不直接 goto）
JWGL_HOME_URL = "https://jwgl.cug.edu.cn/jwglxt/xtgl/index_initMenu.html?jsdm=xs"

# 门户搜索框 / 下拉项选择器（浏览器实测）
_SEARCH_INPUT_SELECTOR = 'input[placeholder="请输入服务或应用"]'
_SEARCH_ITEM_SELECTOR = 'li.lui-pro-search-item:has-text("教务管理")'

# Playwright 可选依赖：未安装时 sync_playwright 为 None（本模块降级为不可用）
try:
    from playwright.sync_api import sync_playwright
except ImportError:  # playwright 未安装（可选依赖，pyproject render 组）
    sync_playwright = None  # type: ignore[assignment]

# Cookie 缓存：避免高频工具调用时每次都重新启动浏览器
_cookie_cache: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 60.0  # 秒；超过则重新取 Cookie（顺带保活会话）


def get_portal_cookie(profile_dir: str | Path | None = None) -> str | None:
    """复用持久化 profile 获取**信息门户**（i.cug.edu.cn）会话 Cookie（headless）。

    与 get_session_cookie 的区别：
        - get_session_cookie 取的是**教务域**（jwgl.cug.edu.cn）会话，供教务连接器用；
        - 本函数取**门户域**（i.cug.edu.cn）会话，供门户数据接口（/data/...）用，
          如办事流程查询、自习室课表等网上厅服务。
    门户会话为长效 CAS 会话（CASTGC），关浏览器仍保持；本函数顺带起到保活作用。

    返回：
        "name=value; ..." Cookie 串；None = Playwright 未安装 / 门户会话失效。
    """
    #  实测：headless 启动取 cookie 存在间歇性失败（异常/超时），
    # 失败重试一次可显著提高成功率（自习室课表列表+详情多次调用时曾偶发 None）
    result = _get_portal_cookie_once(profile_dir)
    if result is None:
        result = _get_portal_cookie_once(profile_dir)
    return result


def _get_portal_cookie_once(profile_dir: str | Path | None = None) -> str | None:
    """取门户 cookie 的单次实现（失败由 get_portal_cookie 重试一次）。"""
    profile = _profile_dir(profile_dir)
    cache_key = f"portal:{profile}"
    now = time.time()
    cached = _cookie_cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    if sync_playwright is None:
        return None
    try:
        with sync_playwright() as pw:
            context = _launch_context(pw, profile, headless=True)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(PORTAL_HOME_URL, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(4000)  # 等待 SPA 渲染与可能的跳转
            except Exception:  # noqa: BLE001 超时/网络异常按会话失效处理
                context.close()
                return None
            if not _is_portal_logged_in(page):
                context.close()
                return None
            # 提取门户域 Cookie（会话 cookie 均挂在 i.cug.edu.cn 域）
            cookies = context.cookies("https://i.cug.edu.cn/")
            context.close()
            if not cookies:
                return None
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            # 校验：必须含门户会话标识 cookie，否则视为未登录。
            # 实测（）i.cug.edu.cn 域会话凭证为 X-AUTH-TOKEN + isMkLogin
            # （CASTGC 在 CAS 域 sfrz.cug.edu.cn 下，不在本域）；兼容常见 SESSION 命名。
            if not any(
                k in cookie_str for k in ("X-AUTH-TOKEN", "isMkLogin", "SESSION", "CASTGC")
            ):
                return None
            _cookie_cache[cache_key] = (now, cookie_str)
            return cookie_str
    except Exception:  # noqa: BLE001 浏览器启动/导航异常按不可用处理
        return None


def _profile_dir(profile_dir: str | Path | None) -> Path:
    """默认 profile 目录；允许通过参数/环境变量覆盖。"""
    if profile_dir:
        return Path(profile_dir)
    env_dir = os.environ.get("JWGL_PW_PROFILE", "")
    if env_dir:
        return Path(env_dir)
    return PROFILE_ROOT / "jwgl"


def _launch_context(pw, profile: Path, headless: bool):
    """启动持久化浏览器上下文：优先复用系统 Chrome，失败退回自带 chromium。

     补充：无头启动附加禁用参数——避免 Chrome 初始化时写系统目录
    （GPU/NVIDIA/Intel 着色器缓存、chrome debug.log、输入法日志）被受限沙箱
    拦截导致启动失败（实测 trae-sandbox 下报 "Not allow operate files"）。
    这些参数对正常用户环境无副作用，属常规无头/CI 加固。
    """
    args = [
        "--disable-gpu",                 # 禁 GPU：避免 NVIDIA/Intel 驱动写盘
        "--disable-logging",             # 禁日志：避免 chrome debug.log 写盘
        "--log-level=3",                 # 仅记录致命错误（配合禁日志）
        "--disable-gpu-shader-disk-cache",  # 禁着色器磁盘缓存（Intel ShaderCache）
        "--no-first-run",                # 跳过首次运行引导（避免写配置）
        "--no-default-browser-check",    # 不做默认浏览器检查
    ]
    profile.mkdir(parents=True, exist_ok=True)
    try:
        return pw.chromium.launch_persistent_context(
            user_data_dir=str(profile), channel="chrome", headless=headless,
            args=args,
        )
    except Exception:  # noqa: BLE001 系统 Chrome 不可用时退回自带内核
        return pw.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=headless, args=args,
        )


def _is_portal_logged_in(page) -> bool:
    """门户页面是否处于登录态（未跳转到统一认证登录页）。"""
    return "authserver" not in page.url


def _enter_jwgl_from_portal(page, context, timeout_ms: int = 20000):
    """在已登录的门户页执行「搜索教务管理 → 点击 → 新标签页进入教务」。

    返回教务系统的新标签页对象；门户未登录/流程失败时抛出异常由调用方处理。
    """
    # 等待门户 SPA 渲染出搜索框
    page.wait_for_selector(_SEARCH_INPUT_SELECTOR, timeout=timeout_ms)
    page.fill(_SEARCH_INPUT_SELECTOR, "教务管理")
    # 等待下拉项出现（门户防抖约 2~3.5 秒，用显式等待而非固定 sleep）
    page.wait_for_selector(_SEARCH_ITEM_SELECTOR, timeout=timeout_ms)
    # 点击后门户以 window.open 新标签页打开教务 SSO 端点
    with page.expect_popup() as popup_info:
        page.click(_SEARCH_ITEM_SELECTOR)
    popup = popup_info.value
    popup.wait_for_load_state("domcontentloaded", timeout=30000)
    return popup


def login_jwgl(profile_dir: str | Path | None = None, timeout: float = 600.0) -> bool:
    """打开可见浏览器，引导用户完成「门户登录 → 进入教务」，登录态持久保存。

    参数：
        profile_dir: 持久化 profile 目录（默认 data/browser_profile/jwgl）
        timeout: 等待用户完成登录的最大秒数
    返回：
        True=已完成登录并进入教务；False=超时/失败
    """
    if sync_playwright is None:
        print("[错误] 未安装 Playwright，请先安装可选依赖：pip install -e '.[render]'")
        return False

    profile = _profile_dir(profile_dir)
    print(f"将在浏览器中打开统一认证门户登录页（持久会话目录：{profile}）")
    print("请在浏览器中完成登录（账号密码 + 滑块验证码）。登录完成后程序自动继续。")
    with sync_playwright() as pw:
        context = _launch_context(pw, profile, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        # 阶段零：导航到统一认证门户登录页。
        # 关键：wait_until 用 domcontentloaded 而非默认 load——学校登录页含第三方
        # 资源，load 事件触发前连接可能被关闭（实测 net::ERR_CONNECTION_CLOSED）；
        # 登录检测是轮询页面 URL（_is_portal_logged_in），DOM 就绪即可继续，
        # 因此导航中途失败不应中止流程，容错后交给阶段一轮询处理。
        try:
            page.goto(PORTAL_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001 导航异常（网络抖动/资源被重置）容错处理
            print(f"[注意] 打开登录页时网络异常（{type(exc).__name__}: {exc}），等待页面恢复…")
            try:
                page.wait_for_timeout(3000)  # 等待浏览器完成已开始的加载
            except Exception:  # noqa: BLE001
                pass
            # 若页面仍停在空白页（URL 未进入 authserver 登录页），则无法继续，明确报错
            if "authserver" not in page.url:
                context.close()
                print("[错误] 无法打开统一认证登录页（网络异常），请检查网络后重试。")
                return False
        # 阶段一：等待用户完成门户登录（URL 离开 authserver 进入门户）
        deadline = time.time() + timeout
        logged_in = False
        while time.time() < deadline:
            try:
                page.wait_for_timeout(2000)
                if _is_portal_logged_in(page):
                    logged_in = True
                    break
            except Exception:  # noqa: BLE001 页面导航中异常则继续轮询
                continue
        if not logged_in:
            context.close()
            print("未在限定时间内完成门户登录，可重新运行本命令。")
            return False
        # 阶段二：确保在门户首页（登录后可能落在二级门户等其它视图，显式回首页）
        try:
            page.goto(PORTAL_HOME_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)  # 等待 SPA 渲染与路由跳转
        except Exception:  # noqa: BLE001
            pass
        try:
            popup = _enter_jwgl_from_portal(page, context)
            popup.wait_for_timeout(3000)  # 等待教务会话建立
        except Exception as exc:  # noqa: BLE001
            context.close()
            print(f"门户登录成功，但自动进入教务失败（{exc}）。当前页面：{page.url}")
            print("可手动在门户中搜索「教务管理」点击进入一次，之后重跑本命令。")
            return False
        context.close()
    print("登录成功，已进入教务系统，会话持久保存；agent 将自动复用该登录态。")
    return True


def get_session_cookie(profile_dir: str | Path | None = None) -> str | None:
    """复用持久化 profile，经门户进入教务获取当前有效 Cookie（headless，顺带保活）。

    返回：
        "name=value; name2=value2; ..." 形式的 Cookie 串；
        None = Playwright 未安装 / 门户会话失效（需重新登录）。
    """
    # 与 get_portal_cookie 相同的间歇性失败对策：失败重试一次
    result = _get_session_cookie_once(profile_dir)
    if result is None:
        result = _get_session_cookie_once(profile_dir)
    return result


def _get_session_cookie_once(profile_dir: str | Path | None = None) -> str | None:
    """取教务 cookie 的单次实现（失败由 get_session_cookie 重试一次）。"""
    profile = _profile_dir(profile_dir)
    cache_key = str(profile)
    now = time.time()
    cached = _cookie_cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    if sync_playwright is None:
        return None

    try:
        with sync_playwright() as pw:
            context = _launch_context(pw, profile, headless=True)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(PORTAL_HOME_URL, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(4000)  # 等待 SPA 渲染与可能的跳转
            except Exception:  # noqa: BLE001 超时/网络异常按会话失效处理
                context.close()
                return None
            if not _is_portal_logged_in(page):
                context.close()
                return None
            try:
                popup = _enter_jwgl_from_portal(page, context)
                popup.wait_for_timeout(3000)  # 等待教务会话建立
            except Exception:  # noqa: BLE001 自动进入教务失败按会话失效处理
                context.close()
                return None
            # 提取 jwgl 域 Cookie：必须用教务落地页 URL（path=/jwglxt），
            # 否则会漏掉 JSESSIONID（其 path 为 /jwglxt 而非 /）
            cookie_url = popup.url if "jwgl.cug.edu.cn" in popup.url else JWGL_HOME_URL
            cookies = context.cookies(cookie_url)
            context.close()
            if not cookies:
                return None
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            # 校验：教务会话必须含服务端会话 cookie（如 JSESSIONID），
            # 仅剩 EXPIRED_MODAL 等非会话 cookie 视为会话未建立
            if "SESSIONID" not in cookie_str and "JSESSION" not in cookie_str:
                return None
            _cookie_cache[cache_key] = (now, cookie_str)
            return cookie_str
    except Exception:  # noqa: BLE001 浏览器启动/导航异常按不可用处理
        return None
