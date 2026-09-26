"""结算窗口后台调度器：领取持久化超时任务并执行。

可靠性要点：

* 任务全部落在 ``settlement_tasks`` 表（pending/leased/completed/failed/
  cancelled），进程重启不丢；
* 领取采用条件更新租约，租约过期的 leased 任务会被重新回收，
  因此 worker 崩溃或重启后任务会继续；
* 执行失败按指数退避重新入队，超过最大尝试次数进入 failed 死信，
  不会被静默吞掉。
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import timedelta
from typing import Callable

from sqlalchemy.orm import Session

from . import repository as repo
from . import service
from .clock import now
from .stages import STATE_COMPLETED, STATE_PAUSED

logger = logging.getLogger("settlement.scheduler")

LEASE_SECONDS = 60
MAX_ATTEMPTS = 5
BASE_BACKOFF = timedelta(seconds=5)

_session_factory: Callable[[], Session] | None = None
_worker_id = f"worker-{os.getpid()}"
_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_poll_interval = float(os.getenv("SETTLEMENT_POLL_SECONDS", "5"))
_enabled = os.getenv("SETTLEMENT_SCHEDULER_ENABLED", "1") == "1"


def configure(
    *,
    session_factory: Callable[[], Session] | None = None,
    poll_interval: float | None = None,
    enabled: bool | None = None,
) -> None:
    """测试或部署侧覆盖会话工厂、轮询间隔与启停。"""
    global _session_factory, _poll_interval, _enabled
    if session_factory is not None:
        _session_factory = session_factory
    if poll_interval is not None:
        _poll_interval = poll_interval
    if enabled is not None:
        _enabled = enabled


def _resolve_factory() -> Callable[[], Session]:
    if _session_factory is not None:
        return _session_factory
    # 延迟导入：测试引擎通过 configure 注入，避免拿到默认文件引擎。
    from ..db import SessionLocal

    return SessionLocal


# ---------------------------------------------------------------------------
# 单轮处理（也供测试直接调用，模拟重启后的恢复）
# ---------------------------------------------------------------------------


def process_once(db: Session, *, worker_id: str | None = None) -> dict[str, int]:
    """领取并处理所有到期任务，返回处理计数。"""
    worker = worker_id or _worker_id
    instant = now()
    tasks = repo.lease_due_tasks(
        db,
        now=instant,
        lease_seconds=LEASE_SECONDS,
        worker_id=worker,
    )
    advanced = 0
    noop = 0
    failed = 0
    for task in tasks:
        outcome = _execute(db, task, worker=worker, instant=instant)
        if outcome == "advanced":
            advanced += 1
        elif outcome == "failed":
            failed += 1
        else:
            noop += 1
    return {"advanced": advanced, "noop": noop, "failed": failed}


def _execute(db: Session, task, *, worker: str, instant) -> str:
    try:
        if task.task_type != "stage_timeout":
            raise ValueError(f"未知任务类型 {task.task_type!r}")

        window = repo.get_window(db, task.plan_version, task.window_id)
        stale = (
            window is None
            or window.state == STATE_COMPLETED
            or window.state == STATE_PAUSED
            or window.current_stage != task.stage
            or window.revision != task.revision
        )
        if stale:
            # 窗口已被人工推进/重开/冻结，遗留任务直接终结，不重复触发。
            repo.complete_task(db, task, now=instant)
        else:
            service.advance_window(
                db,
                plan_version=task.plan_version,
                window_id=task.window_id,
                actor_id=worker,
                actor_role="scheduler",
                note=f"阶段 {task.stage} 期限到达，系统自动推进",
                forced=True,
            )
            # advance_window 自行提交；任务租约在此单独终结。
            repo.complete_task(db, task, now=instant)
        db.commit()
        return "noop" if stale else "advanced"
    except Exception as exc:  # 失败补偿：退避重试或死信
        db.rollback()
        logger.exception(
            "settlement task %s failed", task.id
        )
        reloaded = repo.get_task(db, task.id)
        if reloaded is None:
            return "failed"
        # 尝试次数在失败分支里持久化（租约本身不增加计数，避免崩溃重复计费）。
        reloaded.attempts = (reloaded.attempts or 0) + 1
        if reloaded.attempts >= (reloaded.max_attempts or MAX_ATTEMPTS):
            repo.fail_task(db, reloaded, error=repr(exc))
            result = "failed"
        else:
            backoff = BASE_BACKOFF * (2 ** (reloaded.attempts - 1))
            repo.requeue_task(db, reloaded, run_at=now() + backoff, error=repr(exc))
            result = "noop"
        db.commit()
        return result


# ---------------------------------------------------------------------------
# 后台线程
# ---------------------------------------------------------------------------


def recover_on_startup() -> dict[str, int]:
    """进程启动时执行一轮：pending 到期任务与崩溃遗留的 leased 任务都在此继续。"""
    factory = _resolve_factory()
    db = factory()
    try:
        return process_once(db)
    finally:
        db.close()


def _run_loop() -> None:
    factory = _resolve_factory()
    while not _stop.wait(_poll_interval):
        db = factory()
        try:
            process_once(db)
        except Exception:
            logger.exception("settlement scheduler iteration failed")
        finally:
            db.close()


def start_scheduler() -> None:
    """启动后台调度线程（幂等）。"""
    global _thread
    if not _enabled:
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(
            target=_run_loop,
            name="settlement-scheduler",
            daemon=True,
        )
        _thread.start()


def stop_scheduler() -> None:
    global _thread
    with _lock:
        _stop.set()
        thread = _thread
        _thread = None
    if thread is not None:
        thread.join(timeout=5)
