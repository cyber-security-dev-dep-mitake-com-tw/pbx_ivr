#!/usr/bin/env python3
"""
ARI-driven IVR application — multi-level menus, call recording, bridge/transfer.

Connects to Asterisk via WebSocket and handles calls that enter the
'ivr-app' Stasis application (triggered by pressing * in extensions.conf).

Menu structure:
  Welcome → press 1 (Sales sub-menu), 2 (Support sub-menu),
             0 (Operator direct), 8 (Voicemail), # (Repeat), timeout → hangup

  Sales sub-menu → press 1 (new inquiry), 2 (existing order), 0 (back)
  Support sub-menu → press 1 (billing), 2 (technical), 0 (back)
"""

import asyncio
import json
import logging
import os
import urllib.parse

import httpx
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ARI] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

ARI_HOST = os.environ.get("ARI_HOST", "asterisk")
ARI_PORT = os.environ.get("ARI_PORT", "8088")
ARI_USER = os.environ.get("ARI_USER", "ari-user")
ARI_PASS = os.environ.get("ARI_PASS", "changeme_ari")
APP_NAME = "ivr-app"
ENABLE_RECORDING = os.environ.get("ARI_ENABLE_RECORDING", "false").lower() == "true"
EVENTS_API_URL = os.environ.get("EVENTS_API_URL", "http://ht812_api:8000/events")

_BASE_URL = f"http://{ARI_HOST}:{ARI_PORT}/ari"
_WS_URL = (
    f"ws://{ARI_HOST}:{ARI_PORT}/ari/events"
    f"?app={APP_NAME}"
    f"&api_key={urllib.parse.quote(ARI_USER)}:{urllib.parse.quote(ARI_PASS)}"
    f"&subscribeAll=false"
)

# Digit timeout and menu repeat limit
DIGIT_TIMEOUT = 8.0
MAX_RETRIES = 3


def _line_for_caller(caller: str | None) -> str | None:
    if caller == "1001":
        return "1"
    if caller == "1002":
        return "2"
    return None


class ARIClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=_BASE_URL,
            auth=(ARI_USER, ARI_PASS),
            timeout=10.0,
        )

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        backoff = 1.0
        for attempt in range(3):
            try:
                r = await self._http.request(method, path, **kwargs)
                if r.status_code not in (200, 201, 204):
                    log.warning("ARI %s %s → %s: %s", method, path, r.status_code, r.text[:200])
                try:
                    return r.json()
                except Exception:
                    return {}
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt == 2:
                    log.error("ARI %s %s failed after 3 attempts: %s", method, path, e)
                    return {}
                log.warning("ARI %s %s error (attempt %d): %s — retrying in %.1fs", method, path, attempt + 1, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
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

    async def stop_playback(self, playback_id: str) -> None:
        await self._call("DELETE", f"/playbacks/{playback_id}")

    async def start_recording(self, channel_id: str, name: str) -> dict:
        return await self._call(
            "POST",
            f"/channels/{channel_id}/record",
            params={
                "name": name,
                "format": "wav",
                "maxDurationSeconds": 300,
                "ifExists": "overwrite",
                "beep": "true",
                "terminateOn": "#",
            },
        )

    async def stop_recording(self, recording_name: str) -> None:
        await self._call("POST", f"/recordings/live/{recording_name}/stop")

    async def dial_endpoint(self, endpoint: str, caller_id: str, app: str) -> dict:
        return await self._call(
            "POST",
            "/channels",
            params={
                "endpoint": endpoint,
                "app": app,
                "callerId": caller_id,
            },
        )

    async def create_bridge(self, bridge_type: str = "mixing") -> dict:
        return await self._call(
            "POST",
            "/bridges",
            params={"type": bridge_type},
        )

    async def add_to_bridge(self, bridge_id: str, channel_id: str) -> None:
        await self._call(
            "POST",
            f"/bridges/{bridge_id}/addChannel",
            params={"channel": channel_id},
        )

    async def destroy_bridge(self, bridge_id: str) -> None:
        await self._call("DELETE", f"/bridges/{bridge_id}")

    async def aclose(self) -> None:
        await self._http.aclose()


class EventPublisher:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=3.0)

    async def publish(
        self,
        event_type: str,
        message: str,
        *,
        channel_id: str | None = None,
        caller: str | None = None,
        line: str | None = None,
        digit: str | None = None,
        data: dict | None = None,
    ) -> None:
        try:
            await self._http.post(
                EVENTS_API_URL,
                json={
                    "source": "ari_app",
                    "type": event_type,
                    "message": message,
                    "channel_id": channel_id,
                    "caller": caller,
                    "line": line,
                    "digit": digit,
                    "data": data or {},
                },
            )
        except Exception as e:
            log.debug("event publish failed: %s", e)

    async def aclose(self) -> None:
        await self._http.aclose()


class IVRSession:
    """Manages one active call through the multi-level IVR."""

    def __init__(self, ari: ARIClient, events: EventPublisher, channel_id: str, caller: str) -> None:
        self._ari = ari
        self._events = events
        self.channel_id = channel_id
        self.caller = caller
        self.line = _line_for_caller(caller)
        self._digit_futures: dict[str, asyncio.Future] = {}
        self._recording_name: str | None = None
        self._bridge_id: str | None = None

    # ------------------------------------------------------------------ digit collection

    def _make_digit_future(self) -> asyncio.Future:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._digit_futures[self.channel_id] = fut
        return fut

    def deliver_digit(self, digit: str) -> None:
        fut = self._digit_futures.get(self.channel_id)
        if fut and not fut.done():
            fut.set_result(digit)

    def cancel_pending(self) -> None:
        fut = self._digit_futures.pop(self.channel_id, None)
        if fut and not fut.done():
            fut.cancel()

    async def _collect_digit(self, sound: str) -> str | None:
        """Play a prompt and wait for a single DTMF digit."""
        await self._ari.play(self.channel_id, f"sound:{sound}")
        fut = self._make_digit_future()
        try:
            return await asyncio.wait_for(fut, timeout=DIGIT_TIMEOUT)
        except asyncio.TimeoutError:
            return None
        finally:
            self._digit_futures.pop(self.channel_id, None)

    # ------------------------------------------------------------------ menus

    async def run(self) -> None:
        await self._ari.answer(self.channel_id)
        log.info("answered channel=%s caller=%s", self.channel_id, self.caller)
        await self._events.publish(
            "call_start",
            f"Call answered from {self.caller}",
            channel_id=self.channel_id,
            caller=self.caller,
            line=self.line,
        )

        if ENABLE_RECORDING:
            import time
            self._recording_name = f"ivr_{self.channel_id}_{int(time.time())}"
            await self._ari.start_recording(self.channel_id, self._recording_name)
            log.info("recording started name=%s", self._recording_name)

        await self._main_menu()

    async def _main_menu(self, retries: int = 0) -> None:
        if retries >= MAX_RETRIES:
            await self._ari.play(self.channel_id, "sound:goodbye")
            await self._cleanup()
            return

        digit = await self._collect_digit("tt-weasels")  # replace with custom welcome
        if digit is not None:
            await self._events.publish(
                "dtmf",
                f"Main menu digit {digit}",
                channel_id=self.channel_id,
                caller=self.caller,
                line=self.line,
                digit=digit,
                data={"menu": "main"},
            )
            # Ultimate goal: Line 1 keypad entries propagate to Line 2.
            await self._forward_digit_to_line2(digit)

        if digit is None:
            await self._events.publish(
                "timeout",
                "Main menu timed out",
                channel_id=self.channel_id,
                caller=self.caller,
                line=self.line,
                data={"menu": "main"},
            )
            await self._ari.play(self.channel_id, "sound:goodbye")
            await self._cleanup()
            return

        if digit == "1":
            await self._publish_route("Sales menu")
            await self._submenu_sales()
        elif digit == "2":
            await self._publish_route("Support menu")
            await self._submenu_support()
        elif digit == "0":
            await self._publish_route("Operator")
            await self._connect_operator()
        elif digit == "8":
            await self._publish_route("Voicemail")
            await self._voicemail()
        elif digit == "#":
            await self._main_menu(retries)
        else:
            await self._publish_route("Invalid digit", digit=digit)
            await self._ari.play(self.channel_id, "sound:invalid")
            await self._main_menu(retries + 1)

    async def _submenu_sales(self, retries: int = 0) -> None:
        if retries >= MAX_RETRIES:
            await self._main_menu()
            return

        digit = await self._collect_digit("digits/1")  # replace with custom sales prompt
        if digit is not None:
            await self._events.publish(
                "dtmf",
                f"Sales menu digit {digit}",
                channel_id=self.channel_id,
                caller=self.caller,
                line=self.line,
                digit=digit,
                data={"menu": "sales"},
            )

        if digit == "1":
            log.info("Sales - new inquiry, channel=%s", self.channel_id)
            await self._publish_route("Sales new inquiry to 1001", digit=digit)
            await self._connect_endpoint("PJSIP/1001")
        elif digit == "2":
            log.info("Sales - existing order, channel=%s", self.channel_id)
            await self._publish_route("Sales existing order to 1001", digit=digit)
            await self._connect_endpoint("PJSIP/1001")
        elif digit == "0" or digit is None:
            await self._main_menu()
        else:
            await self._ari.play(self.channel_id, "sound:invalid")
            await self._submenu_sales(retries + 1)

    async def _submenu_support(self, retries: int = 0) -> None:
        if retries >= MAX_RETRIES:
            await self._main_menu()
            return

        digit = await self._collect_digit("digits/2")  # replace with custom support prompt
        if digit is not None:
            await self._events.publish(
                "dtmf",
                f"Support menu digit {digit}",
                channel_id=self.channel_id,
                caller=self.caller,
                line=self.line,
                digit=digit,
                data={"menu": "support"},
            )

        if digit == "1":
            log.info("Support - billing, channel=%s", self.channel_id)
            await self._publish_route("Support billing to 1002", digit=digit)
            await self._connect_endpoint("PJSIP/1002")
        elif digit == "2":
            log.info("Support - technical, channel=%s", self.channel_id)
            await self._publish_route("Support technical to 1002", digit=digit)
            await self._connect_endpoint("PJSIP/1002")
        elif digit == "0" or digit is None:
            await self._main_menu()
        else:
            await self._ari.play(self.channel_id, "sound:invalid")
            await self._submenu_support(retries + 1)

    # ------------------------------------------------------------------ call handling

    async def _connect_operator(self) -> None:
        log.info("Connecting to operator, channel=%s", self.channel_id)
        await self._connect_endpoint("PJSIP/1001")

    async def _connect_endpoint(self, endpoint: str) -> None:
        """Bridge the inbound channel with an outbound call to endpoint."""
        bridge = await self._ari.create_bridge()
        self._bridge_id = bridge.get("id")
        if not self._bridge_id:
            log.error("Failed to create bridge for channel=%s", self.channel_id)
            await self._events.publish(
                "error",
                "Bridge creation failed",
                channel_id=self.channel_id,
                caller=self.caller,
                line=self.line,
                data={"endpoint": endpoint},
            )
            await self._cleanup()
            return

        outbound = await self._ari.dial_endpoint(endpoint, self.caller, APP_NAME)
        outbound_id = outbound.get("id")
        if not outbound_id:
            log.error("Failed to dial %s for channel=%s", endpoint, self.channel_id)
            await self._events.publish(
                "error",
                f"Failed to dial {endpoint}",
                channel_id=self.channel_id,
                caller=self.caller,
                line=self.line,
                data={"endpoint": endpoint},
            )
            await self._ari.destroy_bridge(self._bridge_id)
            self._bridge_id = None
            await self._cleanup()
            return

        await self._ari.add_to_bridge(self._bridge_id, self.channel_id)
        await self._ari.add_to_bridge(self._bridge_id, outbound_id)
        log.info("Bridge created id=%s inbound=%s outbound=%s", self._bridge_id, self.channel_id, outbound_id)
        await self._events.publish(
            "bridge",
            f"Bridge connected to {endpoint}",
            channel_id=self.channel_id,
            caller=self.caller,
            line=self.line,
            data={"bridge_id": self._bridge_id, "endpoint": endpoint, "outbound_id": outbound_id},
        )

    async def _voicemail(self) -> None:
        log.info("Voicemail, channel=%s caller=%s", self.channel_id, self.caller)
        await self._ari.play(self.channel_id, "sound:vm-youhave")
        await self._cleanup()

    async def _cleanup(self) -> None:
        await self._events.publish(
            "hangup",
            "Call cleanup and hangup",
            channel_id=self.channel_id,
            caller=self.caller,
            line=self.line,
        )
        if self._recording_name:
            await self._ari.stop_recording(self._recording_name)
        if self._bridge_id:
            await self._ari.destroy_bridge(self._bridge_id)
            self._bridge_id = None
        await self._ari.hangup(self.channel_id)

    async def _forward_digit_to_line2(self, digit: str) -> None:
        """
        Propagate a DTMF digit entered on FXS Line 1 to Line 2.

        Emits a paired set of events so the Protocol page surfaces the hand-off:
          - a 'route' event describing the Line 1 → Line 2 forward
          - a 'dtmf' event tagged line='2' so the Line 2 panel's DTMF sequence
            reflects the propagated digit (data.forwarded_from='1').

        Only Line 1 (caller 1001) drives this; Line 2's own digits are not
        re-forwarded (no loop). Requires both ports registered to be meaningful;
        without registration there is no Line-1 channel, so this never fires.
        """
        if self.line != "1":
            return
        await self._events.publish(
            "route",
            f"Line 1 digit {digit} forwarded to Line 2",
            channel_id=self.channel_id,
            caller=self.caller,
            line="1",
            digit=digit,
            data={"route": "line1_to_line2", "target_line": "2", "digit": digit},
        )
        await self._events.publish(
            "dtmf",
            f"Line 2 received forwarded digit {digit} from Line 1",
            caller="1002",
            line="2",
            digit=digit,
            data={"menu": "forwarded", "forwarded_from": "1", "source_channel": self.channel_id},
        )
        log.info("forwarded digit=%s line1->line2 channel=%s", digit, self.channel_id)

    async def _publish_route(self, route: str, digit: str | None = None) -> None:
        await self._events.publish(
            "route",
            f"Routing to {route}",
            channel_id=self.channel_id,
            caller=self.caller,
            line=self.line,
            digit=digit,
            data={"route": route},
        )


class IVRApp:
    def __init__(self, ari: ARIClient, events: EventPublisher) -> None:
        self._ari = ari
        self._events = events
        self._sessions: dict[str, IVRSession] = {}

    async def on_stasis_start(self, event: dict) -> None:
        channel = event["channel"]
        cid = channel["id"]
        caller = channel.get("caller", {}).get("number", "unknown")

        # Skip channels that are outbound dials from connect_endpoint
        if event.get("args"):
            log.debug("Ignoring outbound channel %s args=%s", cid, event["args"])
            return

        await self._events.publish(
            "stasis_start",
            f"Channel entered ARI Stasis from {caller}",
            channel_id=cid,
            caller=caller,
            line=_line_for_caller(caller),
        )
        session = IVRSession(self._ari, self._events, cid, caller)
        self._sessions[cid] = session
        asyncio.create_task(session.run())

    async def on_dtmf(self, event: dict) -> None:
        cid = event["channel"]["id"]
        digit = event.get("digit", "")
        session = self._sessions.get(cid)
        if session:
            session.deliver_digit(digit)

    async def on_hangup(self, event: dict) -> None:
        cid = event["channel"]["id"]
        log.info("Hangup channel=%s", cid)
        session = self._sessions.pop(cid, None)
        if session:
            session.cancel_pending()
        await self._events.publish(
            "channel_destroyed",
            "Asterisk channel ended",
            channel_id=cid,
        )

    async def dispatch(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "StasisStart":
            asyncio.create_task(self.on_stasis_start(event))
        elif etype == "ChannelDtmfReceived":
            await self.on_dtmf(event)
        elif etype in ("ChannelHangupRequest", "ChannelDestroyed"):
            await self.on_hangup(event)


async def run() -> None:
    ari = ARIClient()
    events = EventPublisher()
    app = IVRApp(ari, events)
    backoff = 2

    log.info("Connecting to Asterisk ARI at %s", _WS_URL.split("?")[0])
    while True:
        try:
            async with websockets.connect(_WS_URL, ping_interval=20) as ws:
                log.info("Connected — listening for Stasis events app=%s", APP_NAME)
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
    await events.aclose()


if __name__ == "__main__":
    asyncio.run(run())
