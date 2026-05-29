from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .ht812_client import HT812Client, HT812Error
from .models import ActionResponse, PatchConfigRequest, SipStatusResponse

router = APIRouter(prefix="/ht812", tags=["HT812V2"])


def _client(request: Request) -> HT812Client:
    return request.app.state.ht812


@router.get("/config", response_class=Response, responses={200: {"content": {"application/xml": {}}}})
async def get_config(request: Request):
    """Export full device config as XML (also writes a timestamped backup file)."""
    try:
        xml = await _client(request).get_config()
    except HT812Error as e:
        raise HTTPException(502, str(e))
    return Response(content=xml, media_type="application/xml")


@router.patch("/config", response_model=ActionResponse)
async def patch_config(request: Request, body: PatchConfigRequest):
    """Push P-value key/value pairs to the device (e.g. {"P47": "sip.example.com"})."""
    try:
        ok = await _client(request).patch_config(body.params)
    except HT812Error as e:
        raise HTTPException(502, str(e))
    return ActionResponse(success=ok, message="Config updated" if ok else "Update returned non-200")


@router.post("/reboot", response_model=ActionResponse)
async def reboot(request: Request):
    """Trigger a device reboot. Device will be unreachable for ~30 seconds."""
    try:
        ok = await _client(request).reboot()
    except HT812Error as e:
        raise HTTPException(502, str(e))
    return ActionResponse(success=ok, message="Reboot initiated" if ok else "Reboot returned non-200")


@router.post("/factory-reset", response_model=ActionResponse)
async def factory_reset(request: Request):
    """Trigger a factory reset. ALL device settings will be erased."""
    try:
        ok = await _client(request).factory_reset()
    except HT812Error as e:
        raise HTTPException(502, str(e))
    return ActionResponse(success=ok, message="Factory reset initiated" if ok else "Reset returned non-200")


@router.get("/status", response_model=SipStatusResponse)
async def get_status(request: Request):
    """Return live SIP registration status for FXS port 1 and port 2."""
    try:
        data = await _client(request).get_sip_status()
    except HT812Error as e:
        raise HTTPException(502, str(e))
    return SipStatusResponse(**data)
