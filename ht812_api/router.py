import os
import json
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request # pyright: ignore[reportMissingImports]
from fastapi.responses import Response # pyright: ignore[reportMissingImports]

import structlog # pyright: ignore[reportMissingImports]
from asterisk_client import get_registration_contacts
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
_DEBUG_DIR = _BACKUP_DIR / "debug_logs"
_BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "30"))
_DEFAULT_SIP_SERVER = os.environ.get("ASTERISK_SIP_HOST", "host.docker.internal")
_HT812_HOST = os.environ.get("HT812_HOST", "https://192.168.0.160")

_TRANSPORT_VALUES = {
    "udp": "0",
    "tcp": "1",
    "tls": "2",
}
_TRANSPORT_LABELS = {v: k.upper() for k, v in _TRANSPORT_VALUES.items()}
_TRANSPORT_PORTS = {"udp": "5060", "tcp": "5060", "tls": "5061"}
_SENSITIVE_KEYS = {"P34", "P734", "P4120", "P4121"}
_WRITE_ONLY_KEYS = _SENSITIVE_KEYS
_SIP_PASSWORDS = {
    "1001": os.environ.get("SIP_1001_PASS", ""),
    "1002": os.environ.get("SIP_1002_PASS", ""),
}

_SIP_DIAG_KEYS = [
    "P8",
    "P31", "P34", "P35", "P36", "P37", "P40", "P46", "P47", "P48", "P52", "P130",
    "P731", "P734", "P735", "P736", "P737", "P740", "P746", "P830", "P2312", "P2313",
    "P4060", "P4061", "P4090", "P4091", "P4150", "P4151",
    "P4300", "P4301", "P4595", "P4596", "P4669", "P4670", "P4120", "P4121",
    "P28859", "P28860", "P4210", "P4211",
    "P4901", "P4902", "P4921", "P4922",
]

_P_VALUE_NOTES = {
    "P8": "Device mode: 0=bridge, 1=NAT router.",
    "P31": "FXS1 SIP registration/account enabled.",
    "P731": "FXS2 SIP registration/account enabled.",
    "P34": "FXS1 legacy SIP auth password. Write-only; never exported in XML backups.",
    "P734": "FXS2 legacy SIP auth password. Write-only; never exported in XML backups.",
    "P35": "FXS1 SIP User ID.",
    "P36": "FXS1 SIP Authenticate ID.",
    "P735": "FXS2 SIP User ID.",
    "P736": "FXS2 SIP Authenticate ID.",
    "P47": "FXS1 legacy SIP server.",
    "P48": "FXS1 legacy SIP server port.",
    "P2312": "FXS2 legacy SIP server.",
    "P2313": "FXS2 legacy SIP server port.",
    "P130": "FXS1 SIP transport: 0=UDP, 1=TCP, 2=TLS.",
    "P830": "FXS2 SIP transport: 0=UDP, 1=TCP, 2=TLS.",
    "P52": "NAT traversal: 2=keep-alive. This is not SIP transport.",
    "P4060": "FXS1 profile SIP User ID.",
    "P4090": "FXS1 profile Authenticate ID.",
    "P4120": "FXS1 profile SIP auth password. Write-only; this matches the browser UI field in /portSetting/FXSPort.",
    "P4669": "FXS1 profile SIP server URL.",
    "P4150": "FXS1 profile enabled/profile selector.",
    "P4300": "FXS1 profile group/binding.",
    "P4061": "FXS2 profile SIP User ID.",
    "P4091": "FXS2 profile Authenticate ID.",
    "P4121": "FXS2 profile SIP auth password. Write-only; this matches the browser UI field in /portSetting/FXSPort.",
    "P4670": "FXS2 profile SIP server URL.",
    "P4151": "FXS2 profile enabled/profile selector.",
    "P4301": "FXS2 profile group/binding.",
    "P28859": "FXS1 profile password-control field observed in HT812 UI payload.",
    "P28860": "FXS2 profile password-control field observed in HT812 UI payload.",
    "P4210": "FXS1 profile optional field observed in HT812 UI payload.",
    "P4211": "FXS2 profile optional field observed in HT812 UI payload.",
    "P4901": "FXS1 hook state.",
    "P4902": "FXS2 hook state.",
    "P4921": "FXS1 registration status.",
    "P4922": "FXS2 registration status.",
}


def _client(request: Request) -> HT812Client:
    return request.app.state.ht812


def _handle(e: Exception) -> HTTPException:
    detail = {
        "message": str(e),
        "error_type": type(e).__name__,
        "debug_logs_dir": str(_DEBUG_DIR),
        "latest_debug_logs": [str(p) for p in sorted(_DEBUG_DIR.glob("*.json"))[-5:]] if _DEBUG_DIR.exists() else [],
    }
    if isinstance(e, HT812AuthError):
        record_login_failure()
        log.warning("ht812_auth_error", error=str(e))
        return HTTPException(401, detail)
    log.error("ht812_error", error=str(e))
    return HTTPException(502, detail)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _latest_backup() -> Path | None:
    files = sorted(_BACKUP_DIR.glob("ht812_config_*.xml"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _latest_debug_log(suffix: str | None = None) -> Path | None:
    if not _DEBUG_DIR.exists():
        return None
    pattern = f"*_{suffix}.json" if suffix else "*.json"
    files = sorted(_DEBUG_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _read_json_file(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _parse_backup_values(path: Path | None, keys: list[str] | None = None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    text = path.read_text(errors="ignore")
    selected = keys or _SIP_DIAG_KEYS
    values: dict[str, str] = {}
    for key in selected:
        match = re.search(rf"<{key}>(.*?)</{key}>", text, flags=re.DOTALL)
        if match:
            values[key] = match.group(1).strip()
    return values


def _backup_meta(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    stat = path.stat()
    return {
        "filename": path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if str(key) in _SENSITIVE_KEYS:
                redacted[str(key)] = item if isinstance(item, bool) else ("<redacted>" if item not in (None, "") else "")
            else:
                redacted[str(key)] = _redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _compare_values(
    expected: dict[str, str] | None,
    live: dict[str, Any] | None,
    backup: dict[str, str] | None,
) -> dict[str, Any]:
    expected = expected or {}
    live = live or {}
    backup = backup or {}
    keys = sorted(set(_SIP_DIAG_KEYS) | set(expected) | set(live) | set(backup))
    rows = []
    mismatches = []
    for key in keys:
        exp = expected.get(key)
        lv = live.get(key)
        bk = backup.get(key)
        write_only = key in _WRITE_ONLY_KEYS
        display_exp = "<redacted>" if write_only and exp not in (None, "") else exp
        display_live = "<redacted>" if write_only and lv not in (None, "") else lv
        display_backup = "<redacted>" if write_only and bk not in (None, "") else bk
        live_matches_expected = exp is None or lv is None or str(lv) == str(exp)
        backup_matches_expected = exp is None or str(bk) == str(exp)
        if write_only:
            live_matches_expected = True
            backup_matches_expected = True
        row = {
            "key": key,
            "meaning": _P_VALUE_NOTES.get(key, ""),
            "expected": display_exp,
            "live": display_live,
            "latest_backup": display_backup,
            "write_only": write_only,
            "verification": "write_only_not_readable_from_ht812_or_xml" if write_only else "readable",
            "live_matches_expected": live_matches_expected,
            "backup_matches_expected": backup_matches_expected,
            "live_matches_backup": lv is None or bk is None or str(lv) == str(bk),
        }
        rows.append(row)
        if not row["live_matches_expected"] or not row["backup_matches_expected"]:
            mismatches.append(row)
    return {
        "rows": rows,
        "mismatches": mismatches,
        "summary": {
            "checked_keys": len(rows),
            "mismatch_count": len(mismatches),
            "has_live_values": bool(live),
            "has_latest_backup_values": bool(backup),
        },
    }


def _registration_interpretation(values: dict[str, Any] | None) -> dict[str, Any]:
    values = values or {}
    reg1 = str(values.get("P4921", ""))
    reg2 = str(values.get("P4922", ""))
    transport_code = str(values.get("P130") or values.get("P830") or "")
    return {
        "registered": reg1.lower() in ("1", "registered") and reg2.lower() in ("1", "registered"),
        "fxs1_registration_raw": reg1 or None,
        "fxs2_registration_raw": reg2 or None,
        "transport_code": transport_code or None,
        "transport_label": _TRANSPORT_LABELS.get(transport_code),
        "server_fields": {
            "legacy_fxs1": values.get("P47"),
            "legacy_fxs2": values.get("P2312"),
            "profile_fxs1": values.get("P4669"),
            "profile_fxs2": values.get("P4670"),
        },
        "port_fields": {
            "legacy_fxs1": values.get("P48"),
            "legacy_fxs2": values.get("P2313"),
        },
        "next_checks_if_asterisk_has_no_contacts": [
            "Confirm Asterisk PJSIP logger shows an inbound REGISTER. No contacts plus no log means the HT812 did not send a packet to Asterisk.",
            "Confirm the SIP server IP is reachable from the HT812 network, not just from the Mac.",
            "Confirm FXS1/FXS2 SIP authentication passwords were written: P34/P734 and profile passwords P4120/P4121 are write-only, so success can only be inferred from the POST response and later Asterisk REGISTER/auth behavior.",
            "Use UDP or TCP first. TLS requires the HT812 to accept Asterisk's certificate and is not a first-pass registration target.",
            "If settings were just changed, save/apply in the HT812 UI or reboot the HT812 once after writing stable UDP/TCP settings.",
        ],
    }


def _port_snapshot_from_values(values: dict[str, Any], *, offline: bool = False) -> dict[str, Any]:
    values = values or {}
    return {
        "port1": {
            "port": 1,
            "hook": str(values.get("P4901", "")) or "unknown",
            "registered": str(values.get("P4921", "")).lower() in ("1", "registered"),
            "user_id": values.get("P35", ""),
            "sip_server": values.get("P47", ""),
            "sip_port": values.get("P48", "5060"),
        },
        "port2": {
            "port": 2,
            "hook": str(values.get("P4902", "")) or "unknown",
            "registered": str(values.get("P4922", "")).lower() in ("1", "registered"),
            "user_id": values.get("P735", ""),
            "sip_server": values.get("P2312", ""),
            "sip_port": values.get("P2313", "5060"),
        },
        "raw": values,
        "offline": offline,
    }


def _asterisk_expected(transport: str | None = None, sip_server: str | None = None, sip_port: str | None = None) -> dict[str, Any]:
    return {
        "expected_contacts": ["1001", "1002"],
        "configured_transports": {
            "udp": "0.0.0.0:5060",
            "tcp": "0.0.0.0:5060",
            "tls": "0.0.0.0:5061",
        },
        "selected_test": {
            "transport": transport,
            "sip_server": sip_server,
            "sip_port": sip_port,
        },
        "commands_to_run_while_device_is_plugged_in": [
            'docker exec asterisk asterisk -rx "pjsip set logger on"',
            'docker exec asterisk asterisk -rx "pjsip show contacts"',
            'docker logs --tail 120 asterisk',
        ],
        "interpretation": "If show contacts is empty and PJSIP logger/logs show no REGISTER, the blocker is before Asterisk: HT812 did not send SIP to the selected server/port.",
    }


def _expected_registration_values(transport_key: str, sip_server: str, sip_port: str) -> dict[str, str]:
    transport_code = _TRANSPORT_VALUES[transport_key]
    expected = {
        "P35": "1001",
        "P36": "1001",
        "P47": sip_server,
        "P48": sip_port,
        "P130": transport_code,
        "P735": "1002",
        "P736": "1002",
        "P2312": sip_server,
        "P2313": sip_port,
        "P830": transport_code,
        "P52": "2",
        "P4060": "1001",
        "P4090": "1001",
        "P4669": sip_server,
        "P4150": "1",
        "P4300": "1",
        "P4595": "1",
        "P4061": "1002",
        "P4091": "1002",
        "P4670": sip_server,
        "P4151": "1",
        "P4301": "2",
        "P4596": "2",
    }
    if _SIP_PASSWORDS["1001"]:
        expected["P34"] = _SIP_PASSWORDS["1001"]
        expected["P4120"] = _SIP_PASSWORDS["1001"]
    if _SIP_PASSWORDS["1002"]:
        expected["P734"] = _SIP_PASSWORDS["1002"]
        expected["P4121"] = _SIP_PASSWORDS["1002"]
    return expected


def _registration_audit(
    *,
    expected: dict[str, str] | None = None,
    live_values: dict[str, Any] | None = None,
    transport: str | None = None,
    sip_server: str | None = None,
    sip_port: str | None = None,
    asterisk_state: dict[str, Any] | None = None,
    captured_sip_log: dict[str, Any] | None = None,
) -> dict[str, Any]:
    latest_backup = _latest_backup()
    backup_values = _parse_backup_values(latest_backup)
    latest_force = _latest_debug_log("force_register")
    latest_sip_log = _latest_debug_log("sip_log")
    force_data = _read_json_file(latest_force) or {}
    # Prefer a SIP log captured in this same request (correlated by timestamp)
    # over whatever unrelated sip_log file happens to be newest on disk.
    if captured_sip_log is not None:
        sip_data = captured_sip_log
        latest_sip_log = captured_sip_log.get("debug_log_path") or latest_sip_log
    else:
        sip_data = _read_json_file(latest_sip_log) or {}
    force_action = force_data.get("action") if isinstance(force_data, dict) else {}
    force_action = force_action if isinstance(force_action, dict) else {}
    force_readback = force_data.get("readback") if isinstance(force_data, dict) else {}
    force_readback = force_readback if isinstance(force_readback, dict) else {}
    force_applied = bool(force_action.get("apply_ok") or force_action.get("success"))

    snapshot_values = live_values or force_readback or backup_values
    registration = _registration_interpretation(snapshot_values)
    reg1 = registration.get("fxs1_registration_raw")
    reg2 = registration.get("fxs2_registration_raw")
    registered = bool(registration.get("registered"))

    trace_state = "unknown"
    sip_log_raw = sip_data.get("sip_log_raw") if isinstance(sip_data, dict) else ""
    if isinstance(sip_data, dict):
        if sip_data.get("offline"):
            trace_state = "offline"
        elif sip_data.get("sip_log_empty") is True or _sip_log_is_empty(sip_log_raw):
            # The HT812 returns {"results":[{"exist":"false"}]} when it has no SIP
            # trace at all — i.e. the device sent zero SIP packets. Treat as empty,
            # NOT present, otherwise the verdict wrongly implies traffic occurred.
            trace_state = "empty"
        elif sip_data.get("sip_log_empty") is False:
            trace_state = "present"
        elif isinstance(sip_log_raw, str) and sip_log_raw.strip():
            trace_state = "present"

    # Asterisk-side truth (does Asterisk actually hold a live contact?).
    ast = asterisk_state or {}
    ast_reachable = bool(ast.get("reachable"))
    ast_both_registered = bool(ast.get("both_registered"))

    # Three-way verdict: device flag (P4921/P4922) vs Asterisk contact state vs
    # written/expected config. Asterisk is the source of truth when reachable.
    if ast_reachable:
        if ast_both_registered:
            verdict = "registered_confirmed_both_sides" if registered else "asterisk_online_but_device_flag_stale"
        else:
            if registered:
                verdict = "device_says_registered_but_asterisk_has_no_contact"
            elif force_applied and trace_state == "present":
                verdict = "sip_trace_present_but_not_registered"
            elif force_applied:
                verdict = "configured_but_neither_side_registered"
            else:
                verdict = "neither_side_registered"
    elif registered:
        verdict = "registered"
    elif force_applied and trace_state in ("empty", "offline"):
        verdict = "configured_but_no_register_observed"
    elif force_applied and trace_state == "present":
        verdict = "sip_trace_present_but_not_registered"
    elif force_applied:
        verdict = "configured"
    else:
        verdict = "no_force_register_audit_found"

    if expected is None and transport and sip_server and sip_port:
        expected = _expected_registration_values(transport, sip_server, sip_port)

    comparison = _compare_values(expected, snapshot_values, backup_values) if expected else {
        "rows": [],
        "mismatches": [],
        "summary": {
            "checked_keys": 0,
            "mismatch_count": 0,
            "has_live_values": bool(live_values),
            "has_latest_backup_values": bool(backup_values),
        },
    }

    return {
        "verdict": verdict,
        "device": {
            "registered": registered,
            "fxs1_registration_raw": reg1,
            "fxs2_registration_raw": reg2,
            "sip_trace_state": trace_state,
            "snapshot_source": "live" if live_values else "latest_backup",
        },
        "force_register": {
            "found": bool(force_data),
            "applied": force_applied,
            "transport": force_action.get("transport"),
            "transport_code": force_action.get("transport_code"),
            "sip_server": force_action.get("sip_server"),
            "sip_port": force_action.get("sip_port"),
            "password_fields_attempted": force_action.get("password_fields_attempted", []),
            "debug_log_path": str(latest_force) if latest_force else None,
        },
        "sip_log": {
            "found": bool(sip_data),
            "offline": bool(sip_data.get("offline")) if isinstance(sip_data, dict) else False,
            "empty": bool(sip_data.get("sip_log_empty")) if isinstance(sip_data, dict) else None,
            "raw": sip_log_raw if isinstance(sip_log_raw, str) else "",
            "debug_log_path": str(latest_sip_log) if latest_sip_log else None,
        },
        "asterisk": {
            "checked": asterisk_state is not None,
            "reachable": ast_reachable,
            "offline": bool(ast.get("offline")) if ast else None,
            "both_registered": ast_both_registered,
            "endpoints": ast.get("endpoints", {}),
            "error": ast.get("error"),
        },
        "latest_backup": _backup_meta(latest_backup),
        "latest_backup_values": backup_values,
        "comparison": comparison,
        "requested": {
            "transport": transport,
            "sip_server": sip_server,
            "sip_port": sip_port,
        },
    }


def _sip_log_is_empty(text: Any) -> bool:
    """
    True when the HT812 SIP trace is effectively empty. The device returns
    `{"results":[{"exist":"false"}]}` (or whitespace) when it holds no trace —
    meaning it sent zero SIP packets. A non-empty JSON string is not enough to
    conclude SIP traffic occurred, so this sentinel must be detected explicitly.
    """
    if not isinstance(text, str):
        return True
    stripped = text.strip()
    if not stripped:
        return True
    return '"exist":"false"' in stripped.replace(" ", "")


async def _capture_sip_log(request: Request, *, source: str) -> dict[str, Any]:
    """
    Fetch the HT812 device SIP trace and persist it as a sip_log debug log so the
    registration audit can correlate it with this exact request. Offline-safe:
    on device error returns an offline marker instead of raising.
    """
    try:
        text = await _client(request).get_sip_log()
        empty = _sip_log_is_empty(text)
        payload = {
            "timestamp": _now_iso(),
            "endpoint": "sip_log",
            "captured_by": source,
            "sip_log_raw": text,
            "sip_log_empty": empty,
            "device_sent_no_sip": empty,
            "offline": False,
        }
    except HT812Error as e:
        payload = {
            "timestamp": _now_iso(),
            "endpoint": "sip_log",
            "captured_by": source,
            "sip_log_raw": "",
            "sip_log_empty": True,
            "offline": True,
            "error": {"type": type(e).__name__, "message": str(e)},
        }
    payload["debug_log_path"] = _write_debug_log("sip_log", payload)
    return payload


def _write_debug_log(endpoint: str, payload: dict[str, Any]) -> str:
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", endpoint).strip("_") or "ht812"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = _DEBUG_DIR / f"{ts}_{safe}.json"
    payload["debug_log_path"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return str(path)


def _diagnostics(
    request: Request,
    endpoint: str,
    *,
    expected: dict[str, str] | None = None,
    live: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> dict[str, Any]:
    latest = _latest_backup()
    backup_values = _parse_backup_values(latest)
    payload: dict[str, Any] = {
        "timestamp": _now_iso(),
        "endpoint": endpoint,
        "request": {
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query),
            "client": request.client.host if request.client else None,
        },
        "environment": {
            "ht812_host": _HT812_HOST,
            "asterisk_sip_host_default": _DEFAULT_SIP_SERVER,
            "backup_dir": str(_BACKUP_DIR),
            "debug_dir": str(_DEBUG_DIR),
            "backup_keep": _BACKUP_KEEP,
        },
        "action": _redact_sensitive(action or {}),
        "expected_values": _redact_sensitive(expected or {}),
        "live_values": _redact_sensitive(live or {}),
        "latest_backup": _backup_meta(latest),
        "latest_backup_values": backup_values,
        "comparison": _redact_sensitive(_compare_values(expected, live, backup_values)),
        "registration": _registration_interpretation(live or backup_values),
        "asterisk": _asterisk_expected(
            (action or {}).get("transport"),
            (action or {}).get("sip_server"),
            (action or {}).get("sip_port"),
        ),
        "manual_write_only_fields": {
            "legacy_passwords": ["P34", "P734"],
            "profile_passwords": ["P4120", "P4121"],
            "env_sources": {
                "P34": "SIP_1001_PASS",
                "P734": "SIP_1002_PASS",
                "P4120": "SIP_1001_PASS",
                "P4121": "SIP_1002_PASS",
            },
            "note": "These fields are required for SIP auth but are write-only on HT812 firmware and do not appear in API readback or XML backups. Debug logs only record whether the API attempted to write them, never the secret values.",
        },
    }
    if error is not None:
        payload["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": traceback.format_exc(),
        }
    _write_debug_log(endpoint, payload)
    return payload


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
            xml, _ = await _client(request).save_config_snapshot(keep_last=_BACKUP_KEEP)
        except HT812Error as e:
            _diagnostics(request, "get_config_error", error=e)
            raise _handle(e)
    _update_backup_gauge()
    diagnostics = _diagnostics(request, "get_config", live=_parse_backup_values(_latest_backup()))
    log.info("config_backup_saved")
    return Response(
        content=xml,
        media_type="application/xml",
        headers={"X-HT812-Debug-Log": diagnostics["debug_log_path"]},
    )


@router.get(
    "/backups",
    response_model=BackupListResponse,
    summary="List all saved config backup files",
)
async def list_backups(request: Request):
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
    diagnostics = _diagnostics(request, "list_backups")
    return BackupListResponse(count=len(items), backups=items, diagnostics=diagnostics)


@router.post(
    "/snapshot-backup",
    response_model=BackupFile,
    summary="Create and save a timestamped config snapshot",
)
async def snapshot_backup(request: Request):
    REQUEST_COUNT.labels(endpoint="snapshot_backup").inc()
    with REQUEST_LATENCY.labels(endpoint="snapshot_backup").time():
        try:
            _xml, path = await _client(request).save_config_snapshot(keep_last=_BACKUP_KEEP)
        except HT812Error as e:
            _diagnostics(request, "snapshot_backup_error", error=e)
            raise _handle(e)

    stat = path.stat()
    _update_backup_gauge()
    backup_values = _parse_backup_values(path)
    diagnostics = _diagnostics(request, "snapshot_backup", live=backup_values)
    log.info("snapshot_backup_saved", filename=path.name, path=str(path), size_bytes=stat.st_size)
    return BackupFile(
        filename=path.name,
        size_bytes=stat.st_size,
        created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        path=str(path),
        diagnostics=diagnostics,
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
    requested_keys = [key.strip() for key in keys.split(",") if key.strip()]
    try:
        values = await _client(request).get_values(requested_keys)
    except HT812Error as e:
        _diagnostics(request, "get_values_error", expected={key: "" for key in requested_keys}, error=e)
        raise _handle(e)
    diagnostics = _diagnostics(request, "get_values", live=values)
    return GetValuesResponse(values=values, diagnostics=diagnostics)


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
            _diagnostics(request, "patch_config_error", expected=body.params, error=e)
            raise _handle(e)
    diagnostics = _diagnostics(
        request,
        "patch_config",
        expected=body.params,
        action={"params": body.params, "apply": apply, "success": ok},
    )
    log.info("config_patched", params=list(body.params.keys()), apply=apply, success=ok)
    return ActionResponse(
        success=ok,
        message="Config updated" if ok else "Non-success response from device",
        diagnostics=diagnostics,
    )


@router.post(
    "/provision/two-line",
    response_model=ProvisionTwoLineResponse,
    summary="Provision HT812 FXS1/FXS2 SIP registration values including blind write-only passwords when env vars exist",
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
    if _SIP_PASSWORDS["1001"]:
        params["P34"] = _SIP_PASSWORDS["1001"]
    if _SIP_PASSWORDS["1002"]:
        params["P734"] = _SIP_PASSWORDS["1002"]

    REQUEST_COUNT.labels(endpoint="provision_two_line").inc()
    with REQUEST_LATENCY.labels(endpoint="provision_two_line").time():
        try:
            ok = await _client(request).patch_config(params, apply=body.apply)
        except HT812Error as e:
            _diagnostics(request, "provision_two_line_error", expected=params, error=e)
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
            "password_fields_attempted": [key for key in ("P34", "P734") if key in params],
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
            password_manual=not bool(_SIP_PASSWORDS["1001"]),
        ),
        ProvisionLine(
            port=2,
            extension=body.line2_extension,
            sip_server=sip_server,
            sip_port=body.sip_port,
            transport=transport,
            password_manual=not bool(_SIP_PASSWORDS["1002"]),
        ),
    ]
    return ProvisionTwoLineResponse(
        success=ok,
        message=(
            "SIP settings applied. Legacy auth passwords were blind-written from SIP_1001_PASS/SIP_1002_PASS when present; HT812 does not expose them for readback."
            if ok
            else "Non-success response from device"
        ),
        lines=lines,
        params_written=list(_redact_sensitive(params).keys()),
        diagnostics=_diagnostics(
            request,
            "provision_two_line",
            expected=params,
            action={
                "sip_server": sip_server,
                "sip_port": body.sip_port,
                "transport": transport,
                "apply": body.apply,
                "success": ok,
            },
        ),
    )


# ------------------------------------------------------------------ force-register

@router.post(
    "/force-register",
    response_model=ForceRegisterResponse,
    summary="Write ALL SIP P-values (direct + profile system) and return a full debug readback",
)
async def force_register(
    request: Request,
    transport: str = Query("tcp", description="SIP transport, first-pass order TCP→TLS→UDP: tcp, tls, udp"),
    sip_server: str | None = Query(None, description="SIP server address visible from the HT812"),
    sip_port: str | None = Query(None, description="SIP server port; defaults to 5061 for TLS, 5060 otherwise"),
    reboot: bool = Query(False, description="Reboot the HT812 after writing SIP registration settings"),
    write_passwords: bool = Query(True, description="Blind-write SIP password P-values from SIP_1001_PASS/SIP_1002_PASS; values are redacted in logs/responses"),
):
    """
    Writes every SIP-registration-related P-value for both FXS ports — both the
    legacy direct system (P35/P47/P130) AND the firmware-3.7.5 profile system
    (P4060/P4090/P4669/P4150) — then reads them all back so you can see exactly
    what the device accepted. Useful for debugging registration failures.

    Note: SIP auth passwords (P34/P734 and P4120/P4121) are write-only at the
    firmware level. This route can blind-write them from SIP_1001_PASS and
    SIP_1002_PASS, but the HT812 cannot read them back or export them in XML.
    """
    transport_key = transport.lower()
    if transport_key not in _TRANSPORT_VALUES:
        raise HTTPException(400, "transport must be one of: udp, tcp, tls")

    sip_server = sip_server or _DEFAULT_SIP_SERVER
    sip_port = sip_port or ("5061" if transport_key == "tls" else "5060")
    transport_code = _TRANSPORT_VALUES[transport_key]

    params = {
        # ── Legacy direct system (FXS1) ──────────────────────────────────
        "P35":  "1001",          "P36":  "1001",
        "P47":  sip_server,      "P48":  sip_port,
        "P130": transport_code,  "P46":  "60",
        # ── Legacy direct system (FXS2) ──────────────────────────────────
        "P735": "1002",          "P736": "1002",
        "P2312":sip_server,      "P2313":sip_port,
        "P830": transport_code,  "P746": "60",
        "P52":  "2",             # NAT traversal: keep-alive
        # ── Profile system (FXS1, profile row 0) ─────────────────────────
        "P4060":"1001",          "P4090":"1001",
        "P4669":sip_server,      "P4150":"1",
        "P4300":"1",             "P4595":"1",
        # ── Profile system (FXS2, profile row 1) ─────────────────────────
        "P4061":"1002",          "P4091":"1002",
        "P4670":sip_server,      "P4151":"1",
        "P4301":"2",             "P4596":"2",
    }
    password_fields_attempted: list[str] = []
    if write_passwords:
        if _SIP_PASSWORDS["1001"]:
            params["P34"] = _SIP_PASSWORDS["1001"]
            params["P4120"] = _SIP_PASSWORDS["1001"]
            password_fields_attempted.extend(["P34", "P4120"])
        if _SIP_PASSWORDS["1002"]:
            params["P734"] = _SIP_PASSWORDS["1002"]
            params["P4121"] = _SIP_PASSWORDS["1002"]
            password_fields_attempted.extend(["P734", "P4121"])

    REQUEST_COUNT.labels(endpoint="force_register").inc()
    try:
        ok = await _client(request).patch_config(params, apply=True)
    except HT812Error as e:
        _diagnostics(
            request,
            "force_register_error_before_apply",
            expected=params,
            action={
                "transport": transport_key,
                "sip_server": sip_server,
                "sip_port": sip_port,
                "write_passwords": write_passwords,
                "password_fields_attempted": password_fields_attempted,
            },
            error=e,
        )
        raise _handle(e)

    reboot_ok = False
    if reboot:
        try:
            reboot_ok = await _client(request).reboot()
        except HT812Error as e:
            _diagnostics(
                request,
                "force_register_error_reboot",
                expected=params,
                action={
                    "transport": transport_key,
                    "sip_server": sip_server,
                    "sip_port": sip_port,
                    "apply_ok": ok,
                    "write_passwords": write_passwords,
                    "password_fields_attempted": password_fields_attempted,
                },
                error=e,
            )
            raise _handle(e)

    # Read back every written key plus registration status
    readback_keys = [key for key in params.keys() if key not in _WRITE_ONLY_KEYS] + ["P4921", "P4922", "P4901", "P4902", "P8"]
    try:
        readback = await _client(request).get_values(readback_keys)
    except HT812Error as e:
        _diagnostics(
            request,
            "force_register_readback_error",
            expected=params,
            action={
                "transport": transport_key,
                "sip_server": sip_server,
                "sip_port": sip_port,
                "apply_ok": ok,
                "write_passwords": write_passwords,
                "password_fields_attempted": password_fields_attempted,
            },
            error=e,
        )
        readback = {}

    # Capture what the device saw (REGISTER sent? 401? TLS fail? timeout?) and
    # whether Asterisk actually now holds a contact — correlated to this write.
    captured_sip_log = await _capture_sip_log(request, source="force_register")
    asterisk_state = await get_registration_contacts()

    transport_label = {"0": "UDP", "1": "TCP", "2": "TLS"}.get(transport_code, transport.upper())
    diagnostics = _diagnostics(
        request,
        "force_register",
        expected=params,
        live=readback,
        action={
            "transport": transport_label,
            "transport_code": transport_code,
            "sip_server": sip_server,
            "sip_port": sip_port,
            "apply_ok": ok,
            "reboot_requested": reboot,
            "reboot_ok": reboot_ok,
            "write_passwords": write_passwords,
            "password_fields_attempted": password_fields_attempted,
            "password_verification": "not_readable_from_ht812_or_xml; verify through Asterisk REGISTER/auth result",
            "sip_log_debug_path": captured_sip_log.get("debug_log_path"),
            "asterisk_reachable": asterisk_state.get("reachable"),
            "asterisk_both_registered": asterisk_state.get("both_registered"),
            "asterisk_endpoints": asterisk_state.get("endpoints"),
        },
    )
    redacted_params = _redact_sensitive(params)

    event = request.app.state.events.add(CommunicationEventIn(
        source="ht812_api",
        type="force_register",
        message=f"Force-register: wrote {len(params)} P-values. Reg P4921={readback.get('P4921','?')} P4922={readback.get('P4922','?')}",
        data={
            "params_written": redacted_params,
            "readback": readback,
            "apply_ok": ok,
            "reboot_ok": reboot_ok,
            "password_fields_attempted": password_fields_attempted,
            "debug_log_path": diagnostics["debug_log_path"],
        },
    ))
    await request.app.state.event_queue.put(event)

    log.info(
        "force_register",
        sip_server=sip_server,
        transport=transport_label,
        apply_ok=ok,
        reboot=reboot,
        reboot_ok=reboot_ok,
        reg1=readback.get("P4921"),
        reg2=readback.get("P4922"),
    )

    return ForceRegisterResponse(
        success=ok,
        message=(
            f"Wrote {len(params)} P-values ({transport_label} transport). "
            f"Reg status: FXS1={readback.get('P4921','?')} FXS2={readback.get('P4922','?')}. "
            f"Password fields attempted: {', '.join(password_fields_attempted) if password_fields_attempted else 'none'}. "
            "Passwords are write-only on HT812 and are redacted from API output."
        ),
        sip_server=sip_server,
        sip_port=sip_port,
        transport=transport_label,
        params_written=redacted_params,
        readback=readback,
        diagnostics=diagnostics,
    )


@router.get(
    "/diagnostics",
    summary="Offline-safe SIP registration diagnostics from latest backup, with optional live HT812 readback",
)
async def diagnostics_report(
    request: Request,
    transport: str = Query("tcp", description="Expected transport to compare, first-pass order TCP→TLS→UDP: tcp, tls, udp"),
    sip_server: str | None = Query(None, description="Expected SIP server visible from HT812"),
    sip_port: str | None = Query(None, description="Expected SIP port; defaults to 5061 for TLS, 5060 otherwise"),
    live: bool = Query(False, description="When true, also query the HT812. Leave false for offline analysis from backups/debug logs only."),
):
    transport_key = transport.lower()
    if transport_key not in _TRANSPORT_VALUES:
        raise HTTPException(400, "transport must be one of: udp, tcp, tls")
    sip_server = sip_server or _DEFAULT_SIP_SERVER
    sip_port = sip_port or _TRANSPORT_PORTS[transport_key]
    transport_code = _TRANSPORT_VALUES[transport_key]
    expected = _expected_registration_values(transport_key, sip_server, sip_port)

    live_values: dict[str, Any] = {}
    live_error: dict[str, Any] | None = None
    if live:
        try:
            live_values = await _client(request).get_values([key for key in _SIP_DIAG_KEYS if key not in _WRITE_ONLY_KEYS])
        except HT812Error as e:
            live_error = {"type": type(e).__name__, "message": str(e)}

    action = {
        "transport": transport_key,
        "transport_code": transport_code,
        "sip_server": sip_server,
        "sip_port": sip_port,
        "live_requested": live,
        "live_error": live_error,
        "password_fields_configured_in_env": {
            "P34": bool(_SIP_PASSWORDS["1001"]),
            "P734": bool(_SIP_PASSWORDS["1002"]),
            "P4120": bool(_SIP_PASSWORDS["1001"]),
            "P4121": bool(_SIP_PASSWORDS["1002"]),
        },
        "browser_payload_cross_check": {
            "observed_from_user_screenshot": [
                "P4060=1001",
                "P4061=1002",
                "P4090=1001",
                "P4091=1002",
                "P4120=<password>",
                "P4121=<password>",
                "P4150=1",
                "P4151=1",
                "P4300=1",
                "P4301=2",
                "P4669=192.168.2.2",
                "P4670=192.168.2.2",
            ],
            "conclusion": "The API now writes the same profile password P-values P4120/P4121 that the HT812 web UI posts, and the dashboard should target the Asterisk host at 192.168.2.2 instead of the HT812 router address.",
        },
    }
    diagnostics = _diagnostics(
        request,
        "diagnostics",
        expected=expected,
        live=live_values,
        action=action,
    )
    return {
        "success": live_error is None,
        "message": (
            "Offline diagnostics generated from latest backup/debug logs."
            if not live
            else "Live diagnostics generated; see live_error if the HT812 was unreachable."
        ),
        "diagnostics": diagnostics,
    }


@router.get(
    "/status/audit",
    summary="Offline-safe registration audit from backups and debug logs, with optional live HT812 comparison",
)
async def status_audit(
    request: Request,
    transport: str = Query("tcp", description="Expected transport to audit, first-pass order TCP→TLS→UDP: tcp, tls, udp"),
    sip_server: str | None = Query(None, description="Expected SIP server visible from the HT812"),
    sip_port: str | None = Query(None, description="Expected SIP port; defaults to 5061 for TLS, 5060 otherwise"),
    live: bool = Query(True, description="When true (default), query the HT812 for live values; falls back to latest backup offline."),
):
    transport_key = transport.lower()
    if transport_key not in _TRANSPORT_VALUES:
        raise HTTPException(400, "transport must be one of: udp, tcp, tls")

    sip_server = sip_server or _DEFAULT_SIP_SERVER
    sip_port = sip_port or _TRANSPORT_PORTS[transport_key]
    expected = _expected_registration_values(transport_key, sip_server, sip_port)
    live_values: dict[str, Any] = {}
    live_error: dict[str, Any] | None = None
    captured_sip_log: dict[str, Any] | None = None
    if live:
        try:
            live_values = await _client(request).get_values([key for key in _SIP_DIAG_KEYS if key not in _WRITE_ONLY_KEYS])
        except HT812Error as e:
            live_error = {"type": type(e).__name__, "message": str(e)}
        # Capture the device SIP trace alongside this audit (offline-safe).
        captured_sip_log = await _capture_sip_log(request, source="status_audit")

    # Asterisk-side truth — does Asterisk hold a live contact right now?
    asterisk_state = await get_registration_contacts()

    # snapshot_source: live values are only trustworthy if the live read succeeded.
    device_offline = live and live_error is not None
    snapshot_source = "live" if (live and live_error is None) else "latest_backup"

    audit = _registration_audit(
        expected=expected,
        live_values=live_values if live_error is None else None,
        transport=transport_key,
        sip_server=sip_server,
        sip_port=sip_port,
        asterisk_state=asterisk_state,
        captured_sip_log=captured_sip_log,
    )
    audit["snapshot_source"] = snapshot_source
    audit["device_offline"] = device_offline
    diagnostics = _diagnostics(
        request,
        "status_audit",
        expected=expected,
        live=live_values,
        action={
            "transport": transport_key,
            "sip_server": sip_server,
            "sip_port": sip_port,
            "live_requested": live,
            "live_error": live_error,
            "snapshot_source": snapshot_source,
            "asterisk_reachable": asterisk_state.get("reachable"),
            "asterisk_both_registered": asterisk_state.get("both_registered"),
            "sip_log_debug_path": captured_sip_log.get("debug_log_path") if captured_sip_log else None,
        },
    )
    return {
        "success": live_error is None,
        "message": (
            "Offline registration audit generated from backups/debug logs."
            if not live
            else "Live registration audit generated; see live_error if the HT812 was unreachable."
        ),
        "audit": audit,
        "diagnostics": diagnostics,
        "live_requested": live,
        "live_error": live_error,
        "offline": device_offline,
        "snapshot_source": snapshot_source,
    }


@router.get(
    "/diag/registration-bundle",
    summary="Single offline-safe JSON blob aggregating all registration evidence for AI/CLI inspection",
)
async def registration_bundle(request: Request):
    """
    Assembles every registration artifact into one blob: the latest force-register
    debug log, the registration audit (three-way verdict), the latest device SIP
    trace, and the live Asterisk endpoint state. Runs fully offline — any
    unreachable source degrades to a marker rather than failing the request.
    """
    REQUEST_COUNT.labels(endpoint="registration_bundle").inc()
    asterisk_state = await get_registration_contacts()
    latest_force = _latest_debug_log("force_register")
    latest_sip = _latest_debug_log("sip_log")
    audit = _registration_audit(asterisk_state=asterisk_state)
    audit["snapshot_source"] = "latest_backup"
    return {
        "generated_at": _now_iso(),
        "verdict": audit.get("verdict"),
        "audit": audit,
        "asterisk": asterisk_state,
        "latest_force_register": _read_json_file(latest_force) or {},
        "latest_sip_log": _read_json_file(latest_sip) or {},
        "debug_logs_dir": str(_DEBUG_DIR),
        "how_to_read": (
            "verdict is the headline. audit.asterisk = Asterisk-side contact truth; "
            "audit.device = HT812 self-reported flags; audit.comparison = written vs expected config; "
            "audit.sip_log.raw = device SIP trace. All sources are offline-safe."
        ),
    }


# ------------------------------------------------------------------ actions

@router.post("/reboot", response_model=ActionResponse, summary="Reboot the device (~30s downtime)")
async def reboot(request: Request):
    REQUEST_COUNT.labels(endpoint="reboot").inc()
    try:
        ok = await _client(request).reboot()
    except HT812Error as e:
        _diagnostics(request, "reboot_error", error=e)
        raise _handle(e)
    diagnostics = _diagnostics(request, "reboot", action={"success": ok})
    log.warning("device_reboot_triggered", success=ok)
    return ActionResponse(
        success=ok,
        message="Reboot initiated" if ok else "Non-success response from device",
        diagnostics=diagnostics,
    )


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
        _diagnostics(request, "factory_reset_error", action={"reset_type": reset_type}, error=e)
        raise _handle(e)
    diagnostics = _diagnostics(request, "factory_reset", action={"reset_type": reset_type, "success": ok})
    log.warning("device_factory_reset_triggered", reset_type=reset_type, success=ok)
    return ActionResponse(
        success=ok,
        message="Factory reset initiated" if ok else "Non-success response from device",
        diagnostics=diagnostics,
    )


# ------------------------------------------------------------------ status

@router.get("/status/ports", summary="FXS port SIP registration and hook status")
async def port_status(request: Request):
    REQUEST_COUNT.labels(endpoint="port_status").inc()
    try:
        data = await _client(request).get_port_status()
    except HT812Error as e:
        latest = _latest_backup()
        backup_values = _parse_backup_values(latest)
        diagnostics = _diagnostics(request, "port_status", live={}, error=e)
        return {
            **_port_snapshot_from_values(backup_values, offline=True),
            "diagnostics": diagnostics,
            "error": {
                "message": str(e),
                "offline": True,
            },
        }
    live = data.get("raw", {}) if isinstance(data, dict) else {}
    return {**data, "diagnostics": _diagnostics(request, "port_status", live=live)}


@router.get("/status/summary", summary="Combined status for the web dashboard")
async def status_summary(request: Request):
    REQUEST_COUNT.labels(endpoint="status_summary").inc()
    try:
        ports = await _client(request).get_port_status()
    except HT812Error as e:
        latest = _latest_backup()
        backup_values = _parse_backup_values(latest)
        diagnostics = _diagnostics(request, "status_summary", live={}, error=e)
        return {
            "expected": {
                "transport": "selected by force-register/provision request",
                "sip_port": "5060 for UDP/TCP, 5061 for TLS unless overridden",
                "extensions": ["1001", "1002"],
                "write_only_password_fields": ["P34", "P734", "P4120", "P4121"],
                "password_env_available": {
                    "SIP_1001_PASS": bool(_SIP_PASSWORDS["1001"]),
                    "SIP_1002_PASS": bool(_SIP_PASSWORDS["1002"]),
                },
            },
            "ports": _port_snapshot_from_values(backup_values, offline=True),
            "audit": _registration_audit(),
            "diagnostics": diagnostics,
            "offline": True,
            "error": {
                "message": str(e),
                "offline": True,
            },
        }
    live = ports.get("raw", {}) if isinstance(ports, dict) else {}
    return {
        "expected": {
            "transport": "selected by force-register/provision request",
            "sip_port": "5060 for UDP/TCP, 5061 for TLS unless overridden",
            "extensions": ["1001", "1002"],
            "write_only_password_fields": ["P34", "P734", "P4120", "P4121"],
            "password_env_available": {
            "SIP_1001_PASS": bool(_SIP_PASSWORDS["1001"]),
            "SIP_1002_PASS": bool(_SIP_PASSWORDS["1002"]),
            },
        },
        "ports": ports,
        "audit": _registration_audit(),
        "diagnostics": _diagnostics(request, "status_summary", live=live),
        "offline": False,
    }


@router.get("/status/system", summary="Product, vendor info from device")
async def system_info(request: Request):
    REQUEST_COUNT.labels(endpoint="system_info").inc()
    try:
        data = await _client(request).get_system_info()
    except HT812Error as e:
        _diagnostics(request, "system_info_error", error=e)
        raise _handle(e)
    return {"system": data, "diagnostics": _diagnostics(request, "system_info", action={"system": data})}


@router.get("/status/network", summary="Network P-values: IP, subnet, gateway")
async def net_status(request: Request):
    REQUEST_COUNT.labels(endpoint="net_status").inc()
    try:
        data = await _client(request).get_net_status()
    except HT812Error as e:
        _diagnostics(request, "net_status_error", error=e)
        raise _handle(e)
    live = data.get("raw", {}) if isinstance(data, dict) else {}
    return {**data, "diagnostics": _diagnostics(request, "net_status", live=live)}


@router.get("/status/sip-log", summary="Live SIP trace log from the device")
async def sip_log(request: Request):
    REQUEST_COUNT.labels(endpoint="sip_log").inc()
    try:
        text = await _client(request).get_sip_log()
    except HT812Error as e:
        diagnostics = _diagnostics(request, "sip_log", action={"sip_log_empty": True}, error=e)
        return {
            "sip_log_raw": "",
            "sip_log_empty": True,
            "offline": True,
            "audit": _registration_audit(),
            "error": {
                "message": str(e),
                "offline": True,
            },
            "diagnostics": diagnostics,
        }
    empty = _sip_log_is_empty(text)
    diagnostics = _diagnostics(request, "sip_log", action={"sip_log_empty": empty})
    return {
        "sip_log_raw": text,
        "sip_log_empty": empty,
        "device_sent_no_sip": empty,
        "offline": False,
        "audit": _registration_audit(),
        "diagnostics": diagnostics,
    }


# ------------------------------------------------------------------ helpers

def _update_backup_gauge() -> None:
    try:
        count = len(list(_BACKUP_DIR.glob("ht812_config_*.xml")))
        BACKUP_FILE_COUNT.set(count)
    except Exception:
        pass
