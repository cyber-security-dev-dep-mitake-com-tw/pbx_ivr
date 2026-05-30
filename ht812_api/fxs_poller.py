import asyncio
import structlog
from events import CommunicationEventIn

log = structlog.get_logger()

_POLL_INTERVAL = 2.0
_HOOK_LABELS = {"0": "on-hook", "1": "off-hook"}


class FXSPoller:
    """Polls HT812 FXS hook/reg state every 2 s; emits fxs_hook events on transitions."""

    def __init__(self, app) -> None:
        self._app = app
        self._last_hook: dict[int, str] = {}
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="fxs_poller")
        log.info("fxs_poller_started", interval_s=_POLL_INTERVAL)

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            log.info("fxs_poller_stopped")

    async def _run(self) -> None:
        await asyncio.sleep(4.0)  # wait for HT812Client auth to settle
        while True:
            try:
                await self._poll()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("fxs_poll_error", error=str(e))
            await asyncio.sleep(_POLL_INTERVAL)

    async def _poll(self) -> None:
        vals = await self._app.state.ht812.get_values(
            ["P4901", "P4902", "P4921", "P4922"]
        )
        for port, hook_key, reg_key in [
            (1, "P4901", "P4921"),
            (2, "P4902", "P4922"),
        ]:
            hook = vals.get(hook_key, "")
            prev = self._last_hook.get(port)

            if prev is None:
                # First poll — record state without emitting an event
                self._last_hook[port] = hook
                continue

            if hook == prev:
                continue

            self._last_hook[port] = hook
            label = _HOOK_LABELS.get(hook, hook or "unknown")
            prev_label = _HOOK_LABELS.get(prev, prev or "unknown")
            registered = vals.get(reg_key, "") == "1"

            event = self._app.state.events.add(
                CommunicationEventIn(
                    source="ht812_poller",
                    type="fxs_hook",
                    message=f"FXS port {port}: {prev_label} → {label}",
                    line=str(port),
                    data={
                        "port": port,
                        "hook_state": hook,
                        "hook_label": label,
                        "prev_label": prev_label,
                        "registered": registered,
                    },
                )
            )
            await self._app.state.event_queue.put(event)
            log.info(
                "fxs_hook_change",
                port=port,
                prev=prev_label,
                state=label,
                registered=registered,
            )
