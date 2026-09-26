"""关账超时任务的后台 worker。

任务状态持久化在 settlement_tasks 表中；worker 只是“执行器”，因此进程重启
后重新启动 worker（或直接调用 run_due_timeouts）即可继续处理到期任务，
包括持锁进程崩溃后租约过期的陈旧任务。
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

from sqlalchemy.orm import Session

logger = logging.getLogger("settlement.worker")

DEFAULT_POLL_INTERVAL_SECONDS = float(os.getenv("SETTLEMENT_POLL_INTERVAL", "5"))
DEFAULT_LEASE_SECONDS = int(os.getenv("SETTLEMENT_TASK_LEASE", "60"))


class TimeoutWorker:
    """单进程内的守护线程轮询器；多实例部署时靠 claim 的行级条件保证不重复执行。"""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        worker_id: str,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self.worker_id = worker_id
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"settlement-timeout-{self.worker_id}", daemon=True
        )
        self._thread.start()
        logger.info("settlement timeout worker %s started", self.worker_id)

    def stop(self, timeout: float | None = 5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def tick(self) -> dict[str, int]:
        """执行一轮扫描（也可在重启后由外部脚本单独调用）。"""
        from .service import run_due_timeouts

        db = self._session_factory()
        try:
            return run_due_timeouts(db, worker_id=self.worker_id)
        finally:
            db.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.tick()
                if result["processed"] or result["failed"]:
                    logger.info("settlement timeout tick: %s", result)
            except Exception:  # 守护线程绝不让异常杀死循环。
                logger.exception("settlement timeout tick failed")
            self._stop.wait(self.poll_interval)


_default_worker: TimeoutWorker | None = None


def start_default_worker() -> TimeoutWorker | None:
    """按环境变量启动默认 worker；测试或一次性脚本可关闭。"""
    global _default_worker
    if os.getenv("SETTLEMENT_WORKER_ENABLED", "1") != "1":
        return None
    if _default_worker is not None:
        return _default_worker

    import socket

    from ..db import SessionLocal

    _default_worker = TimeoutWorker(
        SessionLocal, worker_id=f"{socket.gethostname()}-{os.getpid()}"
    )
    _default_worker.start()
    return _default_worker


def stop_default_worker() -> None:
    global _default_worker
    if _default_worker is not None:
        _default_worker.stop()
        _default_worker = None
