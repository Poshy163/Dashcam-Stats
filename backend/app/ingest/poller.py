"""The presence poll: notice the car in the driveway, and start pulling.

This does not live in :mod:`app.workers.scheduler`. That scheduler clamps every task to a
thirty-second floor, which is right for scans and retention and useless here: the unit has
no battery, so the whole opportunity is the one to two minutes the engine runs, and thirty
seconds of that can be half the window. Rather than loosen a floor that protects everything
else, ingest owns its own tick.

Only the *transition* to online starts a run. A unit that simply stays on the network -- the
car idling on the driveway -- must not have a new pull started on top of the last one, and
`IngestStatus.try_begin` refuses that anyway; this just avoids asking.
"""

from __future__ import annotations

import asyncio
import contextlib

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.ingest import puller
from app.ingest.models import RunState, UnitState
from app.ingest.status import get_status

log = get_logger(__name__)

#: Floor on the poll interval. Each tick is two sub-second ADB control calls, so this is
#: cheap, but it is still a subprocess pair and not worth spinning on.
MIN_POLL_S = 3.0


class IngestPoller:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._was_online = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="ingest-poller")
        log.info("ingest poller started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        # Stop the ticker first, then the transfer: otherwise the next tick could start a
        # new pull while this one is being wound down.
        await puller.shutdown()
        log.info("ingest poller stopped")

    @property
    def healthy(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def _interval(self) -> float:
        try:
            return max(
                MIN_POLL_S, float(get_settings_service().get_nowait("ingest.poll_interval_s"))
            )
        except Exception:
            return 8.0

    def _enabled(self) -> bool:
        try:
            return bool(get_settings_service().get_nowait("ingest.enabled"))
        except Exception:
            return False

    async def _loop(self) -> None:
        status = get_status()
        while self._running:
            try:
                # A transfer in flight outranks everything else this loop might say. It
                # used to be checked second, so switching the feature off mid-window
                # rewrote the live state to "disabled" -- which hid the Cancel button on
                # the very transfer someone had just decided to stop.
                if status.running:
                    await asyncio.sleep(self._interval())
                    continue

                if not self._enabled():
                    status.set_state(RunState.DISABLED)
                    self._was_online = False
                    await asyncio.sleep(max(MIN_POLL_S, 15.0))
                    continue

                info = await puller.probe_unit()
                status.set_unit_online(info.online)
                if info.state is UnitState.UNAUTHORIZED:
                    status.set_state(RunState.UNAUTHORIZED)
                elif not info.online:
                    status.set_state(RunState.OFFLINE)
                    self._was_online = False
                else:
                    if not self._was_online:
                        log.info("the head unit appeared; starting a pull", address=info.address)
                        # Not awaited: a pull runs for as long as the window lasts and the
                        # poll has to keep ticking underneath it. `start_run` keeps the
                        # reference so the task cannot be collected and shutdown can find it.
                        puller.start_run(trigger="auto")
                    self._was_online = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("ingest poll failed", error=f"{type(exc).__name__}: {exc}")
                self._was_online = False

            await asyncio.sleep(self._interval())


_poller: IngestPoller | None = None


def get_poller() -> IngestPoller:
    global _poller
    if _poller is None:
        _poller = IngestPoller()
    return _poller
