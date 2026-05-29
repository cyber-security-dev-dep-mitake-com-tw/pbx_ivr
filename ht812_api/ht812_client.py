"""
Low-level async HTTP client for the Grandstream HT812V2 CGI API.

Reverse-engineered from firmware 3.7.5 (lighttpd/1.4.69, Vue SPA frontend).

Auth flow:
  POST /cgi-bin/dologin  { username, P2: base64(password) }   (application/x-www-form-urlencoded)
    → { response: "success", body: { role, session_token, default_auth, oem_id } }
  NOTE: The frontend base64-encodes the password before sending. Verified via browser DevTools.
  session_token is appended to every subsequent request:
    POST: &session_token=<token>
    GET:  ?session_token=<token>&_nocache_=<epoch_ms>

Key endpoints:
  GET  /cgi-bin/download_cfg_xml   ?session_token=  → full config XML
  GET  /cgi-bin/export_cfg         ?session_token=  → binary config export
  POST /cgi-bin/api.values.get     { request: "P47,P48,...", session_token }  → P-values
  POST /cgi-bin/api.values.post    { P47: val, ..., update|apply: "1", session_token }
  POST /cgi-bin/rs                 { session_token }  → reboot system
  POST /cgi-bin/unit_reset         { reset_type: "0"|"1"|"2", session_token }
       0 = ISP data, 1 = VoIP data, 2 = full factory reset
  GET  /status/portStatus          ?session_token=  → FXS port SIP registration
  GET  /status/systemInfo          ?session_token=  → firmware, MAC, uptime
  GET  /status/netStatus           ?session_token=  → IP, DNS, DHCP info
"""

import base64
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


_HT812_HOST = os.environ.get("HT812_HOST", "https://192.168.0.160")
_ADMIN_USER = os.environ.get("HT812_ADMIN_USER", "admin")
_ADMIN_PASS = os.environ.get("HT812_ADMIN_PASS", "admin")
_BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backups"))


class HT812Error(Exception):
    pass


class HT812AuthError(HT812Error):
    pass


class HT812Client:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=_HT812_HOST,
            verify=False,         # device uses self-signed TLS cert
            timeout=20.0,
            follow_redirects=True,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        self._session_token: str = ""

    # ------------------------------------------------------------------ auth

    async def _login(self) -> None:
        r = await self._http.post(
            "/cgi-bin/dologin",
            content=f"username={_ADMIN_USER}&P2={_ADMIN_PASS}",
        )
        r.raise_for_status()
        data = r.json()
        if data.get("response") != "success":
            body = data.get("body", "")
            raise HT812AuthError(
                f"Login failed: {body}. "
                "Check HT812_ADMIN_PASS in .env — 'remain<N>' means N attempts left before lockout."
            )
        self._session_token = data["body"]["session_token"]

    async def _ensure_auth(self) -> None:
        if not self._session_token:
            await self._login()

    # ------------------------------------------------------------------ request helpers

    def _post_data(self, extra: dict | None = None) -> str:
        """Build URL-encoded form body with session_token appended."""
        parts = {**(extra or {}), "session_token": self._session_token}
        return "&".join(f"{k}={v}" for k, v in parts.items())

    def _get_params(self, extra: dict | None = None) -> dict:
        """Build GET query params dict with session_token and cache-buster."""
        return {
            **(extra or {}),
            "session_token": self._session_token,
            "_nocache_": str(int(time.time() * 1000)),
        }

    async def _post(self, path: str, data: dict | None = None) -> dict:
        await self._ensure_auth()
        r = await self._http.post(path, content=self._post_data(data))
        r.raise_for_status()
        body = r.json()
        if isinstance(body, dict) and body.get("response") == "error":
            if body.get("body") == "authentication required":
                # Session expired — re-login once
                self._session_token = ""
                await self._login()
                r = await self._http.post(path, content=self._post_data(data))
                r.raise_for_status()
                body = r.json()
        return body

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        await self._ensure_auth()
        r = await self._http.get(path, params=self._get_params(params))
        if r.status_code == 401 or (
            r.headers.get("content-type", "").startswith("text/html")
            and "login" in r.text.lower()[:500]
        ):
            self._session_token = ""
            await self._login()
            r = await self._http.get(path, params=self._get_params(params))
        r.raise_for_status()
        return r

    # ------------------------------------------------------------------ public API

    async def get_config_xml(self) -> str:
        """Download full config as XML; also writes a timestamped backup."""
        r = await self._get("/cgi-bin/download_cfg_xml")
        xml = r.text
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (_BACKUP_DIR / f"ht812_config_{ts}.xml").write_text(xml)
        return xml

    async def get_values(self, p_keys: list[str]) -> dict:
        """Read specific P-value keys. e.g. ['P47', 'P48', 'P52']"""
        result = await self._post(
            "/cgi-bin/api.values.get",
            {"request": ",".join(p_keys)},
        )
        # Response: {"response":"success","body":{"P47":"sip.example.com",...}}
        return result.get("body", result)

    async def patch_config(self, params: dict[str, str], apply: bool = True) -> bool:
        """
        Write P-value settings.
        apply=True  → immediately active (most settings).
        apply=False → staged only, call apply_config() separately.
        """
        flag = "apply" if apply else "update"
        result = await self._post(
            "/cgi-bin/api.values.post",
            {**params, flag: "1"},
        )
        return result.get("response") == "success"

    async def apply_config(self) -> bool:
        """Commit staged changes (use after patch_config(apply=False))."""
        result = await self._post("/cgi-bin/api.values.post", {"apply": "1"})
        return result.get("response") == "success"

    async def reboot(self) -> bool:
        """Reboot the device. It will be unreachable for ~30 seconds."""
        result = await self._post("/cgi-bin/rs")
        return result.get("response") == "success"

    async def factory_reset(self, reset_type: str = "2") -> bool:
        """
        Factory reset.
        reset_type: "0"=ISP data only, "1"=VoIP data only, "2"=full reset (default).
        """
        result = await self._post("/cgi-bin/unit_reset", {"reset_type": reset_type})
        return result.get("response") == "success"

    async def get_port_status(self) -> dict:
        """SIP registration status for FXS port 1 and port 2."""
        r = await self._get("/status/portStatus")
        return r.json()

    async def get_system_info(self) -> dict:
        """Firmware version, MAC, model, uptime."""
        r = await self._get("/status/systemInfo")
        return r.json()

    async def get_net_status(self) -> dict:
        """IP address, DHCP, DNS, gateway info."""
        r = await self._get("/status/netStatus")
        return r.json()

    async def logout(self) -> None:
        if self._session_token:
            await self._http.post("/cgi-bin/dologout", content=self._post_data())
            self._session_token = ""

    async def aclose(self) -> None:
        await self.logout()
        await self._http.aclose()
