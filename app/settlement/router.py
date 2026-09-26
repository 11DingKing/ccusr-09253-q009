"""结算关账窗口的 HTTP 接口：启动、推进、暂停、恢复、重开、状态查询。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from . import service
from .errors import (
    InvalidStageError,
    ResponsibilityError,
    RevisionConflictError,
    SettlementError,
    WindowAlreadyExistsError,
    WindowNotFoundError,
    WindowStateError,
)
from .repository import list_windows
from .schemas import (
    AdvanceIn,
    PauseIn,
    ReopenIn,
    StartWindowIn,
    WindowStatusOut,
    WindowSummary,
)
from .stages import parse_aware

router = APIRouter(prefix="/api/plans/{plan_version}/settlement-windows", tags=["settlement"])


def _status_code_for(exc: SettlementError) -> int:
    if isinstance(exc, WindowNotFoundError):
        return 404
    if isinstance(exc, WindowAlreadyExistsError):
        return 409
    if isinstance(exc, RevisionConflictError):
        return 409
    if isinstance(exc, ResponsibilityError):
        return 403
    if isinstance(exc, WindowStateError):
        return 409
    if isinstance(exc, InvalidStageError):
        return 422
    return 400


def _raise(exc: SettlementError) -> None:
    raise HTTPException(status_code=_status_code_for(exc), detail=str(exc)) from exc


@router.post("", response_model=WindowStatusOut, status_code=status.HTTP_201_CREATED)
def start_window(
    plan_version: str,
    body: StartWindowIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return service.start_window(
            db,
            plan_version=plan_version,
            window_id=body.window_id or _new_window_id(),
            created_by=body.created_by,
            deadline_overrides=(
                {stage: parse_aware(dt) for stage, dt in body.deadlines.items()}
                if body.deadlines
                else None
            ),
            owners=body.owners,
        )
    except SettlementError as exc:
        _raise(exc)


def _new_window_id() -> str:
    import uuid

    return f"SW-{uuid.uuid4().hex[:12]}"


@router.get("", response_model=list[WindowSummary])
def list_settlement_windows(plan_version: str, db: Session = Depends(get_db)) -> Any:
    from .stages import iso

    return [
        {
            "plan_version": w.plan_version,
            "window_id": w.window_id,
            "state": w.state,
            "current_stage": w.current_stage,
            "revision": w.revision,
            "updated_at": iso(w.updated_at),
        }
        for w in list_windows(db, plan_version)
    ]


@router.get("/{window_id}", response_model=WindowStatusOut)
def get_window(
    plan_version: str, window_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return service.window_status(
            db, plan_version=plan_version, window_id=window_id
        )
    except SettlementError as exc:
        _raise(exc)


@router.post("/{window_id}/advance", response_model=WindowStatusOut)
def advance_window(
    plan_version: str,
    window_id: str,
    body: AdvanceIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return service.advance_window(
            db,
            plan_version=plan_version,
            window_id=window_id,
            actor_id=body.actor_id,
            actor_role=body.actor_role,
            note=body.note,
        )
    except SettlementError as exc:
        _raise(exc)


@router.post("/{window_id}/pause", response_model=WindowStatusOut)
def pause_window(
    plan_version: str,
    window_id: str,
    body: PauseIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return service.pause_window(
            db,
            plan_version=plan_version,
            window_id=window_id,
            actor_id=body.actor_id,
            actor_role=body.actor_role,
            reason=body.reason,
        )
    except SettlementError as exc:
        _raise(exc)


@router.post("/{window_id}/resume", response_model=WindowStatusOut)
def resume_window(
    plan_version: str,
    window_id: str,
    body: PauseIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return service.resume_window(
            db,
            plan_version=plan_version,
            window_id=window_id,
            actor_id=body.actor_id,
            actor_role=body.actor_role,
            reason=body.reason,
        )
    except SettlementError as exc:
        _raise(exc)


@router.post("/{window_id}/reopen", response_model=WindowStatusOut)
def reopen_window(
    plan_version: str,
    window_id: str,
    body: ReopenIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return service.reopen_window(
            db,
            plan_version=plan_version,
            window_id=window_id,
            actor_id=body.actor_id,
            actor_role=body.actor_role,
            reason=body.reason,
            basis_revision=body.basis_revision,
        )
    except SettlementError as exc:
        _raise(exc)
