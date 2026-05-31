#!/usr/bin/env python3
"""
Offline audit for HT812 registration state.

Reads the latest XML backup and debug logs from ./backups and prints a compact
verdict that separates:
  - config write acknowledged
  - SIP trace observed
  - HT812 registered or not
  - Asterisk contacts present or not

Optional: run `docker exec asterisk asterisk -rx "pjsip show contacts"` to
check whether Asterisk saw REGISTER contacts.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BACKUP_DIR = ROOT / "backups"
DEBUG_DIR = BACKUP_DIR / "debug_logs"

P_VALUE_KEYS = [
    "P35", "P36", "P47", "P48", "P130", "P46",
    "P735", "P736", "P2312", "P2313", "P830", "P746",
    "P52", "P4060", "P4061", "P4090", "P4091", "P4120", "P4121",
    "P4150", "P4151", "P4300", "P4301", "P4595", "P4596", "P4669", "P4670",
    "P4921", "P4922", "P4901", "P4902", "P8",
]


def latest(path: Path, pattern: str) -> Path | None:
    files = sorted(path.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def parse_backup_values(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    text = path.read_text(errors="ignore")
    values: dict[str, str] = {}
    for key in P_VALUE_KEYS:
        start = f"<{key}>"
        end = f"</{key}>"
        if start in text and end in text:
            try:
                values[key] = text.split(start, 1)[1].split(end, 1)[0].strip()
            except Exception:
                continue
    return values


def reg_state(values: dict[str, str]) -> dict[str, Any]:
    reg1 = str(values.get("P4921", ""))
    reg2 = str(values.get("P4922", ""))
    registered = reg1.lower() in ("1", "registered") and reg2.lower() in ("1", "registered")
    return {
        "registered": registered,
        "fxs1": reg1 or None,
        "fxs2": reg2 or None,
    }


def docker_contacts() -> dict[str, Any]:
    cmd = ["docker", "exec", "asterisk", "asterisk", "-rx", "pjsip show contacts"]
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        return {"available": False, "error": "docker not found"}
    text = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 or "permission denied" in text.lower():
        return {
            "available": False,
            "returncode": result.returncode,
            "raw": text.strip(),
            "error": "docker permission denied",
        }
    return {
        "available": True,
        "returncode": result.returncode,
        "raw": text.strip(),
        "has_contacts": "No objects found." not in text and bool(text.strip()),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Offline HT812 registration audit")
    ap.add_argument("--check-asterisk", action="store_true", help="Also run docker exec asterisk pjsip show contacts")
    args = ap.parse_args()

    backup = latest(BACKUP_DIR, "ht812_config_*.xml")
    force_log = latest(DEBUG_DIR, "*_force_register.json")
    sip_log = latest(DEBUG_DIR, "*_sip_log.json")
    summary_log = latest(DEBUG_DIR, "*_status_summary.json")

    backup_values = parse_backup_values(backup)
    force_data = read_json(force_log) or {}
    sip_data = read_json(sip_log) or {}
    summary_data = read_json(summary_log) or {}

    force_action = force_data.get("action", {}) if isinstance(force_data, dict) else {}
    if not isinstance(force_action, dict):
        force_action = {}
    force_readback = force_data.get("readback", {}) if isinstance(force_data, dict) else {}
    if not isinstance(force_readback, dict):
        force_readback = {}

    snapshot_values = force_readback or backup_values

    audit = {
        "backup": {
            "path": str(backup) if backup else None,
            "values": backup_values,
        },
        "force_register": {
            "path": str(force_log) if force_log else None,
            "applied": bool(force_action.get("apply_ok") or force_action.get("success")),
            "transport": force_action.get("transport"),
            "sip_server": force_action.get("sip_server"),
            "sip_port": force_action.get("sip_port"),
            "password_fields_attempted": force_action.get("password_fields_attempted", []),
            "readback": snapshot_values,
        },
        "sip_log": {
            "path": str(sip_log) if sip_log else None,
            "offline": bool(sip_data.get("offline")) if isinstance(sip_data, dict) else False,
            "empty": bool(sip_data.get("sip_log_empty")) if isinstance(sip_data, dict) else None,
        },
        "summary": {
            "path": str(summary_log) if summary_log else None,
            "offline": bool(summary_data.get("offline")) if isinstance(summary_data, dict) else None,
        },
        "registration": reg_state(snapshot_values),
    }

    audit["verdict"] = (
        "registered"
        if audit["registration"]["registered"]
        else "config_written_but_no_register_observed"
        if audit["force_register"]["applied"] and (
            audit["sip_log"]["offline"] or audit["sip_log"]["empty"] is False or audit["sip_log"]["empty"]
        )
        else "config_written"
        if audit["force_register"]["applied"]
        else "no_force_register_audit_found"
    )

    if args.check_asterisk:
        audit["asterisk_contacts"] = docker_contacts()

    print(json.dumps(audit, indent=2, sort_keys=True))
    print()
    print(f"Verdict: {audit['verdict']}")
    print(f"Backup: {audit['backup']['path'] or 'none'}")
    print(f"Force register applied: {audit['force_register']['applied']}")
    print(f"SIP trace: {'offline' if audit['sip_log']['offline'] else 'empty' if audit['sip_log']['empty'] else 'present/unknown'}")
    print(f"Registered: {audit['registration']['registered']}")
    if args.check_asterisk:
        contacts = audit.get("asterisk_contacts", {})
        print(f"Asterisk contacts: {'present' if contacts.get('has_contacts') else 'none'}")
        if contacts.get("raw"):
            print(contacts["raw"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
