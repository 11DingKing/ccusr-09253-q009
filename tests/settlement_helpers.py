"""结算窗口测试共用工具。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.settlement.clock import set_clock

FIXED_NOW = datetime(2026, 9, 1, 9, 0, 0, tzinfo=timezone.utc)

# 各阶段唯一允许的推进角色
STAGE_ROLES = [
    "data_clerk",
    "compliance_officer",
    "mentor_coordinator",
    "dean_reviewer",
]


def freeze_clock(moment: datetime = FIXED_NOW) -> dict:
    holder = {"now": moment}
    set_clock(lambda: holder["now"])
    return holder


def create_plan(client, plan_version: str = "P-SETTLE-2026", tz: str = "Asia/Shanghai", required: int = 3600):
    resp = client.post(
        "/api/plans",
        json={
            "plan_version": plan_version,
            "iana_timezone": tz,
            "required_seconds": required,
        },
    )
    assert resp.status_code == 201, resp.text
    return plan_version


def internship_checkin(eid: str, student: str, start: str, end: str):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": "internship",
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def mentor_confirm(eid: str, student: str, target: str):
    return {
        "event_id": eid,
        "event_type": "mentor_confirm",
        "student_id": student,
        "payload": {"checkin_event_id": target},
    }


def start_window(client, pv: str, window_id: str = "SW-1", created_by: str = "reg-1", **extra):
    resp = client.post(
        f"/api/plans/{pv}/settlement-windows",
        json={"window_id": window_id, "created_by": created_by, **extra},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def advance(client, pv, window_id, *, role, actor="user", note="ok", expected: int = 200):
    resp = client.post(
        f"/api/plans/{pv}/settlement-windows/{window_id}/advance",
        json={"actor_id": actor, "actor_role": role, "note": note},
    )
    assert resp.status_code == expected, resp.text
    return resp.json()


def run_to_frozen(client, pv, window_id, *, final_role: str = "dean_reviewer", start: bool = True):
    """依次推进到正式冻结；返回最终状态体。"""
    if start:
        start_window(client, pv, window_id)
    body = None
    for i, role in enumerate(STAGE_ROLES):
        role = final_role if i == len(STAGE_ROLES) - 1 else role
        body = advance(client, pv, window_id, role=role, actor=f"{role}-1")
    assert body["current_stage"] == "frozen"
    assert body["state"] == "completed"
    return body
