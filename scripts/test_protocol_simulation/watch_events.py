#!/usr/bin/env python3
"""
Watch the ht812_api SSE event stream and print all events to stdout.

Subscribes to /events/stream (Server-Sent Events) and formats each
incoming event with type-specific icons, DTMF digit highlighting,
and FXS hook-state transitions.

Event types printed:
  dtmf            — DTMF digit received on a channel (digit shown in blue)
  fxs_hook        — FXS phone hook state change (on-hook / off-hook)
  call_start      — Asterisk channel answered
  stasis_start    — Channel entered ARI Stasis app
  hangup          — Call terminated
  channel_destroyed — Asterisk channel fully torn down
  route           — IVR routing decision
  bridge          — Bridge created between channels
  provision       — HT812 SIP settings applied via API
  timeout         — IVR menu timed out waiting for digit

Usage:
  python watch_events.py
  python watch_events.py --api http://localhost:8000

Requirements:
  pip install httpx
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import _env  # loads project .env into os.environ

try:
    import httpx
except ImportError:
    sys.exit("Install httpx first:  pip install httpx")

R = "\033[0m"
COLORS = {
    "dtmf":             "\033[34;1m",
    "fxs_hook":         "\033[32;1m",
    "call_start":       "\033[32m",
    "stasis_start":     "\033[32m",
    "hangup":           "\033[31m",
    "channel_destroyed":"\033[31m",
    "provision":        "\033[35m",
    "route":            "\033[33m",
    "bridge":           "\033[33m",
    "error":            "\033[31;1m",
    "timeout":          "\033[90m",
}
ICONS = {
    "dtmf":             "⌨ ",
    "fxs_hook":         "☎ ",
    "call_start":       "↗ ",
    "stasis_start":     "↗ ",
    "hangup":           "✗ ",
    "channel_destroyed":"✗ ",
    "provision":        "⚙ ",
    "route":            "→ ",
    "bridge":           "⇌ ",
    "error":            "✗ ",
    "timeout":          "⏱ ",
}
DIM = "\033[2m"


def _fmt(raw: str) -> str:
    try:
        ev = json.loads(raw)
    except json.JSONDecodeError:
        return f"  [parse error] {raw}"

    ts_raw = ev.get("created_at", "")
    try:
        t = datetime.fromisoformat(
            ts_raw.replace("Z", "+00:00")
        ).astimezone().strftime("%H:%M:%S")
    except Exception:
        t = ts_raw[:8]

    etype   = ev.get("type", "unknown")
    source  = ev.get("source", "")
    message = ev.get("message", "")
    digit   = ev.get("digit")
    line    = ev.get("line")
    caller  = ev.get("caller")
    data    = ev.get("data", {})

    color = COLORS.get(etype, "\033[37m")
    icon  = ICONS.get(etype, "· ")

    # Normalize line to "1" or "2"
    if line in ("1001",): line = "1"
    if line in ("1002",): line = "2"

    lines: list[str] = []
    lines.append(
        f"  {DIM}{t}{R}  "
        f"{color}{icon}{etype:<22s}{R}  "
        f"{message}"
    )

    extras: list[str] = []
    if digit:
        extras.append(f"\033[34;1mDTMF={digit}{R}")
    if line:
        extras.append(f"line={line}")
    if caller and caller != line:
        extras.append(f"caller={caller}")
    if source:
        extras.append(f"{DIM}src={source}{R}")

    if extras:
        lines.append("    " + "  ".join(extras))

    # FXS hook detail row
    if etype == "fxs_hook" and isinstance(data, dict):
        port      = data.get("port", "?")
        prev_lbl  = data.get("prev_label", "?")
        curr_lbl  = data.get("hook_label", "?")
        reg       = "registered" if data.get("registered") else "unregistered"
        raw_code  = data.get("hook_state", "?")
        lines.append(
            f"    {color}FXS port {port}:  "
            f"{prev_lbl} → {curr_lbl}  "
            f"[P490{port}={raw_code!r}]  [{reg}]{R}"
        )

    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Watch ht812_api SSE event stream")
    ap.add_argument(
        "--api", default="http://localhost:8000",
        help="API base URL (default: http://localhost:8000)",
    )
    args = ap.parse_args()

    url = f"{args.api.rstrip('/')}/events/stream"
    print(f"\nConnecting to {url} …")
    print(
        "  Legend:  ⌨ =DTMF  ☎ =FXS hook  ↗ =call  → =route  ⇌ =bridge  ⚙ =provision\n"
    )
    print("─" * 72)

    with httpx.Client(timeout=None) as client:
        try:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                print("  Connected.  Waiting for events…\n")
                buf = ""
                for chunk in resp.iter_text():
                    buf += chunk
                    while "\n\n" in buf:
                        block, buf = buf.split("\n\n", 1)
                        for ln in block.splitlines():
                            if ln.startswith("data: "):
                                print(_fmt(ln[6:]))
                            # ": keepalive" lines are silently ignored
        except httpx.ConnectError:
            sys.exit(
                f"\nCannot connect to {url}\n"
                "  Is ht812_api running?  →  docker compose up ht812_api\n"
            )
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
