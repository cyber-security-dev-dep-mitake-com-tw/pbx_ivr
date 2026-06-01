"""
Offline-safe Asterisk ARI client for registration verification.

This is the *Asterisk-side* counterpart to ht812_client (the device side).
It answers one question the device flag (P4921/P4922) cannot: does Asterisk
actually hold a SIP contact for extensions 1001/1002 right now?

ARI endpoint state semantics (GET /ari/endpoints/PJSIP/<ext>):
  state="online"  → at least one AOR contact is registered & reachable
  state="offline" → endpoint configured but no live contact (not registered)
  state="unknown" → no qualify / state not yet determined

Everything here runs entirely on the LAN/docker bridge (asterisk:8088); no
internet is required, so it is safe to call on the direct-ethernet link with
wifi down. All failures degrade to a structured offline result instead of
raising, so callers can always assemble a diagnostic bundle.
"""

import os
from typing import Any

import httpx

ARI_HOST = os.environ.get("ARI_HOST", "asterisk")
ARI_PORT = os.environ.get("ARI_PORT", "8088")
ARI_USER = os.environ.get("ARI_USER", "ari-user")
ARI_PASS = os.environ.get("ARI_PASS", "changeme_ari")

_BASE_URL = f"http://{ARI_HOST}:{ARI_PORT}/ari"

# FXS port → SIP extension mapping (matches pjsip.conf endpoints)
_PORT_EXTENSIONS = {1: "1001", 2: "1002"}
# line "1"/"2" → SIP extension
_LINE_EXTENSIONS = {"1": "1001", "2": "1002"}


async def _ari_request(method: str, path: str, params: dict | None = None, timeout: float = 4.0) -> tuple[int, Any]:
    """Single offline-safe ARI request. Returns (status_code, json|None). Never raises."""
    try:
        async with httpx.AsyncClient(
            base_url=_BASE_URL, auth=(ARI_USER, ARI_PASS), timeout=timeout
        ) as client:
            r = await client.request(method, path, params=params)
            try:
                body = r.json()
            except Exception:
                body = None
            return r.status_code, body
    except Exception as e:  # offline / Asterisk down
        return 0, {"error": f"{type(e).__name__}: {e}"}


async def find_active_channel_for_line(line: str) -> dict:
    """
    Find the live Asterisk channel for FXS line "1"/"2" (caller 1001/1002).

    Returns {"found": bool, "channel_id": str|None, "name": str|None,
             "state": str|None, "error": str|None}. Offline-safe.
    """
    ext = _LINE_EXTENSIONS.get(str(line))
    if not ext:
        return {"found": False, "channel_id": None, "error": f"unknown line {line!r}"}
    status, body = await _ari_request("GET", "/channels")
    if status != 200 or not isinstance(body, list):
        err = body.get("error") if isinstance(body, dict) else f"ARI status {status}"
        return {"found": False, "channel_id": None, "error": err}
    for ch in body:
        caller = (ch.get("caller") or {}).get("number") or ""
        name = ch.get("name", "")
        # Match by caller id or the PJSIP/<ext> channel name
        if caller == ext or f"PJSIP/{ext}-" in name:
            return {
                "found": True,
                "channel_id": ch.get("id"),
                "name": name,
                "state": ch.get("state"),
                "error": None,
            }
    return {"found": False, "channel_id": None, "error": f"no active channel for line {line} ({ext})"}


async def send_dtmf_to_channel(channel_id: str, digit: str) -> dict:
    """
    Inject a real DTMF digit into a live channel via ARI
    POST /channels/{id}/dtmf. Offline-safe; returns {"ok": bool, "error": ...}.
    """
    status, body = await _ari_request("POST", f"/channels/{channel_id}/dtmf", params={"dtmf": digit})
    if status in (200, 204):
        return {"ok": True, "error": None}
    err = body.get("error") if isinstance(body, dict) else f"ARI status {status}"
    return {"ok": False, "error": err}


async def get_registration_contacts(timeout: float = 4.0) -> dict:
    """
    Query Asterisk ARI for the live registration state of extensions 1001/1002.

    Returns an offline-safe dict; never raises. Shape:
      {
        "reachable": bool,          # was Asterisk ARI reachable at all
        "offline": bool,            # inverse of reachable (for FE parity)
        "error": str | None,
        "endpoints": {
          "1001": {"state": "online"|"offline"|..., "registered": bool,
                    "channel_count": int, "port": 1},
          "1002": {...},
        },
        "both_registered": bool,    # both 1001 and 1002 have a live contact
      }
    """
    result: dict = {
        "reachable": False,
        "offline": True,
        "error": None,
        "endpoints": {},
        "both_registered": False,
    }
    try:
        async with httpx.AsyncClient(
            base_url=_BASE_URL, auth=(ARI_USER, ARI_PASS), timeout=timeout
        ) as client:
            for port, ext in _PORT_EXTENSIONS.items():
                r = await client.get(f"/endpoints/PJSIP/{ext}")
                if r.status_code != 200:
                    result["endpoints"][ext] = {
                        "state": "error",
                        "registered": False,
                        "channel_count": 0,
                        "port": port,
                        "http_status": r.status_code,
                    }
                    continue
                data = r.json()
                state = str(data.get("state", "unknown"))
                result["endpoints"][ext] = {
                    "state": state,
                    "registered": state.lower() == "online",
                    "channel_count": len(data.get("channel_ids", []) or []),
                    "port": port,
                }
        result["reachable"] = True
        result["offline"] = False
        result["both_registered"] = all(
            result["endpoints"].get(ext, {}).get("registered")
            for ext in _PORT_EXTENSIONS.values()
        )
    except (httpx.TransportError, httpx.TimeoutException, httpx.HTTPError) as e:
        result["error"] = f"{type(e).__name__}: {e}"
    except Exception as e:  # never let Asterisk-side issues break the audit
        result["error"] = f"{type(e).__name__}: {e}"
    return result
