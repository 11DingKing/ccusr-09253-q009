"""结算窗口 HTTP 接口测试：启动/推进/暂停/恢复/重开/状态查询。"""

from __future__ import annotations

from datetime import timedelta

from tests.conftest import NY_PLAN
from tests.settlement_helpers import (
    FIXED_NOW,
    advance,
    create_plan,
    freeze_clock,
    mentor_confirm,
    run_to_frozen,
    start_window,
    internship_checkin,
)


def test_start_window_initializes_five_stages_with_owners_and_deadlines(client):
    freeze_clock()
    pv = create_plan(client)
    body = start_window(
        client,
        pv,
        owners={"data_cutoff": "alice"},
        deadlines={"data_cutoff": "2026-09-02T09:00:00+00:00"},
    )
    assert body["state"] == "active"
    assert body["current_stage"] == "data_cutoff"
    assert body["revision"] == 1
    assert [s["stage"] for s in body["stages"]] == [
        "data_cutoff",
        "exception_list",
        "grace_entry",
        "review_signoff",
        "frozen",
    ]
    first = body["stages"][0]
    assert first["owner_role"] == "data_clerk"
    assert first["owner_id"] == "alice"
    assert first["deadline_at"] == "2026-09-02T09:00:00Z"
    assert first["task_id"] is not None
    # frozen 是终态，没有期限
    assert body["stages"][-1]["deadline_at"] is None
    # 数据截止材料记录了事件边界
    assert body["current_materials"]["data_cutoff"]["event_cutoff_id"] is None
    # 审计起始动作
    assert body["audit"][0]["action"] == "start"


def test_start_duplicate_window_returns_409(client):
    freeze_clock()
    pv = create_plan(client)
    start_window(client, pv)
    resp = client.post(
        f"/api/plans/{pv}/settlement-windows",
        json={"window_id": "SW-1", "created_by": "reg-1"},
    )
    assert resp.status_code == 409


def test_start_window_for_unknown_plan_returns_404(client):
    freeze_clock()
    resp = client.post(
        "/api/plans/NOPE/settlement-windows",
        json={"window_id": "SW-1", "created_by": "reg-1"},
    )
    assert resp.status_code == 404


def test_deadline_without_timezone_is_rejected(client):
    freeze_clock()
    pv = create_plan(client)
    resp = client.post(
        f"/api/plans/{pv}/settlement-windows",
        json={
            "window_id": "SW-1",
            "created_by": "reg-1",
            "deadlines": {"data_cutoff": "2026-09-02T09:00:00"},
        },
    )
    assert resp.status_code == 422


def test_stage_must_be_advanced_by_its_responsible_role(client):
    freeze_clock()
    pv = create_plan(client)
    start_window(client, pv)
    # data_cutoff 只能由 data_clerk 推进
    body = advance(
        client, pv, "SW-1", role="registrar", actor="reg-1", expected=403
    )
    assert "data_clerk" in body["detail"]


def test_stages_progress_in_order_and_collect_materials(client):
    freeze_clock()
    pv = create_plan(client)
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                internship_checkin(
                    "E-01", "S1",
                    "2026-08-15T08:00:00+08:00", "2026-08-15T10:00:00+08:00",
                ),
                {
                    "event_id": "E-02",
                    "event_type": "leave_correction",
                    "student_id": "S1",
                    "payload": {"adjustment_seconds": -900, "reason": "deduct"},
                },
            ]
        },
    )
    start_window(client, pv)
    # -> exception_list: 产出异常清单
    body = advance(client, pv, "SW-1", role="data_clerk", actor="dc")
    assert body["current_stage"] == "exception_list"
    exc = body["current_materials"]["exception_list"]
    assert exc["counts"]["pending_confirmations"] == 1
    assert exc["pending_confirmations"][0]["event_id"] == "E-01"
    assert exc["counts"]["negative_adjustments"] == 1
    assert exc["counts"]["deficient_students"] == 1
    assert body["stages"][0]["completed_by"] == "dc"

    # 宽限期内导师补确认
    client.post(
        f"/api/plans/{pv}/events",
        json={"events": [mentor_confirm("E-09", "S1", "E-01")]},
    )
    body = advance(client, pv, "SW-1", role="compliance_officer", actor="co")
    assert body["current_stage"] == "grace_entry"
    assert body["current_materials"]["grace_entry"]["cutoff_at_entry"] == "E-02"

    # -> review_signoff：宽限截止边界扩展到 E-09
    body = advance(client, pv, "SW-1", role="mentor_coordinator", actor="mc")
    grace = body["current_materials"]["grace_entry"]
    assert grace["cutoff_after"] == "E-09"
    assert grace["events_added"] == ["E-09"]

    # -> frozen：dean_reviewer 与 registrar 都可执行最终一步
    body = advance(
        client, pv, "SW-1", role="dean_reviewer", actor="dean", note="签署确认"
    )
    assert body["state"] == "completed"
    assert body["current_stage"] == "frozen"
    assert body["current_materials"]["review_signoff"]["actor_id"] == "dean"
    assert body["current_materials"]["frozen"]["freeze_id"] == "SW-1-r1"
    assert body["revisions"][0]["status"] == "sealed"
    assert body["revisions"][0]["freeze_id"] == "SW-1-r1"


def test_registrar_may_perform_final_freeze(client):
    freeze_clock()
    pv = create_plan(client)
    start_window(client, pv)
    advance(client, pv, "SW-1", role="data_clerk")
    advance(client, pv, "SW-1", role="compliance_officer")
    advance(client, pv, "SW-1", role="mentor_coordinator")
    body = advance(client, pv, "SW-1", role="registrar", actor="reg-1")
    assert body["current_stage"] == "frozen"
    assert body["current_materials"]["frozen"]["frozen_by_role"] == "registrar"


def test_cannot_advance_paused_window_without_resume(client):
    freeze_clock()
    pv = create_plan(client)
    start_window(client, pv)
    resp = client.post(
        f"/api/plans/{pv}/settlement-windows/SW-1/pause",
        json={"actor_id": "reg-1", "actor_role": "registrar", "reason": "等待学院数据"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "paused"
    # 暂停期间推进被拒
    advance(client, pv, "SW-1", role="data_clerk", expected=409)
    # 非教务处不能恢复
    resp = client.post(
        f"/api/plans/{pv}/settlement-windows/SW-1/resume",
        json={"actor_id": "x", "actor_role": "data_clerk", "reason": "继续"},
    )
    assert resp.status_code == 403
    resp = client.post(
        f"/api/plans/{pv}/settlement-windows/SW-1/resume",
        json={"actor_id": "reg-1", "actor_role": "registrar", "reason": "继续"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "active"
    body = advance(client, pv, "SW-1", role="data_clerk")
    assert body["current_stage"] == "exception_list"


def test_pause_cancels_due_timeout_and_resume_reinstates_it(client):
    clock = freeze_clock()
    pv = create_plan(client)
    start_window(
        client, pv,
        deadlines={"data_cutoff": "2026-09-01T10:00:00+00:00"},
    )
    # 暂停后即使越过期限也不应自动推进（任务已取消）
    client.post(
        f"/api/plans/{pv}/settlement-windows/SW-1/pause",
        json={"actor_id": "r", "actor_role": "registrar", "reason": "hold"},
    )
    clock["now"] += timedelta(days=2)
    body = client.get(
        f"/api/plans/{pv}/settlement-windows/SW-1"
    ).json()
    assert body["current_stage"] == "data_cutoff"
    assert all(t["status"] == "cancelled" for t in body["tasks"] if t["revision"] == 1)


def test_reopen_requires_different_role_and_creates_new_revision(client):
    clock = freeze_clock()
    pv = create_plan(client)
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                internship_checkin(
                    "E-01", "S1",
                    "2026-08-15T08:00:00+08:00", "2026-08-15T10:00:00+08:00",
                )
            ]
        },
    )
    run_to_frozen(client, pv, "SW-1")

    # 冻结教务员不能自己批准重开
    resp = client.post(
        f"/api/plans/{pv}/settlement-windows/SW-1/reopen",
        json={"actor_id": "dean", "actor_role": "dean_reviewer", "reason": "改"},
    )
    assert resp.status_code == 403
    resp = client.post(
        f"/api/plans/{pv}/settlement-windows/SW-1/reopen",
        json={"actor_id": "reg-1", "actor_role": "registrar", "reason": "改"},
    )
    assert resp.status_code == 403
    # 原签署人本人即使换一个不同角色也不能批准
    resp = client.post(
        f"/api/plans/{pv}/settlement-windows/SW-1/reopen",
        json={"actor_id": "dean_reviewer-1", "actor_role": "provost", "reason": "改"},
    )
    assert resp.status_code == 403

    # 冻结后导师补确认，副校长（不同角色、不同人）批准重开
    client.post(
        f"/api/plans/{pv}/events",
        json={"events": [mentor_confirm("E-77", "S1", "E-01")]},
    )
    clock["now"] += timedelta(days=1)
    resp = client.post(
        f"/api/plans/{pv}/settlement-windows/SW-1/reopen",
        json={
            "actor_id": "provost-1",
            "actor_role": "provost",
            "reason": "导师补确认，需要重新结算",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revision"] == 2
    assert body["current_stage"] == "data_cutoff"
    assert body["state"] == "active"
    assert body["cutoff_event_id"] == "E-77"
    statuses = [(r["revision"], r["status"]) for r in body["revisions"]]
    assert (1, "sealed") in statuses
    assert (2, "open") in statuses


def test_old_freeze_material_is_not_overwritten_after_reopen(client):
    clock = freeze_clock()
    pv = create_plan(client)
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                internship_checkin(
                    "E-01", "S1",
                    "2026-08-15T08:00:00+08:00", "2026-08-15T10:00:00+08:00",
                )
            ]
        },
    )
    run_to_frozen(client, pv, "SW-1")
    old = client.get(f"/api/plans/{pv}/freezes/SW-1-r1").json()
    assert old["students"][0]["total_seconds"] == 0
    assert old["event_cutoff_id"] == "E-01"

    client.post(
        f"/api/plans/{pv}/events",
        json={"events": [mentor_confirm("E-77", "S1", "E-01")]},
    )
    clock["now"] += timedelta(days=1)
    resp = client.post(
        f"/api/plans/{pv}/settlement-windows/SW-1/reopen",
        json={"actor_id": "p", "actor_role": "provost", "reason": "补确认"},
    )
    assert resp.status_code == 200
    run_to_frozen(client, pv, "SW-1", start=False)

    # 旧快照保持不变
    old_again = client.get(f"/api/plans/{pv}/freezes/SW-1-r1").json()
    assert old_again["students"][0]["total_seconds"] == 0
    assert old_again["event_cutoff_id"] == "E-01"
    # 新快照反映补确认，且是独立 freeze_id
    new = client.get(f"/api/plans/{pv}/freezes/SW-1-r2").json()
    assert new["event_cutoff_id"] == "E-77"
    assert new["students"][0]["total_seconds"] == 7200
    # 差异接口可对比两个版本
    diff = client.get(
        f"/api/plans/{pv}/freezes/SW-1-r1/diff/SW-1-r2"
    ).json()
    assert diff["students_affected"] == 1
    assert diff["student_changes"][0]["fields"]["total_seconds"] == {
        "before": 0,
        "after": 7200,
    }


def test_reopen_only_allowed_from_frozen(client):
    freeze_clock()
    pv = create_plan(client)
    start_window(client, pv)
    resp = client.post(
        f"/api/plans/{pv}/settlement-windows/SW-1/reopen",
        json={"actor_id": "p", "actor_role": "provost", "reason": "x"},
    )
    assert resp.status_code == 409


def test_status_unknown_window_returns_404(client):
    freeze_clock()
    pv = create_plan(client)
    resp = client.get(f"/api/plans/{pv}/settlement-windows/GONE")
    assert resp.status_code == 404


def test_list_windows(client):
    freeze_clock()
    pv = create_plan(client)
    start_window(client, pv, "SW-A")
    start_window(client, pv, "SW-B")
    rows = client.get(f"/api/plans/{pv}/settlement-windows").json()
    assert {w["window_id"] for w in rows} == {"SW-A", "SW-B"}


def test_cross_timezone_deadline_is_interpreted_as_absolute_instant(client, db):
    """纽约本地给出的期限换算为 UTC 绝对时刻，跨时区不错位。"""
    clock = freeze_clock(FIXED_NOW)  # 2026-09-01 09:00 UTC
    pv = create_plan(client, plan_version=NY_PLAN["plan_version"], tz="America/New_York")
    # 纽约 2026-09-01 09:00（EDT, UTC-4）== 13:00 UTC
    body = start_window(
        client, pv,
        deadlines={"data_cutoff": "2026-09-01T09:00:00-04:00"},
    )
    assert body["stages"][0]["deadline_at"] == "2026-09-01T13:00:00Z"

    # 12:00 UTC：对纽约当地看似已过 09:00，但绝对期限 13:00 UTC 未到
    clock["now"] = FIXED_NOW.replace(hour=12)
    from app.settlement import worker
    from tests.conftest import TestSessionLocal

    worker_db = TestSessionLocal()
    result = worker.process_once(worker_db)
    worker_db.close()
    assert result == {"advanced": 0, "noop": 0, "failed": 0}
    db.expire_all()
    assert (
        client.get(f"/api/plans/{NY_PLAN['plan_version']}/settlement-windows/SW-1")
        .json()["current_stage"]
        == "data_cutoff"
    )

    # 13:30 UTC：越过绝对期限，自动推进
    clock["now"] = FIXED_NOW.replace(hour=13, minute=30)
    worker_db = TestSessionLocal()
    result = worker.process_once(worker_db)
    worker_db.close()
    assert result["advanced"] == 1
    db.expire_all()
    assert (
        client.get(f"/api/plans/{NY_PLAN['plan_version']}/settlement-windows/SW-1")
        .json()["current_stage"]
        == "exception_list"
    )
