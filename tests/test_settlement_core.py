"""关账窗口核心行为：并发推进、跨时区截止、超时任务重启恢复与失败补偿。"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app import settlement
from app.models import SettlementTaskState as TaskModel
from app.settlement import stages as stage_defs
from app.settlement.stages import Stage, local_midnight_deadline
from tests.conftest import NY_PLAN, SHANGHAI_PLAN, TestSessionLocal


def _ensure_plan(db, plan=SHANGHAI_PLAN):
    from app import services

    services.ensure_plan(
        db,
        plan_version=plan["plan_version"],
        iana_timezone=plan["iana_timezone"],
        required_seconds=plan["required_seconds"],
    )
    return plan["plan_version"]


def _open(db, pv="P-SH-2024", window_id="SW-C", now=None, durations=None):
    return settlement.open_window(
        db,
        window_id=window_id,
        plan_version=pv,
        actor_id="steward-1",
        note="open",
        stage_durations_hours=durations,
        now=now,
    )


# ---------------------------------------------------------------------------
# 并发推进
# ---------------------------------------------------------------------------


def test_concurrent_advance_only_one_succeeds(db):
    pv = _ensure_plan(db)
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    _open(db, pv, now=t0)

    results: list[str] = []

    def _advance():
        session = TestSessionLocal()
        try:
            settlement.advance_window(
                session,
                window_id="SW-C",
                actor_id="steward-1",
                actor_role="data_steward",
                note="concurrent cutoff",
                now=t0 + timedelta(minutes=5),
            )
            results.append("ok")
        except settlement.SettlementError:
            results.append("conflict")
        finally:
            session.close()

    threads = [threading.Thread(target=_advance) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["conflict", "conflict", "conflict", "ok"]
    w = settlement.get_window(db, "SW-C")
    assert w["current_stage"] == "anomaly_review"
    # 只有一条 advance 审计。
    advances = [a for a in w["audit"] if a["action"] == "advance"]
    assert len(advances) == 1


def test_concurrent_freeze_never_overwrites_material(db):
    pv = _ensure_plan(db)
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    _open(db, pv, now=t0)
    # 单线程把窗口推进到签署阶段（异常强制放行）。
    role_note = [
        ("steward-1", "data_steward"),
        ("auditor-1", "compliance_auditor"),
        ("coord-1", "program_coordinator"),
    ]
    for i, (actor, role) in enumerate(role_note):
        settlement.advance_window(
            db,
            window_id="SW-C",
            actor_id=actor,
            actor_role=role,
            note=f"step {i}",
            force=True,
            now=t0 + timedelta(hours=i + 1),
        )

    outcomes: list[str] = []

    def _freeze():
        session = TestSessionLocal()
        try:
            settlement.advance_window(
                session,
                window_id="SW-C",
                actor_id="dean-1",
                actor_role="dean",
                note="sign concurrently",
                now=t0 + timedelta(hours=10),
            )
            outcomes.append("ok")
        except settlement.SettlementError:
            outcomes.append("conflict")
        finally:
            session.close()

    threads = [threading.Thread(target=_freeze) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    w = settlement.get_window(db, "SW-C")
    assert w["status"] == "frozen"
    assert w["freeze_id"] == "SW-C-r1"


def test_concurrent_task_claim_is_mutually_exclusive(db):
    pv = _ensure_plan(db)
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    _open(db, pv, now=t0)
    # data_cutoff 期限已到。
    claimed_by: list[str] = []
    barrier = threading.Barrier(4)

    def _claim(worker: str):
        session = TestSessionLocal()
        try:
            barrier.wait()
            tasks = settlement.claim_due_tasks(
                session, worker_id=worker, now=t0 + timedelta(hours=48)
            )
            claimed_by.extend(worker for _t in tasks)
        finally:
            session.close()

    threads = [threading.Thread(target=_claim, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 每个 revision 只有一条超时任务，只能被一个 worker 拿到。
    assert len(claimed_by) == 1


# ---------------------------------------------------------------------------
# 跨时区截止
# ---------------------------------------------------------------------------


def test_local_calendar_deadline_converts_to_utc_across_zones(db):
    pv_sh = _ensure_plan(db, SHANGHAI_PLAN)
    w = settlement.open_window(
        db,
        window_id="SW-SH",
        plan_version=pv_sh,
        actor_id="s",
        note="n",
        now=datetime(2026, 9, 29, tzinfo=timezone.utc),
        stage_deadlines={
            Stage.DATA_CUTOFF: local_midnight_deadline(
                date(2026, 10, 1), "Asia/Shanghai"
            )
        },
    )
    cutoff = next(s for s in w["stages"] if s["stage"] == "data_cutoff")
    # 上海 2026-10-01 00:00 (UTC+8) == 2026-09-30 16:00Z。
    assert cutoff["deadline_utc"] == "2026-09-30T16:00:00Z"

    pv_ny = _ensure_plan(db, NY_PLAN)

    def _ny_deadline(day):
        return settlement.open_window(
            db,
            window_id=f"SW-NY-{day}",
            plan_version=pv_ny,
            actor_id="s",
            note="n",
            now=datetime(2024, 10, 31, tzinfo=timezone.utc),
            stage_deadlines={
                Stage.DATA_CUTOFF: local_midnight_deadline(date(2024, 11, day), "America/New_York")
            },
        )

    # DST 回拨发生在本地 11-03 02:00：当日 0 点仍是 EDT(UTC-4)=04:00Z；
    # 次日 0 点已切到 EST(UTC-5)=05:00Z，跨日的本地同一时刻相差一小时。
    d1 = next(
        s for s in _ny_deadline(3)["stages"] if s["stage"] == "data_cutoff"
    )
    d2 = next(
        s for s in _ny_deadline(4)["stages"] if s["stage"] == "data_cutoff"
    )
    assert d1["deadline_utc"] == "2024-11-03T04:00:00Z"
    assert d2["deadline_utc"] == "2024-11-04T05:00:00Z"


def test_timeout_fires_just_after_deadline_regardless_of_zone_offset(db):
    pv = _ensure_plan(db, SHANGHAI_PLAN)
    deadline = datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc)  # 上海 10-01 00:00
    settlement.open_window(
        db,
        window_id="SW-TZ",
        plan_version=pv,
        actor_id="s",
        note="n",
        stage_deadlines={Stage.DATA_CUTOFF: deadline},
    )

    # 截止前一分钟：不触发。
    before = settlement.run_due_timeouts(
        db, worker_id="w", now=deadline - timedelta(minutes=1)
    )
    assert before == {"processed": 0, "failed": 0, "dead_lettered": 0}

    # 到期那一刻：登记 stage_timeout 审计。
    after = settlement.run_due_timeouts(db, worker_id="w", now=deadline)
    assert after["processed"] == 1
    w = settlement.get_window(db, "SW-TZ")
    timeouts = [a for a in w["audit"] if a["action"] == "stage_timeout"]
    assert len(timeouts) == 1
    assert timeouts[0]["detail"]["deadline_utc"] == "2026-09-30T16:00:00Z"

    # 幂等：再次扫描不会重复登记。
    again = settlement.run_due_timeouts(db, worker_id="w", now=deadline + timedelta(hours=1))
    assert again["processed"] == 0


def test_paused_window_timeout_is_cancelled_and_resume_rearms(db):
    pv = _ensure_plan(db)
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    w = _open(db, pv, window_id="SW-P", now=t0, durations={"data_cutoff": 24})
    original_deadline = next(
        s for s in w["stages"] if s["stage"] == "data_cutoff"
    )["deadline_utc"]

    settlement.pause_window(
        db,
        window_id="SW-P",
        actor_id="steward-1",
        actor_role="data_steward",
        note="hold",
        now=t0 + timedelta(hours=2),
    )
    # 即使超过原期限，暂停期间不触发超时。
    settlement.run_due_timeouts(db, worker_id="w", now=t0 + timedelta(hours=48))
    w = settlement.get_window(db, "SW-P")
    assert [a for a in w["audit"] if a["action"] == "stage_timeout"] == []

    settlement.resume_window(
        db,
        window_id="SW-P",
        actor_id="steward-1",
        actor_role="data_steward",
        note="back",
        now=t0 + timedelta(hours=10),
    )
    w = settlement.get_window(db, "SW-P")
    stage = next(s for s in w["stages"] if s["stage"] == "data_cutoff")
    # 暂停了 8 小时，剩余 22 小时顺延。
    assert stage["deadline_utc"] > original_deadline


# ---------------------------------------------------------------------------
# 失败补偿与重启恢复
# ---------------------------------------------------------------------------


def test_failed_timeout_retries_with_backoff_then_succeeds(db, monkeypatch):
    pv = _ensure_plan(db)
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    _open(db, pv, now=t0, durations={"data_cutoff": 1})

    # 注入失败：模拟 worker 处理时数据库/下游异常。
    calls = {"n": 0}
    real_load = settlement.service._load_stages

    def _flaky(session, window_id, revision):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient outage")
        return real_load(session, window_id, revision)

    monkeypatch.setattr(settlement.service, "_load_stages", _flaky)

    due = t0 + timedelta(hours=2)
    first = settlement.run_due_timeouts(db, worker_id="w1", now=due)
    assert first == {"processed": 0, "failed": 1, "dead_lettered": 0}

    task = db.execute(select(TaskModel)).scalars().one()
    assert task.attempts == 1
    assert task.status == "pending"
    assert task.run_at_utc > due  # 指数退避
    assert "simulated transient outage" in (task.last_error or "")

    # 退避窗口内不会被拾取。
    early = settlement.run_due_timeouts(db, worker_id="w1", now=due + timedelta(seconds=1))
    assert early["failed"] == 0 and early["processed"] == 0

    # 退避后重试成功（模拟进程重启：新 worker、无内存状态）。
    monkeypatch.undo()
    second = settlement.run_due_timeouts(
        db, worker_id="w2-after-restart", now=task.run_at_utc
    )
    assert second == {"processed": 1, "failed": 0, "dead_lettered": 0}
    db.expire_all()
    task = db.execute(select(TaskModel)).scalars().one()
    assert task.status == "done"


def test_repeated_failures_go_to_dead_letter(db, monkeypatch):
    pv = _ensure_plan(db)
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    _open(db, pv, now=t0, durations={"data_cutoff": 1})
    task = db.execute(select(TaskModel)).scalars().one()
    task.max_attempts = 2
    db.commit()

    def _boom(session, window_id, revision):
        raise RuntimeError("permanent outage")

    monkeypatch.setattr(settlement.service, "_load_stages", _boom)

    now = t0 + timedelta(hours=2)
    r1 = settlement.run_due_timeouts(db, worker_id="w", now=now)
    assert r1["failed"] == 1 and r1["dead_lettered"] == 0
    db.expire_all()
    task = db.execute(select(TaskModel)).scalars().one()
    now = task.run_at_utc
    r2 = settlement.run_due_timeouts(db, worker_id="w", now=now)
    assert r2["failed"] == 1 and r2["dead_lettered"] == 1
    db.expire_all()
    task = db.execute(select(TaskModel)).scalars().one()
    assert task.status == "dead_letter"
    assert task.completed_at_utc is not None


def test_stale_lease_from_crashed_worker_is_reclaimed_after_restart(db):
    pv = _ensure_plan(db)
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    _open(db, pv, now=t0, durations={"data_cutoff": 1})

    # worker A 认领任务后崩溃（锁仍在，租约 60 秒）。
    claimed_a = settlement.claim_due_tasks(
        db, worker_id="worker-A", now=t0 + timedelta(hours=2), lease_seconds=60
    )
    assert len(claimed_a) == 1

    # worker B 立即扫描：租约未过期，拿不到。
    claimed_b = settlement.claim_due_tasks(
        db, worker_id="worker-B",
        now=t0 + timedelta(hours=2, seconds=30),
        lease_seconds=60,
    )
    assert claimed_b == []

    # “重启”后租约已过期：worker B 接管。
    claimed_b2 = settlement.claim_due_tasks(
        db, worker_id="worker-B",
        now=t0 + timedelta(hours=2, minutes=2),
        lease_seconds=60,
    )
    assert len(claimed_b2) == 1
    assert claimed_b2[0].locked_by == "worker-B"


def test_worker_tick_processes_due_task_after_simulated_restart(db):
    """worker 无内存状态：重启后的新 worker 仅凭持久化任务即可继续处理。"""
    from app.settlement.worker import TimeoutWorker

    pv = _ensure_plan(db)
    # 窗口开在相对当前不久的将来，确保真实时钟的 tick 尚未到期。
    t0 = stage_defs.utc_now() + timedelta(days=10)
    _open(db, pv, window_id="SW-W", now=t0, durations={"data_cutoff": 1})

    # 第一个 worker 进程启动后立刻崩溃；重启为全新实例，无任何内存状态。
    TimeoutWorker(TestSessionLocal, worker_id="proc-A", poll_interval=999)
    worker_b = TimeoutWorker(TestSessionLocal, worker_id="proc-B", poll_interval=999)
    counts = worker_b.tick()  # 真实当前时刻：尚未到期。
    assert counts["processed"] == 0

    # 注入到期时刻（等价于时钟走到期限后重启的 worker 扫描）。
    due = settlement.run_due_timeouts(
        db, worker_id="proc-B", now=t0 + timedelta(hours=2)
    )
    assert due["processed"] == 1
    w = settlement.get_window(db, "SW-W")
    assert any(a["action"] == "stage_timeout" for a in w["audit"])


def test_superseded_revision_timeout_does_not_fire(db):
    pv = _ensure_plan(db)
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    _open(db, pv, now=t0, durations={"data_cutoff": 1})
    # 走完 r1 并立即重开（r1 冻结后 superseded）。
    for i, (actor, role) in enumerate(
        [
            ("steward-1", "data_steward"),
            ("auditor-1", "compliance_auditor"),
            ("coord-1", "program_coordinator"),
            ("dean-1", "dean"),
        ]
    ):
        settlement.advance_window(
            db,
            window_id="SW-C",
            actor_id=actor,
            actor_role=role,
            note=f"s{i}",
            force=True,
            now=t0 + timedelta(minutes=i + 1),
        )
    settlement.reopen_window(
        db,
        window_id="SW-C",
        actor_id="steward-1",
        actor_role="data_steward",
        approver_id="senate-1",
        approver_role="academic_senate",
        reason="late events",
        now=t0 + timedelta(hours=1),
    )
    # 对 r1 残留到期任务的扫描不应再产生 timeout 审计。
    settlement.run_due_timeouts(db, worker_id="w", now=t0 + timedelta(hours=48))
    r1 = settlement.get_window(db, "SW-C", revision=1)
    assert [a for a in r1["audit"] if a["action"] == "stage_timeout"] == []
