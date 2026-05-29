#!/usr/bin/env python3
"""
Dynamic IVR AGI script.

Asterisk calls this via: AGI(ivr_dynamic.py)
Communication is via stdin/stdout using the AGI protocol.

Logic:
  - Time-of-day routing: business hours → sales menu, after-hours → voicemail
  - VIP caller IDs can be routed directly
  - Falls back to basic DTMF menu if no rule matches
"""

import sys
from datetime import datetime, timezone, timedelta

BUSINESS_START = 9   # 09:00 local
BUSINESS_END = 17    # 17:00 local
LOCAL_TZ_OFFSET = -8  # hours from UTC — adjust for your timezone

VIP_CALLERS = {
    # "5551234567": "1001",  # route this caller directly to extension 1001
}


def send(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def recv() -> str:
    return sys.stdin.readline().strip()


def get_variable(name: str) -> str:
    send(f"GET VARIABLE {name}")
    response = recv()
    # Response: 200 result=1 (value)
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
    # Response: 200 result=<ascii_code>
    try:
        code = int(resp.split("=")[-1].strip())
        return chr(code) if code > 0 else ""
    except (ValueError, IndexError):
        return ""


def is_business_hours() -> bool:
    now_utc = datetime.now(timezone.utc)
    local_offset = timedelta(hours=LOCAL_TZ_OFFSET)
    local_now = now_utc + local_offset
    return BUSINESS_START <= local_now.hour < BUSINESS_END and local_now.weekday() < 5


def main() -> None:
    # Read AGI environment variables sent by Asterisk
    env: dict[str, str] = {}
    while True:
        line = sys.stdin.readline().strip()
        if not line:
            break
        if ":" in line:
            key, _, val = line.partition(":")
            env[key.strip()] = val.strip()

    caller_id = env.get("agi_callerid", "unknown")

    # VIP routing
    if caller_id in VIP_CALLERS:
        dest = VIP_CALLERS[caller_id]
        exec_app("Dial", f"PJSIP/{dest},30")
        send("HANGUP")
        return

    if not is_business_hours():
        # After hours → leave a voicemail
        playback("after-hours")          # custom sound; falls back silently if missing
        exec_app("VoiceMail", "1001@default,u")
        send("HANGUP")
        return

    # Business hours → dynamic prompt + digit collection
    playback("press-1-sales")
    digit = wait_digit(8)

    if digit == "1":
        exec_app("Dial", "PJSIP/1001,30,tT")
    elif digit == "2":
        exec_app("Dial", "PJSIP/1002,30,tT")
    elif digit == "0":
        exec_app("Dial", f"PJSIP/1001,30,tT")
    else:
        # No valid input — fall back to main DTMF IVR
        goto("ivr-main", "s", "1")
        return

    send("HANGUP")


if __name__ == "__main__":
    main()
