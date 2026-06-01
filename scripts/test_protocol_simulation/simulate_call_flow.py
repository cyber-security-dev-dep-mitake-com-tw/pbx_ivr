#!/usr/bin/env python3
"""
Simulate a full IVR call flow via Asterisk ARI.

Originates a test channel into the 'ivr-app' Stasis application,
waits for StasisStart, then injects DTMF digits on a configurable
schedule to walk through the IVR menu tree.

All ARI events and DTMF key codes are printed in real-time.
Routing events published to ht812_api are visible in watch_events.py
and the web dashboard Protocol / Timeline tabs.

Scenarios
─────────
  support   Press 2 (Support) → 1 (Billing)      [default]
  sales     Press 1 (Sales)   → 1 (New inquiry)
  operator  Press 0 → direct to operator (1001)
  main      Press # → repeat main menu

IVR menu map (Stasis app)
─────────────────────────
  Welcome prompt → digit:
    1 → Sales sub-menu        2 → Support sub-menu
    0 → Operator (PJSIP/1001) 8 → Voicemail
    # → Repeat                timeout → hangup

  Sales:    1=new inquiry → 1001   2=existing order → 1001   0=back
  Support:  1=billing → 1002       2=technical → 1002         0=back

Usage:
  python simulate_call_flow.py
  python simulate_call_flow.py --scenario sales
  python simulate_call_flow.py --caller 1002 --scenario operator

Requirements:
  pip install httpx websockets
"""

import argparse
import asyncio
import json
import os
import sys
import urllib.parse
from datetime import datetime

import _env  # loads project .env into os.environ

try:
    import httpx
    import websockets
except ImportError:
    sys.exit("Install requirements:  pip install httpx websockets")

BOLD  = "\033[1m"
DIM   = "\033[2m"
GREEN = "\033[32;1m"
BLUE  = "\033[34;1m"
YELLOW= "\033[33m"
RED   = "\033[31m"
R     = "\033[0m"

# Each scenario: list of (wait_seconds_after_stasis_start, digit)
SCENARIOS: dict[str, list[tuple[float, str]]] = {
    "support":  [(3.5, "2"), (3.0, "1")],
    "sales":    [(3.5, "1"), (3.0, "1")],
    "operator": [(3.5, "0")],
    "main":     [(3.5, "#")],
}


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


async def _originate(client: httpx.AsyncClient, caller: str) -> dict:
    r = await client.post(
        "/channels",
        params={
            # DTMF injected on the ;2 leg reaches the Stasis ;1 leg as a received
            # digit (ChannelDtmfReceived) that ari_app acts on.
            "endpoint":  "Local/s@ivr-main",
            "app":       "ivr-app",
            "callerId":  caller,
            "timeout":   30,
        },
    )
    if r.status_code not in (200, 201):
        sys.exit(f"\nOriginate failed: HTTP {r.status_code}\n{r.text[:300]}\n")
    return r.json()


async def _find_sibling_leg(client: httpx.AsyncClient, stasis_name: str) -> str | None:
    """
    Given the Stasis ;1 leg name (e.g. 'Local/s@ivr-main-00000003;1'), find the
    id of its ;2 sibling. DTMF sent on ;2 is received on ;1 → ari_app sees it.
    """
    if not stasis_name.endswith(";1"):
        return None
    sibling_name = stasis_name[:-2] + ";2"
    try:
        r = await client.get("/channels")
        for ch in r.json():
            if ch.get("name") == sibling_name:
                return ch.get("id")
    except Exception:
        pass
    return None


async def _dtmf(client: httpx.AsyncClient, channel_id: str, digit: str) -> None:
    r = await client.post(
        f"/channels/{channel_id}/dtmf",
        params={"dtmf": digit, "duration": 150, "between": 100},
    )
    icon = f"{GREEN}✓{R}" if r.status_code in (200, 201, 204) else f"{RED}✗{R}"
    print(f"  [{ts()}] {icon} DTMF injected: {BLUE}[{digit}]{R}  (ARI {r.status_code})")


async def _hangup(client: httpx.AsyncClient, channel_id: str) -> None:
    try:
        await client.delete(f"/channels/{channel_id}")
    except Exception:
        pass


async def _run(ari_url: str, ws_url: str, user: str, password: str, caller: str, scenario: str) -> None:
    steps = SCENARIOS[scenario]

    async with httpx.AsyncClient(
        base_url=ari_url,
        auth=(user, password),
        timeout=15.0,
    ) as client:
        print(f"[{ts()}] Originating channel into ivr-app  (caller={caller}) …")
        try:
            channel = await _originate(client, caller)
        except httpx.ConnectError:
            sys.exit(
                f"\nCannot reach ARI at {ari_url}\n"
                "  Is Asterisk running?  →  docker compose up asterisk\n"
            )

        channel_id = channel.get("id", "")
        print(f"[{ts()}] Channel created: {DIM}{channel_id}{R}")
        print(f"[{ts()}] Waiting for StasisStart …\n")

        deadline = asyncio.get_event_loop().time() + 30
        seq_task: asyncio.Task | None = None

        async def _inject_sequence(inject_id: str) -> None:
            base = asyncio.get_event_loop().time()
            for wait, digit in steps:
                elapsed = asyncio.get_event_loop().time() - base
                remaining = wait - elapsed
                if remaining > 0:
                    print(
                        f"  [{ts()}] {DIM}Waiting {remaining:.1f}s for IVR prompt …{R}"
                    )
                    await asyncio.sleep(remaining)
                base = asyncio.get_event_loop().time()
                # Inject on the ;2 leg so ari_app receives it as a caller keypress.
                await _dtmf(client, inject_id, digit)

            await asyncio.sleep(2.0)
            print(f"\n[{ts()}] Sequence complete — hanging up …")
            await _hangup(client, channel_id)

        try:
            async with websockets.connect(ws_url, ping_interval=20) as ws:
                async for raw in ws:
                    event  = json.loads(raw)
                    etype  = event.get("type", "")
                    cid    = event.get("channel", {}).get("id", "")

                    if cid and cid != channel_id:
                        continue

                    if etype == "StasisStart" and seq_task is None:
                        print(f"[{ts()}] {GREEN}StasisStart{R} — IVR session active.\n")
                        stasis_name = event.get("channel", {}).get("name", "")
                        leg2 = await _find_sibling_leg(client, stasis_name)
                        inject_id = leg2 or channel_id
                        if leg2:
                            print(f"  [{ts()}] {DIM}Injecting DTMF on caller leg {leg2}{R}")
                        else:
                            print(f"  [{ts()}] {YELLOW}sibling ;2 leg not found — falling back to Stasis leg{R}")
                        seq_task = asyncio.create_task(_inject_sequence(inject_id))

                    elif etype == "ChannelDtmfReceived":
                        d = event.get("digit", "")
                        print(f"  [{ts()}] {YELLOW}ChannelDtmfReceived{R}: [{BLUE}{d}{R}]")

                    elif etype in ("ChannelHangupRequest", "ChannelDestroyed"):
                        print(f"\n[{ts()}] {RED}Channel ended{R} ({etype}).")
                        break

                    elif etype not in ("ChannelStateChange",):
                        print(f"  [{ts()}] {DIM}ARI event: {etype}{R}")

                    if asyncio.get_event_loop().time() > deadline:
                        print(f"\n[{ts()}] {RED}Timeout{R} — hanging up …")
                        await _hangup(client, channel_id)
                        break

        except websockets.exceptions.ConnectionClosed as exc:
            print(f"\n[{ts()}] WebSocket closed: {exc}")
        except KeyboardInterrupt:
            print(f"\n[{ts()}] Interrupted — hanging up …")
            await _hangup(client, channel_id)
        finally:
            if seq_task:
                seq_task.cancel()


def main() -> None:
    ap = argparse.ArgumentParser(description="Simulate an IVR call flow via Asterisk ARI")
    ap.add_argument("--ari",      default="http://localhost:8088/ari")
    ap.add_argument("--user",     default=os.environ.get("ARI_USER", "ari-user"))
    ap.add_argument("--password", default=os.environ.get("ARI_PASS", "changeme_ari"))
    ap.add_argument("--caller",   default="1001",
                    help="Caller ID used for the simulated call (default: 1001)")
    ap.add_argument("--scenario", default="support", choices=list(SCENARIOS),
                    help=f"IVR path to simulate (default: support).  Options: {', '.join(SCENARIOS)}")
    args = ap.parse_args()

    ari_base = args.ari.rstrip("/")
    host_part = ari_base.replace("http://", "").replace("https://", "")
    ws_url = (
        f"ws://{host_part}/events"
        f"?app=ivr-app"
        f"&api_key={urllib.parse.quote(args.user)}:{urllib.parse.quote(args.password)}"
        f"&subscribeAll=false"
    )

    print("=" * 62)
    print(f"  {BOLD}HT812 + Asterisk IVR — Protocol Simulation{R}")
    print("=" * 62)
    print(f"  ARI:      {ari_base}")
    print(f"  WS:       {ws_url.split('?')[0]}")
    print(f"  Caller:   {args.caller}")
    print(f"  Scenario: {args.scenario}")
    steps = SCENARIOS[args.scenario]
    print(f"  Steps:    {[(f'+{w}s', d) for w, d in steps]}")
    print("=" * 62)
    print()

    asyncio.run(_run(ari_base, ws_url, args.user, args.password, args.caller, args.scenario))

    print(
        f"\n{DIM}  Tip: run watch_events.py in a second terminal to see the full "
        f"event trace including IVR routing decisions.{R}\n"
    )


if __name__ == "__main__":
    main()
