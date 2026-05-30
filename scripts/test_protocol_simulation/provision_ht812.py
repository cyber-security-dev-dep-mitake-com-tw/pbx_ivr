#!/usr/bin/env python3
"""
Provision Grandstream HT812 with two SIP lines (1001 / 1002) via TCP.

Run while the laptop is connected directly to the HT812 LAN (NET2) port.
Device must be reachable at --host (default 192.168.2.1); your NIC must be
192.168.2.2 / 255.255.255.0.

After this script succeeds:
  1. Open https://192.168.2.1 in a browser
  2. FXS Port 1 → SIP Auth Password → enter the password for 1001
  3. FXS Port 2 → SIP Auth Password → enter the password for 1002
  4. Click Apply
  (The HT812 firmware ignores API writes for P34/P734 — manual entry is required.)

Usage:
  python provision_ht812.py
  python provision_ht812.py --host 192.168.2.1 --sip-server 192.168.2.2
  python provision_ht812.py --ext1 1001 --ext2 1002 --sip-port 5060

Requirements:
  pip install httpx
"""

import argparse
import asyncio
import base64
import sys

try:
    import httpx
except ImportError:
    sys.exit("Install httpx first:  pip install httpx")

TCP = "1"          # P130/P830 transport value for TCP
EXPIRY = "60"      # SIP registration expiry (seconds)


async def _login(client: httpx.AsyncClient, password: str) -> str:
    p2 = base64.b64encode(password.encode()).decode()
    try:
        r = await client.post("/cgi-bin/dologin", content=f"username=admin&P2={p2}")
        r.raise_for_status()
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        sys.exit(
            f"\nCannot reach HT812 at {client.base_url}\n"
            f"  Error: {exc}\n\n"
            "  Checklist:\n"
            "  · Ethernet cable in the LAN (NET2) port, not WAN (NET1)\n"
            "  · Your NIC is set to 192.168.2.2 / 255.255.255.0\n"
            "  · Device is powered on (Power LED solid)\n"
        )
    data = r.json()
    if data.get("response") != "success":
        body = data.get("body", "")
        sys.exit(
            f"\nLogin failed: {body}\n"
            "  Default password after factory reset is 'admin'.\n"
            "  If you see 'remain<N>', N attempts left before 5-minute lockout.\n"
        )
    return data["body"]["session_token"]


async def _apply(client: httpx.AsyncClient, token: str, params: dict[str, str]) -> str:
    body = "&".join(f"{k}={v}" for k, v in params.items()) + f"&apply=1&session_token={token}"
    r = await client.post("/cgi-bin/api.values.post", content=body)
    r.raise_for_status()
    data = r.json()
    # Device rotates session token on every apply=1
    new_tok = data.get("body", {})
    if isinstance(new_tok, dict) and new_tok.get("token"):
        token = new_tok["token"]
    if data.get("response") != "success":
        print(f"  Warning: device returned non-success — {data}")
    return token


async def _read(client: httpx.AsyncClient, token: str, keys: list[str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for key in keys:
        r = await client.post(
            "/cgi-bin/api.values.get",
            content=f"request={key}&session_token={token}",
        )
        r.raise_for_status()
        merged.update(r.json().get("body", {}))
    return merged


async def main() -> None:
    ap = argparse.ArgumentParser(description="Provision HT812 two-line SIP/TCP")
    ap.add_argument("--host",       default="192.168.2.1",  help="HT812 IP (default: 192.168.2.1)")
    ap.add_argument("--password",   default="admin",         help="Admin password (default: admin)")
    ap.add_argument("--sip-server", default="192.168.2.2",
                    help="Asterisk host reachable from HT812 (default: 192.168.2.2)")
    ap.add_argument("--sip-port",   default="5060",          help="SIP port (default: 5060)")
    ap.add_argument("--ext1",       default="1001",          help="FXS1 extension (default: 1001)")
    ap.add_argument("--ext2",       default="1002",          help="FXS2 extension (default: 1002)")
    args = ap.parse_args()

    base_url = f"https://{args.host}"
    print(f"\nConnecting to HT812 at {base_url} …")

    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,          # device uses self-signed cert
        timeout=20.0,
        follow_redirects=True,
        headers={
            "Content-Type":    "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        },
    ) as client:
        token = await _login(client, args.password)
        print(f"Authenticated  (session: {token[:14]}…)\n")

        params = {
            # ── FXS Port 1  (extension 1001) ────────────────────
            "P47":   args.sip_server,   # SIP server hostname / IP
            "P48":   args.sip_port,     # SIP server port
            "P35":   args.ext1,         # SIP User ID
            "P36":   args.ext1,         # SIP Authenticate ID
            "P130":  TCP,               # Transport: 1=TCP
            "P46":   EXPIRY,            # Registration expiry (s)
            # ── FXS Port 2  (extension 1002) ────────────────────
            "P2312": args.sip_server,
            "P2313": args.sip_port,
            "P735":  args.ext2,
            "P736":  args.ext2,
            "P830":  TCP,
            "P746":  EXPIRY,
            # ── Global ──────────────────────────────────────────
            "P52":   TCP,               # Preferred SIP transport
        }

        print("  Parameters to apply:")
        print(f"  FXS1  ext={args.ext1}  server={args.sip_server}:{args.sip_port}  transport=TCP")
        print(f"  FXS2  ext={args.ext2}  server={args.sip_server}:{args.sip_port}  transport=TCP")
        print(f"  P-keys: {', '.join(params)}\n")

        token = await _apply(client, token, params)
        print("  Settings committed to device.\n")

        # Verify by reading back the written values
        verify = ["P35", "P735", "P47", "P2312", "P130", "P830", "P48", "P2313"]
        vals = await _read(client, token, verify)

        labels = {
            "P35":   "FXS1 SIP User ID",
            "P735":  "FXS2 SIP User ID",
            "P47":   "FXS1 SIP server",
            "P2312": "FXS2 SIP server",
            "P48":   "FXS1 SIP port",
            "P2313": "FXS2 SIP port",
            "P130":  "FXS1 transport (1=TCP)",
            "P830":  "FXS2 transport (1=TCP)",
        }
        print("  Read-back verification:")
        all_ok = True
        for k, label in labels.items():
            v = vals.get(k, "?")
            ok = v not in ("?", "", None)
            icon = "✓" if ok else "✗"
            if not ok:
                all_ok = False
            print(f"  {icon}  {label:30s} = {v}")

        if not all_ok:
            print("\n  Some fields could not be verified — check device connectivity.")

    print("""
────────────────────────────────────────────────────────────
  NEXT STEPS — set SIP passwords in the HT812 web UI
────────────────────────────────────────────────────────────

  1. Open  https://192.168.2.1  in a browser
  2. FXS Port 1 → SIP Auth Password → enter password for 1001
  3. FXS Port 2 → SIP Auth Password → enter password for 1002
  4. Click Apply / Save

  Passwords must match pjsip.conf auth1001/auth1002 on Asterisk.
  Check SIP_1001_PASS / SIP_1002_PASS in .env for the values.

  After saving, run fxs_monitor.py to confirm registration:
    python fxs_monitor.py --host 192.168.2.1
────────────────────────────────────────────────────────────
""")


if __name__ == "__main__":
    asyncio.run(main())
