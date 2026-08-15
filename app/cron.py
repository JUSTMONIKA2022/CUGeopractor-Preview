# -*- coding: utf-8 -*-
"""CLI 定时任务：后台定期刷新渠道缓存。

设计（对应 CLI 大改-定时任务）：
    - 任务配置持久化到 data/cache/cron.json（data/ 已 gitignore）；
    - 单例后台线程（daemon）每 30 秒检查一次，到点刷新对应渠道缓存；
    - 命令入口 /cron list|add <channel> <分钟>|remove <id>|stop（见 commands.py）。

说明：定时刷新与"动态生成+落盘缓存"配合——定时把各渠道最新内容刷进缓存，
用户通过 /cache_* 命令读缓存时总能看到较新数据（无需每次实时请求）。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from app.cache_store import CHANNELS, refresh_channel

# 任务配置持久化路径
CRON_FILE = Path("data/cache/cron.json")
# 调度检查间隔（秒）
CHECK_INTERVAL = 30.0

# 内存任务表：task_id -> {"channel", "interval_min", "last_run", "max_runs", "execute_count", "done"}
_tasks: dict[str, dict] = {}
_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop_flag = threading.Event()


def _load() -> None:
    """从磁盘加载任务配置。"""
    global _tasks
    if not CRON_FILE.exists():
        return
    try:
        _tasks = json.loads(CRON_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 损坏配置按空处理
        _tasks = {}
    # 兼容旧版本任务（无 max_runs/execute_count 字段）：补齐默认值
    # （需求：定时任务增加执行次数功能，旧任务按"不限次数"处理）
    for t in _tasks.values():
        t.setdefault("max_runs", None)
        t.setdefault("execute_count", 0)
        t.setdefault("done", False)


def _save() -> None:
    """任务配置落盘（原子写）。"""
    CRON_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CRON_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CRON_FILE)


def add_task(channel: str, interval_min: float, max_runs: int | None = None) -> str:
    """新增定时刷新任务，返回 task_id。

    参数：
        channel: 渠道名（须在 CHANNELS 内）
        interval_min: 刷新间隔（分钟）
        max_runs: 最多执行次数（None=不限次数，持续刷新；达到次数后任务标记完成）
    """
    if channel not in CHANNELS:
        raise ValueError(f"未知渠道：{channel}（可选：{', '.join(CHANNELS)}）")
    if interval_min <= 0:
        raise ValueError("间隔必须 > 0 分钟")
    if max_runs is not None and max_runs <= 0:
        raise ValueError("执行次数必须 > 0（或不填=不限次数）")
    task_id = uuid.uuid4().hex[:8]
    with _lock:
        _tasks[task_id] = {
            "channel": channel,
            "interval_min": float(interval_min),
            "last_run": 0.0,
            "max_runs": max_runs,
            "execute_count": 0,
            "done": False,
        }
        _save()
    return task_id


def remove_task(task_id: str) -> bool:
    """删除任务，返回是否找到。"""
    with _lock:
        if task_id in _tasks:
            del _tasks[task_id]
            _save()
            return True
        return False


def list_tasks() -> list[dict]:
    """列出全部任务（含下次刷新时间/执行次数/完成状态）。"""
    now = time.time()
    result = []
    with _lock:
        for tid, t in _tasks.items():
            next_run = t["last_run"] + t["interval_min"] * 60
            result.append(
                {
                    "id": tid,
                    "channel": t["channel"],
                    "interval_min": t["interval_min"],
                    "last_run": int(t["last_run"]),
                    "next_run": int(next_run) if next_run > now and not t.get("done") else 0,
                    "execute_count": t.get("execute_count", 0),
                    "max_runs": t.get("max_runs"),
                    "done": t.get("done", False),
                }
            )
    return result


def _worker() -> None:
    """后台循环：到点刷新对应渠道缓存。

    执行次数逻辑（需求）：任务可设定 max_runs（最大执行次数），
    每刷新一次 execute_count +1；达到 max_runs 后标记 done，不再参与调度
    （保留记录供 /cron list 查看，用户可 remove 删除）。
    """
    while not _stop_flag.is_set():
        now = time.time()
        due: list[str] = []
        with _lock:
            for tid, t in _tasks.items():
                if t.get("done"):
                    continue  # 已完成任务跳过调度
                if now - t["last_run"] >= t["interval_min"] * 60:
                    due.append(tid)
        for tid in due:
            with _lock:
                t = _tasks.get(tid)
                if t is None:
                    continue
                t["last_run"] = now
            # 在锁外执行网络刷新，避免阻塞其他命令
            try:
                refresh_channel(t["channel"])
            except Exception:  # noqa: BLE001 单次刷新失败不中断调度
                pass
            # 刷新后累计执行次数；达到上限则标记完成（仍保留在任务表供查看）
            with _lock:
                t = _tasks.get(tid)
                if t is None:
                    continue
                t["execute_count"] = t.get("execute_count", 0) + 1
                if t.get("max_runs") is not None and t["execute_count"] >= t["max_runs"]:
                    t["done"] = True
                _save()
        _stop_flag.wait(CHECK_INTERVAL)


def start() -> None:
    """启动后台调度线程（幂等）。"""
    global _thread
    _load()
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_flag.clear()
        _thread = threading.Thread(target=_worker, name="cugeopractor-cron", daemon=True)
        _thread.start()


def stop() -> None:
    """停止后台调度线程。"""
    global _thread
    _stop_flag.set()
    if _thread is not None:
        _thread.join(timeout=5)
        _thread = None
