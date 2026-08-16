"""The presence poll: notice the car in the driveway, and start pulling.

This does not live in :mod:`app.workers.scheduler`. That scheduler clamps every task to a
thirty-second floor, which is right for scans and retention and useless here: the unit has
no battery, so the whole opportunity is the one to two minutes the engine runs, and thirty
seconds of that can be half the window. Rather than loosen a floor that protects everything
else, ingest owns its own tick.

The transition to online starts the first run, and the unit staying on the network starts
the next one. That second half was missing, and it was costing whole windows: a pull fired
once on arrival and then nothing happened for as long as the car sat there. The segment
being recorded when the plan was drawn is skipped on purpose, everything the camera closes
during the transfer arrives too late to be in it, and a run cut short by the link dropping
has files it never reached -- all of which waited for the *next* window, which here means
the next time somebody drives. One measured 13.5 GB run took seven minutes, and the camera
wrote about 2 MB/s throughout.

Never two at once: a run in flight returns this loop early, and `IngestStatus.try_begin`
refuses a second claim regardless.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.ingest import adb, puller
from app.ingest.models import RunState, UnitState
from app.ingest.status import get_status

log = get_logger(__name__)

#: Floor on the poll interval.
#:
#: A tick against an absent car is now one TCP connect that fails in under half a second,
#: not the three ``adb`` process spawns it used to be, so looking often is affordable in a
#: way it was not. That matters more than it sounds: the window is sixty to a hundred and
#: twenty seconds and the transfer runs at ~34 MB/s, so four seconds spent not noticing the
#: car has arrived is about 140 MB of footage that waits until tomorrow.
MIN_POLL_S = 1.0

#: How long to leave a present-but-nothing-to-copy unit alone before asking again.
#:
#: The re-check below exists because a window is not over when the first pull finishes, but
#: it must not turn into a card listing every couple of seconds for as long as the car sits
#: on the driveway. A run that found nothing has just proved the card is drained; the only
#: thing that changes that is the recorder closing another segment, which on this camera is
#: every five minutes. Thirty seconds is well inside that and costs one `stat` of the card.
IDLE_RECHECK_S = 30.0


class IngestPoller:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._was_online = False
        #: When the last run that found nothing finished, so the re-check can back off.
        self._idle_since: float = 0.0

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

    def _should_drain_again(self, status) -> bool:
        """Whether to start another pull at a unit that is already here.

        Only reached with no run in flight -- the loop returns early while one is running --
        so this is purely "has anything changed since the last one finished".

        Two cases, and they want opposite answers. A run that *moved files* says the card
        had a backlog, and the reasons it stopped are all reasons to go again immediately:
        the segment being recorded when the plan was drawn is now closed, more have been
        written during the transfer, and a run cut short by the car leaving has files left
        that it never reached. A run that found *nothing* has proved the card is drained,
        and asking again straight away would list it every couple of seconds for as long as
        the car sits there -- so that one waits out :data:`IDLE_RECHECK_S`, which is well
        inside the five minutes it takes the camera to close another segment.

        Errors and offline runs are left to the arrival transition rather than retried in a
        loop; a unit that is answering its ADB port but failing every pull should not have
        that failure driven at it on a timer.
        """
        if status.state is RunState.IDLE:
            now = time.monotonic()
            if self._idle_since and now - self._idle_since < IDLE_RECHECK_S:
                return False
            self._idle_since = now
            return True
        if status.state in (RunState.OK, RunState.PARTIAL):
            self._idle_since = 0.0
            return True
        # CANCELLED is deliberately not in that list. Somebody pressed Stop; starting the
        # same transfer again two seconds later is not a re-drain, it is ignoring them.
        return False

    def _address(self) -> str:
        try:
            return str(get_settings_service().get_nowait("ingest.unit_adb_address") or "").strip()
        except Exception:
            return ""

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

                # One socket before three subprocesses. The car is absent for all but a few
                # minutes of the day, and on every one of those ticks the expensive question
                # -- reconnect, get-state, resolve the card -- has the same answer as a
                # refused connection to the ADB port, for a small fraction of the cost.
                # Nothing is concluded from an *open* port beyond "worth asking properly":
                # it says something is listening, not that it is authorised or that its card
                # is mounted, and both of those still come from `describe`.
                if not await adb.is_listening(self._address()):
                    status.set_unit_online(False)
                    status.set_state(RunState.OFFLINE)
                    self._was_online = False
                    await asyncio.sleep(self._interval())
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
                        self._idle_since = 0.0
                        # Not awaited: a pull runs for as long as the window lasts and the
                        # poll has to keep ticking underneath it. `start_run` keeps the
                        # reference so the task cannot be collected and shutdown can find it.
                        #
                        # `info` is handed over rather than left to be re-derived. This
                        # describe has just reconnected and resolved the card one line
                        # above; doing it again inside the run tore down the link it had
                        # only just proven good, and paid for the round trip twice at the
                        # most expensive moment of the day.
                        puller.start_run(trigger="auto", info=info)
                    elif self._should_drain_again(status):
                        # The window is not over when the first pull ends, and it used to be
                        # treated as though it were: this fired once on arrival and then sat
                        # idle for as long as the car stayed. Everything the recorder closed
                        # during the transfer, and the segment that was still being written
                        # when the plan was drawn up, waited for the *next* window -- which
                        # on this deployment is the next time somebody drives.
                        #
                        # A 13.5 GB run takes seven minutes, and the camera writes about
                        # 2 MB/s while it does, so that one window alone left ~840 MB behind
                        # with the car still sitting on the driveway.
                        log.info("the head unit is still here; looking for more to copy")
                        puller.start_run(trigger="auto", info=info)
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
