import logging
import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from dtmf_router import router as dtmf_router
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
        await app.state.ht812.save_config_snapshot(keep_last=_BACKUP_KEEP)
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
app.include_router(dtmf_router)

_CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001,http://localhost:3002,http://127.0.0.1:3002",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: FastAPIRequest, exc: Exception) -> JSONResponse:
    """Return JSON 502 with CORS headers so browser clients see the actual error."""
    origin = request.headers.get("origin", "")
    cors_header = origin if origin in _CORS_ORIGINS else (_CORS_ORIGINS[0] if _CORS_ORIGINS else "*")
    log.error("unhandled_exception", error=str(exc), path=request.url.path)
    return JSONResponse(
        status_code=502,
        content={"detail": str(exc)},
        headers={"Access-Control-Allow-Origin": cors_header},
    )


@app.get("/health", tags=["Ops"])
async def health():
    return {"status": "ok"}


@app.get("/metrics", tags=["Ops"], response_class=Response, include_in_schema=False)
async def metrics():
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
