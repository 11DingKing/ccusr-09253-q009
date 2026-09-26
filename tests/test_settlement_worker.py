"""结算窗口后台任务：超时推进、重启恢复与失败补偿。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import update

from app.models import SettlementTask
from app.settlement import service as ss, worker
from tests.conftest import TestSessionLocal
from tests.settlement_helpers import FIXED_NOW, create_plan, freeze_clock


def _window_status():
    db = TestSessionLocal()
    try:
        return ss.window_status(db, plan_version="P-W1", window_id="SW-1")
    finally:
        db.close()


def test_timeout_does_not_fire_before_deadline_then_forces_advance(client, db):
    clock = freeze_clock()
    create_plan(client, plan_version="P-W1")
    client.post(
        "/api/plans/P-W1/settlement-windows",
        json={
            "window_id": "SW-1",
            "created_by": "r",
            "deadlines": {"data_cutoff": "2026-09-01T10:00:00+00:00"},
        },
    )
    wdb = TestSessionLocal()
    assert worker.process_once(wdb) == {"advanced": 0, "noop": 0, "failed": 0}
    wdb.close()
    db.expire_all()
    assert _window_status()["current_stage"] == "data_cutoff"

    clock["now"] = FIXED_NOW.replace(hour=10, minute=1)
    wdb = TestSessionLocal()
    result = worker.process_once(wdb)
    wdb.close()
    assert result["advanced"] == 1

    db.expire_all()
    body = _window_status()
    assert body["current_stage"] == "exception_list"
    assert body["stages"][0]["timed_out"] is True
    forced = [a for a in body["audit"] if a["action"] == "advance"]
    assert forced[0]["forced"] is True
    assert forced[0]["actor_role"] == "scheduler"
    # 旧任务已终结，新阶段有自己的任务
    by_stage = {t["stage"]: t["status"] for t in body["tasks"]}
    assert by_stage["data_cutoff"] == "completed"
    assert by_stage["exception_list"] == "pending"


def test_scheduler_chains_all_stages_to_frozen(client, db):
    clock = freeze_clock()
    create_plan(client, plan_version="P-W1")
    client.post(
        "/api/plans/P-W1/settlement-windows",
        json={"window_id": "SW-1", "created_by": "r"},
    )
    # 默认每阶段 1-3 天；每轮跳足够长时间，最终到达 frozen
    for _ in range(6):
        clock["now"] += timedelta(days=4)
        wdb = TestSessionLocal()
        worker.process_once(wdb)
        wdb.close()
    db.expire_all()
    body = _window_status()
    assert body["current_stage"] == "frozen"
    assert body["state"] == "completed"
    assert {t["status"] for t in body["tasks"]} == {"completed"}
    # 超时推进也产出了正式冻结材料与独立 freeze_id
    assert body["current_materials"]["frozen"]["freeze_id"] == "SW-1-r1"


def test_stale_lease_is_recovered_after_restart(client, db):
    clock = freeze_clock()
    create_plan(client, plan_version="P-W1")
    client.post(
        "/api/plans/P-W1/settlement-windows",
        json={
            "window_id": "SW-1",
            "created_by": "r",
            "deadlines": {"data_cutoff": "2026-09-01T10:00:00+00:00"},
        },
    )
    # 模拟 worker 崩溃：任务被 leased 且租约时间停在很久以前
    body = _window_status()
    task_id = body["stages"][0]["task_id"]
    wdb = TestSessionLocal()
    wdb.execute(
        update(SettlementTask)
        .where(SettlementTask.id == task_id)
        .values(
            status="leased",
            leased_by="crashed-worker",
            leased_at=FIXED_NOW - timedelta(hours=2),
        )
    )
    wdb.commit()
    wdb.close()

    # “重启”：全新会话执行一轮恢复，期限未到时不抢，到期后回收推进
    clock["now"] = FIXED_NOW.replace(hour=10, minute=1)
    restart_db = TestSessionLocal()
    result = worker.process_once(restart_db, worker_id="restarted-worker")
    restart_db.close()
    assert result["advanced"] == 1
    db.expire_all()
    assert _window_status()["current_stage"] == "exception_list"


def test_failed_task_retries_with_backoff_then_dead_letters(client, db, monkeypatch):
    clock = freeze_clock()
    create_plan(client, plan_version="P-W1")
    client.post(
        "/api/plans/P-W1/settlement-windows",
        json={
            "window_id": "SW-1",
            "created_by": "r",
            "deadlines": {"data_cutoff": "2026-09-01T10:00:00+00:00"},
        },
    )
    calls = {"n": 0}

    def always_fail(*a, **k):
        calls["n"] += 1
        raise RuntimeError("settlement backend unavailable")

    monkeypatch.setattr(worker.service, "advance_window", always_fail)

    # 退避序列：5s、10s、20s、40s……逐次执行直到达到最大尝试次数
    moment = FIXED_NOW.replace(hour=10, minute=1)
    clock["now"] = moment
    for _ in range(worker.MAX_ATTEMPTS + 1):
        wdb = TestSessionLocal()
        worker.process_once(wdb)
        wdb.close()
        moment += timedelta(minutes=1)  # 总比退避久
        clock["now"] = moment

    db.expire_all()
    body = _window_status()
    failed = [t for t in body["tasks"] if t["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["attempts"] == worker.MAX_ATTEMPTS
    assert "backend unavailable" in (failed[0]["last_error"] or "")
    # 窗口没有被错误推进
    assert body["current_stage"] == "data_cutoff"


def test_transient_failure_is_compensated_by_later_retry(client, db, monkeypatch):
    clock = freeze_clock()
    create_plan(client, plan_version="P-W1")
    client.post(
        "/api/plans/P-W1/settlement-windows",
        json={
            "window_id": "SW-1",
            "created_by": "r",
            "deadlines": {"data_cutoff": "2026-09-01T10:00:00+00:00"},
        },
    )
    state = {"fail": True}
    original = ss.advance_window

    def flaky(session, **kwargs):
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("temporary blip")
        return original(session, **kwargs)

    monkeypatch.setattr(worker.service, "advance_window", flaky)

    clock["now"] = FIXED_NOW.replace(hour=10, minute=1)
    wdb = TestSessionLocal()
    worker.process_once(wdb)  # 首次失败，退避重排
    wdb.close()
    db.expire_all()
    body = _window_status()
    assert body["current_stage"] == "data_cutoff"
    retried = [t for t in body["tasks"] if t["attempts"] == 1]
    assert len(retried) == 1 and retried[0]["status"] == "pending"

    clock["now"] += timedelta(minutes=1)
    wdb = TestSessionLocal()
    worker.process_once(wdb)  # 退避后重试成功
    wdb.close()
    db.expire_all()
    assert _window_status()["current_stage"] == "exception_list"


def test_task_for_superseded_stage_is_completed_as_stale(client, db):
    clock = freeze_clock()
    create_plan(client, plan_version="P-W1")
    client.post(
        "/api/plans/P-W1/settlement-windows",
        json={
            "window_id": "SW-1",
            "created_by": "r",
            "deadlines": {"data_cutoff": "2026-09-01T10:00:00+00:00"},
        },
    )
    # 人工先推进到 exception_list
    client.post(
        "/api/plans/P-W1/settlement-windows/SW-1/advance",
        json={"actor_id": "dc", "actor_role": "data_clerk", "note": "manual"},
    )
    db.expire_all()
    # 模拟一条漏过取消的旧阶段任务（如旧版本遗留/取消丢失）重新到期
    body = _window_status()
    old_task_id = next(
        t["id"] for t in body["tasks"] if t["stage"] == "data_cutoff"
    )
    wdb = TestSessionLocal()
    wdb.execute(
        update(SettlementTask)
        .where(SettlementTask.id == old_task_id)
        .values(status="pending", run_at=FIXED_NOW.replace(hour=9))
    )
    wdb.commit()
    wdb.close()
    clock["now"] = FIXED_NOW.replace(hour=10, minute=1)
    wdb = TestSessionLocal()
    result = worker.process_once(wdb)
    wdb.close()
    assert result["advanced"] == 0 and result["noop"] == 1
    db.expire_all()
    body = _window_status()
    assert body["current_stage"] == "exception_list"
    assert body["stages"][0]["timed_out"] is False


def test_resume_reinstates_timeout_which_then_fires(client, db):
    clock = freeze_clock()
    create_plan(client, plan_version="P-W1")
    client.post(
        "/api/plans/P-W1/settlement-windows",
        json={
            "window_id": "SW-1",
            "created_by": "r",
            "deadlines": {"data_cutoff": "2026-09-01T10:00:00+00:00"},
        },
    )
    client.post(
        "/api/plans/P-W1/settlement-windows/SW-1/pause",
        json={"actor_id": "r", "actor_role": "registrar", "reason": "hold"},
    )
    # 越过原期限后才恢复：恢复时任务立即到期
    clock["now"] = FIXED_NOW.replace(hour=12)
    resp = client.post(
        "/api/plans/P-W1/settlement-windows/SW-1/resume",
        json={"actor_id": "r", "actor_role": "registrar", "reason": "continue"},
    )
    assert resp.status_code == 200
    db.expire_all()
    wdb = TestSessionLocal()
    result = worker.process_once(wdb)
    wdb.close()
    assert result["advanced"] == 1
    db.expire_all()
    assert _window_status()["current_stage"] == "exception_list"
