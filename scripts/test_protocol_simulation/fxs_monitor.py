#!/usr/bin/env python3
"""
Monitor Grandstream HT812 FXS hook states in real-time.

Polls P4901/P4902 (hook) and P4921/P4922 (registration) every 2 seconds
and prints state transitions with timestamps and an ASCII phone diagram.

FXS key codes
─────────────
  P4901 / P4902 — hook state
    "0" / "On Hook"  = on-hook  (handset resting, line idle)
    "1" / "Off Hook" = off-hook (handset lifted, line in use)

  P4921 / P4922 — SIP registration state
    "0" / "Not Registered" = not registered
    "1" / "Registered"     = registered

Note: firmware 3.7.5 returns human-readable strings ("On Hook", "Registered"),
      older firmware returns "0" / "1".  Both formats are handled.

Usage:
  python fxs_monitor.py
  python fxs_monitor.py --host 192.168.2.1

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
    """Normalise firmware hook state to '0' (on-hook) or '1' (off-hook)."""
    s = v.strip().lower()
    if s in ("1", "off hook", "off-hook"):   return "1"
    if s in ("0", "on hook",  "on-hook"):    return "0"
    return v  # unknown — pass through for display


def _norm_reg(v: str) -> str:
    """Normalise firmware registration state to '0' (no) or '1' (yes)."""
    s = v.strip().lower()
    if s in ("1", "registered"):             return "1"
    if s in ("0", "not registered"):         return "0"
    return v


async def _login(client: httpx.AsyncClient, password: str) -> str | None:
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


async def _get(
    client: httpx.AsyncClient,
    token: str,
    keys: list[str],
) -> dict[str, str] | None:
    """Return P-value dict, or None if session is invalid (triggers re-auth)."""
    merged: dict[str, str] = {}
    for key in keys:
        r = await client.post(
            "/cgi-bin/api.values.get",
            content=f"request={key}&session_token={token}",
        )
        r.raise_for_status()
        data = r.json()
        # Detect expired / invalid session
        if isinstance(data, dict) and data.get("response") == "error":
            return None
        body = data.get("body", {})
        if isinstance(body, dict):
            merged.update(body)
    # Also treat all-empty as a session failure
    if merged and not any(merged.values()):
        return None
    return merged


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


async def monitor(host: str, password: str) -> None:
    base_url = f"https://{host}"
    print(f"\nConnecting to HT812 at {base_url} …")

    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=20.0,
        headers={
            "Content-Type":     "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        },
    ) as client:
        token = await _login(client, password)
        if not token:
            sys.exit(1)
        print("Authenticated  (Ctrl-C to stop)\n")
        print(
            "  Legend:  ━━[ ⌂ ]━━ = on-hook (idle)   "
            "━━[(  )]━━ = off-hook (active)\n"
            f"  Polling P4901/P4902 (hook) + P4921/P4922 (reg) every {POLL}s\n"
        )
        print("─" * 72)

        keys = ["P4901", "P4902", "P4921", "P4922"]
        last: dict[str, str] = {}

        while True:
            try:
                vals = await _get(client, token, keys)
            except Exception as exc:
                print(f"  {DIM}Poll error: {exc} — retrying{RESET}")
                await asyncio.sleep(POLL)
                continue

            # Session expired — re-authenticate silently
            if vals is None:
                print(f"  {DIM}Session expired — re-authenticating …{RESET}")
                new_token = await _login(client, password)
                if new_token:
                    token = new_token
                await asyncio.sleep(POLL)
                continue

            separator_needed = False
            for port, hk, rk in [(1, "P4901", "P4921"), (2, "P4902", "P4922")]:
                hook = _norm_hook(vals.get(hk, ""))
                reg  = _norm_reg(vals.get(rk, ""))
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
    _ht_host = (
        os.environ.get("HT812_HOST", "https://192.168.2.1")
        .replace("https://", "").replace("http://", "")
    )
    ap.add_argument("--host",     default=_ht_host)
    ap.add_argument("--password", default=os.environ.get("HT812_ADMIN_PASS", "admin"))
    args = ap.parse_args()
    try:
        asyncio.run(monitor(args.host, args.password))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
