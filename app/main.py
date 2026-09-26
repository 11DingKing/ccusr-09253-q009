"""服务端业务模块。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import SessionLocal, engine
from .models import Base
from .routers import router
from .settlement import worker
from .settlement.router import router as settlement_router

logger = logging.getLogger("settlement")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 幂等建表；结算任务表也在此就绪，重启后可恢复在途超时任务。
    Base.metadata.create_all(engine)
    worker.configure(session_factory=SessionLocal)
    worker.start_scheduler()
    try:
        recovered = worker.recover_on_startup()
        if any(recovered.values()):
            logger.info("settlement startup recovery: %s", recovered)
    except Exception:  # pragma: no cover - 恢复失败不应阻断启动
        logger.exception("settlement startup recovery failed")
    yield
    worker.stop_scheduler()


app = FastAPI(
    title="Practice Hours Guard",
    version="0.1.0",
    description=(
        "Event-sourced practice-hours compliance service. Check-ins, mentor "
        "confirmations and leave corrections are append-only; compliance is "
        "derived by replay and can be frozen into an immutable snapshot. "
        "Term settlement runs through a controlled, revisioned close window: "
        "data cutoff, exception list, grace entry, review sign-off and freeze."
    ),
    lifespan=lifespan,
)

app.include_router(router)
app.include_router(settlement_router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
