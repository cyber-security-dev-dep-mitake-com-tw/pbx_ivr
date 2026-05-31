import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

import structlog
from events import CommunicationEventIn
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
    ForceRegisterResponse,
    GetValuesResponse,
    PatchConfigRequest,
    PortStatusResponse,
    ProvisionLine,
    ProvisionTwoLineRequest,
    ProvisionTwoLineResponse,
    SystemInfoResponse,
)

log = structlog.get_logger()
router = APIRouter(prefix="/ht812", tags=["HT812V2"])

_DEFAULT_BACKUP_DIR = Path(__file__).resolve().parent.parent / "backups"
_BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", str(_DEFAULT_BACKUP_DIR)))
_BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "30"))
_DEFAULT_SIP_SERVER = os.environ.get("ASTERISK_SIP_HOST", "host.docker.internal")

_TRANSPORT_VALUES = {
    "udp": "0",
    "tcp": "1",
    "tls": "2",
}


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
            xml, _ = await _client(request).save_config_snapshot()
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


@router.post(
    "/snapshot-backup",
    response_model=BackupFile,
    summary="Create and save a timestamped config snapshot",
)
async def snapshot_backup(request: Request):
    REQUEST_COUNT.labels(endpoint="snapshot_backup").inc()
    with REQUEST_LATENCY.labels(endpoint="snapshot_backup").time():
        try:
            _xml, path = await _client(request).save_config_snapshot()
        except HT812Error as e:
            raise _handle(e)

    stat = path.stat()
    _update_backup_gauge()
    log.info("snapshot_backup_saved", filename=path.name, path=str(path), size_bytes=stat.st_size)
    return BackupFile(
        filename=path.name,
        size_bytes=stat.st_size,
        created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        path=str(path),
    )


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


@router.post(
    "/provision/two-line",
    response_model=ProvisionTwoLineResponse,
    summary="Provision HT812 FXS1/FXS2 SIP registration values except write-only passwords",
)
async def provision_two_line(request: Request, body: ProvisionTwoLineRequest):
    transport = body.transport.lower()
    if transport not in _TRANSPORT_VALUES:
        raise HTTPException(400, "transport must be one of: udp, tcp, tls")

    sip_server = body.sip_server or _DEFAULT_SIP_SERVER
    params = {
        # FXS port 1
        "P47": sip_server,
        "P48": body.sip_port,
        "P35": body.line1_extension,
        "P36": body.line1_extension,
        "P130": _TRANSPORT_VALUES[transport],
        "P46": "60",
        # FXS port 2
        "P2312": sip_server,
        "P2313": body.sip_port,
        "P735": body.line2_extension,
        "P736": body.line2_extension,
        "P830": _TRANSPORT_VALUES[transport],
        "P746": "60",
    }

    REQUEST_COUNT.labels(endpoint="provision_two_line").inc()
    with REQUEST_LATENCY.labels(endpoint="provision_two_line").time():
        try:
            ok = await _client(request).patch_config(params, apply=body.apply)
        except HT812Error as e:
            raise _handle(e)

    event = request.app.state.events.add(CommunicationEventIn(
        source="ht812_api",
        type="provision",
        message="HT812 two-line SIP settings applied" if ok else "HT812 two-line SIP settings returned non-success",
        data={
            "sip_server": sip_server,
            "sip_port": body.sip_port,
            "transport": transport,
            "params": list(params.keys()),
            "passwords_manual": ["P34", "P734"],
        },
    ))
    await request.app.state.event_queue.put(event)

    lines = [
        ProvisionLine(
            port=1,
            extension=body.line1_extension,
            sip_server=sip_server,
            sip_port=body.sip_port,
            transport=transport,
            password_manual=True,
        ),
        ProvisionLine(
            port=2,
            extension=body.line2_extension,
            sip_server=sip_server,
            sip_port=body.sip_port,
            transport=transport,
            password_manual=True,
        ),
    ]
    return ProvisionTwoLineResponse(
        success=ok,
        message=(
            "Non-password SIP settings applied. Set FXS1/FXS2 SIP auth passwords manually in the HT812 UI."
            if ok
            else "Non-success response from device"
        ),
        lines=lines,
        params_written=list(params.keys()),
    )


# ------------------------------------------------------------------ snapshot-backup

@router.post(
    "/snapshot-backup",
    response_model=BackupFile,
    summary="Save a timestamped XML config snapshot and return the file metadata",
)
async def snapshot_backup(request: Request):
    REQUEST_COUNT.labels(endpoint="snapshot_backup").inc()
    try:
        _, path = await _client(request).save_config_snapshot(keep_last=_BACKUP_KEEP)
    except HT812Error as e:
        raise _handle(e)
    _update_backup_gauge()
    stat = path.stat()
    log.info("snapshot_backup_saved", path=str(path))
    return BackupFile(
        filename=path.name,
        size_bytes=stat.st_size,
        created_at=__import__("datetime").datetime.fromtimestamp(
            stat.st_mtime, tz=__import__("datetime").timezone.utc
        ),
        path=str(path),
    )


# ------------------------------------------------------------------ force-register

@router.post(
    "/force-register",
    response_model=ForceRegisterResponse,
    summary="Write ALL SIP P-values (direct + profile system) and return a full debug readback",
)
async def force_register(
    request: Request,
    transport: str = Query("udp", description="SIP transport: udp, tcp, tls"),
):
    """
    Writes every SIP-registration-related P-value for both FXS ports — both the
    legacy direct system (P35/P47/P130) AND the firmware-3.7.5 profile system
    (P4060/P4090/P4669/P4150) — then reads them all back so you can see exactly
    what the device accepted. Useful for debugging registration failures.

    Note: SIP auth passwords (P34/P734 and P4120/P4121) are write-only at the
    firmware level and cannot be verified here; they MUST be set in the HT812
    web UI.
    """
    sip_server = _DEFAULT_SIP_SERVER
    sip_port   = "5060"
    transport_code = _TRANSPORT_VALUES.get(transport.lower(), "0")

    params = {
        # ── Legacy direct system (FXS1) ──────────────────────────────────
        "P35":  "1001",          "P36":  "1001",
        "P47":  sip_server,      "P48":  sip_port,
        "P130": transport_code,  "P46":  "60",
        # ── Legacy direct system (FXS2) ──────────────────────────────────
        "P735": "1002",          "P736": "1002",
        "P2312":sip_server,      "P2313":sip_port,
        "P830": transport_code,  "P746": "60",
        "P52":  transport_code,  # global preferred transport
        # ── Profile system (FXS1, profile row 0) ─────────────────────────
        "P4060":"1001",          "P4090":"1001",
        "P4669":sip_server,      "P4150":"1",
        "P4300":"1",             "P4595":"1",
        # ── Profile system (FXS2, profile row 1) ─────────────────────────
        "P4061":"1002",          "P4091":"1002",
        "P4670":sip_server,      "P4151":"1",
        "P4301":"2",             "P4596":"2",
    }

    REQUEST_COUNT.labels(endpoint="force_register").inc()
    try:
        ok = await _client(request).patch_config(params, apply=True)
    except HT812Error as e:
        raise _handle(e)

    # Read back every written key plus registration status
    readback_keys = list(params.keys()) + ["P4921", "P4922", "P4901", "P4902", "P8"]
    try:
        readback = await _client(request).get_values(readback_keys)
    except HT812Error:
        readback = {}

    transport_label = {"0": "UDP", "1": "TCP", "2": "TLS"}.get(transport_code, transport.upper())

    event = request.app.state.events.add(CommunicationEventIn(
        source="ht812_api",
        type="force_register",
        message=f"Force-register: wrote {len(params)} P-values. Reg P4921={readback.get('P4921','?')} P4922={readback.get('P4922','?')}",
        data={
            "params_written": params,
            "readback": readback,
            "apply_ok": ok,
        },
    ))
    await request.app.state.event_queue.put(event)

    log.info(
        "force_register",
        sip_server=sip_server,
        transport=transport_label,
        apply_ok=ok,
        reg1=readback.get("P4921"),
        reg2=readback.get("P4922"),
    )

    return ForceRegisterResponse(
        success=ok,
        message=(
            f"Wrote {len(params)} P-values ({transport_label} transport). "
            f"Reg status: FXS1={readback.get('P4921','?')} FXS2={readback.get('P4922','?')}. "
            f"SIP passwords (P34/P734, P4120/P4121) must still be set via HT812 web UI."
        ),
        sip_server=sip_server,
        sip_port=sip_port,
        transport=transport_label,
        params_written=params,
        readback=readback,
    )


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


@router.get("/status/summary", summary="Combined status for the web dashboard")
async def status_summary(request: Request):
    REQUEST_COUNT.labels(endpoint="status_summary").inc()
    try:
        ports = await _client(request).get_port_status()
    except HT812Error as e:
        raise _handle(e)
    return {
        "expected": {
            "transport": "tcp",
            "sip_port": "5060",
            "extensions": ["1001", "1002"],
            "manual_password_fields": ["P34", "P734"],
        },
        "ports": ports,
    }


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
