#!/usr/bin/env python3
"""
ARI-driven IVR application.

Connects to Asterisk via WebSocket and handles calls that enter the
'ivr-app' Stasis application (triggered by pressing * in extensions.conf).

Event flow:
  StasisStart        → answer channel, play welcome prompt
  ChannelDtmfReceived → route based on digit pressed
  ChannelHangupRequest → clean up

Asterisk REST API docs: https://docs.asterisk.org/Latest_API/API_Documentation/Asterisk_REST_Interface/
"""

import asyncio
import json
import logging
import os
import urllib.parse

import httpx
import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ARI] %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ARI_HOST = os.environ.get("ARI_HOST", "asterisk")
ARI_PORT = os.environ.get("ARI_PORT", "8088")
ARI_USER = os.environ.get("ARI_USER", "ari-user")
ARI_PASS = os.environ.get("ARI_PASS", "changeme_ari")
APP_NAME = "ivr-app"

_BASE_URL = f"http://{ARI_HOST}:{ARI_PORT}/ari"
_WS_URL = (
    f"ws://{ARI_HOST}:{ARI_PORT}/ari/events"
    f"?app={APP_NAME}&api_key={urllib.parse.quote(ARI_USER)}:{urllib.parse.quote(ARI_PASS)}"
    f"&subscribeAll=false"
)


class ARIClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=_BASE_URL,
            auth=(ARI_USER, ARI_PASS),
            timeout=10.0,
        )

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        r = await self._http.request(method, path, **kwargs)
        if r.status_code not in (200, 201, 204):
            log.warning("ARI %s %s → %s: %s", method, path, r.status_code, r.text[:200])
        try:
            return r.json()
        except Exception:
            return {}

    async def answer(self, channel_id: str) -> None:
        await self._call("POST", f"/channels/{channel_id}/answer")

    async def hangup(self, channel_id: str) -> None:
        await self._call("DELETE", f"/channels/{channel_id}")

    async def play(self, channel_id: str, media: str) -> dict:
        return await self._call(
            "POST",
            f"/channels/{channel_id}/play",
            params={"media": media},
        )

    async def dial(self, endpoint: str, caller_id: str) -> dict:
        return await self._call(
            "POST",
            "/channels",
            params={
                "endpoint": endpoint,
                "app": APP_NAME,
                "callerId": caller_id,
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()


class IVRApp:
    def __init__(self, ari: ARIClient) -> None:
        self._ari = ari
        # Track which channel is waiting for a digit after playing a prompt
        self._pending: dict[str, asyncio.Future] = {}

    async def on_stasis_start(self, event: dict) -> None:
        channel = event["channel"]
        cid = channel["id"]
        caller = channel.get("caller", {}).get("number", "unknown")
        log.info("StasisStart: channel=%s caller=%s", cid, caller)

        await self._ari.answer(cid)
        # Play a prompt then wait for DTMF
        await self._ari.play(cid, "sound:tt-weasels")  # replace with real prompt

        # Create a future to be resolved when a digit arrives
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[cid] = fut

        try:
            digit = await asyncio.wait_for(fut, timeout=10.0)
        except asyncio.TimeoutError:
            log.info("Channel %s timed out waiting for digit, hanging up", cid)
            await self._ari.hangup(cid)
            return
        finally:
            self._pending.pop(cid, None)

        log.info("Channel %s pressed: %s", cid, digit)
        await self._route(cid, digit, caller)

    async def _route(self, channel_id: str, digit: str, caller: str) -> None:
        routing = {
            "1": "PJSIP/1001",
            "2": "PJSIP/1002",
            "0": "PJSIP/1001",
        }
        endpoint = routing.get(digit)
        if endpoint:
            log.info("Dialing %s for channel %s", endpoint, channel_id)
            await self._ari.dial(endpoint, caller)
        else:
            await self._ari.play(channel_id, "sound:invalid")
        await self._ari.hangup(channel_id)

    async def on_dtmf(self, event: dict) -> None:
        cid = event["channel"]["id"]
        digit = event.get("digit", "")
        fut = self._pending.get(cid)
        if fut and not fut.done():
            fut.set_result(digit)

    async def on_hangup(self, event: dict) -> None:
        cid = event["channel"]["id"]
        log.info("Hangup: channel=%s", cid)
        fut = self._pending.pop(cid, None)
        if fut and not fut.done():
            fut.cancel()

    async def dispatch(self, event: dict) -> None:
        etype = event.get("type")
        handlers = {
            "StasisStart": self.on_stasis_start,
            "ChannelDtmfReceived": self.on_dtmf,
            "ChannelHangupRequest": self.on_hangup,
        }
        handler = handlers.get(etype)
        if handler:
            asyncio.create_task(handler(event))


async def run() -> None:
    ari = ARIClient()
    app = IVRApp(ari)

    log.info("Connecting to Asterisk ARI at %s", _WS_URL)
    backoff = 2
    while True:
        try:
            async with websockets.connect(_WS_URL, ping_interval=20) as ws:
                log.info("Connected — listening for Stasis events (app=%s)", APP_NAME)
                backoff = 2
                async for raw in ws:
                    event = json.loads(raw)
                    await app.dispatch(event)
        except (websockets.ConnectionClosed, OSError) as e:
            log.warning("WebSocket disconnected: %s — retrying in %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except Exception as e:
            log.exception("Unexpected error: %s", e)
            await asyncio.sleep(backoff)
    await ari.aclose()


if __name__ == "__main__":
    asyncio.run(run())
