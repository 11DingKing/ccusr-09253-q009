"""学期结算关账窗口：五阶段状态机、责任角色与期限。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo


class Stage(StrEnum):
    DATA_CUTOFF = "data_cutoff"
    ANOMALY_REVIEW = "anomaly_review"
    GRACE_PERIOD = "grace_period"
    REVIEW_SIGNOFF = "review_signoff"
    FROZEN = "frozen"


# 阶段顺序；FROZEN 是终态，不分配待办期限。
STAGE_ORDER: tuple[Stage, ...] = (
    Stage.DATA_CUTOFF,
    Stage.ANOMALY_REVIEW,
    Stage.GRACE_PERIOD,
    Stage.REVIEW_SIGNOFF,
    Stage.FROZEN,
)

# 每个阶段的责任角色（重开批准人必须不同于签署阶段的角色）。
STAGE_OWNER_ROLES: dict[Stage, str] = {
    Stage.DATA_CUTOFF: "data_steward",
    Stage.ANOMALY_REVIEW: "compliance_auditor",
    Stage.GRACE_PERIOD: "program_coordinator",
    Stage.REVIEW_SIGNOFF: "dean",
    Stage.FROZEN: "dean",
}

# 各阶段默认办理时长（从进入阶段起算）。
DEFAULT_STAGE_DURATIONS: dict[Stage, timedelta] = {
    Stage.DATA_CUTOFF: timedelta(hours=24),
    Stage.ANOMALY_REVIEW: timedelta(hours=48),
    Stage.GRACE_PERIOD: timedelta(hours=72),
    Stage.REVIEW_SIGNOFF: timedelta(hours=24),
}

# 窗口运行状态（current_stage 由 stages 表推导）。
STATUS_IN_PROGRESS = "in_progress"
STATUS_PAUSED = "paused"
STATUS_FROZEN = "frozen"
STATUS_SUPERSEDED = "superseded"

# 后台任务状态。
TASK_PENDING = "pending"
TASK_DONE = "done"
TASK_CANCELLED = "cancelled"
TASK_DEAD_LETTER = "dead_letter"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """所有期限统一以 UTC 存储和比较。"""
    if value.tzinfo is None:
        raise ValueError("deadline must be timezone-aware (RFC 3339)")
    return value.astimezone(timezone.utc)


def local_midnight_deadline(local_day: date, tz_name: str) -> datetime:
    """培养方案所在时区的某日 00:00（含 DST 偏移）转换为 UTC 期限。"""
    zone = ZoneInfo(tz_name)
    naive = datetime.combine(local_day, time(0, 0))
    return naive.replace(tzinfo=zone).astimezone(timezone.utc)


def parse_deadline(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return as_utc(value)
    return as_utc(datetime.fromisoformat(value))


def next_stage(stage: Stage) -> Stage | None:
    idx = STAGE_ORDER.index(stage)
    if idx + 1 >= len(STAGE_ORDER):
        return None
    return STAGE_ORDER[idx + 1]


def default_deadline(stage: Stage, entered_at: datetime) -> datetime:
    return entered_at + DEFAULT_STAGE_DURATIONS[stage]
