"""关账窗口 API：启动、推进、暂停、重开、状态查询与不可变材料。"""

from __future__ import annotations

from tests.conftest import SHANGHAI_PLAN


def _plan(client, plan=SHANGHAI_PLAN):
    resp = client.post("/api/plans", json=plan)
    assert resp.status_code == 201, resp.text
    return plan["plan_version"]


def _checkin(eid, student, start, end, activity_type="internship"):
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


def _correction(eid, student, seconds, reason="fix"):
    return {
        "event_id": eid,
        "event_type": "leave_correction",
        "student_id": student,
        "payload": {"adjustment_seconds": seconds, "reason": reason},
    }


def _open(client, pv, window_id="SW-1", durations=None):
    body = {
        "window_id": window_id,
        "plan_version": pv,
        "actor_id": "steward-1",
        "actor_role": "data_steward",
        "note": "term end",
    }
    if durations is not None:
        body["stage_durations_hours"] = durations
    resp = client.post("/api/settlements", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _advance(client, window_id, actor_id, actor_role, note="go", force=False):
    return client.post(
        f"/api/settlements/{window_id}/advance",
        json={
            "actor_id": actor_id,
            "actor_role": actor_role,
            "note": note,
            "force": force,
        },
    )


def _drive_to_frozen(client, window_id, signer="dean-1"):
    """从当前阶段继续推进到冻结。"""
    steps = {
        "data_cutoff": ("steward-1", "data_steward", "cutoff", True),
        "anomaly_review": ("auditor-1", "compliance_auditor", "review", True),
        "grace_period": ("coord-1", "program_coordinator", "grace", False),
        "review_signoff": (signer, "dean", "signoff", True),
    }
    resp = None
    for _ in range(5):
        current = client.get(f"/api/settlements/{window_id}").json()
        if current["status"] == "frozen":
            return current
        actor, role, note, force = steps[current["current_stage"]]
        resp = _advance(client, window_id, actor, role, note, force)
        assert resp.status_code == 200, (role, resp.text)
    raise AssertionError("window did not reach frozen state")


def test_open_requires_registered_plan(client):
    resp = client.post(
        "/api/settlements",
        json={"plan_version": "NOPE", "actor_id": "s", "note": "x"},
    )
    assert resp.status_code == 404


def test_window_lists_five_stages_with_roles_and_deadlines(client):
    pv = _plan(client)
    w = _open(client, pv)
    assert w["revision"] == 1
    assert w["status"] == "in_progress"
    assert w["current_stage"] == "data_cutoff"
    assert [s["stage"] for s in w["stages"]] == [
        "data_cutoff",
        "anomaly_review",
        "grace_period",
        "review_signoff",
        "frozen",
    ]
    roles = {s["stage"]: s["owner_role"] for s in w["stages"]}
    assert roles == {
        "data_cutoff": "data_steward",
        "anomaly_review": "compliance_auditor",
        "grace_period": "program_coordinator",
        "review_signoff": "dean",
        "frozen": "dean",
    }
    # 每个待办阶段都有 UTC 期限。
    for stage in w["stages"][:4]:
        assert stage["deadline_utc"].endswith("Z")


def test_stage_transition_requires_responsible_role(client):
    pv = _plan(client)
    _open(client, pv)
    # dean 不能替数据责任人推进数据截止。
    resp = _advance(client, "SW-1", "dean-1", "dean", "go")
    assert resp.status_code == 409
    assert "requires role" in resp.json()["detail"]
    # 无 note 不允许推进。
    resp = client.post(
        "/api/settlements/SW-1/advance",
        json={"actor_id": "steward-1", "actor_role": "data_steward", "note": ""},
    )
    assert resp.status_code == 422


def test_full_close_flow_freezes_and_records_audit(client):
    pv = _plan(client)
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                ),
                _correction("E-02", "S1", 900),
            ]
        },
    )
    _open(client, pv)

    # 数据截止：固化边界，生成异常清单（实习打卡未确认）。
    resp = _advance(client, "SW-1", "steward-1", "data_steward", "cut")
    assert resp.status_code == 200
    w = resp.json()
    assert w["current_stage"] == "anomaly_review"
    assert w["data_cutoff_event_id"] == "E-02"
    assert w["anomaly_summary"]["total"] == 1

    # 异常未处理时不能直接推进。
    resp = _advance(client, "SW-1", "auditor-1", "compliance_auditor", "rev")
    assert resp.status_code == 409

    # 导师补确认到达，复核清零。
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                {
                    "event_id": "E-03",
                    "event_type": "mentor_confirm",
                    "student_id": "S1",
                    "payload": {"checkin_event_id": "E-01"},
                }
            ]
        },
    )
    resp = client.post(
        "/api/settlements/SW-1/anomalies/resolve",
        json={
            "actor_id": "auditor-1",
            "actor_role": "compliance_auditor",
            "note": "mentor confirmed",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["anomalies_resolved"] is True
    assert _advance(
        client, "SW-1", "auditor-1", "compliance_auditor", "review done"
    ).json()["current_stage"] == "grace_period"

    # 宽限期内的请假修正进入最终边界。
    client.post(
        f"/api/plans/{pv}/events",
        json={"events": [_correction("E-04", "S1", 1800, "late leave fix")]},
    )
    resp = _advance(client, "SW-1", "coord-1", "program_coordinator", "grace done")
    assert resp.json()["current_stage"] == "review_signoff"
    assert resp.json()["event_cutoff_id"] == "E-04"

    w = _drive_to_frozen(client, "SW-1")
    assert w["status"] == "frozen"
    assert w["current_stage"] == "frozen"
    assert w["freeze_id"] == "SW-1-r1"
    assert w["signed_off_by"] == "dean-1"

    snap = client.get(f"/api/plans/{pv}/freezes/SW-1-r1").json()
    assert snap["event_cutoff_id"] == "E-04"
    assert snap["students"][0]["total_seconds"] == 7200 + 900 + 1800

    actions = [a["action"] for a in w["audit"]]
    assert actions == ["open", "advance", "resolve_anomalies", "advance", "advance", "advance"]


def test_pause_blocks_advance_and_resume_shifts_deadline(client):
    pv = _plan(client)
    w = _open(client, pv)
    original_deadline = w["stages"][0]["deadline_utc"]
    resp = client.post(
        "/api/settlements/SW-1/pause",
        json={
            "actor_id": "steward-1",
            "actor_role": "data_steward",
            "note": "awaiting data",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"
    assert _advance(client, "SW-1", "steward-1", "data_steward", "go").status_code == 409

    resp = client.post(
        "/api/settlements/SW-1/resume",
        json={
            "actor_id": "steward-1",
            "actor_role": "data_steward",
            "note": "back",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_progress"
    # 暂停需要 note；错误角色不能暂停。
    assert (
        client.post(
            "/api/settlements/SW-1/pause",
            json={"actor_id": "dean-1", "actor_role": "dean", "note": "x"},
        ).status_code
        == 409
    )
    assert original_deadline is not None  # 剩余时长顺延，新期限不早于恢复时刻。


def test_reopen_requires_different_role_and_person_and_keeps_old_material(client):
    pv = _plan(client)
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T11:00:00+08:00",
                )
            ]
        },
    )
    _open(client, pv)
    _drive_to_frozen(client, "SW-1", signer="dean-1")
    old_total = client.get(f"/api/plans/{pv}/freezes/SW-1-r1").json()[
        "students"
    ][0]["total_seconds"]

    # 非冻结态约束之外：批准角色必须是 academic_senate。
    bad_role = client.post(
        "/api/settlements/SW-1/reopen",
        json={
            "actor_id": "steward-1",
            "actor_role": "data_steward",
            "approver_id": "dean-1",
            "approver_role": "dean",
            "reason": "late correction",
        },
    )
    assert bad_role.status_code == 409
    assert "academic_senate" in bad_role.json()["detail"]

    # 批准人不能是原签署人；发起人与批准人不能同人。
    same_person = client.post(
        "/api/settlements/SW-1/reopen",
        json={
            "actor_id": "steward-1",
            "actor_role": "data_steward",
            "approver_id": "dean-1",
            "approver_role": "academic_senate",
            "reason": "late correction",
        },
    )
    assert same_person.status_code == 409

    ok = client.post(
        "/api/settlements/SW-1/reopen",
        json={
            "actor_id": "steward-1",
            "actor_role": "data_steward",
            "approver_id": "senate-1",
            "approver_role": "academic_senate",
            "reason": "late mentor confirmation E-05",
        },
    )
    assert ok.status_code == 200, ok.text
    r2 = ok.json()
    assert r2["revision"] == 2
    assert r2["status"] == "in_progress"
    assert r2["current_stage"] == "data_cutoff"

    r1 = client.get("/api/settlements/SW-1?revision=1").json()
    assert r1["status"] == "superseded"
    assert r1["superseded_by_revision"] == 2
    assert r1["freeze_id"] == "SW-1-r1"

    # 旧冻结材料不可覆盖。
    old = client.get(f"/api/plans/{pv}/freezes/SW-1-r1").json()
    assert old["students"][0]["total_seconds"] == old_total

    # 新版本走完全程后生成 r2 材料，r1 依旧不变。
    client.post(
        f"/api/plans/{pv}/events",
        json={"events": [_correction("E-09", "S1", 3600, "post reopen")]},
    )
    _drive_to_frozen(client, "SW-1", signer="dean-2")
    new = client.get(f"/api/plans/{pv}/freezes/SW-1-r2").json()
    assert new["event_cutoff_id"] == "E-09"
    assert new["students"][0]["total_seconds"] == old_total + 3600
    assert (
        client.get(f"/api/plans/{pv}/freezes/SW-1-r1").json()["students"][0][
            "total_seconds"
        ]
        == old_total
    )

    # 列表默认返回每个窗口的最新版本。
    listed = client.get("/api/settlements").json()
    assert [(w["window_id"], w["revision"]) for w in listed] == [("SW-1", 2)]


def test_local_calendar_deadlines_via_api_convert_to_utc(client):
    pv = _plan(client)
    resp = client.post(
        "/api/settlements",
        json={
            "window_id": "SW-TZ",
            "plan_version": pv,
            "actor_id": "s",
            "note": "x",
            "deadlines": [
                {"stage": "data_cutoff", "deadline_local_day": "2026-10-01"},
                {"stage": "grace_period", "deadline_local_day": "2026-10-08"},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    by_stage = {s["stage"]: s for s in resp.json()["stages"]}
    # Asia/Shanghai 本地午夜 = 前一日 16:00Z。
    assert by_stage["data_cutoff"]["deadline_utc"] == "2026-09-30T16:00:00Z"
    assert by_stage["grace_period"]["deadline_utc"] == "2026-10-07T16:00:00Z"


def test_duplicate_open_rejected_and_missing_window_404(client):
    pv = _plan(client)
    _open(client, pv, window_id="SW-X")
    resp = client.post(
        "/api/settlements",
        json={"window_id": "SW-X", "plan_version": pv, "actor_id": "s", "note": "x"},
    )
    assert resp.status_code == 409
    assert client.get("/api/settlements/NOPE").status_code == 404


def test_event_after_grace_deadline_is_excluded_from_final_freeze(client):
    pv = _plan(client)
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                    activity_type="regular",
                )
            ]
        },
    )
    _open(client, pv)
    _advance(client, "SW-1", "steward-1", "data_steward", "cut")
    _advance(
        client, "SW-1", "auditor-1", "compliance_auditor", "rev", force=True
    )
    # 宽限期结束：边界取当时最新事件（E-01）。
    grace = _advance(client, "SW-1", "coord-1", "program_coordinator", "grace")
    assert grace.json()["event_cutoff_id"] == "E-01"
    # 签署阶段才到达的修正超出补录宽限，不得进入冻结快照。
    client.post(
        f"/api/plans/{pv}/events",
        json={"events": [_correction("E-77", "S1", 5000, "too late")]},
    )
    w = _drive_to_frozen(client, "SW-1")
    assert w["event_cutoff_id"] == "E-01"
    snap = client.get(f"/api/plans/{pv}/freezes/SW-1-r1").json()
    assert snap["event_cutoff_id"] == "E-01"
    assert snap["students"][0]["total_seconds"] == 7200
