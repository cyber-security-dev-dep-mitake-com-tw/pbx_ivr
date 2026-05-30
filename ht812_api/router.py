import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

import structlog
from ht812_client import HT812AuthError, HT812Client, HT812Error
from metrics import (
    BACKUP_FILE_COUNT,
    REQUEST_COUNT,
    REQUEST_LATENCY,
    record_login_failure,
)
from models import (
    ActionResponse,
    BackupFile,
    BackupListResponse,
    GetValuesResponse,
    PatchConfigRequest,
    PortStatusResponse,
    SystemInfoResponse,
)

log = structlog.get_logger()
router = APIRouter(prefix="/ht812", tags=["HT812V2"])

_BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backups"))


def _client(request: Request) -> HT812Client:
    return request.app.state.ht812


def _handle(e: Exception) -> HTTPException:
    if isinstance(e, HT812AuthError):
        record_login_failure()
        log.warning("ht812_auth_error", error=str(e))
        return HTTPException(401, str(e))
    log.error("ht812_error", error=str(e))
    return HTTPException(502, str(e))


# ------------------------------------------------------------------ config

@router.get(
    "/config",
    response_class=Response,
    responses={200: {"content": {"application/xml": {}}}},
    summary="Export full config as XML (also saves timestamped backup)",
)
async def get_config(request: Request):
    REQUEST_COUNT.labels(endpoint="get_config").inc()
    with REQUEST_LATENCY.labels(endpoint="get_config").time():
        try:
            xml = await _client(request).get_config_xml()
        except HT812Error as e:
            raise _handle(e)
    _update_backup_gauge()
    log.info("config_backup_saved")
    return Response(content=xml, media_type="application/xml")


@router.get(
    "/backups",
    response_model=BackupListResponse,
    summary="List all saved config backup files",
)
async def list_backups():
    REQUEST_COUNT.labels(endpoint="list_backups").inc()
    files = sorted(_BACKUP_DIR.glob("ht812_config_*.xml"), key=lambda p: p.stat().st_mtime, reverse=True)
    items = []
    for f in files:
        stat = f.stat()
        items.append(BackupFile(
            filename=f.name,
            size_bytes=stat.st_size,
            created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            path=str(f),
        ))
    return BackupListResponse(count=len(items), backups=items)


@router.get(
    "/values",
    response_model=GetValuesResponse,
    summary="Read specific P-value settings",
)
async def get_values(
    request: Request,
    keys: str = Query(..., description="Comma-separated P-value keys, e.g. P47,P48,P52"),
):
    REQUEST_COUNT.labels(endpoint="get_values").inc()
    try:
        values = await _client(request).get_values(keys.split(","))
    except HT812Error as e:
        raise _handle(e)
    return GetValuesResponse(values=values)


@router.patch(
    "/config",
    response_model=ActionResponse,
    summary="Write P-value settings (apply=true commits immediately)",
)
async def patch_config(
    request: Request,
    body: PatchConfigRequest,
    apply: bool = Query(True, description="Apply immediately vs stage only"),
):
    REQUEST_COUNT.labels(endpoint="patch_config").inc()
    with REQUEST_LATENCY.labels(endpoint="patch_config").time():
        try:
            ok = await _client(request).patch_config(body.params, apply=apply)
        except HT812Error as e:
            raise _handle(e)
    log.info("config_patched", params=list(body.params.keys()), apply=apply, success=ok)
    return ActionResponse(success=ok, message="Config updated" if ok else "Non-success response from device")


# ------------------------------------------------------------------ actions

@router.post("/reboot", response_model=ActionResponse, summary="Reboot the device (~30s downtime)")
async def reboot(request: Request):
    REQUEST_COUNT.labels(endpoint="reboot").inc()
    try:
        ok = await _client(request).reboot()
    except HT812Error as e:
        raise _handle(e)
    log.warning("device_reboot_triggered", success=ok)
    return ActionResponse(success=ok, message="Reboot initiated" if ok else "Non-success response from device")


@router.post(
    "/factory-reset",
    response_model=ActionResponse,
    summary="Factory reset (reset_type: 0=ISP, 1=VoIP, 2=full)",
)
async def factory_reset(
    request: Request,
    reset_type: str = Query("2", description="0=ISP data, 1=VoIP data, 2=full factory reset"),
):
    if reset_type not in ("0", "1", "2"):
        raise HTTPException(400, "reset_type must be 0, 1, or 2")
    REQUEST_COUNT.labels(endpoint="factory_reset").inc()
    try:
        ok = await _client(request).factory_reset(reset_type)
    except HT812Error as e:
        raise _handle(e)
    log.warning("device_factory_reset_triggered", reset_type=reset_type, success=ok)
    return ActionResponse(success=ok, message="Factory reset initiated" if ok else "Non-success response from device")


# ------------------------------------------------------------------ status

@router.get("/status/ports", summary="FXS port SIP registration and hook status")
async def port_status(request: Request):
    REQUEST_COUNT.labels(endpoint="port_status").inc()
    try:
        data = await _client(request).get_port_status()
    except HT812Error as e:
        raise _handle(e)
    return data


@router.get("/status/system", summary="Product, vendor info from device")
async def system_info(request: Request):
    REQUEST_COUNT.labels(endpoint="system_info").inc()
    try:
        data = await _client(request).get_system_info()
    except HT812Error as e:
        raise _handle(e)
    return data


@router.get("/status/network", summary="Network P-values: IP, subnet, gateway")
async def net_status(request: Request):
    REQUEST_COUNT.labels(endpoint="net_status").inc()
    try:
        data = await _client(request).get_net_status()
    except HT812Error as e:
        raise _handle(e)
    return data


@router.get(
    "/status/sip-log",
    response_class=Response,
    responses={200: {"content": {"application/json": {}}}},
    summary="Live SIP trace log from the device",
)
async def sip_log(request: Request):
    REQUEST_COUNT.labels(endpoint="sip_log").inc()
    try:
        text = await _client(request).get_sip_log()
    except HT812Error as e:
        raise _handle(e)
    return Response(content=text, media_type="application/json")


# ------------------------------------------------------------------ helpers

def _update_backup_gauge() -> None:
    try:
        count = len(list(_BACKUP_DIR.glob("ht812_config_*.xml")))
        BACKUP_FILE_COUNT.set(count)
    except Exception:
        pass
