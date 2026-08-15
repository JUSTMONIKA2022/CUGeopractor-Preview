# -*- coding: utf-8 -*-
"""全局限速器与重试工具（防封禁核心）。

设计说明（对应渠道规划红线 2）：
    - 所有抓取型渠道（官方个人系统 / 社区抓取）统一走本模块限速，
      避免高频请求触发目标站点风控被封禁；
    - 支持"最小请求间隔 + 随机抖动"：抖动让请求时间分布更接近真人行为；
    - 提供指数退避重试：网络抖动/临时失败时按 1s→2s→4s… 递增等待并重试；
    - 提供轻量熔断：同一渠道连续失败达到阈值后冷却一段时间，避免雪崩。

用法示例：
    limiter = get_rate_limiter("cug_course", interval=5.0, jitter=1.5)
    with limiter.guard():
        resp = http_request(...)
"""

from __future__ import annotations

import random
import threading
import time

# 全局限速器注册表（按渠道名隔离，互不干扰）
_RATE_LIMITERS: dict[str, "RateLimiter"] = {}
_LOCK = threading.Lock()


class RateLimiter:
    """单渠道请求限速器（进程内线程安全）。"""

    def __init__(self, min_interval: float = 3.0, jitter: float = 1.0) -> None:
        """初始化。

        参数：
            min_interval: 相邻请求的最小间隔（秒）
            jitter:       随机抖动幅度（秒）；实际等待 = min_interval + U(0, jitter)
        """
        self._min_interval = max(0.5, min_interval)
        self._jitter = max(0.0, jitter)
        self._last_ts = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        """在发起请求前调用：若距上次请求不足间隔，则 sleep 补齐并加抖动。

        说明：抖动随机化避免"固定间隔"被目标站点识别为爬虫特征。
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_ts
            need = self._min_interval + random.uniform(0, self._jitter)
            if elapsed < need:
                time.sleep(need - elapsed)
            self._last_ts = time.monotonic()

    def __enter__(self) -> "RateLimiter":
        """进入上下文时先限速（配合 with 使用）。"""
        self.wait()
        return self

    def __exit__(self, *exc) -> bool:
        """上下文退出，无额外清理。"""
        return False


class CircuitBreaker:
    """轻量熔断器：连续失败达到阈值后冷却，避免对失效渠道持续轰炸。"""

    def __init__(self, fail_threshold: int = 5, cooldown: float = 60.0) -> None:
        self._fail_threshold = fail_threshold
        self._cooldown = cooldown
        self._fail_count = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        """当前是否允许发起请求（熔断打开时返回 False）。"""
        with self._lock:
            if time.monotonic() < self._open_until:
                return False
            return True

    def record_success(self) -> None:
        """记录一次成功：重置失败计数并关闭熔断。"""
        with self._lock:
            self._fail_count = 0
            self._open_until = 0.0

    def record_failure(self) -> None:
        """记录一次失败：达到阈值后打开熔断并进入冷却。"""
        with self._lock:
            self._fail_count += 1
            if self._fail_count >= self._fail_threshold:
                self._open_until = time.monotonic() + self._cooldown
                self._fail_count = 0


def get_rate_limiter(name: str, interval: float = 3.0, jitter: float = 1.0) -> RateLimiter:
    """获取（或创建）指定渠道的限速器（全局注册表，重复获取复用同一实例）。"""
    global _RATE_LIMITERS
    with _LOCK:
        if name not in _RATE_LIMITERS:
            _RATE_LIMITERS[name] = RateLimiter(interval, jitter)
        return _RATE_LIMITERS[name]


def backoff_retry(fn, retries: int = 3, base_delay: float = 1.0, jitter: float = 0.5):
    """指数退避重试：调用 fn()，失败时按 base * 2^n 递增等待后重试。

    参数：
        fn:         无参可调用对象（发起一次请求并返回结果）
        retries:    最多重试次数（默认 3）
        base_delay: 首次等待基数（秒）
        jitter:     等待时间的随机抖动幅度
    返回：
        fn 的返回值；重试耗尽后抛出最后一次异常。
    说明：
        - 仅对"可重试"的网络波动做退避，业务性错误（如 403/会话失效）
          由调用方自行判断是否重试；
        - 每次重试前同样受对应限速器约束（由调用方在 fn 内或外层处理）。
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 统一收集最后一次异常用于抛出
            last_exc = exc
            if attempt < retries:
                delay = base_delay * (2 ** attempt) + random.uniform(0, jitter)
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc
