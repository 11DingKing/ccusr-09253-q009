"""结算窗口领域服务：启动、推进、暂停、恢复、重开与状态查询。

所有状态变更遵循同一套约束：

* 五个阶段严格顺序推进，每步记录责任人、期限与审计轨迹；
* 窗口行带 ``row_version`` 乐观锁，并发推进只有一方成功，其余得到 409；
* 超时任务持久化在 ``settlement_tasks``，由后台调度器领取执行，
  崩溃/重启后通过过期租约回收继续，失败按指数退避补偿重试；
* 重开只能由不同于复核签署人与冻结教务员的角色批准，并产生新版本，
  每个版本冻结出独立 freeze_id 的快照，旧材料永不覆盖。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.snapshot import Snapshot, build_snapshot
from ..models import Event, Freeze
from ..repository import get_plan, load_events, max_event_id
from . import repository as repo
from .clock import now
from .errors import (
    InvalidStageError,
    ResponsibilityError,
    RevisionConflictError,
    WindowAlreadyExistsError,
    WindowNotFoundError,
    WindowStateError,
)
from .stages import (
    DEFAULT_OWNER_ROLES,
    REOPEN_FORBIDDEN_ROLES,
    STAGES,
    STATE_ACTIVE,
    STATE_COMPLETED,
    STATE_PAUSED,
    STAGE_LABELS,
    StageDeadlineInfo,
    iso,
    next_stage_of,
    parse_aware,
)

# 未显式给定期限时，各阶段的默认时长（frozen 为终态，无期限）。
DEFAULT_STAGE_DURATION: dict[str, timedelta] = {
    "data_cutoff": timedelta(days=1),
    "exception_list": timedelta(days=2),
    "grace_entry": timedelta(days=3),
    "review_signoff": timedelta(days=2),
    "frozen": timedelta(0),
}

FREEZE_ID_SUFFIX = "-r"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _require_window(db: Session, plan_version: str, window_id: str):
    window = repo.get_window(db, plan_version, window_id)
    if window is None:
        raise WindowNotFoundError(
            f"settlement window '{window_id}' for plan '{plan_version}' does not exist"
        )
    return window


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise WindowNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def _records(window) -> dict[str, Any]:
    records = dict(window.stage_records or {})
    records.setdefault("audit", [])
    records.setdefault("materials", {})
    return records


def _audit(
    records: dict[str, Any],
    *,
    at: datetime,
    action: str,
    actor_id: str,
    actor_role: str,
    reason: str,
    revision: int,
    from_stage: str | None = None,
    to_stage: str | None = None,
    forced: bool = False,
) -> None:
    records["audit"].append(
        {
            "seq": len(records["audit"]) + 1,
            "at": iso(at),
            "action": action,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "from_stage": from_stage,
            "to_stage": to_stage,
            "revision": revision,
            "reason": reason,
            "forced": forced,
        }
    )


def _stage_deadline(
    stage: str, entered_at: datetime, overrides: dict[str, datetime]
) -> datetime | None:
    if stage == "frozen":
        return None
    if stage in overrides:
        return overrides[stage]
    return entered_at + DEFAULT_STAGE_DURATION[stage]


def _freeze_id_for(window_id: str, revision: int) -> str:
    return f"{window_id}{FREEZE_ID_SUFFIX}{revision}"


def _owner_of(window, stage: str) -> dict[str, str | None]:
    for item in window.stages:
        if item["stage"] == stage:
            return {"owner_role": item["owner_role"], "owner_id": item.get("owner_id")}
    raise InvalidStageError(f"unknown stage '{stage}'")


def _snapshot_at(
    db: Session,
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    cutoff_event_id: str | None,
    freeze_id: str | None = None,
) -> Snapshot:
    events = load_events(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=timezone_name,
        required_seconds=required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff_event_id,
    )


def _exception_material(snapshot: Snapshot) -> dict[str, Any]:
    pending: list[dict[str, Any]] = []
    negative: list[dict[str, Any]] = []
    deficient: list[dict[str, Any]] = []
    for student in snapshot.students:
        for checkin in student["checkins"]:
            if checkin["status"] == "PENDING":
                pending.append(
                    {
                        "student_id": student["student_id"],
                        "event_id": checkin["event_id"],
                        "activity_type": checkin["activity_type"],
                        "pending_seconds": checkin["raw_seconds"],
                    }
                )
        for adjustment in student["adjustments"]:
            if adjustment["seconds"] < 0:
                negative.append(
                    {
                        "student_id": student["student_id"],
                        "event_id": adjustment["event_id"],
                        "seconds": adjustment["seconds"],
                        "reason": adjustment["reason"],
                    }
                )
        if not student["meets_requirement"]:
            deficient.append(
                {
                    "student_id": student["student_id"],
                    "total_seconds": student["total_seconds"],
                    "required_seconds": snapshot.required_seconds,
                    "shortfall_seconds": snapshot.required_seconds
                    - student["total_seconds"],
                }
            )
    return {
        "generated_at": snapshot.generated_at,
        "pending_confirmations": pending,
        "negative_adjustments": negative,
        "deficient_students": deficient,
        "counts": {
            "pending_confirmations": len(pending),
            "negative_adjustments": len(negative),
            "deficient_students": len(deficient),
        },
    }


def _events_between(
    db: Session,
    plan_version: str,
    lower: str | None,
    upper: str | None,
) -> list[str]:
    if upper is None:
        return []
    stmt = select(Event.event_id).where(
        Event.plan_version == plan_version, Event.event_id <= upper
    )
    if lower is not None:
        stmt = stmt.where(Event.event_id > lower)
    return sorted(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# 持久化超时任务
# ---------------------------------------------------------------------------


def _timeout_task_key(
    plan_version: str, window_id: str, revision: int, stage: str
) -> str:
    return f"timeout:{plan_version}:{window_id}:r{revision}:{stage}"


def _schedule_stage_timeout(
    db: Session,
    *,
    plan_version: str,
    window_id: str,
    revision: int,
    stage: str,
    run_at: datetime,
) -> str:
    """安排阶段超时任务；必须在窗口行乐观锁抢占成功后调用。

    返回确定性的 task_key（阶段记录只存 key，数字 id 在查询时解析）。
    """
    key = _timeout_task_key(plan_version, window_id, revision, stage)
    task = repo.insert_task(
        db,
        {
            "task_key": key,
            "plan_version": plan_version,
            "window_id": window_id,
            "revision": revision,
            "stage": stage,
            "task_type": "stage_timeout",
            "run_at": run_at,
            "status": "pending",
        },
    )
    assert task is not None
    return key


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


def start_window(
    db: Session,
    *,
    plan_version: str,
    window_id: str,
    created_by: str,
    deadline_overrides: dict[str, datetime] | None = None,
    owners: dict[str, str] | None = None,
) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    instant = now()
    overrides = deadline_overrides or {}
    owners = owners or {}
    for stage in overrides:
        if stage not in STAGES:
            raise InvalidStageError(f"未知阶段 {stage!r}，无法设定期限")

    first_stage = STAGES[0]
    deadline = _stage_deadline(first_stage, instant, overrides)
    assert deadline is not None
    cutoff_event_id = max_event_id(db, plan_version)
    local_deadline = deadline.astimezone(ZoneInfo(plan.iana_timezone))
    task_key = _timeout_task_key(plan_version, window_id, 1, first_stage)

    stages_config = [
        {
            "stage": stage,
            "owner_role": DEFAULT_OWNER_ROLES[stage],
            "owner_id": owners.get(stage),
        }
        for stage in STAGES
    ]
    stage_records: dict[str, Any] = {
        "audit": [],
        "materials": {
            "data_cutoff": {
                "event_cutoff_id": cutoff_event_id,
                "cutoff_boundary_at": iso(instant),
                "deadline_at": iso(deadline),
                "deadline_at_local": local_deadline.isoformat(),
                "timezone": plan.iana_timezone,
            }
        },
        first_stage: {
            "entered_at": iso(instant),
            "deadline_at": iso(deadline),
            "deadline_at_local": local_deadline.isoformat(),
            "owner_role": DEFAULT_OWNER_ROLES[first_stage],
            "owner_id": owners.get(first_stage),
            "task_key": task_key,
            "timed_out": False,
            "completed_at": None,
        },
    }
    _audit(
        stage_records,
        at=instant,
        action="start",
        actor_id=created_by,
        actor_role="registrar",
        reason="开启结算关账窗口",
        revision=1,
        to_stage=first_stage,
    )

    window = repo.insert_window(
        db,
        values={
            "plan_version": plan_version,
            "window_id": window_id,
            "state": STATE_ACTIVE,
            "current_stage": first_stage,
            "revision": 1,
            "cutoff_event_id": cutoff_event_id,
            "cut_off_at": instant,
            "stages": stages_config,
            "stage_records": stage_records,
            "created_by": created_by,
            "updated_at": instant,
            "row_version": 1,
        },
    )
    if window is None:
        db.rollback()
        raise WindowAlreadyExistsError(
            f"settlement window '{window_id}' for plan '{plan_version}' already exists"
        )
    # 窗口抢占成功后才落超时任务，避免并发起动在唯一键上互相撞车。
    _schedule_stage_timeout(
        db,
        plan_version=plan_version,
        window_id=window_id,
        revision=1,
        stage=first_stage,
        run_at=deadline,
    )
    db.commit()
    return window_status(db, plan_version=plan_version, window_id=window_id)


# ---------------------------------------------------------------------------
# 推进
# ---------------------------------------------------------------------------


def advance_window(
    db: Session,
    *,
    plan_version: str,
    window_id: str,
    actor_id: str,
    actor_role: str,
    note: str = "",
    forced: bool = False,
) -> dict[str, Any]:
    window = _require_window(db, plan_version, window_id)
    instant = now()
    current = window.current_stage

    if window.state == STATE_COMPLETED:
        raise WindowStateError("窗口已正式冻结，推进前必须由其他角色批准重开")
    if window.state == STATE_PAUSED and not forced:
        raise WindowStateError("窗口已暂停，恢复后才能继续推进")
    if current == "frozen":
        raise InvalidStageError("冻结是最终阶段，不能继续推进")

    nxt = next_stage_of(current)
    assert nxt is not None
    owner = _owner_of(window, current)
    # 最后一步同时承载“复核签署”（dean_reviewer）与“正式冻结”（registrar）：
    # 签署人确认后可直接触发封存，教务员也可代为执行正式冻结。
    allowed_roles = {owner["owner_role"]}
    if nxt == "frozen":
        allowed_roles.add(DEFAULT_OWNER_ROLES["frozen"])
    if not forced and actor_role not in allowed_roles:
        raise ResponsibilityError(
            f"阶段 {current} 必须由 {owner['owner_role']} 推进"
            + (
                "（最终冻结也可由 registrar 执行）"
                if nxt == "frozen"
                else ""
            )
            + f"，当前操作角色为 {actor_role}"
        )

    expected_version = window.row_version
    records = _records(window)

    # 离开当前阶段
    stage_entry = dict(records.get(current, {}))
    stage_entry["completed_at"] = iso(instant)
    stage_entry["completed_by"] = actor_id
    stage_entry["completed_by_role"] = actor_role
    if forced:
        stage_entry["timed_out"] = True
    records[current] = stage_entry

    # 进入新阶段时产出该阶段需要的材料
    cutoff_event_id = window.cutoff_event_id
    plan = _require_plan(db, plan_version)
    if nxt == "exception_list":
        snapshot = _snapshot_at(
            db,
            plan_version=plan_version,
            timezone_name=plan.iana_timezone,
            required_seconds=plan.required_seconds,
            cutoff_event_id=cutoff_event_id,
        )
        records["materials"]["exception_list"] = _exception_material(snapshot)
    elif nxt == "grace_entry":
        records["materials"]["grace_entry"] = {
            "opened_at": iso(instant),
            "cutoff_at_entry": cutoff_event_id,
        }
    elif nxt == "review_signoff":
        new_cutoff = max_event_id(db, plan_version)
        records["materials"]["grace_entry"].update(
            {
                "closed_at": iso(instant),
                "cutoff_before": cutoff_event_id,
                "cutoff_after": new_cutoff,
                "events_added": _events_between(
                    db, plan_version, cutoff_event_id, new_cutoff
                ),
            }
        )
        cutoff_event_id = new_cutoff
    elif nxt == "frozen":
        records["materials"]["review_signoff"] = {
            "signed_at": iso(instant),
            "actor_id": actor_id,
            "actor_role": actor_role,
            "note": note,
            "forced": forced,
        }

    # 正式冻结所需的快照与材料在抢锁前算好（重计算幂等），
    # 随后与窗口行的唯一一次条件更新一并落库，避免二次更新错锁。
    freeze_snapshot: Snapshot | None = None
    if nxt == "frozen":
        freeze_snapshot = _snapshot_at(
            db,
            plan_version=plan_version,
            timezone_name=plan.iana_timezone,
            required_seconds=plan.required_seconds,
            cutoff_event_id=cutoff_event_id,
            freeze_id=_freeze_id_for(window_id, window.revision),
        )
        records["materials"]["frozen"] = {
            "freeze_id": _freeze_id_for(window_id, window.revision),
            "event_cutoff_id": cutoff_event_id,
            "snapshot_generated_at": freeze_snapshot.generated_at,
            "frozen_by": actor_id,
            "frozen_by_role": actor_role,
            "forced": forced,
            "student_count": len(freeze_snapshot.students),
        }

    # 新阶段的进入信息（frozen 无期限、无任务）。
    deadline = _stage_deadline(nxt, instant, {})
    next_owner = _owner_of(window, nxt)

    # 先作废旧阶段（含本版本其它在途）超时任务，再排新阶段任务。
    repo.cancel_tasks(
        db,
        plan_version=plan_version,
        window_id=window_id,
        revision=window.revision,
    )
    next_task_key: str | None = None
    if deadline is not None:
        next_task_key = _timeout_task_key(
            plan_version, window_id, window.revision, nxt
        )
    records[nxt] = {
        "entered_at": iso(instant),
        "deadline_at": iso(deadline) if deadline else None,
        "owner_role": next_owner["owner_role"],
        "owner_id": next_owner.get("owner_id"),
        "task_key": next_task_key,
        "timed_out": False,
        "completed_at": None,
    }
    _audit(
        records,
        at=instant,
        action="advance",
        actor_id=actor_id,
        actor_role=actor_role,
        reason=note or ("阶段期限到达，系统强制推进" if forced else "人工推进"),
        revision=window.revision,
        from_stage=current,
        to_stage=nxt,
        forced=forced,
    )

    new_state = STATE_COMPLETED if nxt == "frozen" else STATE_ACTIVE

    # 单一串行化点：乐观锁条件更新，并发推进只有一方成功。
    ok = repo.conditional_update_window(
        db,
        plan_version=plan_version,
        window_id=window_id,
        expected_row_version=expected_version,
        values={
            "current_stage": nxt,
            "state": new_state,
            "stage_records": records,
            "cutoff_event_id": cutoff_event_id,
            "updated_at": instant,
        },
    )
    if not ok:
        db.rollback()
        raise RevisionConflictError("并发推进冲突，只有一次推进能够生效")

    # 抢锁成功后才为新阶段落超时任务行（唯一键冲突意味着并发，安全失败）。
    if deadline is not None:
        _schedule_stage_timeout(
            db,
            plan_version=plan_version,
            window_id=window_id,
            revision=window.revision,
            stage=nxt,
            run_at=deadline,
        )

    if nxt == "frozen":
        _seal_revision(
            db,
            window=window,
            records=records,
            snapshot=freeze_snapshot,
            cutoff_event_id=cutoff_event_id,
            at=instant,
        )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise RevisionConflictError("并发推进冲突，只有一次推进能够生效")
    return window_status(db, plan_version=plan_version, window_id=window_id)


def _seal_revision(
    db: Session,
    *,
    window,
    records: dict[str, Any],
    snapshot: Snapshot,
    cutoff_event_id: str | None,
    at: datetime,
) -> None:
    """插入冻结快照与版本封存行（只新增，不更新窗口行、不覆盖旧材料）。

    冻结材料已随窗口行的唯一一次条件更新落库，这里只负责旁路透存：
    Freeze 快照行与 SettlementRevision 版本行。
    """
    revision = window.revision
    freeze_id = _freeze_id_for(window.window_id, revision)
    db.add(
        Freeze(
            plan_version=window.plan_version,
            freeze_id=freeze_id,
            snapshot=snapshot.to_dict(),
            event_cutoff_id=cutoff_event_id,
        )
    )
    materials = dict(records["materials"])
    existing = repo.get_revision(
        db, window.plan_version, window.window_id, revision
    )
    if existing is None:
        # 首版：此前没有版本行，直接封存。
        repo.insert_revision(
            db,
            values={
                "plan_version": window.plan_version,
                "window_id": window.window_id,
                "revision": revision,
                "basis_revision": revision - 1 or None,
                "status": "sealed",
                "cutoff_event_id": cutoff_event_id,
                "freeze_id": freeze_id,
                "materials": materials,
                "sealed_at": at,
            },
        )
    else:
        # 重开产生的 open 版本行就地封存——是同一版本的状态推进，不是覆盖旧版本。
        repo.seal_revision_row(
            db,
            existing,
            cutoff_event_id=cutoff_event_id,
            freeze_id=freeze_id,
            materials=materials,
            sealed_at=at,
        )


# ---------------------------------------------------------------------------
# 暂停 / 恢复
# ---------------------------------------------------------------------------


def pause_window(
    db: Session,
    *,
    plan_version: str,
    window_id: str,
    actor_id: str,
    actor_role: str,
    reason: str,
) -> dict[str, Any]:
    window = _require_window(db, plan_version, window_id)
    if window.state != STATE_ACTIVE:
        raise WindowStateError(f"只有进行中的窗口可以暂停，当前为 {window.state}")
    if actor_role not in {"registrar", "admin"}:
        raise ResponsibilityError("只有教务处（registrar/admin）可以暂停窗口")
    instant = now()
    records = _records(window)
    _audit(
        records,
        at=instant,
        action="pause",
        actor_id=actor_id,
        actor_role=actor_role,
        reason=reason,
        revision=window.revision,
    )
    ok = repo.conditional_update_window(
        db,
        plan_version=plan_version,
        window_id=window_id,
        expected_row_version=window.row_version,
        values={"state": STATE_PAUSED, "stage_records": records, "updated_at": instant},
    )
    if not ok:
        db.rollback()
        raise RevisionConflictError("并发状态变更冲突，请重试")
    repo.cancel_tasks(
        db,
        plan_version=plan_version,
        window_id=window_id,
        revision=window.revision,
    )
    db.commit()
    return window_status(db, plan_version=plan_version, window_id=window_id)


def resume_window(
    db: Session,
    *,
    plan_version: str,
    window_id: str,
    actor_id: str,
    actor_role: str,
    reason: str,
) -> dict[str, Any]:
    window = _require_window(db, plan_version, window_id)
    if window.state != STATE_PAUSED:
        raise WindowStateError(f"只有暂停的窗口可以恢复，当前为 {window.state}")
    if actor_role not in {"registrar", "admin"}:
        raise ResponsibilityError("只有教务处（registrar/admin）可以恢复窗口")
    instant = now()
    records = _records(window)
    current = window.current_stage
    stage_entry = dict(records.get(current, {}))
    deadline_str = stage_entry.get("deadline_at")
    task_key: str | None = stage_entry.get("task_key")
    run_at: datetime | None = None
    if deadline_str and current != "frozen":
        # 期限已过则立即到期，交给调度器强制推进；否则沿用原期限。
        run_at = parse_aware(deadline_str)
        if run_at < instant:
            run_at = instant
        if task_key is None:
            task_key = _timeout_task_key(
                plan_version, window_id, window.revision, current
            )
        stage_entry["task_key"] = task_key
        records[current] = stage_entry
    _audit(
        records,
        at=instant,
        action="resume",
        actor_id=actor_id,
        actor_role=actor_role,
        reason=reason,
        revision=window.revision,
    )
    ok = repo.conditional_update_window(
        db,
        plan_version=plan_version,
        window_id=window_id,
        expected_row_version=window.row_version,
        values={"state": STATE_ACTIVE, "stage_records": records, "updated_at": instant},
    )
    if not ok:
        db.rollback()
        raise RevisionConflictError("并发状态变更冲突，请重试")
    if run_at is not None and task_key is not None:
        # 抢锁成功后重新启用该阶段的超时任务（同一 key，不新增行）。
        _reactivate_timeout_task(
            db,
            task_key=task_key,
            run_at=run_at,
            plan_version=plan_version,
            window_id=window_id,
            revision=window.revision,
            stage=current,
        )
    db.commit()
    return window_status(db, plan_version=plan_version, window_id=window_id)


def _reactivate_timeout_task(
    db: Session,
    *,
    task_key: str,
    run_at: datetime,
    plan_version: str,
    window_id: str,
    revision: int,
    stage: str,
) -> None:
    task = repo.get_task_by_key(db, task_key)
    if task is None:
        # 极端情况下任务行缺失：按当前版本参数补排。
        _schedule_stage_timeout(
            db,
            plan_version=plan_version,
            window_id=window_id,
            revision=revision,
            stage=stage,
            run_at=run_at,
        )
    else:
        repo.reset_task(db, task, run_at=run_at)


# ---------------------------------------------------------------------------
# 重开（新版本）
# ---------------------------------------------------------------------------


def reopen_window(
    db: Session,
    *,
    plan_version: str,
    window_id: str,
    actor_id: str,
    actor_role: str,
    reason: str,
    basis_revision: int | None = None,
) -> dict[str, Any]:
    window = _require_window(db, plan_version, window_id)
    if window.state != STATE_COMPLETED or window.current_stage != "frozen":
        raise WindowStateError("只有已正式冻结的窗口才能重开")
    if actor_role in REOPEN_FORBIDDEN_ROLES:
        raise ResponsibilityError(
            "重开必须由不同于复核签署人（dean_reviewer）与冻结教务员（registrar）"
            f"的角色批准，当前角色 {actor_role} 不允许"
        )

    prior = repo.list_revisions(db, plan_version, window_id)
    sealed = [r for r in prior if r.status == "sealed"]
    if not sealed:
        raise WindowStateError("没有可作为重开基础的已封存版本")
    basis = basis_revision or max(r.revision for r in sealed)
    basis_row = next((r for r in sealed if r.revision == basis), None)
    if basis_row is None:
        raise InvalidStageError(f"版本 {basis} 不存在或未封存，不能作为重开基础")

    # 批准人不能就是该版本的签署人或冻结执行人本人。
    frozen_material = basis_row.materials.get("frozen", {})
    signoff_material = basis_row.materials.get("review_signoff", {})
    forbidden_persons = {
        frozen_material.get("frozen_by"),
        signoff_material.get("actor_id"),
    }
    forbidden_persons.discard(None)
    if actor_id in forbidden_persons:
        raise ResponsibilityError("重开批准人不能是原签署人或原冻结执行人本人")

    instant = now()
    new_revision = window.revision + 1
    first_stage = STAGES[0]
    deadline = _stage_deadline(first_stage, instant, {})
    assert deadline is not None

    # 重开后再次数据截止：把冻结后补交的导师确认/请假修正纳入新版本边界。
    new_cutoff = max_event_id(db, plan_version)
    new_task_key = _timeout_task_key(
        plan_version, window_id, new_revision, first_stage
    )

    # 旧版本阶段条目与材料保留在审计与封存行中；工作区重建。
    records = {"audit": list((window.stage_records or {}).get("audit", []))}
    records[first_stage] = {
        "entered_at": iso(instant),
        "deadline_at": iso(deadline),
        "owner_role": DEFAULT_OWNER_ROLES[first_stage],
        "owner_id": _owner_of(window, first_stage).get("owner_id"),
        "task_key": new_task_key,
        "timed_out": False,
        "completed_at": None,
    }
    records["materials"] = {
        "data_cutoff": {
            "event_cutoff_id": new_cutoff,
            "reopened_from_revision": basis,
            "reopened_at": iso(instant),
            "deadline_at": iso(deadline),
        }
    }
    _audit(
        records,
        at=instant,
        action="reopen",
        actor_id=actor_id,
        actor_role=actor_role,
        reason=reason,
        revision=new_revision,
        from_stage="frozen",
        to_stage=first_stage,
    )

    ok = repo.conditional_update_window(
        db,
        plan_version=plan_version,
        window_id=window_id,
        expected_row_version=window.row_version,
        values={
            "state": STATE_ACTIVE,
            "current_stage": first_stage,
            "revision": new_revision,
            "cutoff_event_id": new_cutoff,
            "cut_off_at": instant,
            "stage_records": records,
            "updated_at": instant,
        },
    )
    if not ok:
        db.rollback()
        raise RevisionConflictError("并发重开冲突，请重试")

    # 抢锁成功后作废旧版本在途任务并为新版本排期（唯一键冲突=并发，安全失败）。
    repo.cancel_tasks(db, plan_version=plan_version, window_id=window_id)
    _schedule_stage_timeout(
        db,
        plan_version=plan_version,
        window_id=window_id,
        revision=new_revision,
        stage=first_stage,
        run_at=deadline,
    )

    # 新版本的 open 材料行与旧 sealed 行并存，绝不覆盖。
    repo.insert_revision(
        db,
        values={
            "plan_version": plan_version,
            "window_id": window_id,
            "revision": new_revision,
            "basis_revision": basis,
            "status": "open",
            "cutoff_event_id": new_cutoff,
            "freeze_id": None,
            "materials": {"reopened_from_revision": basis, "reason": reason},
            "reopened_reason": reason,
        },
    )
    db.commit()
    return window_status(db, plan_version=plan_version, window_id=window_id)


# ---------------------------------------------------------------------------
# 状态查询
# ---------------------------------------------------------------------------


def _stage_infos(window, tasks_by_key: dict[str, Any]) -> list[StageDeadlineInfo]:
    records = window.stage_records or {}
    result: list[StageDeadlineInfo] = []
    for item in window.stages:
        stage = item["stage"]
        entry = records.get(stage, {})
        task_key = entry.get("task_key")
        task = tasks_by_key.get(task_key) if task_key else None
        result.append(
            StageDeadlineInfo(
                stage=stage,
                label=STAGE_LABELS[stage],
                owner_role=item["owner_role"],
                owner_id=item.get("owner_id"),
                deadline_at=entry.get("deadline_at"),
                entered_at=entry.get("entered_at"),
                completed_at=entry.get("completed_at"),
                completed_by=entry.get("completed_by"),
                task_id=task.id if task is not None else None,
                timed_out=bool(entry.get("timed_out", False)),
            )
        )
    return result


def window_status(db: Session, *, plan_version: str, window_id: str) -> dict[str, Any]:
    window = _require_window(db, plan_version, window_id)
    records = window.stage_records or {}
    revision_rows = repo.list_revisions(db, plan_version, window_id)
    tasks = repo.list_tasks_for_window(db, plan_version, window_id)
    tasks_by_key = {t.task_key: t for t in tasks}
    has_current_revision_row = any(
        r.revision == window.revision for r in revision_rows
    )

    revision_out = [
        {
            "revision": r.revision,
            "status": r.status,
            "basis_revision": r.basis_revision,
            "cutoff_event_id": r.cutoff_event_id,
            "freeze_id": r.freeze_id,
            "sealed_at": iso(r.sealed_at),
            "reopened_reason": r.reopened_reason,
        }
        for r in sorted(revision_rows, key=lambda r: r.revision)
    ]
    if not has_current_revision_row:
        revision_out.append(
            {
                "revision": window.revision,
                "status": "in_progress",
                "basis_revision": window.revision - 1 or None,
                "cutoff_event_id": window.cutoff_event_id,
                "freeze_id": None,
                "sealed_at": None,
                "reopened_reason": None,
            }
        )

    return {
        "plan_version": plan_version,
        "window_id": window_id,
        "state": window.state,
        "current_stage": window.current_stage,
        "revision": window.revision,
        "created_by": window.created_by,
        "created_at": iso(window.created_at),
        "updated_at": iso(window.updated_at),
        "cutoff_event_id": window.cutoff_event_id,
        "cut_off_at": iso(window.cut_off_at),
        "stages": [s.to_out() for s in _stage_infos(window, tasks_by_key)],
        "current_materials": records.get("materials", {}),
        "audit": records.get("audit", []),
        "revisions": revision_out,
        "tasks": [
            {
                "id": t.id,
                "type": t.task_type,
                "stage": t.stage,
                "revision": t.revision,
                "run_at": iso(t.run_at),
                "status": t.status,
                "attempts": t.attempts,
                "last_error": t.last_error,
            }
            for t in tasks
        ],
    }
