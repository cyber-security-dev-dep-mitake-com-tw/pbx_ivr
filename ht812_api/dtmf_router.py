"""
Interactive DTMF endpoints for the Protocol page keypad.

Two modes, mirroring the question "show key-code transmitting (simulation AND
real call)":

  POST /dtmf/simulate  — emit a *simulated* DTMF event into the event store/SSE
                         stream without touching Asterisk. For line 1 it also
                         emits the paired Line 1 → Line 2 forward, so the UI
                         shows propagation even with no live call. Fully offline.

  POST /dtmf/send      — inject a *real* DTMF digit into the live Asterisk
                         channel for that line via ARI. Requires an active call
                         (handset off-hook + in the Stasis app). Offline-safe:
                         returns found=false with a reason if no channel exists.

Both reuse the existing EventStore + event_queue → /events/stream pipeline, so
whatever happens is visible live on the Protocol and Timeline pages.
"""

from fastapi import APIRouter, HTTPException, Query, Request

from asterisk_client import find_active_channel_for_line, send_dtmf_to_channel
from events import CommunicationEventIn

router = APIRouter(prefix="/dtmf", tags=["DTMF"])

_VALID_DIGITS = set("0123456789*#")
_LINE_EXT = {"1": "1001", "2": "1002"}


async def _publish(request: Request, event: CommunicationEventIn):
    stored = request.app.state.events.add(event)
    await request.app.state.event_queue.put(stored)
    return stored


def _validate(line: str, digit: str) -> None:
    if line not in _LINE_EXT:
        raise HTTPException(400, "line must be '1' or '2'")
    if digit not in _VALID_DIGITS:
        raise HTTPException(400, "digit must be 0-9, * or #")


@router.post("/simulate", summary="Emit a simulated DTMF keypress (no Asterisk; drives the UI)")
async def simulate_dtmf(
    request: Request,
    line: str = Query(..., description="FXS line: '1' or '2'"),
    digit: str = Query(..., description="DTMF digit 0-9, * or #"),
):
    _validate(line, digit)
    ext = _LINE_EXT[line]
    events = [
        await _publish(request, CommunicationEventIn(
            source="web_sim",
            type="dtmf",
            message=f"[SIM] Line {line} keypad: {digit}",
            caller=ext,
            line=line,
            digit=digit,
            data={"menu": "simulation", "simulated": True},
        ))
    ]
    # Mirror the ari_app Line 1 -> Line 2 forward so simulation shows propagation.
    if line == "1":
        events.append(await _publish(request, CommunicationEventIn(
            source="web_sim",
            type="route",
            message=f"[SIM] Line 1 digit {digit} forwarded to Line 2",
            line="1",
            digit=digit,
            data={"route": "line1_to_line2", "target_line": "2", "digit": digit, "simulated": True},
        )))
        events.append(await _publish(request, CommunicationEventIn(
            source="web_sim",
            type="dtmf",
            message=f"[SIM] Line 2 received forwarded digit {digit} from Line 1",
            caller="1002",
            line="2",
            digit=digit,
            data={"menu": "forwarded", "forwarded_from": "1", "simulated": True},
        )))
    return {"ok": True, "mode": "simulate", "line": line, "digit": digit, "events_emitted": len(events)}


@router.post("/send", summary="Inject a real DTMF digit into the live Asterisk channel for a line")
async def send_dtmf(
    request: Request,
    line: str = Query(..., description="FXS line: '1' or '2'"),
    digit: str = Query(..., description="DTMF digit 0-9, * or #"),
):
    _validate(line, digit)
    ext = _LINE_EXT[line]
    channel = await find_active_channel_for_line(line)
    if not channel.get("found"):
        # Offline-safe: no live call (or Asterisk unreachable). Tell the UI plainly.
        await _publish(request, CommunicationEventIn(
            source="web_live",
            type="error",
            message=f"[LIVE] No active call on Line {line} to send digit {digit}",
            line=line,
            digit=digit,
            data={"reason": channel.get("error"), "mode": "live"},
        ))
        return {"ok": False, "mode": "live", "line": line, "digit": digit,
                "reason": channel.get("error"), "hint": "Pick up the handset and dial * into the IVR first."}

    result = await send_dtmf_to_channel(channel["channel_id"], digit)
    await _publish(request, CommunicationEventIn(
        source="web_live",
        type="dtmf" if result["ok"] else "error",
        message=(f"[LIVE] Sent digit {digit} to Line {line} ({ext})"
                 if result["ok"] else f"[LIVE] Failed to send digit {digit} to Line {line}"),
        channel_id=channel["channel_id"],
        caller=ext,
        line=line,
        digit=digit,
        data={"mode": "live", "channel": channel["name"], "error": result.get("error")},
    ))
    return {"ok": result["ok"], "mode": "live", "line": line, "digit": digit,
            "channel_id": channel["channel_id"], "error": result.get("error")}
