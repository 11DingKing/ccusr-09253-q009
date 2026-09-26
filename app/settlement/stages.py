"""结算窗口的固定阶段定义与角色权限。

窗口必须依次穿过五个阶段，每个阶段都有明确的责任角色与期限：

1. ``data_cutoff``     数据截止 —— 确定事件边界与计划本地截止时刻
2. ``exception_list``  异常清单 —— 列出待确认签到与负向修正等待办
3. ``grace_entry``     补录宽限 —— 允许在宽限期内补交事件
4. ``review_signoff``  复核签署 —— 不同角色复核并电子签署
5. ``frozen``          正式冻结 —— 生成不可覆盖的冻结快照（终态）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# 窗口状态
STATE_ACTIVE = "active"
STATE_PAUSED = "paused"
STATE_COMPLETED = "completed"

WINDOW_STATES = frozenset({STATE_ACTIVE, STATE_PAUSED, STATE_COMPLETED})

# 五个固定阶段（有序）。frozen 是窗口的最终阶段。
STAGES: tuple[str, ...] = (
    "data_cutoff",
    "exception_list",
    "grace_entry",
    "review_signoff",
    "frozen",
)

# 每个阶段固定的责任角色（启动时可指定具体责任人）。
DEFAULT_OWNER_ROLES: dict[str, str] = {
    "data_cutoff": "data_clerk",
    "exception_list": "compliance_officer",
    "grace_entry": "mentor_coordinator",
    "review_signoff": "dean_reviewer",
    "frozen": "registrar",
}

# 重开必须由与“复核签署人”和“执行冻结的教务员”都不同的角色批准。
REOPEN_FORBIDDEN_ROLES = frozenset({"dean_reviewer", "registrar"})

# 阶段中文说明，用于状态查询与审计。
STAGE_LABELS: dict[str, str] = {
    "data_cutoff": "数据截止",
    "exception_list": "异常清单",
    "grace_entry": "补录宽限",
    "review_signoff": "复核签署",
    "frozen": "正式冻结",
}


@dataclass(frozen=True)
class StageDeadlineInfo:
    """阶段责任人与期限。"""

    stage: str
    label: str
    owner_role: str
    owner_id: str | None
    deadline_at: str | None
    entered_at: str | None
    completed_at: str | None
    completed_by: str | None
    task_id: int | None
    timed_out: bool

    def to_out(self) -> dict:
        return {
            "stage": self.stage,
            "label": STAGE_LABELS[self.stage],
            "owner_role": self.owner_role,
            "owner_id": self.owner_id,
            "deadline_at": self.deadline_at,
            "entered_at": self.entered_at,
            "completed_at": self.completed_at,
            "completed_by": self.completed_by,
            "task_id": self.task_id,
            "timed_out": self.timed_out,
        }


def stage_index(stage: str) -> int:
    return STAGES.index(stage)


def next_stage_of(stage: str) -> str | None:
    idx = stage_index(stage)
    if idx + 1 < len(STAGES):
        return STAGES[idx + 1]
    return None


def parse_aware(value: str | datetime) -> datetime:
    """把输入解析为带时区的 UTC 时间戳；naive 时间戳直接拒绝。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"时间戳必须带时区信息: {value!r}")
    return dt.astimezone(timezone.utc)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    # SQLite 读回的时间戳不带时区，统一按 UTC 解释。
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
