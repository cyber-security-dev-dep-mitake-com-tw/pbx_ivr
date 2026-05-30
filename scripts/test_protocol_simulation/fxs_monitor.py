#!/usr/bin/env python3
"""
Monitor Grandstream HT812 FXS hook states in real-time.

Polls P4901/P4902 (hook) and P4921/P4922 (registration) every 2 seconds
and prints state transitions with timestamps and an ASCII phone diagram.

FXS key codes
─────────────
  P4901 / P4902 — hook state
    0 = on-hook  (handset resting, line idle)
    1 = off-hook (handset lifted, line in use or dialling)

  P4921 / P4922 — SIP registration state
    0 = not registered
    1 = registered

Usage:
  python fxs_monitor.py
  python fxs_monitor.py --host 192.168.2.1 --password admin

Requirements:
  pip install httpx
"""

import argparse
import asyncio
import base64
import sys
from datetime import datetime

try:
    import httpx
except ImportError:
    sys.exit("Install httpx first:  pip install httpx")

POLL = 2.0

HOOK_LABEL  = {"0": "ON-HOOK ", "1": "OFF-HOOK"}
HOOK_SYMBOL = {"0": "━━[ ⌂ ]━━", "1": "━━[(  )]━━"}
HOOK_COLOR  = {"0": "\033[33m",  "1": "\033[32;1m"}
RESET       = "\033[0m"
DIM         = "\033[2m"
BOLD        = "\033[1m"


async def _login(client: httpx.AsyncClient, password: str) -> str:
    p2 = base64.b64encode(password.encode()).decode()
    try:
        r = await client.post("/cgi-bin/dologin", content=f"username=admin&P2={p2}")
        r.raise_for_status()
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        sys.exit(f"Cannot reach HT812: {exc}")
    data = r.json()
    if data.get("response") != "success":
        sys.exit(f"Login failed: {data.get('body')}")
    return data["body"]["session_token"]


async def _get(client: httpx.AsyncClient, token: str, keys: list[str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for key in keys:
        r = await client.post(
            "/cgi-bin/api.values.get",
            content=f"request={key}&session_token={token}",
        )
        r.raise_for_status()
        merged.update(r.json().get("body", {}))
    return merged


def _row(port: int, hook: str, reg: str, changed: bool) -> str:
    ts    = datetime.now().strftime("%H:%M:%S.%f")[:12]
    label = HOOK_LABEL.get(hook, f"?={hook!r} ")
    sym   = HOOK_SYMBOL.get(hook, "━━[ ? ]━━")
    color = HOOK_COLOR.get(hook, "")
    reg_s = f"{BOLD}REG{RESET}" if reg == "1" else f"{DIM}---{RESET}"
    chg   = f"  {BOLD}◀ CHANGED{RESET}" if changed else ""
    code  = f"{DIM}P490{port}={hook!r} P492{port}={reg!r}{RESET}"
    return (
        f"  {DIM}{ts}{RESET}  FXS{port}  "
        f"{color}{sym}  {label}{RESET}  [{reg_s}]  "
        f"{code}{chg}"
    )


async def monitor(host: str, password: str) -> None:
    base_url = f"https://{host}"
    print(f"\nConnecting to HT812 at {base_url} …")

    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=20.0,
        headers={
            "Content-Type":    "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        },
    ) as client:
        token = await _login(client, password)
        print(f"Authenticated  (Ctrl-C to stop)\n")
        print(
            f"  Legend:  ━━[ ⌂ ]━━ = on-hook (idle)   "
            f"━━[(  )]━━ = off-hook (active)\n"
            f"  Polling P4901/P4902 (hook) + P4921/P4922 (reg) every {POLL}s\n"
        )
        print("─" * 72)

        keys = ["P4901", "P4902", "P4921", "P4922"]
        last: dict[str, str] = {}

        while True:
            try:
                vals = await _get(client, token, keys)
            except Exception as exc:
                print(f"  Poll error: {exc}")
                await asyncio.sleep(POLL)
                continue

            separator_needed = False
            for port, hk, rk in [(1, "P4901", "P4921"), (2, "P4902", "P4922")]:
                hook = vals.get(hk, "")
                reg  = vals.get(rk, "")
                changed = hk in last and hook != last[hk]
                if changed:
                    separator_needed = True
                last[hk] = hook
                last[rk] = reg
                print(_row(port, hook, reg, changed))

            if separator_needed:
                print("─" * 72)

            await asyncio.sleep(POLL)


def main() -> None:
    ap = argparse.ArgumentParser(description="Monitor HT812 FXS hook states")
    ap.add_argument("--host",     default="192.168.2.1")
    ap.add_argument("--password", default="admin")
    args = ap.parse_args()
    try:
        asyncio.run(monitor(args.host, args.password))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
