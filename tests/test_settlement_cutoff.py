"""结算窗口跨时区截止与补录宽限边界测试。"""

from __future__ import annotations

from datetime import timedelta

from tests.conftest import NY_PLAN
from tests.settlement_helpers import (
    FIXED_NOW,
    advance,
    create_plan,
    freeze_clock,
    internship_checkin,
    mentor_confirm,
    run_to_frozen,
    start_window,
)


def _checkin(eid, student, start, end, activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def test_data_cutoff_records_deadline_in_plan_local_timezone(client):
    freeze_clock()
    pv = create_plan(client)
    body = start_window(client, pv)
    material = body["current_materials"]["data_cutoff"]
    # UTC 09:00 + 1 天默认期限 == 上海本地 17:00（+08:00）
    assert material["deadline_at"] == "2026-09-02T09:00:00Z"
    assert material["deadline_at_local"].endswith("+08:00")
    assert material["deadline_at_local"].startswith("2026-09-02T17:00:00")
    assert material["timezone"] == "Asia/Shanghai"


def test_events_after_cutoff_excluded_from_exception_list_but_in_grace(client):
    freeze_clock()
    pv = create_plan(client)
    # 截止前只有 E-01（待确认实习）
    client.post(
        f"/api/plans/{pv}/events",
        json={"events": [internship_checkin(
            "E-01", "S1",
            "2026-08-15T08:00:00+08:00", "2026-08-15T10:00:00+08:00",
        )]},
    )
    start_window(client, pv)

    # 截止后、异常清单生成后才到达的请假修正，不影响异常清单
    body = advance(client, pv, "SW-1", role="data_clerk")
    assert body["current_materials"]["exception_list"]["counts"] == {
        "pending_confirmations": 1,
        "negative_adjustments": 0,
        "deficient_students": 1,
    }

    # 宽限期内：导师补确认 + 请假修正
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                mentor_confirm("E-50", "S1", "E-01"),
                {
                    "event_id": "E-51",
                    "event_type": "leave_correction",
                    "student_id": "S1",
                    "payload": {"adjustment_seconds": 1800, "reason": "补录学时"},
                },
            ]
        },
    )
    body = advance(client, pv, "SW-1", role="compliance_officer")  # -> grace_entry
    assert body["current_materials"]["grace_entry"]["cutoff_at_entry"] == "E-01"

    # 宽限结束：边界扩展，纳入 E-50/E-51
    body = advance(client, pv, "SW-1", role="mentor_coordinator")
    grace = body["current_materials"]["grace_entry"]
    assert grace["cutoff_before"] == "E-01"
    assert grace["cutoff_after"] == "E-51"
    assert grace["events_added"] == ["E-50", "E-51"]

    # 复核签署后正式冻结，冻结快照反映宽限期补录
    body = advance(client, pv, "SW-1", role="dean_reviewer")
    freeze = client.get(f"/api/plans/{pv}/freezes/SW-1-r1").json()
    s1 = freeze["students"][0]
    assert s1["confirmed_seconds"] == 7200
    assert s1["adjustment_seconds"] == 1800
    assert s1["total_seconds"] == 7200 + 1800
    assert freeze["event_cutoff_id"] == "E-51"


def test_events_arriving_after_grace_closed_are_excluded_until_reopen(client):
    clock = freeze_clock()
    pv = create_plan(client)
    client.post(
        f"/api/plans/{pv}/events",
        json={"events": [_checkin(
            "E-01", "S1",
            "2026-08-15T08:00:00+08:00", "2026-08-15T10:00:00+08:00",
        )]},
    )
    run_to_frozen(client, pv, "SW-1")
    # 冻结后才到达的补确认：不属于 r1
    client.post(
        f"/api/plans/{pv}/events",
        json={"events": [
            {
                "event_id": "E-90",
                "event_type": "leave_correction",
                "student_id": "S1",
                "payload": {"adjustment_seconds": 3600, "reason": "late"},
            }
        ]},
    )
    r1 = client.get(f"/api/plans/{pv}/freezes/SW-1-r1").json()
    assert r1["event_cutoff_id"] == "E-01"
    assert r1["students"][0]["total_seconds"] == 7200

    # 重开后新数据截止把 E-90 纳入 r2
    clock["now"] += timedelta(days=1)
    resp = client.post(
        f"/api/plans/{pv}/settlement-windows/SW-1/reopen",
        json={"actor_id": "p", "actor_role": "provost", "reason": "late correction"},
    )
    assert resp.status_code == 200
    assert resp.json()["cutoff_event_id"] == "E-90"
    run_to_frozen(client, pv, "SW-1", start=False)
    r2 = client.get(f"/api/plans/{pv}/freezes/SW-1-r2").json()
    assert r2["event_cutoff_id"] == "E-90"
    assert r2["students"][0]["total_seconds"] == 7200 + 3600


def test_new_york_cutoff_boundary_uses_absolute_time(client, db):
    """跨时区：以 UTC 绝对时刻判定期限，纽约本地时钟不影响判定。"""
    from app.settlement import worker
    from tests.conftest import TestSessionLocal

    clock = freeze_clock(FIXED_NOW)
    pv = create_plan(
        client,
        plan_version=NY_PLAN["plan_version"],
        tz="America/New_York",
    )
    body = start_window(
        client, pv,
        deadlines={"data_cutoff": "2026-09-01T09:00:00-04:00"},
    )
    # 本地 09:00 EDT == UTC 13:00；材料同时保留本地与 UTC 表示
    local = body["current_materials"]["data_cutoff"]["deadline_at_local"]
    assert local.startswith("2026-09-01T09:00:00-04:00")

    # UTC 12:59 未到期
    clock["now"] = FIXED_NOW.replace(hour=12, minute=59)
    wdb = TestSessionLocal()
    assert worker.process_once(wdb)["advanced"] == 0
    wdb.close()
    db.expire_all()
    # UTC 13:01 到期
    clock["now"] = FIXED_NOW.replace(hour=13, minute=1)
    wdb = TestSessionLocal()
    assert worker.process_once(wdb)["advanced"] == 1
    wdb.close()
    db.expire_all()
    body = client.get(
        f"/api/plans/{NY_PLAN['plan_version']}/settlement-windows/SW-1"
    ).json()
    assert body["current_stage"] == "exception_list"
