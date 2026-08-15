# -*- coding: utf-8 -*-
"""限速器/重试/熔断单元测试。"""

import time

import pytest

from app.rate_limit import CircuitBreaker, RateLimiter, backoff_retry, get_rate_limiter


def test_rate_limiter_wait_enforces_interval():
    """相邻请求的间隔不应小于配置值（允许少量抖动误差）。"""
    limiter = RateLimiter(min_interval=0.2, jitter=0.0)
    start = time.monotonic()
    limiter.wait()
    limiter.wait()  # 第二次应立即受间隔约束
    elapsed = time.monotonic() - start
    assert elapsed >= 0.19, "第二次请求前应等待至少 0.2s"


def test_get_rate_limiter_singleton():
    """同名渠道应复用同一限速器实例。"""
    a = get_rate_limiter("channel-x", interval=1.0)
    b = get_rate_limiter("channel-x", interval=99.0)
    assert a is b, "同名渠道应返回同一实例（复用既有间隔配置）"


def test_backoff_retry_succeeds_on_retry():
    """前两次失败、第三次成功时，退避重试应返回结果。"""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("temporary")
        return "ok"

    assert backoff_retry(flaky, retries=3, base_delay=0.01, jitter=0.0) == "ok"
    assert calls["n"] == 3


def test_backoff_retry_raises_after_exhausted():
    """始终失败时，退避重试应抛出最后一次异常。"""
    with pytest.raises(ValueError):
        backoff_retry(lambda: (_ for _ in ()).throw(ValueError("boom")), retries=2, base_delay=0.01, jitter=0.0)


def test_circuit_breaker_opens_and_cools():
    """连续失败达到阈值后应打开熔断；冷却后恢复允许。"""
    breaker = CircuitBreaker(fail_threshold=3, cooldown=0.05)
    assert breaker.allow() is True
    for _ in range(3):
        breaker.record_failure()
    assert breaker.allow() is False, "达到阈值后应拒绝请求"
    breaker.record_success()  # 手动恢复（模拟冷却后成功）
    assert breaker.allow() is True
