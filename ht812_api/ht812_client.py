"""
Low-level async HTTP client for the Grandstream HT812V2 CGI API.

Auth flow:
  1. GET  /cgi-bin/loginrealm  -> one-time challenge token
  2. POST /cgi-bin/login       -> username + MD5(password+token) -> session cookie
  3. All subsequent calls reuse the session cookie until it expires.
"""

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx


_HT812_HOST = os.environ.get("HT812_HOST", "https://192.168.0.160")
_ADMIN_USER = os.environ.get("HT812_ADMIN_USER", "admin")
_ADMIN_PASS = os.environ.get("HT812_ADMIN_PASS", "admin")
_BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backups"))


class HT812Error(Exception):
    pass


class HT812Client:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=_HT812_HOST,
            verify=False,  # self-signed cert on device
            timeout=15.0,
            follow_redirects=True,
        )
        self._cookie: dict = {}

    async def _get_challenge(self) -> str:
        r = await self._client.get("/cgi-bin/loginrealm")
        r.raise_for_status()
        # Response is plain text: the challenge token
        return r.text.strip()

    async def _login(self) -> None:
        token = await self._get_challenge()
        hashed = hashlib.md5((_ADMIN_PASS + token).encode()).hexdigest()
        r = await self._client.post(
            "/cgi-bin/login",
            data={"username": _ADMIN_USER, "password": hashed},
        )
        r.raise_for_status()
        if "session" not in str(r.cookies).lower() and r.status_code not in (200, 302):
            raise HT812Error(f"Login failed: {r.text[:200]}")
        self._cookie = dict(r.cookies)

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Make an authenticated request, retrying once if session expired."""
        for attempt in range(2):
            r = await self._client.request(
                method, path, cookies=self._cookie, **kwargs
            )
            if r.status_code == 401 or "login" in r.url.path.lower() and attempt == 0:
                await self._login()
                continue
            r.raise_for_status()
            return r
        raise HT812Error("Authentication failed after retry")

    async def get_config(self) -> str:
        """Export full device config as XML string, also saves a timestamped backup."""
        r = await self._request("GET", "/cgi-bin/api-get_config")
        xml = r.text
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (_BACKUP_DIR / f"ht812_config_{ts}.xml").write_text(xml)
        return xml

    async def patch_config(self, params: dict[str, str]) -> bool:
        """Push key-value config params to the device (Grandstream P-value style)."""
        r = await self._request("POST", "/cgi-bin/update", data=params)
        return r.status_code == 200

    async def reboot(self) -> bool:
        r = await self._request("POST", "/cgi-bin/reboot")
        return r.status_code == 200

    async def factory_reset(self) -> bool:
        r = await self._request("POST", "/cgi-bin/factory_reset")
        return r.status_code == 200

    async def get_sip_status(self) -> dict:
        """
        Returns registration status for both FXS ports.
        The /cgi-bin/api-get_accounts response is device-firmware-dependent;
        we parse the raw text and expose it alongside structured fields.
        """
        r = await self._request("GET", "/cgi-bin/api-get_accounts")
        raw = r.text

        def _parse_port(raw: str, port_index: int) -> dict:
            # Grandstream responses use patterns like "P{n}=value" or JSON-ish text
            registered = bool(re.search(rf"port{port_index}.*?registered", raw, re.I))
            user_match = re.search(rf"(?:user|account){port_index}[=:]\s*(\S+)", raw, re.I)
            server_match = re.search(rf"(?:server|sip){port_index}[=:]\s*(\S+)", raw, re.I)
            return {
                "port": port_index,
                "registered": registered,
                "user": user_match.group(1) if user_match else None,
                "server": server_match.group(1) if server_match else None,
                "raw": raw[:500],
            }

        return {
            "port1": _parse_port(raw, 1),
            "port2": _parse_port(raw, 2),
        }

    async def aclose(self) -> None:
        await self._client.aclose()
