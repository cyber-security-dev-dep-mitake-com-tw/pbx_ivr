from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from ht812_client import HT812AuthError, HT812Client, HT812Error
from models import (
    ActionResponse,
    GetValuesResponse,
    PatchConfigRequest,
    PortStatusResponse,
    SystemInfoResponse,
)

router = APIRouter(prefix="/ht812", tags=["HT812V2"])


def _client(request: Request) -> HT812Client:
    return request.app.state.ht812


def _handle(e: Exception) -> HTTPException:
    if isinstance(e, HT812AuthError):
        return HTTPException(401, str(e))
    return HTTPException(502, str(e))


# ------------------------------------------------------------------ config

@router.get(
    "/config",
    response_class=Response,
    responses={200: {"content": {"application/xml": {}}}},
    summary="Export full config as XML (also saves timestamped backup)",
)
async def get_config(request: Request):
    try:
        xml = await _client(request).get_config_xml()
    except HT812Error as e:
        raise _handle(e)
    return Response(content=xml, media_type="application/xml")


@router.get(
    "/values",
    response_model=GetValuesResponse,
    summary="Read specific P-value settings",
)
async def get_values(
    request: Request,
    keys: str = Query(..., description="Comma-separated P-value keys, e.g. P47,P48,P52"),
):
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
    try:
        ok = await _client(request).patch_config(body.params, apply=apply)
    except HT812Error as e:
        raise _handle(e)
    return ActionResponse(success=ok, message="Config updated" if ok else "Non-success response from device")


# ------------------------------------------------------------------ actions

@router.post("/reboot", response_model=ActionResponse, summary="Reboot the device (~30s downtime)")
async def reboot(request: Request):
    try:
        ok = await _client(request).reboot()
    except HT812Error as e:
        raise _handle(e)
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
    try:
        ok = await _client(request).factory_reset(reset_type)
    except HT812Error as e:
        raise _handle(e)
    return ActionResponse(success=ok, message="Factory reset initiated" if ok else "Non-success response from device")


# ------------------------------------------------------------------ status

@router.get("/status/ports", response_model=PortStatusResponse, summary="FXS port SIP registration status")
async def port_status(request: Request):
    try:
        data = await _client(request).get_port_status()
    except HT812Error as e:
        raise _handle(e)
    return PortStatusResponse(raw=data)


@router.get("/status/system", response_model=SystemInfoResponse, summary="Firmware, MAC, model, uptime")
async def system_info(request: Request):
    try:
        data = await _client(request).get_system_info()
    except HT812Error as e:
        raise _handle(e)
    return SystemInfoResponse(raw=data)


@router.get("/status/network", response_model=SystemInfoResponse, summary="IP, DHCP, DNS, gateway")
async def net_status(request: Request):
    try:
        data = await _client(request).get_net_status()
    except HT812Error as e:
        raise _handle(e)
    return SystemInfoResponse(raw=data)
