#!/usr/bin/env python3
"""
Monitor Grandstream HT812 FXS hook/registration states in real-time.

IMPORTANT — reads through the ht812_api service, NOT the device directly.
The HT812 allows only ONE admin session and locks out after a few competing
logins. This script therefore polls the API's /ht812/status/ports endpoint, so
the only thing ever authenticating to the device is the single ht812_api
session. Run as many monitors as you like — none of them touch the device login.

(If you really need to bypass the API and hit the device directly — e.g. during
initial bring-up before Docker is up — use --direct, but then make sure NOTHING
else is logged in, including the browser and the container.)

FXS key codes
─────────────
  P4901 / P4902 — hook:  "0"/"On Hook" = on-hook,  "1"/"Off Hook" = off-hook
  P4921 / P4922 — reg:   "0"/"Not Registered",     "1"/"Registered"

Usage:
  python fxs_monitor.py                       # via API (recommended)
  python fxs_monitor.py --api http://localhost:8000
  python fxs_monitor.py --direct              # talk to the device directly

Requirements:
  pip install httpx
"""

import argparse
import asyncio
import base64
import os
import sys
from datetime import datetime

import _env  # loads project .env into os.environ

try:
    import httpx
except ImportError:
    sys.exit("Install httpx first:  pip install httpx")

POLL = 2.0

HOOK_LABEL  = {"0": "ON-HOOK ", "1": "OFF-HOOK"}
HOOK_SYMBOL = {"0": "━━[ ⌂ ]━━", "1": "━━[(  )]━━"}
HOOK_COLOR  = {"0": "\033[33m",  "1": "\033[32;1m"}
RESET = "\033[0m"
DIM   = "\033[2m"
BOLD  = "\033[1m"
RED   = "\033[31m"


def _norm_hook(v: str) -> str:
    s = str(v).strip().lower()
    if s in ("1", "off hook", "off-hook"):  return "1"
    if s in ("0", "on hook",  "on-hook"):   return "0"
    return str(v)


def _norm_reg(v: str) -> str:
    s = str(v).strip().lower()
    if s in ("1", "registered"):            return "1"
    if s in ("0", "not registered"):        return "0"
    return str(v)


def _row(port: int, hook: str, reg: str, changed: bool) -> str:
    ts    = datetime.now().strftime("%H:%M:%S.%f")[:12]
    label = HOOK_LABEL.get(hook, f"?={hook!r:12s}")
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


# ------------------------------------------------------------------ via API

async def _poll_api(client: httpx.AsyncClient, api: str) -> dict[str, str]:
    """Return {'P4901':..,'P4921':..,'P4902':..,'P4922':..} via ht812_api."""
    r = await client.get(f"{api.rstrip('/')}/ht812/status/ports")
    r.raise_for_status()
    data = r.json()
    raw = data.get("raw", {})
    p1, p2 = data.get("port1", {}), data.get("port2", {})
    return {
        "P4901": p1.get("hook", raw.get("P4901", "")),
        "P4921": "1" if p1.get("registered") else raw.get("P4921", "0"),
        "P4902": p2.get("hook", raw.get("P4902", "")),
        "P4922": "1" if p2.get("registered") else raw.get("P4922", "0"),
    }


async def monitor_api(api: str) -> None:
    print(f"\nMonitoring via ht812_api at {api} (device session is shared — safe)\n")
    print(
        "  Legend:  ━━[ ⌂ ]━━ = on-hook (idle)   ━━[(  )]━━ = off-hook (active)\n"
        f"  Polling {api}/ht812/status/ports every {POLL}s\n"
    )
    print("─" * 72)
    last: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                vals = await _poll_api(client, api)
            except Exception as exc:
                print(f"  {DIM}API error: {exc} — is ht812_api up? retrying{RESET}")
                await asyncio.sleep(POLL)
                continue
            _render(vals, last)
            await asyncio.sleep(POLL)


# ------------------------------------------------------------------ direct (bypass)

async def _login_direct(client: httpx.AsyncClient, password: str) -> str | None:
    p2 = base64.b64encode(password.encode()).decode()
    try:
        r = await client.post("/cgi-bin/dologin", content=f"username=admin&P2={p2}")
        r.raise_for_status()
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        sys.exit(f"Cannot reach HT812: {exc}")
    data = r.json()
    if data.get("response") != "success":
        print(f"  {RED}Login failed: {data.get('body')}{RESET}")
        return None
    return data["body"]["session_token"]


async def monitor_direct(host: str, password: str) -> None:
    base_url = f"https://{host}"
    print(f"\n⚠ DIRECT mode — talking to {base_url}. Ensure nothing else is logged in.\n")
    async with httpx.AsyncClient(
        base_url=base_url, verify=False, timeout=20.0,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "X-Requested-With": "XMLHttpRequest"},
    ) as client:
        token = await _login_direct(client, password)
        if not token:
            sys.exit(1)
        print("Authenticated  (Ctrl-C to stop)\n")
        print("─" * 72)
        keys = ["P4901", "P4902", "P4921", "P4922"]
        last: dict[str, str] = {}
        while True:
            merged: dict[str, str] = {}
            try:
                for key in keys:
                    r = await client.post(
                        "/cgi-bin/api.values.get",
                        content=f"request={key}&session_token={token}",
                    )
                    data = r.json()
                    if data.get("response") == "error":
                        token = await _login_direct(client, password) or token
                        merged = {}
                        break
                    merged.update(data.get("body", {}))
            except Exception as exc:
                print(f"  {DIM}Poll error: {exc}{RESET}")
                await asyncio.sleep(POLL)
                continue
            if merged:
                _render(merged, last)
            await asyncio.sleep(POLL)


# ------------------------------------------------------------------ shared render

def _render(vals: dict[str, str], last: dict[str, str]) -> None:
    separator = False
    for port, hk, rk in [(1, "P4901", "P4921"), (2, "P4902", "P4922")]:
        hook = _norm_hook(vals.get(hk, ""))
        reg  = _norm_reg(vals.get(rk, ""))
        changed = hk in last and hook != last[hk]
        if changed:
            separator = True
        last[hk] = hook
        last[rk] = reg
        print(_row(port, hook, reg, changed))
    if separator:
        print("─" * 72)


def main() -> None:
    ap = argparse.ArgumentParser(description="Monitor HT812 FXS hook states (via API by default)")
    ap.add_argument("--api", default=os.environ.get("VITE_API_BASE_URL", "http://localhost:8000"),
                    help="ht812_api base URL (default: http://localhost:8000)")
    ap.add_argument("--direct", action="store_true",
                    help="Bypass the API and log into the device directly (use with care)")
    _ht_host = (os.environ.get("HT812_HOST", "https://192.168.2.1")
                .replace("https://", "").replace("http://", ""))
    ap.add_argument("--host",     default=_ht_host, help="device host (only with --direct)")
    ap.add_argument("--password", default=os.environ.get("HT812_ADMIN_PASS", "admin"),
                    help="admin password (only with --direct)")
    args = ap.parse_args()
    try:
        if args.direct:
            asyncio.run(monitor_direct(args.host, args.password))
        else:
            asyncio.run(monitor_api(args.api))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
