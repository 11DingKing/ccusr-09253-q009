"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    TypeDecorator,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TZDateTime(TypeDecorator):
    """以 ISO-8601 文本持久化、始终还原为 UTC aware datetime。

    SQLite 的 DateTime(timezone=True) 在读回时会丢弃 tzinfo，导致与
    timezone-aware 时间比较时报错；关账窗口的所有期限都要可靠地按 UTC 比较，
    因此统一使用本类型。
    """

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("TZDateTime requires timezone-aware datetimes")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(
            tzinfo=timezone.utc
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Plan(Base):
    __tablename__ = "plans"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    iana_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    required_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("required_seconds >= 0", name="ck_plans_required_nonneg"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("event_id", "plan_version", name="uq_events_event_id_plan"),
        Index("ix_events_plan_student", "plan_version", "student_id"),
    )


class Freeze(Base):
    __tablename__ = "freezes"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    freeze_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    event_cutoff_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class SettlementWindow(Base):
    """学期结算关账窗口（含版本与五阶段状态机）。"""

    __tablename__ = "settlement_windows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False)
    window_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    iana_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    opened_by: Mapped[str] = mapped_column(String(128), nullable=False)
    note: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # 数据截止阶段固化的事件边界（用于异常清单）。
    data_cutoff_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 最终冻结时采用的事件边界（含补录宽限期内的补录）。
    event_cutoff_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    anomaly_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    anomalies_resolved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    grace_deadline_utc: Mapped[datetime | None] = mapped_column(
        TZDateTime, nullable=True
    )
    signed_off_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    freeze_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    superseded_by_revision: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    paused_at_utc: Mapped[datetime | None] = mapped_column(
        TZDateTime, nullable=True
    )
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    __table_args__ = (
        UniqueConstraint("window_id", "revision", name="uq_settlement_window_revision"),
        Index("ix_settlement_plan", "plan_version"),
    )

    __mapper_args__ = {"version_id_col": lock_version}


class SettlementStage(Base):
    """关账流程中的单个阶段（责任人与期限）。"""

    __tablename__ = "settlement_stages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    window_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_role: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    deadline_utc: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False
    )
    entered_at_utc: Mapped[datetime | None] = mapped_column(
        TZDateTime, nullable=True
    )
    completed_at_utc: Mapped[datetime | None] = mapped_column(
        TZDateTime, nullable=True
    )
    note: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    __table_args__ = (
        UniqueConstraint(
            "window_id", "revision", "stage", name="uq_settlement_stage_uniq"
        ),
        ForeignKeyConstraint(
            ["window_id", "revision"],
            ["settlement_windows.window_id", "settlement_windows.revision"],
            name="fk_settlement_stage_window",
        ),
        Index("ix_settlement_stages_due", "deadline_utc"),
    )


class SettlementAudit(Base):
    """关账操作审计日志（append-only）。"""

    __tablename__ = "settlement_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    window_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(64), nullable=False)
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at_utc: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("window_id", "revision", "seq", name="uq_settlement_audit_seq"),
        Index("ix_settlement_audit_window", "window_id", "revision"),
    )


class SettlementTaskState(Base):
    """持久化的后台任务表，保证超时处理在进程重启后继续。"""

    __tablename__ = "settlement_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    window_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    run_at_utc: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    last_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_until_utc: Mapped[datetime | None] = mapped_column(
        TZDateTime, nullable=True
    )
    completed_at_utc: Mapped[datetime | None] = mapped_column(
        TZDateTime, nullable=True
    )
    created_at_utc: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, default=_utcnow
    )
    updated_at_utc: Mapped[datetime] = mapped_column(
        TZDateTime,
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "task_type",
            "window_id",
            "revision",
            name="uq_settlement_task_unique",
        ),
        Index("ix_settlement_tasks_due", "status", "run_at_utc"),
    )
