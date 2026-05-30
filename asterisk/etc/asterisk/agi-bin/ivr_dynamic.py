#!/usr/bin/env python3
"""
Dynamic IVR AGI script.

Asterisk calls this via: AGI(ivr_dynamic.py)
Communication is via stdin/stdout using the AGI protocol.

Logic:
  - Time-of-day routing: business hours → sales menu, after-hours → voicemail
  - VIP caller IDs loaded from /etc/asterisk/vip_callers.json (or AGI_VIP_FILE env)
  - Falls back to basic DTMF menu if no rule matches

VIP file format (JSON):
  { "5551234567": "1001", "5559876543": "1002" }
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BUSINESS_START = int(os.environ.get("BUSINESS_START", "9"))
BUSINESS_END = int(os.environ.get("BUSINESS_END", "17"))
LOCAL_TZ_OFFSET = int(os.environ.get("LOCAL_TZ_OFFSET", "-8"))

_VIP_FILE = Path(os.environ.get("AGI_VIP_FILE", "/etc/asterisk/vip_callers.json"))


def _load_vip_callers() -> dict[str, str]:
    try:
        if _VIP_FILE.exists():
            return json.loads(_VIP_FILE.read_text())
    except Exception:
        pass
    return {}


def send(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def recv() -> str:
    return sys.stdin.readline().strip()


def get_variable(name: str) -> str:
    send(f"GET VARIABLE {name}")
    response = recv()
    if "(" in response and ")" in response:
        return response.split("(", 1)[1].rstrip(")")
    return ""


def exec_app(app: str, args: str = "") -> str:
    send(f"EXEC {app} {args}")
    return recv()


def goto(context: str, exten: str, priority: str = "1") -> None:
    exec_app("Goto", f"{context},{exten},{priority}")


def playback(sound: str) -> None:
    exec_app("Playback", sound)


def wait_digit(timeout: int = 5) -> str:
    send(f"WAIT FOR DIGIT {timeout * 1000}")
    resp = recv()
    try:
        code_str = resp.split("=")[-1].strip().split()[0]
        code = int(code_str)
        return chr(code) if code > 0 else ""
    except (ValueError, IndexError):
        return ""


def is_business_hours() -> bool:
    now_utc = datetime.now(timezone.utc)
    local_offset = timedelta(hours=LOCAL_TZ_OFFSET)
    local_now = now_utc + local_offset
    return BUSINESS_START <= local_now.hour < BUSINESS_END and local_now.weekday() < 5


def main() -> None:
    env: dict[str, str] = {}
    while True:
        line = sys.stdin.readline().strip()
        if not line:
            break
        if ":" in line:
            key, _, val = line.partition(":")
            env[key.strip()] = val.strip()

    caller_id = env.get("agi_callerid", "unknown")
    vip_callers = _load_vip_callers()

    if caller_id in vip_callers:
        dest = vip_callers[caller_id]
        exec_app("Dial", f"PJSIP/{dest},30")
        send("HANGUP")
        return

    if not is_business_hours():
        playback("after-hours")
        exec_app("VoiceMail", "1001@default,u")
        send("HANGUP")
        return

    # Business hours — play menu and collect digit
    playback("press-1-sales")
    digit = wait_digit(8)

    if digit == "1":
        exec_app("Dial", "PJSIP/1001,30,tT")
    elif digit == "2":
        exec_app("Dial", "PJSIP/1002,30,tT")
    elif digit == "0":
        exec_app("Dial", "PJSIP/1001,30,tT")
    else:
        goto("ivr-main", "s", "1")
        return

    send("HANGUP")


if __name__ == "__main__":
    main()
