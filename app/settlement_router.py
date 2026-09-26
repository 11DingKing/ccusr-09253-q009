"""结算关账窗口 API：启动、推进、暂停/恢复、重开、状态查询与后台任务。"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import settlement
from .db import get_db
from .models import SettlementTaskState as TaskModel
from .settlement.stages import local_midnight_deadline
from .settlement_schemas import (
    AdvanceWindowIn,
    OpenWindowIn,
    PauseWindowIn,
    ReopenWindowIn,
    ResolveAnomalyIn,
    TaskOut,
    WindowOut,
)

router = APIRouter(prefix="/api/settlements", tags=["settlements"])


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, settlement.SettlementNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


def _local_deadlines(
    body: OpenWindowIn, timezone_name: str
) -> dict[Any, Any] | None:
    if not body.deadlines:
        return None
    result: dict[Any, Any] = {}
    for item in body.deadlines:
        if item.deadline_local_day is not None:
            day = date.fromisoformat(item.deadline_local_day)
            result[settlement.stages.Stage(item.stage)] = (
                local_midnight_deadline(day, timezone_name)
            )
    return result


@router.post("", response_model=WindowOut, status_code=status.HTTP_201_CREATED)
def open_settlement(body: OpenWindowIn, db: Session = Depends(get_db)) -> Any:
    # 时区以培养方案注册的 IANA 时区为准，本地日期期限据此换算 UTC。
    from .repository import get_plan

    plan = get_plan(db, body.plan_version)
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail=f"plan version '{body.plan_version}' is not registered",
        )
    window_id = body.window_id or f"SW-{body.plan_version}"
    try:
        return settlement.open_window(
            db,
            window_id=window_id,
            plan_version=body.plan_version,
            actor_id=body.actor_id,
            actor_role=body.actor_role,
            note=body.note,
            stage_durations_hours=body.stage_durations_hours,
            stage_owners=body.stage_owners,
            stage_deadlines=_local_deadlines(body, plan.iana_timezone),
        )
    except settlement.SettlementError as exc:
        raise _translate(exc) from exc


@router.post(
    "/{window_id}/advance",
    response_model=WindowOut,
)
def advance_settlement(
    window_id: str,
    body: AdvanceWindowIn,
    revision: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Any:
    try:
        return settlement.advance_window(
            db,
            window_id=window_id,
            revision=revision,
            actor_id=body.actor_id,
            actor_role=body.actor_role,
            note=body.note,
            force=body.force,
        )
    except (settlement.SettlementError, settlement.SettlementNotFoundError) as exc:
        raise _translate(exc) from exc


@router.post("/{window_id}/anomalies/resolve", response_model=WindowOut)
def resolve_anomalies(
    window_id: str,
    body: ResolveAnomalyIn,
    revision: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Any:
    try:
        return settlement.resolve_anomaly(
            db,
            window_id=window_id,
            revision=revision,
            actor_id=body.actor_id,
            actor_role=body.actor_role,
            note=body.note,
        )
    except (settlement.SettlementError, settlement.SettlementNotFoundError) as exc:
        raise _translate(exc) from exc


@router.post("/{window_id}/pause", response_model=WindowOut)
def pause_settlement(
    window_id: str,
    body: PauseWindowIn,
    revision: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Any:
    try:
        return settlement.pause_window(
            db,
            window_id=window_id,
            revision=revision,
            actor_id=body.actor_id,
            actor_role=body.actor_role,
            note=body.note,
        )
    except (settlement.SettlementError, settlement.SettlementNotFoundError) as exc:
        raise _translate(exc) from exc


@router.post("/{window_id}/resume", response_model=WindowOut)
def resume_settlement(
    window_id: str,
    body: PauseWindowIn,
    revision: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Any:
    try:
        return settlement.resume_window(
            db,
            window_id=window_id,
            revision=revision,
            actor_id=body.actor_id,
            actor_role=body.actor_role,
            note=body.note,
        )
    except (settlement.SettlementError, settlement.SettlementNotFoundError) as exc:
        raise _translate(exc) from exc


@router.post("/{window_id}/reopen", response_model=WindowOut)
def reopen_settlement(
    window_id: str, body: ReopenWindowIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return settlement.reopen_window(
            db,
            window_id=window_id,
            actor_id=body.actor_id,
            actor_role=body.actor_role,
            approver_id=body.approver_id,
            approver_role=body.approver_role,
            reason=body.reason,
        )
    except (settlement.SettlementError, settlement.SettlementNotFoundError) as exc:
        raise _translate(exc) from exc


@router.get("", response_model=list[WindowOut])
def list_settlements(
    plan_version: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Any:
    return settlement.list_windows(db, plan_version=plan_version)


@router.get("/{window_id}", response_model=WindowOut)
def get_settlement(
    window_id: str,
    revision: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Any:
    try:
        return settlement.get_window(db, window_id, revision)
    except settlement.SettlementNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{window_id}/tasks", response_model=list[TaskOut])
def list_tasks(
    window_id: str,
    revision: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Any:
    stmt = select(TaskModel).where(TaskModel.window_id == window_id)
    if revision is not None:
        stmt = stmt.where(TaskModel.revision == revision)
    stmt = stmt.order_by(TaskModel.revision, TaskModel.id)
    return list(db.execute(stmt).scalars().all())


@router.post("/run-timeouts")
def run_timeouts(
    worker_id: str = Query(default="api-manual"),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """手动触发一次到期任务处理；常驻 worker 也调用同一逻辑。"""
    return settlement.run_due_timeouts(db, worker_id=worker_id)
