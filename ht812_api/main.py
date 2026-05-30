import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ht812_client import HT812Client
from metrics import BACKUP_FILE_COUNT
from router import router

# ------------------------------------------------------------------ logging

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

log = structlog.get_logger()

# ------------------------------------------------------------------ scheduler

scheduler = AsyncIOScheduler()
_BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "30"))


async def _scheduled_backup(app: FastAPI) -> None:
    try:
        await app.state.ht812.get_config_xml(keep_last=_BACKUP_KEEP)
        log.info("scheduled_backup_completed")
    except Exception as e:
        log.error("scheduled_backup_failed", error=str(e))


# ------------------------------------------------------------------ app

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ht812 = HT812Client()

    # Initialize backup file count gauge
    backup_dir = Path(os.environ.get("BACKUP_DIR", "/backups"))
    BACKUP_FILE_COUNT.set(len(list(backup_dir.glob("ht812_config_*.xml"))))

    scheduler.add_job(
        _scheduled_backup,
        "interval",
        hours=24,
        args=[app],
        id="daily_backup",
        replace_existing=True,
    )
    scheduler.start()
    log.info("startup_complete", backup_interval_hours=24, backup_keep=_BACKUP_KEEP)

    yield

    scheduler.shutdown(wait=False)
    await app.state.ht812.aclose()
    log.info("shutdown_complete")


app = FastAPI(
    title="HT812V2 Control API",
    description=(
        "Full control API for the Grandstream HT812V2 ATA: "
        "config snapshot/patch, reboot, factory reset, SIP status, observability."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/health", tags=["Ops"])
async def health():
    return {"status": "ok"}


@app.get("/metrics", tags=["Ops"], response_class=Response, include_in_schema=False)
async def metrics():
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
