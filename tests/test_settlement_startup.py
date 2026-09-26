"""进程重启后通过 lifespan 恢复在途超时任务的集成测试。"""

from __future__ import annotations

from datetime import timedelta

from collections.abc import Iterator

from fastapi.testclient import TestClient
from sqlalchemy import update

from app.db import get_db
from app.main import app
from app.models import SettlementTask
from tests.conftest import TestSessionLocal
from tests.settlement_helpers import FIXED_NOW, create_plan


def _override_db() -> None:
    def _gen() -> Iterator:
        session = TestSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _gen


def test_lifespan_startup_recovers_due_timeout():
    from app.settlement.clock import set_clock

    _override_db()
    set_clock(lambda: FIXED_NOW)
    # 用同一文件测试库，但通过新的 TestClient 上下文模拟“重启”。
    with TestClient(app) as client:
        create_plan(client, plan_version="P-RESTART")
        client.post(
            "/api/plans/P-RESTART/settlement-windows",
            json={
                "window_id": "SW-1",
                "created_by": "r",
                "deadlines": {"data_cutoff": "2026-09-01T10:00:00+00:00"},
            },
        )

    # 服务“停止”期间时间越过期限
    set_clock(lambda: FIXED_NOW.replace(hour=11))

    # 重启：lifespan 的 recover_on_startup 应直接强制推进
    with TestClient(app) as client:
        body = client.get(
            "/api/plans/P-RESTART/settlement-windows/SW-1"
        ).json()
        assert body["current_stage"] == "exception_list"
        assert body["stages"][0]["timed_out"] is True
    app.dependency_overrides.pop(get_db, None)


def test_lifespan_startup_recovers_crashed_lease():
    from app.settlement.clock import set_clock

    _override_db()
    set_clock(lambda: FIXED_NOW)
    with TestClient(app) as client:
        create_plan(client, plan_version="P-LEASE")
        client.post(
            "/api/plans/P-LEASE/settlement-windows",
            json={
                "window_id": "SW-1",
                "created_by": "r",
                "deadlines": {"data_cutoff": "2026-09-01T10:00:00+00:00"},
            },
        )
        body = client.get(
            "/api/plans/P-LEASE/settlement-windows/SW-1"
        ).json()
        task_id = body["stages"][0]["task_id"]

    # 模拟崩溃：任务停在 leased，租约远早于当前时间
    wdb = TestSessionLocal()
    wdb.execute(
        update(SettlementTask)
        .where(SettlementTask.id == task_id)
        .values(
            status="leased",
            leased_by="crashed",
            leased_at=FIXED_NOW - timedelta(hours=3),
            run_at=FIXED_NOW.replace(hour=10),
        )
    )
    wdb.commit()
    wdb.close()

    set_clock(lambda: FIXED_NOW.replace(hour=11))
    with TestClient(app) as client:
        body = client.get(
            "/api/plans/P-LEASE/settlement-windows/SW-1"
        ).json()
        assert body["current_stage"] == "exception_list"
        completed = [
            t
            for t in body["tasks"]
            if t["stage"] == "data_cutoff" and t["status"] == "completed"
        ]
        assert completed
    app.dependency_overrides.pop(get_db, None)
