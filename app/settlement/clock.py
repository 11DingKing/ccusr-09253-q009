"""可替换的时钟，便于测试超时与跨时区截止。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

_override: Callable[[], datetime] | None = None


def now() -> datetime:
    """当前 UTC 时间；测试中可被 ``set_clock`` 固定。"""
    if _override is not None:
        value = _override()
        if value.tzinfo is None:
            raise ValueError("clock override must return timezone-aware datetime")
        return value.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def set_clock(func: Callable[[], datetime] | None) -> None:
    global _override
    _override = func


def reset_clock() -> None:
    global _override
    _override = None
