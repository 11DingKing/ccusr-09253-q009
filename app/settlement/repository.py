"""结算窗口持久化：窗口、版本材料与持久化后台任务。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..models import (
    SettlementRevision,
    SettlementTask,
    SettlementWindow,
)


class RevisionSealConflict(RuntimeError):
    """版本行已被并发封存，不能重复封存。"""


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite 不保留时区，读回的 naive 时间戳按 UTC 处理。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------


def insert_window(db: Session, values: dict[str, Any]) -> SettlementWindow | None:
    """插入新窗口；若 (plan, window) 已存在则返回 None（不覆盖）。"""
    stmt = sqlite_insert(SettlementWindow).values(**values)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "window_id"]
    ).returning(SettlementWindow.row_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    if inserted is None:
        return None
    db.flush()
    return get_window(db, values["plan_version"], values["window_id"])


def get_window(
    db: Session, plan_version: str, window_id: str
) -> SettlementWindow | None:
    return db.get(SettlementWindow, (plan_version, window_id))


def list_windows(db: Session, plan_version: str | None = None) -> list[SettlementWindow]:
    stmt = select(SettlementWindow).order_by(
        SettlementWindow.plan_version, SettlementWindow.window_id
    )
    if plan_version is not None:
        stmt = stmt.where(SettlementWindow.plan_version == plan_version)
    return list(db.execute(stmt).scalars().all())


def conditional_update_window(
    db: Session,
    *,
    plan_version: str,
    window_id: str,
    expected_row_version: int,
    values: dict[str, Any],
) -> bool:
    """乐观锁条件更新；row_version 不匹配（并发推进）时返回 False。"""
    if not values:
        return True
    stmt = (
        update(SettlementWindow)
        .where(SettlementWindow.plan_version == plan_version)
        .where(SettlementWindow.window_id == window_id)
        .where(SettlementWindow.row_version == expected_row_version)
        .values(**values, row_version=SettlementWindow.row_version + 1)
    )
    result = db.execute(stmt)
    return (result.rowcount or 0) == 1


# ---------------------------------------------------------------------------
# 版本材料（不可变）
# ---------------------------------------------------------------------------


def insert_revision(db: Session, values: dict[str, Any]) -> SettlementRevision:
    row = SettlementRevision(**values)
    db.add(row)
    db.flush()
    return row


def get_revision(
    db: Session, plan_version: str, window_id: str, revision: int
) -> SettlementRevision | None:
    stmt = select(SettlementRevision).where(
        SettlementRevision.plan_version == plan_version,
        SettlementRevision.window_id == window_id,
        SettlementRevision.revision == revision,
    )
    return db.execute(stmt).scalar_one_or_none()


def list_revisions(
    db: Session, plan_version: str, window_id: str
) -> list[SettlementRevision]:
    stmt = (
        select(SettlementRevision)
        .where(
            SettlementRevision.plan_version == plan_version,
            SettlementRevision.window_id == window_id,
        )
        .order_by(SettlementRevision.revision)
    )
    return list(db.execute(stmt).scalars().all())


def seal_revision_row(
    db: Session,
    row: SettlementRevision,
    *,
    cutoff_event_id: str | None,
    freeze_id: str,
    materials: dict[str, Any],
    sealed_at: datetime,
) -> None:
    """把重开时产生的 open 版本行推进为 sealed。

    合并而非替换原有材料（重开原因等），且只作用于当前版本行，
    历史 sealed 版本永远不会被这条语句触及。
    """
    merged = {**(row.materials or {}), **materials}
    stmt = (
        update(SettlementRevision)
        .where(SettlementRevision.id == row.id)
        .where(SettlementRevision.status == "open")
        .values(
            status="sealed",
            cutoff_event_id=cutoff_event_id,
            freeze_id=freeze_id,
            materials=merged,
            sealed_at=sealed_at,
        )
    )
    result = db.execute(stmt)
    if (result.rowcount or 0) != 1:
        raise RevisionSealConflict(
            f"revision {row.revision} is not open and cannot be sealed"
        )


# ---------------------------------------------------------------------------
# 持久化后台任务
# ---------------------------------------------------------------------------


def insert_task(db: Session, values: dict[str, Any]) -> SettlementTask | None:
    stmt = sqlite_insert(SettlementTask).values(**values)
    stmt = stmt.on_conflict_do_nothing(index_elements=["task_key"])
    db.execute(stmt)
    db.flush()
    return get_task_by_key(db, values["task_key"])


def reset_task(
    db: Session,
    task: SettlementTask,
    *,
    run_at: datetime,
    status: str = "pending",
) -> None:
    """重新启用一个已取消/曾租约的任务（同一 task_key 不新增行）。"""
    stmt = (
        update(SettlementTask)
        .where(SettlementTask.id == task.id)
        .values(
            status=status,
            run_at=run_at,
            leased_by=None,
            leased_at=None,
            completed_at=None,
        )
    )
    db.execute(stmt)
    db.flush()


def get_task_by_key(db: Session, task_key: str) -> SettlementTask | None:
    stmt = select(SettlementTask).where(SettlementTask.task_key == task_key)
    return db.execute(stmt).scalar_one_or_none()


def get_task(db: Session, task_id: int) -> SettlementTask | None:
    return db.get(SettlementTask, task_id)


def cancel_tasks(
    db: Session,
    *,
    plan_version: str,
    window_id: str,
    revision: int | None = None,
    stage: str | None = None,
) -> int:
    stmt = (
        update(SettlementTask)
        .where(SettlementTask.plan_version == plan_version)
        .where(SettlementTask.window_id == window_id)
        .where(SettlementTask.status.in_(["pending", "leased"]))
        .values(status="cancelled")
    )
    if revision is not None:
        stmt = stmt.where(SettlementTask.revision == revision)
    if stage is not None:
        stmt = stmt.where(SettlementTask.stage == stage)
    result = db.execute(stmt)
    return result.rowcount or 0


def lease_due_tasks(
    db: Session,
    *,
    now: datetime,
    lease_seconds: int,
    worker_id: str,
    limit: int = 16,
) -> list[SettlementTask]:
    """领取到期任务；同时回收租约过期的崩溃任务（重启补偿）。"""
    lease_cutoff = datetime.fromtimestamp(
        now.timestamp() - lease_seconds, tz=now.tzinfo
    )
    candidate_ids_stmt = select(SettlementTask.id).where(
        SettlementTask.status.in_(["pending", "leased"])
    )
    candidate_ids = list(db.execute(candidate_ids_stmt).scalars().all())

    due: list[SettlementTask] = []
    for task_id in candidate_ids:
        task = db.get(SettlementTask, task_id)
        if task is None:
            continue
        run_at = _as_utc(task.run_at)
        leased_at = _as_utc(task.leased_at)
        ready = task.status == "pending" and run_at <= now
        stale = task.status == "leased" and leased_at is not None and leased_at <= lease_cutoff
        if not ready and not stale:
            continue
        stmt = (
            update(SettlementTask)
            .where(SettlementTask.id == task_id)
            .where(SettlementTask.status.in_(["pending", "leased"]))
            .values(status="leased", leased_by=worker_id, leased_at=now)
            .returning(SettlementTask.id)
        )
        if db.execute(stmt).scalar_one_or_none() is not None:
            db.flush()
            due.append(db.get(SettlementTask, task_id))
        if len(due) >= limit:
            break
    return due


def requeue_task(
    db: Session,
    task: SettlementTask,
    *,
    run_at: datetime,
    error: str,
) -> None:
    stmt = (
        update(SettlementTask)
        .where(SettlementTask.id == task.id)
        .values(
            status="pending",
            run_at=run_at,
            attempts=task.attempts,
            last_error=error[:2000],
            leased_by=None,
            leased_at=None,
        )
    )
    db.execute(stmt)


def fail_task(db: Session, task: SettlementTask, *, error: str) -> None:
    stmt = (
        update(SettlementTask)
        .where(SettlementTask.id == task.id)
        .values(
            status="failed",
            attempts=task.attempts,
            last_error=error[:2000],
            leased_by=None,
            leased_at=None,
        )
    )
    db.execute(stmt)


def complete_task(db: Session, task: SettlementTask, *, now: datetime) -> None:
    stmt = (
        update(SettlementTask)
        .where(SettlementTask.id == task.id)
        .values(
            status="completed",
            leased_by=None,
            leased_at=None,
            completed_at=now,
            last_error=None,
        )
    )
    db.execute(stmt)


def list_tasks_for_window(
    db: Session, plan_version: str, window_id: str
) -> list[SettlementTask]:
    stmt = (
        select(SettlementTask)
        .where(
            SettlementTask.plan_version == plan_version,
            SettlementTask.window_id == window_id,
        )
        .order_by(SettlementTask.id)
    )
    return list(db.execute(stmt).scalars().all())
