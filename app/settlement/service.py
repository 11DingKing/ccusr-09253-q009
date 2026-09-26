"""关账流程的领域服务：状态机、异常清单、版本化重开与后台超时任务。"""

from __future__ import annotations

from datetime import timedelta
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from ..core.snapshot import Snapshot, build_snapshot
from ..models import (
    Freeze as FreezeModel,
    SettlementAudit as AuditModel,
)
from ..models import (
    SettlementStage as StageModel,
)
from ..models import (
    SettlementTaskState as TaskModel,
)
from ..models import (
    SettlementWindow as WindowModel,
)
from ..repository import (
    get_freeze,
    get_plan,
    load_events,
    max_event_id,
)
from .stages import (
    DEFAULT_STAGE_DURATIONS,
    STAGE_ORDER,
    STAGE_OWNER_ROLES,
    STATUS_FROZEN,
    STATUS_IN_PROGRESS,
    STATUS_PAUSED,
    STATUS_SUPERSEDED,
    Stage,
    TASK_CANCELLED,
    TASK_DEAD_LETTER,
    TASK_DONE,
    TASK_PENDING,
    as_utc,
    next_stage,
    utc_now,
)

TASK_TYPE_STAGE_TIMEOUT = "stage_timeout"

# 重开时要求的批准角色：必须不同于原复核签署责任人角色（dean）。
REOPEN_APPROVER_ROLE = "academic_senate"


class SettlementError(ValueError):
    """关账流程约束被违反。"""


class SettlementNotFoundError(LookupError):
    pass


def _commit(db: Session) -> None:
    """提交窗口变更；并发推进导致版本冲突时转成业务错误（仅一方成功）。"""
    try:
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        raise SettlementError(
            "concurrent modification detected; the window was changed by another "
            "request, please reload and retry"
        ) from exc


# ---------------------------------------------------------------------------
# 异常清单
# ---------------------------------------------------------------------------


def build_anomaly_summary(snapshot: Snapshot) -> dict[str, Any]:
    """从快照提取异常项：导师尚未确认的实习打卡等。

    导师补确认与请假修正是关账窗口要消化的两类尾部事件，这里列出
    仍处于 PENDING 的打卡，供异常复核阶段处理。
    """
    items: list[dict[str, Any]] = []
    for student in snapshot.students:
        for checkin in student["checkins"]:
            if checkin["status"] != "CONFIRMED":
                items.append(
                    {
                        "student_id": student["student_id"],
                        "type": "unconfirmed_mentor_checkin",
                        "event_id": checkin["event_id"],
                        "activity_id": checkin["activity_id"],
                        "pending_seconds": checkin["raw_seconds"],
                    }
                )
    return {
        "generated_at": snapshot.generated_at,
        "total": len(items),
        "items": items,
    }


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def _get_window_row(
    db: Session, window_id: str, revision: int | None = None
) -> WindowModel:
    stmt = (
        select(WindowModel)
        .where(WindowModel.window_id == window_id)
        .order_by(WindowModel.revision.desc())
    )
    if revision is not None:
        stmt = stmt.where(WindowModel.revision == revision)
    row = db.execute(stmt.limit(1)).scalars().first()
    if row is None:
        raise SettlementNotFoundError(
            f"settlement window '{window_id}'"
            + (f" revision {revision}" if revision is not None else "")
            + " does not exist"
        )
    return row


def _load_stages(db: Session, window_id: str, revision: int) -> list[StageModel]:
    stmt = (
        select(StageModel)
        .where(StageModel.window_id == window_id)
        .where(StageModel.revision == revision)
        .order_by(StageModel.ordinal)
    )
    return list(db.execute(stmt).scalars().all())


def _load_audits(db: Session, window_id: str, revision: int) -> list[AuditModel]:
    stmt = (
        select(AuditModel)
        .where(AuditModel.window_id == window_id)
        .where(AuditModel.revision == revision)
        .order_by(AuditModel.seq)
    )
    return list(db.execute(stmt).scalars().all())


def _current_stage(row: WindowModel, stages: list[StageModel]) -> str:
    if row.status == STATUS_FROZEN:
        return Stage.FROZEN.value
    for stage in stages:
        if stage.completed_at_utc is None:
            return stage.stage
    return Stage.FROZEN.value


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(_UTC).isoformat().replace("+00:00", "Z")


_UTC = utc_now().tzinfo


def _stage_to_dict(stage: StageModel, now: datetime) -> dict[str, Any]:
    completed = stage.completed_at_utc is not None
    return {
        "stage": stage.stage,
        "ordinal": stage.ordinal,
        "owner_role": stage.owner_role,
        "owner_id": stage.owner_id or None,
        "deadline_utc": _iso(stage.deadline_utc),
        "entered_at_utc": _iso(stage.entered_at_utc),
        "completed_at_utc": _iso(stage.completed_at_utc),
        "overdue": (not completed and now >= stage.deadline_utc),
        "note": stage.note,
    }


def serialize_window(db: Session, row: WindowModel) -> dict[str, Any]:
    now = utc_now()
    stages = _load_stages(db, row.window_id, row.revision)
    audits = _load_audits(db, row.window_id, row.revision)
    current = _current_stage(row, stages)
    active_stage = next(
        (
            s
            for s in stages
            if s.stage == current and s.completed_at_utc is None
        ),
        None,
    )
    return {
        "window_id": row.window_id,
        "revision": row.revision,
        "plan_version": row.plan_version,
        "status": row.status,
        "timezone": row.iana_timezone,
        "opened_by": row.opened_by,
        "note": row.note,
        "current_stage": current,
        "data_cutoff_event_id": row.data_cutoff_event_id,
        "event_cutoff_id": row.event_cutoff_id,
        "anomaly_summary": row.anomaly_summary,
        "anomalies_resolved": row.anomalies_resolved,
        "grace_deadline_utc": _iso(row.grace_deadline_utc),
        "signed_off_by": row.signed_off_by,
        "freeze_id": row.freeze_id,
        "superseded_by_revision": row.superseded_by_revision,
        "active_stage_overdue": (
            active_stage is not None and now >= active_stage.deadline_utc
        ),
        "paused_at_utc": _iso(row.paused_at_utc),
        "created_at_utc": _iso(row.created_at),
        "updated_at_utc": _iso(row.updated_at),
        "stages": [_stage_to_dict(s, now) for s in stages],
        "audit": [
            {
                "seq": a.seq,
                "action": a.action,
                "actor_id": a.actor_id,
                "actor_role": a.actor_role,
                "from_status": a.from_status,
                "to_status": a.to_status,
                "reason": a.reason,
                "detail": a.detail,
                "occurred_at_utc": _iso(a.occurred_at_utc),
            }
            for a in audits
        ],
    }


def get_window(
    db: Session, window_id: str, revision: int | None = None
) -> dict[str, Any]:
    return serialize_window(db, _get_window_row(db, window_id, revision))


def list_windows(db: Session, plan_version: str | None = None) -> list[dict[str, Any]]:
    stmt = select(WindowModel).order_by(
        WindowModel.window_id, WindowModel.revision.desc()
    )
    if plan_version is not None:
        stmt = stmt.where(WindowModel.plan_version == plan_version)
    rows = list(db.execute(stmt).scalars().all())
    latest: dict[str, WindowModel] = {}
    for row in rows:  # 已按 revision 降序，首个即最新。
        latest.setdefault(row.window_id, row)
    return [serialize_window(db, row) for row in latest.values()]


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


def _next_audit_seq(db: Session, window_id: str, revision: int) -> int:
    stmt = (
        select(AuditModel.seq)
        .where(AuditModel.window_id == window_id)
        .where(AuditModel.revision == revision)
        .order_by(AuditModel.seq.desc())
        .limit(1)
    )
    last = db.execute(stmt).scalar_one_or_none()
    return (last or 0) + 1


def _append_audit(
    db: Session,
    *,
    window_id: str,
    revision: int,
    action: str,
    actor_id: str,
    actor_role: str,
    from_status: str,
    to_status: str,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditModel(
            window_id=window_id,
            revision=revision,
            seq=_next_audit_seq(db, window_id, revision),
            action=action,
            actor_id=actor_id,
            actor_role=actor_role,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
            detail=detail or {},
        )
    )


# ---------------------------------------------------------------------------
# 快照辅助
# ---------------------------------------------------------------------------


def _snapshot_for(
    db: Session, row: WindowModel, *, cutoff_event_id: str | None
) -> Snapshot:
    plan = get_plan(db, row.plan_version)
    assert plan is not None
    events = load_events(db, row.plan_version)
    return build_snapshot(
        events,
        plan_version=row.plan_version,
        timezone_name=row.iana_timezone,
        required_seconds=plan.required_seconds,
        event_cutoff_id=cutoff_event_id,
    )


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


def open_window(
    db: Session,
    *,
    window_id: str,
    plan_version: str,
    actor_id: str,
    actor_role: str = STAGE_OWNER_ROLES[Stage.DATA_CUTOFF],
    note: str = "",
    stage_durations_hours: dict[str, float] | None = None,
    stage_owners: dict[str, str] | None = None,
    stage_deadlines: dict[Stage, datetime] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """启动新的结算关账窗口（revision=1），进入数据截止阶段。"""
    instant = as_utc(now or utc_now())
    plan = get_plan(db, plan_version)
    if plan is None:
        raise SettlementError(f"plan version '{plan_version}' is not registered")

    existing = (
        db.execute(
            select(WindowModel).where(WindowModel.window_id == window_id).limit(1)
        )
        .scalars()
        .first()
    )
    if existing is not None:
        raise SettlementError(
            f"settlement window '{window_id}' already exists "
            f"(latest revision {existing.revision}); reopen to create a new version"
        )

    durations = dict(DEFAULT_STAGE_DURATIONS)
    for key, hours in (stage_durations_hours or {}).items():
        if hours <= 0:
            raise SettlementError("stage duration must be positive")
        durations[Stage(key)] = timedelta(hours=hours)
    owners = {Stage(k): v for k, v in (stage_owners or {}).items()}
    deadlines = (
        {stage: as_utc(dl) for stage, dl in stage_deadlines.items()}
        if stage_deadlines
        else {}
    )
    for stage, deadline in deadlines.items():
        if deadline <= instant:
            raise SettlementError(
                f"deadline for stage '{stage.value}' must be in the future"
            )

    window = WindowModel(
        plan_version=plan_version,
        window_id=window_id,
        revision=1,
        status=STATUS_IN_PROGRESS,
        iana_timezone=plan.iana_timezone,
        opened_by=actor_id,
        note=note,
    )
    db.add(window)

    first_deadline = instant
    for ordinal, stage in enumerate(STAGE_ORDER):
        if stage == Stage.FROZEN:
            deadline = instant  # 终态无待办期限。
            entered_at = None
        else:
            deadline = deadlines.get(stage, instant + durations[stage])
            entered_at = instant if ordinal == 0 else None
            if ordinal == 0:
                first_deadline = deadline
        owner = owners.get(stage) or (actor_id if ordinal == 0 else "")
        db.add(
            StageModel(
                window_id=window_id,
                revision=1,
                stage=stage.value,
                ordinal=ordinal,
                owner_role=STAGE_OWNER_ROLES[stage],
                owner_id=owner,
                entered_at_utc=entered_at,
                deadline_utc=deadline,
            )
        )

    _append_audit(
        db,
        window_id=window_id,
        revision=1,
        action="open",
        actor_id=actor_id,
        actor_role=actor_role,
        from_status="-",
        to_status=STATUS_IN_PROGRESS,
        reason=note or "open settlement window",
        detail={
            "first_stage": Stage.DATA_CUTOFF.value,
            "deadlines_utc": {
                s.value: _iso(deadlines.get(s, instant + durations[s]))
                for s in durations
            },
        },
    )
    _schedule_timeout(db, window_id, 1, Stage.DATA_CUTOFF, first_deadline)
    _commit(db)
    return serialize_window(db, _get_window_row(db, window_id, 1))


# ---------------------------------------------------------------------------
# 推进
# ---------------------------------------------------------------------------


def _require_active(row: WindowModel) -> None:
    if row.status == STATUS_SUPERSEDED:
        raise SettlementError(
            f"revision {row.revision} has been superseded; operate on the new revision"
        )
    if row.status == STATUS_FROZEN:
        raise SettlementError("window is already frozen")
    if row.status == STATUS_PAUSED:
        raise SettlementError("window is paused; resume before advancing")


def _require_stage_owner(
    stage_row: StageModel, actor_id: str, actor_role: str
) -> None:
    if stage_row.owner_role != actor_role:
        raise SettlementError(
            f"stage '{stage_row.stage}' requires role '{stage_row.owner_role}', "
            f"actor has role '{actor_role}'"
        )
    if stage_row.owner_id and stage_row.owner_id != actor_id:
        raise SettlementError(
            f"stage '{stage_row.stage}' is assigned to '{stage_row.owner_id}'"
        )


def advance_window(
    db: Session,
    *,
    window_id: str,
    revision: int | None = None,
    actor_id: str,
    actor_role: str,
    note: str = "",
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """把窗口从当前阶段推进到下一阶段；进入 FROZEN 时生成不可变冻结材料。"""
    instant = as_utc(now or utc_now())
    row = _get_window_row(db, window_id, revision)
    _require_active(row)
    stages = _load_stages(db, row.window_id, row.revision)
    current_row = next(s for s in stages if s.completed_at_utc is None)
    current = Stage(current_row.stage)
    target = next_stage(current)
    if target is None:
        raise SettlementError("window has no further stage")

    _require_stage_owner(current_row, actor_id, actor_role)
    if not note.strip():
        raise SettlementError("advancing a stage requires a note")

    detail: dict[str, Any] = {}
    if current == Stage.DATA_CUTOFF:
        # 数据截止：固化事件边界（event_id 上界），据此生成首份异常清单。
        cutoff = max_event_id(db, row.plan_version)
        row.data_cutoff_event_id = cutoff
        summary = build_anomaly_summary(
            _snapshot_for(db, row, cutoff_event_id=cutoff)
        )
        row.anomaly_summary = summary
        detail["event_cutoff_id"] = cutoff
        detail["anomalies_total"] = summary["total"]
    elif current == Stage.ANOMALY_REVIEW:
        # 异常复核：必须确认异常已处理，或由该阶段责任人强制放行并留痕。
        summary = row.anomaly_summary or {"total": 0, "items": []}
        if summary.get("total", 0) > 0 and not row.anomalies_resolved and not force:
            raise SettlementError(
                f"{summary['total']} anomalies unresolved; resolve them or pass force=true"
            )
        row.anomalies_resolved = True
        detail["anomalies_total"] = summary.get("total", 0)
        detail["forced"] = bool(force and summary.get("total", 0) > 0)
    elif current == Stage.GRACE_PERIOD:
        # 补录宽限结束：把宽限期内补录纳入边界，重新统计仍未消化的异常。
        new_cutoff = max_event_id(db, row.plan_version)
        row.event_cutoff_id = new_cutoff
        summary = build_anomaly_summary(
            _snapshot_for(db, row, cutoff_event_id=new_cutoff)
        )
        row.anomaly_summary = summary
        if summary["total"] > 0:
            # 宽限期补录后仍有异常：签署阶段必须重新确认或强制放行。
            row.anomalies_resolved = False
        detail["data_cutoff_event_id"] = row.data_cutoff_event_id
        detail["event_cutoff_id"] = new_cutoff
        detail["remaining_anomalies"] = summary["total"]
    elif current == Stage.REVIEW_SIGNOFF:
        summary = row.anomaly_summary or {"total": 0, "items": []}
        if summary.get("total", 0) > 0 and not row.anomalies_resolved and not force:
            raise SettlementError(
                f"{summary['total']} anomalies remain after grace; "
                "resolve them or pass force=true to sign off with exceptions"
            )
        row.signed_off_by = actor_id
        detail["signed_off_with_exceptions"] = bool(
            force and summary.get("total", 0) > 0
        )

    current_row.completed_at_utc = instant
    current_row.note = note

    if target == Stage.FROZEN:
        freeze_id = f"{row.window_id}-r{row.revision}"
        _freeze(db, row, freeze_id, instant)
        row.freeze_id = freeze_id
        row.status = STATUS_FROZEN
        frozen_row = next(s for s in stages if s.stage == Stage.FROZEN.value)
        frozen_row.entered_at_utc = instant
        frozen_row.completed_at_utc = instant
        frozen_row.owner_id = actor_id
        detail["freeze_id"] = freeze_id
        _cancel_timeouts(db, row.window_id, row.revision)
    else:
        target_row = next(s for s in stages if s.stage == target.value)
        target_row.entered_at_utc = instant
        if target == Stage.GRACE_PERIOD:
            row.grace_deadline_utc = target_row.deadline_utc
        _schedule_timeout(
            db, row.window_id, row.revision, target, target_row.deadline_utc
        )

    _append_audit(
        db,
        window_id=row.window_id,
        revision=row.revision,
        action="advance",
        actor_id=actor_id,
        actor_role=actor_role,
        from_status=current.value,
        to_status=target.value,
        reason=note,
        detail=detail,
    )
    _commit(db)
    return serialize_window(db, _get_window_row(db, row.window_id, row.revision))


def resolve_anomaly(
    db: Session,
    *,
    window_id: str,
    revision: int | None = None,
    actor_id: str,
    actor_role: str,
    note: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """异常复核责任人确认清单已处理（导师补确认/请假修正已补齐）。"""
    instant = as_utc(now or utc_now())
    row = _get_window_row(db, window_id, revision)
    _require_active(row)
    stages = _load_stages(db, row.window_id, row.revision)
    current_row = next(s for s in stages if s.completed_at_utc is None)
    if Stage(current_row.stage) != Stage.ANOMALY_REVIEW:
        raise SettlementError("anomalies can only be resolved during anomaly_review")
    _require_stage_owner(current_row, actor_id, actor_role)
    if not note.strip():
        raise SettlementError("resolving anomalies requires a note")
    # 数据截止时的原始清单保持留痕；复核时按当前全量事件核对剩余异常数。
    remaining = build_anomaly_summary(
        _snapshot_for(db, row, cutoff_event_id=None)
    )
    if remaining["total"] > 0:
        raise SettlementError(
            f"{remaining['total']} anomalies still open; wait for mentor "
            "confirmations/corrections or advance this stage with force=true"
        )
    row.anomalies_resolved = True
    _append_audit(
        db,
        window_id=row.window_id,
        revision=row.revision,
        action="resolve_anomalies",
        actor_id=actor_id,
        actor_role=actor_role,
        from_status=Stage.ANOMALY_REVIEW.value,
        to_status=Stage.ANOMALY_REVIEW.value,
        reason=note,
        detail={"remaining": remaining["total"]},
    )
    _commit(db)
    return serialize_window(db, _get_window_row(db, row.window_id, row.revision))


def _freeze(db: Session, row: WindowModel, freeze_id: str, instant: datetime) -> Snapshot:
    """在当前事务内生成正式冻结材料；同一 freeze_id 永远不可覆盖。

    不在这里 commit：冻结材料必须与窗口状态、阶段完成和审计在同一事务内
    原子落库，并发推进时由窗口行的乐观锁裁决唯一胜者。
    """
    existing = get_freeze(db, row.plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot)

    snap = _snapshot_for(db, row, cutoff_event_id=row.event_cutoff_id)
    snap.freeze_id = freeze_id
    snap.generated_at = instant.astimezone(_UTC).isoformat().replace("+00:00", "Z")
    stmt = (
        sqlite_insert(FreezeModel)
        .values(
            plan_version=row.plan_version,
            freeze_id=freeze_id,
            snapshot=snap.to_dict(),
            event_cutoff_id=row.event_cutoff_id,
        )
        .on_conflict_do_nothing(index_elements=["plan_version", "freeze_id"])
    )
    db.execute(stmt)
    try:
        db.flush()
    except StaleDataError as exc:
        # 并发推进：窗口行版本已被另一事务推进，本事务落败。
        db.rollback()
        raise SettlementError(
            "concurrent modification detected; the window was changed by another "
            "request, please reload and retry"
        ) from exc
    stored = get_freeze(db, row.plan_version, freeze_id)
    assert stored is not None
    # 并发下若另一事务已先写入（唯一键冲突被忽略），返回既有的不可变快照。
    return Snapshot.from_dict(stored.snapshot)


# ---------------------------------------------------------------------------
# 暂停 / 恢复
# ---------------------------------------------------------------------------


def pause_window(
    db: Session,
    *,
    window_id: str,
    revision: int | None = None,
    actor_id: str,
    actor_role: str,
    note: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    instant = as_utc(now or utc_now())
    row = _get_window_row(db, window_id, revision)
    if row.status != STATUS_IN_PROGRESS:
        raise SettlementError("only an in-progress window can be paused")
    if not note.strip():
        raise SettlementError("pausing requires a note")
    stages = _load_stages(db, row.window_id, row.revision)
    current_row = next(s for s in stages if s.completed_at_utc is None)
    _require_stage_owner(current_row, actor_id, actor_role)
    row.status = STATUS_PAUSED
    row.paused_at_utc = instant
    # 暂停期间计时停止：取消后台任务，恢复时按剩余时间重建。
    _cancel_timeouts(db, row.window_id, row.revision)
    _append_audit(
        db,
        window_id=row.window_id,
        revision=row.revision,
        action="pause",
        actor_id=actor_id,
        actor_role=actor_role,
        from_status=STATUS_IN_PROGRESS,
        to_status=STATUS_PAUSED,
        reason=note,
        detail={"stage": current_row.stage, "paused_at_utc": _iso(instant)},
    )
    _commit(db)
    return serialize_window(db, _get_window_row(db, row.window_id, row.revision))


def resume_window(
    db: Session,
    *,
    window_id: str,
    revision: int | None = None,
    actor_id: str,
    actor_role: str,
    note: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    instant = as_utc(now or utc_now())
    row = _get_window_row(db, window_id, revision)
    if row.status != STATUS_PAUSED:
        raise SettlementError("only a paused window can be resumed")
    stages = _load_stages(db, row.window_id, row.revision)
    current_row = next(s for s in stages if s.completed_at_utc is None)
    _require_stage_owner(current_row, actor_id, actor_role)
    # 把暂停前的剩余时长（原期限 - 暂停时刻）整体顺延。
    paused_at = as_utc(row.paused_at_utc)
    remaining = current_row.deadline_utc - paused_at
    if remaining < timedelta(0):
        remaining = timedelta(0)
    new_deadline = instant + remaining
    current_row.deadline_utc = new_deadline
    row.status = STATUS_IN_PROGRESS
    row.paused_at_utc = None
    _schedule_timeout(
        db, row.window_id, row.revision, Stage(current_row.stage), new_deadline
    )
    _append_audit(
        db,
        window_id=row.window_id,
        revision=row.revision,
        action="resume",
        actor_id=actor_id,
        actor_role=actor_role,
        from_status=STATUS_PAUSED,
        to_status=STATUS_IN_PROGRESS,
        reason=note,
        detail={
            "stage": current_row.stage,
            "new_deadline_utc": _iso(new_deadline),
            "paused_for_seconds": int((instant - paused_at).total_seconds()),
        },
    )
    _commit(db)
    return serialize_window(db, _get_window_row(db, row.window_id, row.revision))


# ---------------------------------------------------------------------------
# 重开（新版本，需不同角色批准，旧材料不可覆盖）
# ---------------------------------------------------------------------------


def reopen_window(
    db: Session,
    *,
    window_id: str,
    actor_id: str,
    actor_role: str,
    approver_id: str,
    approver_role: str,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """把已冻结窗口重开为新 revision。

    - 窗口必须已冻结；
    - 批准角色必须不同于原复核签署角色（dean），且批准人不能是原签署人；
    - 旧 revision 标记 superseded，旧冻结快照保持不可变，新 revision 使用新 freeze_id。
    """
    instant = as_utc(now or utc_now())
    old = _get_window_row(db, window_id)
    if old.status != STATUS_FROZEN:
        raise SettlementError("only a frozen window can be reopened")
    if not reason.strip():
        raise SettlementError("reopening requires a reason")
    if approver_role != REOPEN_APPROVER_ROLE:
        raise SettlementError(
            f"reopening must be approved by role '{REOPEN_APPROVER_ROLE}', "
            f"got '{approver_role}'"
        )
    if approver_role == STAGE_OWNER_ROLES[Stage.REVIEW_SIGNOFF]:
        raise SettlementError("approver role must differ from the sign-off role")
    if old.signed_off_by and approver_id == old.signed_off_by:
        raise SettlementError("approver must be a different person from the signer")
    if actor_id == approver_id:
        raise SettlementError("requester and approver must be different people")

    new_revision = old.revision + 1
    old.status = STATUS_SUPERSEDED
    old.superseded_by_revision = new_revision

    new = WindowModel(
        plan_version=old.plan_version,
        window_id=window_id,
        revision=new_revision,
        status=STATUS_IN_PROGRESS,
        iana_timezone=old.iana_timezone,
        opened_by=actor_id,
        note=f"reopened r{new_revision}: {reason}",
    )
    db.add(new)

    old_stages = _load_stages(db, window_id, old.revision)
    first_deadline = instant
    for stage_enum in STAGE_ORDER:
        old_stage = next(s for s in old_stages if s.stage == stage_enum.value)
        if stage_enum == Stage.FROZEN:
            deadline = instant
            entered_at = None
            duration = None
        elif old_stage.entered_at_utc is not None:
            # 保留旧版本实际使用的时长配置。
            duration = old_stage.deadline_utc - as_utc(old_stage.entered_at_utc)
            deadline = instant + duration
            entered_at = instant if stage_enum == Stage.DATA_CUTOFF else None
        else:
            duration = DEFAULT_STAGE_DURATIONS[stage_enum]
            deadline = instant + duration
            entered_at = instant if stage_enum == Stage.DATA_CUTOFF else None
        if stage_enum == Stage.DATA_CUTOFF:
            first_deadline = deadline
        db.add(
            StageModel(
                window_id=window_id,
                revision=new_revision,
                stage=stage_enum.value,
                ordinal=old_stage.ordinal,
                owner_role=old_stage.owner_role,
                owner_id=actor_id if stage_enum == Stage.DATA_CUTOFF else "",
                entered_at_utc=entered_at,
                deadline_utc=deadline,
            )
        )

    _append_audit(
        db,
        window_id=window_id,
        revision=old.revision,
        action="reopen",
        actor_id=actor_id,
        actor_role=actor_role,
        from_status=STATUS_FROZEN,
        to_status=STATUS_SUPERSEDED,
        reason=reason,
        detail={
            "new_revision": new_revision,
            "approver_id": approver_id,
            "approver_role": approver_role,
            "old_freeze_id": old.freeze_id,
        },
    )
    _append_audit(
        db,
        window_id=window_id,
        revision=new_revision,
        action="open",
        actor_id=actor_id,
        actor_role=actor_role,
        from_status="-",
        to_status=STATUS_IN_PROGRESS,
        reason=f"revision {new_revision} opened via approved reopen",
        detail={
            "based_on_revision": old.revision,
            "approver_id": approver_id,
            "approver_role": approver_role,
        },
    )
    _schedule_timeout(db, window_id, new_revision, Stage.DATA_CUTOFF, first_deadline)
    _commit(db)
    return serialize_window(db, _get_window_row(db, window_id, new_revision))


# ---------------------------------------------------------------------------
# 持久化后台超时任务（崩溃 / 重启后继续，失败补偿）
# ---------------------------------------------------------------------------


def _schedule_timeout(
    db: Session,
    window_id: str,
    revision: int,
    stage: Stage,
    run_at: datetime,
) -> None:
    """创建或重置某版本窗口的阶段超时任务（每个 revision 一行，跨阶段复用）。"""
    run_at = as_utc(run_at)
    stmt = sqlite_insert(TaskModel).values(
        task_type=TASK_TYPE_STAGE_TIMEOUT,
        window_id=window_id,
        revision=revision,
        run_at_utc=run_at,
        status=TASK_PENDING,
        attempts=0,
        last_error=None,
        locked_by=None,
        locked_until_utc=None,
        completed_at_utc=None,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["task_type", "window_id", "revision"],
        set_={
            "run_at_utc": run_at,
            "status": TASK_PENDING,
            "attempts": 0,
            "last_error": None,
            "locked_by": None,
            "locked_until_utc": None,
            "completed_at_utc": None,
        },
    )
    db.execute(stmt)


def _cancel_timeouts(db: Session, window_id: str, revision: int) -> None:
    tasks = list(
        db.execute(
            select(TaskModel)
            .where(TaskModel.window_id == window_id)
            .where(TaskModel.revision == revision)
            .where(TaskModel.status == TASK_PENDING)
        ).scalars()
    )
    for task in tasks:
        task.status = TASK_CANCELLED
        task.completed_at_utc = utc_now()


def claim_due_tasks(
    db: Session,
    *,
    worker_id: str,
    now: datetime | None = None,
    lease_seconds: int = 60,
    limit: int = 16,
) -> list[TaskModel]:
    """原子认领到期任务。

    - 到期且无锁（或租约已过期——例如持锁进程崩溃/重启）的任务才能被认领；
    - 用条件 UPDATE 保证多 worker / 多线程并发下每个任务只被一方拿到。
    """
    instant = as_utc(now or utc_now())
    lease_until = instant + timedelta(seconds=lease_seconds)
    candidates = list(
        db.execute(
            select(TaskModel.id)
            .where(TaskModel.status == TASK_PENDING)
            .where(TaskModel.run_at_utc <= instant)
            .order_by(TaskModel.run_at_utc)
            .limit(limit)
        ).scalars()
    )
    claimed: list[TaskModel] = []
    for task_id in candidates:
        stmt = (
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .where(TaskModel.status == TASK_PENDING)
            .where(
                or_(
                    TaskModel.locked_until_utc.is_(None),
                    TaskModel.locked_until_utc < instant,
                )
            )
            .values(locked_by=worker_id, locked_until_utc=lease_until)
        )
        result = db.execute(stmt)
        if result.rowcount == 1:
            task = db.get(TaskModel, task_id)
            assert task is not None
            claimed.append(task)
    db.commit()
    return claimed


def _reschedule_failed(task: TaskModel, instant: datetime) -> None:
    """失败补偿：指数退避后重试；超过上限进入死信。"""
    task.attempts += 1
    if task.attempts >= task.max_attempts:
        task.status = TASK_DEAD_LETTER
        task.completed_at_utc = instant
    else:
        backoff = timedelta(seconds=min(300, 2 ** task.attempts))
        task.run_at_utc = instant + backoff
    task.locked_by = None
    task.locked_until_utc = None


def run_due_timeouts(
    db: Session,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> dict[str, int]:
    """处理所有到期任务：对逾期阶段登记审计（幂等，不自动跳阶段）。

    重启后再次调用即可拾取持久化的到期任务（含锁过期的陈旧任务）。
    返回 {processed, failed, dead_lettered} 计数。
    """
    instant = as_utc(now or utc_now())
    processed = failed = dead_lettered = 0
    for task in claim_due_tasks(db, worker_id=worker_id, now=instant):
        task_id = task.id
        try:
            row = (
                db.execute(
                    select(WindowModel)
                    .where(WindowModel.window_id == task.window_id)
                    .where(WindowModel.revision == task.revision)
                )
                .scalars()
                .first()
            )
            if row is None:
                raise SettlementError("window vanished")
            stages = _load_stages(db, task.window_id, task.revision)
            active = next((s for s in stages if s.completed_at_utc is None), None)
            should_fire = (
                row.status == STATUS_IN_PROGRESS
                and active is not None
                and active.stage != Stage.FROZEN.value
                and instant >= active.deadline_utc
            )
            if should_fire:
                _append_audit(
                    db,
                    window_id=task.window_id,
                    revision=task.revision,
                    action="stage_timeout",
                    actor_id="system",
                    actor_role="scheduler",
                    from_status=active.stage,
                    to_status=active.stage,
                    reason=f"stage '{active.stage}' passed its deadline",
                    detail={
                        "deadline_utc": _iso(active.deadline_utc),
                        "attempt": task.attempts + 1,
                    },
                )
                processed += 1
            task.status = TASK_DONE
            task.completed_at_utc = instant
            task.locked_by = None
            task.locked_until_utc = None
            task.last_error = None
            db.commit()
        except Exception as exc:  # 失败补偿：退避重试或死信。
            db.rollback()
            failed += 1
            stale = db.get(TaskModel, task_id)
            if stale is not None:
                stale.last_error = str(exc)[:1024]
                _reschedule_failed(stale, instant)
                if stale.status == TASK_DEAD_LETTER:
                    dead_lettered += 1
                db.commit()
    return {"processed": processed, "failed": failed, "dead_lettered": dead_lettered}
