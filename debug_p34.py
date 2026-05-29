#!/usr/bin/env python3
"""
Debug script: test reading and writing SIP auth passwords P34/P734,
with correct handling of session token rotation on apply=1.

Run from project root:
  python3 debug_p34.py
"""

import asyncio
import base64
import os

import httpx

HT812_HOST = os.environ.get("HT812_HOST", "https://192.168.0.160")
ADMIN_USER = os.environ.get("HT812_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("HT812_ADMIN_PASS", "Mitake123")
SIP_PASS   = os.environ.get("SIP_1001_PASS", "Mitake123")


async def main():
    http = httpx.AsyncClient(
        base_url=HT812_HOST,
        verify=False,
        timeout=20.0,
        follow_redirects=True,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        },
    )

    token = ""

    async def login():
        nonlocal token
        p2 = base64.b64encode(ADMIN_PASS.encode()).decode()
        r = await http.post("/cgi-bin/dologin", content=f"username={ADMIN_USER}&P2={p2}")
        data = r.json()
        if data.get("response") != "success":
            raise RuntimeError(f"Login failed: {data}")
        token = data["body"]["session_token"]
        print(f"  token={token[:12]}...")

    def tok():
        return f"&session_token={token}"

    async def post(path, body=""):
        nonlocal token
        r = await http.post(path, content=body + tok())
        data = r.json()
        # Capture rotated token
        if isinstance(data, dict):
            body_obj = data.get("body", {})
            if isinstance(body_obj, dict) and body_obj.get("token"):
                token = body_obj["token"]
                print(f"  [token rotated → {token[:12]}...]")
        return data

    async def read(key):
        data = await post("/cgi-bin/api.values.get", f"request={key}")
        return data.get("body", {}).get(key, "<missing>")

    # Login
    print("=== Login ===")
    await login()

    # Read current state
    print("\n=== Read current SIP config ===")
    for key in ["P47", "P48", "P35", "P36", "P34", "P2312", "P2313", "P735", "P736", "P734"]:
        val = await read(key)
        print(f"  {key} = {val!r}")

    # Write all SIP params including passwords
    print(f"\n=== Write full SIP config (passwords={SIP_PASS!r}) ===")
    data = await post(
        "/cgi-bin/api.values.post",
        (
            "P47=192.168.0.100"
            "&P48=5060"
            "&P35=1001"
            "&P36=1001"
            f"&P34={SIP_PASS}"
            "&P2312=192.168.0.100"
            "&P2313=5060"
            "&P735=1002"
            "&P736=1002"
            f"&P734={SIP_PASS}"
            "&apply=1"
        ),
    )
    print(f"  response: {data}")

    # Read back to confirm
    print("\n=== Read back after write ===")
    for key in ["P47", "P35", "P36", "P34", "P735", "P736", "P734", "P4921", "P4922"]:
        val = await read(key)
        print(f"  {key} = {val!r}")

    # Trigger re-registration
    print("\n=== Trigger re-registration ===")
    data = await post("/cgi-bin/api-phone_operation", "cmd=register")
    print(f"  response: {data}")

    # Check registration state after a brief pause
    import time
    print("\n  Waiting 5 seconds for registration attempt...")
    await asyncio.sleep(5)

    print("\n=== Registration state ===")
    for key in ["P4921", "P4922"]:
        val = await read(key)
        label = "port 1" if key == "P4921" else "port 2"
        status = "Registered" if val == "1" else f"Not registered (raw={val!r})"
        print(f"  {key} ({label}): {status}")

    await http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
