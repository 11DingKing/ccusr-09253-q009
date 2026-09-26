"""结算窗口 API 的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .stages import STAGES


class ActorBody(BaseModel):
    actor_id: str = Field(..., min_length=1, max_length=128)
    actor_role: str = Field(..., min_length=1, max_length=64)


class StartWindowIn(BaseModel):
    window_id: str | None = Field(None, min_length=1, max_length=128)
    created_by: str = Field(..., min_length=1, max_length=128)
    # 阶段 -> 具体责任人（可选）
    owners: dict[str, str] | None = None
    # 阶段 -> 该阶段期限（必须带时区；内部统一转 UTC）
    deadlines: dict[str, datetime] | None = None

    @field_validator("owners")
    @classmethod
    def _check_owner_stages(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v:
            unknown = set(v) - set(STAGES)
            if unknown:
                raise ValueError(f"未知阶段: {sorted(unknown)}")
        return v

    @field_validator("deadlines")
    @classmethod
    def _check_deadline_stages(cls, v: dict[str, datetime] | None) -> dict[str, datetime] | None:
        if v:
            unknown = set(v) - set(STAGES)
            if unknown:
                raise ValueError(f"未知阶段: {sorted(unknown)}")
            for stage, dt in v.items():
                if dt.tzinfo is None:
                    raise ValueError(f"阶段 {stage} 的期限必须带时区信息")
        return v


class AdvanceIn(ActorBody):
    note: str = Field("", max_length=1000)


class PauseIn(ActorBody):
    reason: str = Field(..., min_length=1, max_length=1000)


class ReopenIn(ActorBody):
    reason: str = Field(..., min_length=1, max_length=1000)
    basis_revision: int | None = Field(None, ge=1)


class WindowStatusOut(BaseModel):
    plan_version: str
    window_id: str
    state: str
    current_stage: str
    revision: int
    created_by: str
    created_at: str
    updated_at: str | None = None
    cutoff_event_id: str | None = None
    cut_off_at: str | None = None
    stages: list[dict[str, Any]]
    current_materials: dict[str, Any]
    audit: list[dict[str, Any]]
    revisions: list[dict[str, Any]]
    tasks: list[dict[str, Any]]


class WindowSummary(BaseModel):
    plan_version: str
    window_id: str
    state: str
    current_stage: str
    revision: int
    updated_at: str | None = None
