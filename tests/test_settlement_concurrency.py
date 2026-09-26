"""结算窗口并发推进/暂停/重开测试。"""

from __future__ import annotations

import threading
from app.settlement import service as ss
from app.settlement.errors import RevisionConflictError
from tests.conftest import TestSessionLocal
from tests.settlement_helpers import FIXED_NOW, freeze_clock


def _seed_plan(required: int = 3600, tz: str = "Asia/Shanghai"):
    db = TestSessionLocal()
    from app import services

    services.ensure_plan(
        db,
        plan_version="P-CONC",
        iana_timezone=tz,
        required_seconds=required,
    )
    db.commit()
    db.close()


def _start_window(window_id: str = "SW-C"):
    db = TestSessionLocal()
    ss.start_window(db, plan_version="P-CONC", window_id=window_id, created_by="r")
    db.close()


def _status(window_id: str = "SW-C"):
    db = TestSessionLocal()
    try:
        return ss.window_status(db, plan_version="P-CONC", window_id=window_id)
    finally:
        db.close()


def test_concurrent_advance_only_one_wins():
    freeze_clock()
    _seed_plan()
    _start_window()
    barrier = threading.Barrier(4)
    outcomes: list[str] = []
    lock = threading.Lock()

    def _advance() -> None:
        session = TestSessionLocal()
        barrier.wait()
        try:
            ss.advance_window(
                session,
                plan_version="P-CONC",
                window_id="SW-C",
                actor_id="dc-1",
                actor_role="data_clerk",
            )
            with lock:
                outcomes.append("ok")
        except RevisionConflictError:
            with lock:
                outcomes.append("conflict")
        except Exception as exc:  # pragma: no cover - 只允许上述两类结果
            with lock:
                outcomes.append(f"error:{exc!r}")
        finally:
            session.close()

    threads = [threading.Thread(target=_advance) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes).count("ok") == 1
    assert sorted(outcomes).count("conflict") == 3
    body = _status()
    # 只前进了一个阶段，行版本只增加一次
    assert body["current_stage"] == "exception_list"
    assert body["revision"] == 1


def test_concurrent_pause_and_advance_leave_consistent_state():
    freeze_clock()
    _seed_plan()
    _start_window()
    barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def _pause() -> None:
        session = TestSessionLocal()
        barrier.wait()
        try:
            ss.pause_window(
                session,
                plan_version="P-CONC",
                window_id="SW-C",
                actor_id="r",
                actor_role="registrar",
                reason="hold",
            )
            tag = "paused"
        except RevisionConflictError:
            tag = "conflict"
        with lock:
            results.append(tag)
        session.close()

    def _advance() -> None:
        session = TestSessionLocal()
        barrier.wait()
        try:
            ss.advance_window(
                session,
                plan_version="P-CONC",
                window_id="SW-C",
                actor_id="dc-1",
                actor_role="data_clerk",
            )
            tag = "advanced"
        except RevisionConflictError:
            tag = "conflict"
        with lock:
            results.append(tag)
        session.close()

    threads = [threading.Thread(target=_pause), threading.Thread(target=_advance)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(results) != {"conflict", "conflict"}
    body = _status()
    # 状态要么暂停在 data_cutoff，要么活跃在 exception_list，绝不二者兼有
    if body["state"] == "paused":
        assert body["current_stage"] == "data_cutoff"
    else:
        assert body["state"] == "active"
        assert body["current_stage"] == "exception_list"


def test_concurrent_reopen_creates_only_one_new_revision():
    freeze_clock()
    _seed_plan()
    _start_window()
    db = TestSessionLocal()
    for role in ["data_clerk", "compliance_officer", "mentor_coordinator", "dean_reviewer"]:
        ss.advance_window(
            db,
            plan_version="P-CONC",
            window_id="SW-C",
            actor_id=f"{role}-1",
            actor_role=role,
        )
    db.close()

    # 冻结后补一条事件
    db = TestSessionLocal()
    from app import services

    services.import_events(
        db,
        plan_version="P-CONC",
        events=[
            {
                "event_id": "E-99",
                "event_type": "leave_correction",
                "student_id": "S1",
                "payload": {"adjustment_seconds": 600, "reason": "makeup"},
            }
        ],
    )
    db.close()

    freeze_clock(FIXED_NOW.replace(day=2))
    barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def _reopen(actor: str) -> None:
        session = TestSessionLocal()
        barrier.wait()
        try:
            ss.reopen_window(
                session,
                plan_version="P-CONC",
                window_id="SW-C",
                actor_id=actor,
                actor_role="provost",
                reason="late correction",
            )
            tag = "ok"
        except RevisionConflictError:
            tag = "conflict"
        with lock:
            results.append(tag)
        session.close()

    threads = [
        threading.Thread(target=_reopen, args=("provost-a",)),
        threading.Thread(target=_reopen, args=("provost-b",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results).count("ok") == 1
    assert sorted(results).count("conflict") == 1
    body = _status()
    assert body["revision"] == 2
    statuses = {(r["revision"], r["status"]) for r in body["revisions"]}
    assert statuses == {(1, "sealed"), (2, "open")}


def test_advance_after_optimistic_conflict_can_still_succeed_on_retry():
    freeze_clock()
    _seed_plan()
    _start_window()

    # 模拟先被并发方抢先推进：用旧 row_version 推进必败，用最新状态重试成功。
    db = TestSessionLocal()
    ss.advance_window(
        db,
        plan_version="P-CONC",
        window_id="SW-C",
        actor_id="dc-1",
        actor_role="data_clerk",
    )
    db.close()

    db = TestSessionLocal()
    # 当前阶段已是 exception_list，由 compliance_officer 继续推进
    body = ss.advance_window(
        db,
        plan_version="P-CONC",
        window_id="SW-C",
        actor_id="co-1",
        actor_role="compliance_officer",
    )
    db.close()
    assert body["current_stage"] == "grace_entry"
    assert _status()["current_stage"] == "grace_entry"
