import logging
import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from events import EventStore
from events_router import router as events_router
from fxs_poller import FXSPoller
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
    app.state.events = EventStore()
    app.state.event_queue = asyncio.Queue()

    app.state.fxs_poller = FXSPoller(app)
    app.state.fxs_poller.start()

    # Initialize backup file count gauge
    default_backup_dir = Path(__file__).resolve().parent.parent / "backups"
    backup_dir = Path(os.environ.get("BACKUP_DIR", str(default_backup_dir)))
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

    app.state.fxs_poller.stop()
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
app.include_router(events_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(","),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["Ops"])
async def health():
    return {"status": "ok"}


@app.get("/metrics", tags=["Ops"], response_class=Response, include_in_schema=False)
async def metrics():
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
