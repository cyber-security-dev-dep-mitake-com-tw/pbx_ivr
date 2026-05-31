#!/usr/bin/env python3
"""
Send DTMF digits to an active Asterisk channel via ARI.

Lists current channels and lets you pick one (or supply --channel),
then injects each digit with configurable timing and prints the result.

IVR menu map
────────────
  Main menu   (ivr-main / ivr-app Stasis):
    1 → Sales sub-menu       2 → Support sub-menu
    0 → Operator (1001)      8 → Voicemail
    9 → AGI IVR              * → ARI Stasis IVR
    # → Repeat menu

  Sales sub-menu:
    1 → New inquiry (→ 1001)  2 → Existing order (→ 1001)  0 → Back

  Support sub-menu:
    1 → Billing (→ 1002)      2 → Technical (→ 1002)       0 → Back

Examples:
  # Send digit 1 to the only active channel
  python send_dtmf_to_ivr.py --digits 1

  # Walk Sales→new-inquiry on a specific channel
  python send_dtmf_to_ivr.py --digits 11 --channel <channel-id>

  # Support→billing with 0.5 s between digits
  python send_dtmf_to_ivr.py --digits 21 --delay 0.5

Requirements:
  pip install httpx
"""

import argparse
import asyncio
import os
import sys

import _env  # loads project .env into os.environ

try:
    import httpx
except ImportError:
    sys.exit("Install httpx first:  pip install httpx")

BOLD  = "\033[1m"
BLUE  = "\033[34;1m"
GREEN = "\033[32m"
DIM   = "\033[2m"
R     = "\033[0m"


async def _list_channels(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get("/channels")
    r.raise_for_status()
    return r.json()


async def _send(client: httpx.AsyncClient, channel_id: str, digit: str, duration: int) -> bool:
    r = await client.post(
        f"/channels/{channel_id}/dtmf",
        params={"dtmf": digit, "before": 0, "between": 100, "duration": duration, "after": 0},
    )
    ok = r.status_code in (200, 201, 204)
    if not ok:
        print(f"  {DIM}Warning: ARI returned {r.status_code} for {digit!r}: {r.text[:80]}{R}")
    return ok


async def main() -> None:
    ap = argparse.ArgumentParser(description="Send DTMF digits to an Asterisk ARI channel")
    ap.add_argument("--ari",      default="http://localhost:8088/ari",
                    help="ARI base URL (default: http://localhost:8088/ari)")
    ap.add_argument("--user",     default=os.environ.get("ARI_USER", "ari-user"))
    ap.add_argument("--password", default=os.environ.get("ARI_PASS", "changeme_ari"))
    ap.add_argument("--channel",  default=None,
                    help="Channel ID — omit to list and pick")
    ap.add_argument("--digits",   required=True,
                    help="Digit string to inject, e.g. '21' for Support→billing")
    ap.add_argument("--delay",    type=float, default=0.8,
                    help="Seconds between digits (default: 0.8)")
    ap.add_argument("--duration", type=int,   default=120,
                    help="DTMF tone duration ms (default: 120)")
    args = ap.parse_args()

    async with httpx.AsyncClient(
        base_url=args.ari,
        auth=(args.user, args.password),
        timeout=10.0,
    ) as client:
        channel_id = args.channel

        if not channel_id:
            try:
                channels = await _list_channels(client)
            except httpx.ConnectError:
                sys.exit(
                    f"\nCannot reach ARI at {args.ari}\n"
                    "  Is Asterisk running?  →  docker compose up asterisk\n"
                )

            if not channels:
                sys.exit(
                    "\nNo active channels.\n"
                    "  Make a call first (pick up the handset on an FXS port),\n"
                    "  or run simulate_call_flow.py to originate a test call.\n"
                )

            print(f"\n{BOLD}Active channels ({len(channels)}){R}\n")
            for i, ch in enumerate(channels):
                cid   = ch.get("id", "")
                state = ch.get("state", "")
                num   = ch.get("caller", {}).get("number", "")
                name  = ch.get("name", "")
                print(f"  [{i}]  {DIM}{cid[:36]}{R}  state={state}  caller={num}  {DIM}{name}{R}")

            if len(channels) == 1:
                channel_id = channels[0]["id"]
                print(f"\n  Auto-selected: {channel_id}")
            else:
                idx = input("\n  Select channel index: ").strip()
                channel_id = channels[int(idx)]["id"]

        print(f"\n{BOLD}Sending DTMF: {BLUE}{args.digits}{R}  →  channel {channel_id[:36]}")
        print(f"{DIM}  delay={args.delay}s  duration={args.duration}ms  ARI={args.ari}{R}\n")

        sent = 0
        for i, digit in enumerate(args.digits):
            ok = await _send(client, channel_id, digit, args.duration)
            icon = f"{GREEN}✓{R}" if ok else "✗"
            print(f"  {icon}  {BLUE}[{digit}]{R}  digit {i+1}/{len(args.digits)}")
            sent += ok
            if i < len(args.digits) - 1:
                await asyncio.sleep(args.delay)

        print(f"\n  {sent}/{len(args.digits)} digit(s) sent successfully.")
        print(
            f"  {DIM}Watch the web dashboard (Protocol tab) or run watch_events.py "
            f"to see IVR routing events.{R}\n"
        )


if __name__ == "__main__":
    asyncio.run(main())
