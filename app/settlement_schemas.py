"""关账窗口 API 的请求 / 响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class StageDeadlineIn(BaseModel):
    """按培养方案所在时区本地日历日设定阶段期限（跨时区安全）。"""

    stage: str
    owner_id: str | None = None
    deadline_local_day: str = Field(
        ...,
        description="该时区本地日期 (YYYY-MM-DD)，截止时刻为当日 00:00",
    )


class OpenWindowIn(BaseModel):
    window_id: str | None = Field(default=None, min_length=1, max_length=128)
    plan_version: str = Field(..., min_length=1, max_length=128)
    actor_id: str = Field(..., min_length=1, max_length=128)
    actor_role: str = Field(default="data_steward", min_length=1, max_length=64)
    note: str = Field(default="", max_length=512)
    # 统一时长覆盖（小时），未给的阶段使用默认时长。
    stage_durations_hours: dict[str, float] | None = None
    # 各阶段责任人（按角色匹配，不填则进入阶段时由推进人承担）。
    stage_owners: dict[str, str] | None = None
    # 按本地日历日设置的绝对期限，优先级高于 duration_hours。
    deadlines: list[StageDeadlineIn] | None = None

    @field_validator("deadlines")
    @classmethod
    def _validate_deadlines(cls, v):
        if v:
            stages_seen = [item.stage for item in v]
            if len(stages_seen) != len(set(stages_seen)):
                raise ValueError("duplicate stage in deadlines")
        return v


class AdvanceWindowIn(BaseModel):
    actor_id: str = Field(..., min_length=1, max_length=128)
    actor_role: str = Field(..., min_length=1, max_length=64)
    note: str = Field(..., min_length=1, max_length=512)
    force: bool = False


class ResolveAnomalyIn(BaseModel):
    actor_id: str = Field(..., min_length=1, max_length=128)
    actor_role: str = Field(default="compliance_auditor", min_length=1, max_length=64)
    note: str = Field(..., min_length=1, max_length=512)


class PauseWindowIn(BaseModel):
    actor_id: str = Field(..., min_length=1, max_length=128)
    actor_role: str = Field(..., min_length=1, max_length=64)
    note: str = Field(..., min_length=1, max_length=512)


class ReopenWindowIn(BaseModel):
    # 发起人（可以是原流程中的数据责任人）。
    actor_id: str = Field(..., min_length=1, max_length=128)
    actor_role: str = Field(default="data_steward", min_length=1, max_length=64)
    # 批准人：必须是不同于签署角色（dean）的另一角色，且不是原签署人。
    approver_id: str = Field(..., min_length=1, max_length=128)
    approver_role: str = Field(default="academic_senate", min_length=1, max_length=64)
    reason: str = Field(..., min_length=1, max_length=512)


class StageOut(BaseModel):
    stage: str
    ordinal: int
    owner_role: str
    owner_id: str | None
    deadline_utc: str
    entered_at_utc: str | None
    completed_at_utc: str | None
    overdue: bool
    note: str


class AuditOut(BaseModel):
    seq: int
    action: str
    actor_id: str
    actor_role: str
    from_status: str
    to_status: str
    reason: str
    detail: dict[str, Any]
    occurred_at_utc: str


class WindowOut(BaseModel):
    window_id: str
    revision: int
    plan_version: str
    status: str
    timezone: str
    opened_by: str
    note: str
    current_stage: str | None
    data_cutoff_event_id: str | None
    event_cutoff_id: str | None
    anomaly_summary: dict[str, Any] | None
    anomalies_resolved: bool
    grace_deadline_utc: str | None
    signed_off_by: str | None
    freeze_id: str | None
    superseded_by_revision: int | None
    active_stage_overdue: bool
    paused_at_utc: str | None
    created_at_utc: str | None
    updated_at_utc: str | None
    stages: list[StageOut]
    audit: list[AuditOut]


class TaskOut(BaseModel):
    id: int
    task_type: str
    window_id: str
    revision: int
    run_at_utc: datetime
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None
    locked_by: str | None
    completed_at_utc: datetime | None
