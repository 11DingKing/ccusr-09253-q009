"""服务端业务模块。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import engine
from .models import Base
from .routers import router
from .settlement_router import router as settlement_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> Iterator[None]:
    # 项目当前未接入迁移脚本，启动时保证表存在（CREATE TABLE IF NOT EXISTS）。
    Base.metadata.create_all(engine)
    worker = None
    if os.getenv("SETTLEMENT_WORKER_ENABLED", "1") == "1":
        from .settlement.worker import start_default_worker

        worker = start_default_worker()
    try:
        yield
    finally:
        if worker is not None:
            from .settlement.worker import stop_default_worker

            stop_default_worker()


app = FastAPI(
    title="Practice Hours Guard",
    version="0.1.0",
    description=(
        "Event-sourced practice-hours compliance service. Check-ins, mentor "
        "confirmations and leave corrections are append-only; compliance is "
        "derived by replay and can be frozen into an immutable snapshot. "
        "Settlement windows add a controlled five-stage close process "
        "(data cutoff, anomaly review, grace period, review sign-off, freeze) "
        "with owners, deadlines and versioned, cross-role-approved reopening."
    ),
    lifespan=lifespan,
)

app.include_router(router)
app.include_router(settlement_router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
