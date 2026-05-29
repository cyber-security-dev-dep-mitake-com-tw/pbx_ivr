"""
Low-level async HTTP client for the Grandstream HT812V2 CGI API.

Reverse-engineered from firmware 3.7.5 (lighttpd/1.4.69, Vue SPA frontend).
Verified live against device EC74D7F6602A on 2026-05-29.

Auth flow:
  POST /cgi-bin/dologin  { username, P2: base64(password) }
    → { response: "success", body: { role, session_token, default_auth, oem_id } }
  NOTE: password must be base64-encoded — verified via browser DevTools.
  session_token appended to every subsequent request:
    POST body: &session_token=<token>
    GET query: ?session_token=<token>&_nocache_=<epoch_ms>

Key endpoints (all confirmed working):
  POST /cgi-bin/api.values.get   { request=P47&request=P48&..., session_token }  → P-values
  POST /cgi-bin/api.values.post  { P47=val, ..., update|apply: 1, session_token }
  GET  /cgi-bin/download_cfg_xml ?session_token=  → full XML config (1880 P-values)
  POST /cgi-bin/api-get_system_base_info { session_token } → product/vendor info
  POST /cgi-bin/rs               { session_token }  → reboot system
  POST /cgi-bin/unit_reset       { reset_type: 0|1|2, session_token }
  POST /cgi-bin/api-phone_operation { cmd: extend, session_token } → extend session

Port status P-values (read via api.values.get):
  P4901/P4902 = hook state (FXS port 1/2)
  P4921/P4922 = SIP registration state (FXS port 1/2)
  P35/P735    = SIP User ID (FXS port 1/2)
  P47/P2312   = SIP server (FXS port 1/2)

NOTE: /status/systemInfo, /status/portStatus, /status/netStatus are Vue Router SPA
      routes — they return HTML, not JSON. Data must be fetched via cgi-bin API calls.
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
        # A single persistent client maintains cookies and connection pooling.
        # Critical: the session token must come from the SAME login session.
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
        p2 = base64.b64encode(_ADMIN_PASS.encode()).decode()
        r = await self._http.post(
            "/cgi-bin/dologin",
            content=f"username={_ADMIN_USER}&P2={p2}",
        )
        r.raise_for_status()
        data = r.json()
        if data.get("response") != "success":
            body = data.get("body", "")
            raise HT812AuthError(
                f"Login failed: {body}. "
                "Check HT812_ADMIN_PASS in .env. "
                "'remain<N>' means N attempts left before lockout (5 min wait to clear)."
            )
        self._session_token = data["body"]["session_token"]

    async def _ensure_auth(self) -> None:
        if not self._session_token:
            await self._login()

    # ------------------------------------------------------------------ helpers

    def _tok(self) -> str:
        return f"&session_token={self._session_token}"

    def _get_params(self, extra: dict | None = None) -> dict:
        return {
            **(extra or {}),
            "session_token": self._session_token,
            "_nocache_": str(int(time.time() * 1000)),
        }

    async def _post(self, path: str, body: str = "") -> dict:
        await self._ensure_auth()
        r = await self._http.post(path, content=body + self._tok())
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("response") == "error":
            reason = data.get("body", {})
            if isinstance(reason, dict) and reason.get("reason") == "invalid session":
                self._session_token = ""
                await self._login()
                r = await self._http.post(path, content=body + self._tok())
                r.raise_for_status()
                data = r.json()
        return data

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        await self._ensure_auth()
        r = await self._http.get(path, params=self._get_params(params))
        r.raise_for_status()
        return r

    # ------------------------------------------------------------------ public API

    async def get_config_xml(self) -> str:
        """Download full device config as XML; saves a timestamped backup."""
        r = await self._get("/cgi-bin/download_cfg_xml")
        xml = r.text
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (_BACKUP_DIR / f"ht812_config_{ts}.xml").write_text(xml)
        return xml

    async def get_values(self, p_keys: list[str]) -> dict:
        """
        Read P-value settings by key.
        The device only returns the last request= field when multiple are sent,
        so we issue one request per key and merge the results.
        For bulk reads, prefer get_config_xml() which returns all 1880 P-values at once.
        """
        merged: dict = {}
        for key in p_keys:
            data = await self._post("/cgi-bin/api.values.get", f"request={key}")
            merged.update(data.get("body", {}))
        return merged

    async def patch_config(self, params: dict[str, str], apply: bool = True) -> bool:
        """
        Write P-value settings.
        apply=True  → commit immediately (most settings).
        apply=False → stage only; call apply_config() separately.
        """
        flag = "apply" if apply else "update"
        body = "&".join(f"{k}={v}" for k, v in params.items()) + f"&{flag}=1"
        data = await self._post("/cgi-bin/api.values.post", body)
        return data.get("response") == "success"

    async def apply_config(self) -> bool:
        """Commit staged changes (use after patch_config(apply=False))."""
        data = await self._post("/cgi-bin/api.values.post", "apply=1")
        return data.get("response") == "success"

    async def get_system_info(self) -> dict:
        """Firmware product/vendor from api-get_system_base_info."""
        data = await self._post("/cgi-bin/api-get_system_base_info")
        return data.get("body", data)

    async def get_port_status(self) -> dict:
        """
        SIP registration and hook status for FXS port 1 and port 2.
        Reads P-values: P4901/P4902 (hook), P4921/P4922 (reg), P35/P735 (user ID),
        P47/P2312 (SIP server).
        """
        keys = ["P4901", "P4902", "P4921", "P4922", "P35", "P735", "P47", "P2312", "P48", "P2313"]
        vals = await self.get_values(keys)
        return {
            "port1": {
                "port": 1,
                "hook": vals.get("P4901", ""),
                "registered": vals.get("P4921", "") == "1",
                "user_id": vals.get("P35", ""),
                "sip_server": vals.get("P47", ""),
                "sip_port": vals.get("P48", "5060"),
            },
            "port2": {
                "port": 2,
                "hook": vals.get("P4902", ""),
                "registered": vals.get("P4922", "") == "1",
                "user_id": vals.get("P735", ""),
                "sip_server": vals.get("P2312", ""),
                "sip_port": vals.get("P2313", "5060"),
            },
            "raw": vals,
        }

    async def get_net_status(self) -> dict:
        """Network IP, DNS, gateway settings via P-values."""
        keys = ["P9", "P10", "P11", "P12", "P13", "P14", "P15", "P16", "P21", "P22", "P1701"]
        vals = await self.get_values(keys)
        # P9-P12 = IP octets, P13-P16 = subnet mask, P17? = gateway
        return {"raw": vals}

    async def extend_session(self) -> None:
        """Keep session alive (device has idle timeout; call periodically if needed)."""
        await self._post("/cgi-bin/api-phone_operation", "cmd=extend&arg=")

    async def reboot(self) -> bool:
        """Reboot the device. Unreachable for ~30 seconds after this call."""
        data = await self._post("/cgi-bin/rs")
        return data.get("response") == "success"

    async def factory_reset(self, reset_type: str = "2") -> bool:
        """
        Factory reset.
        reset_type: "0"=ISP data, "1"=VoIP data, "2"=full (default).
        """
        data = await self._post("/cgi-bin/unit_reset", f"reset_type={reset_type}")
        return data.get("response") == "success"

    async def logout(self) -> None:
        if self._session_token:
            try:
                await self._http.post("/cgi-bin/dologout", content=self._tok().lstrip("&"))
            except Exception:
                pass
            self._session_token = ""

    async def aclose(self) -> None:
        await self.logout()
        await self._http.aclose()
