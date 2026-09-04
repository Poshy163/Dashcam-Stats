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
from app.ingest import (
    adb,
    band,
    carplay_timing,
    health,
    puller,
    radio_coordinator,
    radios,
    unit_logs,
)
from app.ingest.models import RunState, UnitInfo, UnitState, ingest_setting
from app.ingest.status import get_status

log = get_logger(__name__)

#: Floor on the poll interval.
#:
#: A tick against an absent car is now one TCP connect that fails in under half a second,
#: not the three ``adb`` process spawns it used to be, so looking often is affordable in a
#: way it was not. That matters more than it sounds: even the five-minute resting window
#: is finite and the transfer runs at ~34 MB/s, so four seconds spent not noticing the
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

#: Delays before retrying a failed pull while the same unit remains online.
#:
#: A transfer can fail for a one-off reason (the observed case was a receive timeout with
#: the ADB socket still reachable), so requiring the unit to disappear before trying again
#: strands a ready bundle indefinitely.  The finite schedule gives transient failures
#: another chance without turning a persistently broken unit into an unbounded retry loop.
#: The budget is reset when the visit ends or a non-error result is observed.
ERROR_RETRY_DELAYS_S = (15.0, 30.0, 60.0)

#: Do not start a top-up with less than this left on the unit's sleep countdown.
#:
#: A top-up no longer widens the window, so the countdown keeps running underneath it and
#: the unit sleeps on schedule whether or not a copy is in flight. Starting one this close
#: to the boundary just quiets both radios, moves a segment or two, and gets cut off by the
#: sleep it could not extend -- churn with nothing to show for it, and the next arrival
#: would have collected the same files anyway. Ninety seconds covers a small batch with the
#: radio capture and restore either side of it.
REDRAIN_MIN_COUNTDOWN_S = 90.0


class IngestPoller:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._was_online = False
        #: When the last run that found nothing finished, so the re-check can back off.
        self._idle_since: float = 0.0
        #: Retries already started for consecutive errors during this online visit.
        self._error_retries_started = 0
        #: The unit as this visit first described it, kept only while the arrival gate is
        #: holding. See the note in :meth:`_loop`; cleared the moment the port goes quiet.
        self._visit_info: UnitInfo | None = None

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
        # Local watcher sessions only; the script on the unit keeps running, by design --
        # the app going down is exactly the kind of absence it exists to cover.
        await health.shutdown()
        await unit_logs.shutdown()
        await carplay_timing.shutdown()
        await band.shutdown()
        log.info("ingest poller stopped")

    @property
    def healthy(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    # Every accessor below falls back to the *schema* default rather than to a literal
    # written here. Two of these had already drifted: the poll interval fell back to 8 s
    # against a default of 2, and the arrival gate's uptime threshold fell back to 0
    # against a default of 120 -- which does not weaken the gate, it disables it, so a pull
    # fires as the car is pulling off the driveway rather than when it arrives.

    def _interval(self) -> float:
        return max(MIN_POLL_S, float(ingest_setting("poll_interval_s")))

    def _enabled(self) -> bool:
        return bool(ingest_setting("enabled"))

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

        A failed run gets a small, finite retry budget during the same online visit. A
        receive timeout does not mean the ready files disappeared, and waiting for another
        offline/online edge can strand them for a whole day. The delays grow between tries
        and the budget stops after :data:`ERROR_RETRY_DELAYS_S`, so a persistently broken
        unit is not driven in a hot loop.

        The run that moved files waits out a cooldown first. Each run quiets the unit's
        Bluetooth and hotspot and restores them on the way out, so going again the instant
        it finished meant the radios came back and were cut again within a second, over and
        over, for as long as the card kept yielding something -- which is what the driver
        saw as the unit's screen and phone connection "glitching" after a backup. The
        cooldown leaves the radios on, and the screen alone, for a minute between passes;
        the sweeps inside the run itself catch most of what used to need a re-drain anyway.
        """
        if status.state is not RunState.ERROR:
            # Success (or any other explicit outcome) ends an error streak. CANCELLED is
            # still terminal below; clearing an old error budget does not restart it.
            self._error_retries_started = 0

        if status.state is RunState.IDLE:
            now = time.monotonic()
            if self._idle_since and now - self._idle_since < IDLE_RECHECK_S:
                return False
            self._idle_since = now
            return True
        if status.state in (RunState.OK, RunState.PARTIAL):
            self._idle_since = 0.0
            if status.since_finished() < self._redrain_cooldown_s():
                return False
            # Let it sleep. Only while the ignition is off is a countdown actually
            # running; while driving the figure is the full window and never trips this.
            remaining = status.sleep_countdown_remaining_s()
            about_to_sleep = (
                status.ignition_state == "off"
                and remaining is not None
                and remaining < REDRAIN_MIN_COUNTDOWN_S
            )
            return not about_to_sleep
        if status.state is RunState.ERROR:
            if self._error_retries_started >= len(ERROR_RETRY_DELAYS_S):
                return False
            delay = ERROR_RETRY_DELAYS_S[self._error_retries_started]
            if status.since_finished() < delay:
                return False
            self._error_retries_started += 1
            return True
        # CANCELLED is deliberately not in that list. Somebody pressed Stop; starting the
        # same transfer again two seconds later is not a re-drain, it is ignoring them.
        return False

    def _address(self) -> str:
        # Normalised, because the cheap presence check and adb itself disagreed about
        # an address with no port: the socket connect succeeded against the default port
        # while `adb -s <host>` answered "device not found", so a poller pointed at
        # `192.168.1.122` took the expensive branch on every tick forever.
        return adb.normalised_address(str(ingest_setting("unit_adb_address") or ""))

    def _redrain_cooldown_s(self) -> float:
        return max(0.0, float(ingest_setting("redrain_cooldown_s") or 0))

    def _min_uptime_s(self) -> float:
        return max(0.0, float(ingest_setting("min_uptime_s") or 0))

    async def _recover_pending_while_disabled(self) -> bool:
        """Reconcile only a durable interrupted transition while ingest is disabled.

        The database-only first probe is load-bearing: a normally disabled installation
        must make zero network/ADB contact.  Once a pending row exists, a cheap socket
        probe on the configured and last-known endpoints lets the safety state machine
        notice the car's next arrival without enabling ordinary footage ingestion.
        """
        stored_address = await radio_coordinator.pending_recovery_address()
        if stored_address is None:
            return False

        candidates: list[str] = []
        for candidate in (self._address(), stored_address):
            candidate = adb.normalised_address(candidate)
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        for candidate in candidates:
            if not await adb.is_listening(candidate):
                continue
            if await puller.reconcile_pending_in_awake_window(candidate):
                log.info(
                    "reconciled an interrupted radio transition while ingest is disabled",
                    address=candidate,
                )
                return True
        return False

    async def _arrival_ready(self, address: str) -> bool:
        """Whether an automatic pull may start, or the unit has only just booted.

        The arrival gate. The unit has no battery, so its uptime is the length of the
        current drive — a car pulling back onto the driveway has been running for the whole
        trip, one pulling off it has just booted. Holding the first pull until the uptime
        clears the threshold is what makes a backup happen on the way *in* rather than as a
        doomed few seconds on the way *out*, when the car is about to drive out of range.

        Only the first pull of a visit is gated; once a window is under way its follow-on
        drains are not, because by then the unit is plainly parked. A hold is published to
        the status so the Backup page can say why nothing is moving, and cleared the moment
        it passes. An unreadable uptime is treated as ready: a backup that quietly stops
        happening is a worse failure than one that starts a little early.
        """
        threshold = self._min_uptime_s()
        if threshold <= 0:
            get_status().set_arrival(None, held=False, reason=None)
            return True
        uptime = await adb.uptime(address)
        if uptime is None:
            get_status().set_arrival(None, held=False, reason=None)
            log.debug("could not read the unit's uptime; not holding the pull for it")
            return True
        if uptime >= threshold:
            get_status().set_arrival(uptime, held=False, reason=None)
            return True
        reason = (
            f"waiting until the unit has been running for {int(threshold)}s before backing "
            f"up, so footage is pulled when you arrive rather than as you leave — it has "
            f"been up {int(uptime)}s. Re-checked every few seconds while the car is here"
        )
        was_held = get_status().arrival_hold
        get_status().set_arrival(uptime, held=True, reason=reason)
        if not was_held:
            # Once per hold episode, not once per tick: this is re-evaluated every poll
            # while the car sits there, and a line each time would bury the log.
            log.info(
                "holding the automatic backup until the unit has been running longer; it "
                "has only just booted, the signature of leaving rather than arriving",
                uptime_s=int(uptime),
                threshold_s=int(threshold),
            )
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
                    # Feature disable stops new ingest work, not a previously committed
                    # promise to restore the driver's radios/logger. This path remains a
                    # DB lookup only unless such a transition actually exists.
                    await self._recover_pending_while_disabled()
                    status.set_state(RunState.DISABLED)
                    self._was_online = False
                    self._error_retries_started = 0
                    self._visit_info = None
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
                    self._error_retries_started = 0
                    self._visit_info = None
                    await asyncio.sleep(self._interval())
                    continue

                # A tick that cannot start anything must not pay for a describe.
                #
                # `probe_unit` is four adb process spawns -- disconnect, connect, get-state
                # and a shell to resolve the card -- and it tears down and rebuilds the ADB
                # transport each time. That is the right price once per decision; it was
                # being paid on *every* tick a present unit was seen. On the deployment the
                # recorder-health card exists for -- a unit parked at home, or a bench unit
                # -- that is roughly 7,200 spawns an hour, forever, to re-answer a question
                # whose answer had already been "not yet" for hours. The cooldown between
                # re-drains does the same thing on a shorter clock while the car sits on the
                # driveway.
                #
                # Asked once per tick and remembered, because `_should_drain_again` is not
                # a pure predicate: on the IDLE path it arms `_idle_since` and returns
                # True, so a second call microseconds later sees the backoff it just set
                # and returns False. Calling it here *and* at the branch below meant the
                # empty-card re-check could never actually start a pull -- the unit was
                # re-probed every thirty seconds forever and nothing was ever fetched.
                drain_again = self._was_online and self._should_drain_again(status)
                # Healthy results wait here for their normal re-drain clock. ERROR waits
                # here for its bounded retry clock (or permanently once that visit's retry
                # budget is exhausted), instead of reconnecting ADB on every poll tick.
                # The cheap socket check above still notices the visit ending, and a due
                # retry takes the full probe path before it starts. Other failure states
                # deliberately keep probing so an authorization/card-state change is seen.
                if (
                    self._was_online
                    and not drain_again
                    and status.state
                    in (RunState.IDLE, RunState.OK, RunState.PARTIAL, RunState.ERROR)
                ):
                    # Cheap and non-destructive, and throttled inside: this is what keeps
                    # the recorder-health card current on a unit that stays present.
                    health.on_unit_present(self._address())
                    unit_logs.on_unit_present(self._address())
                    band.on_unit_present(self._address())
                    await asyncio.sleep(self._interval())
                    continue

                # During an arrival hold the only thing that changes tick to tick is the
                # uptime, so the describe from the first tick of the visit is reused rather
                # than repeated. Only a *good* describe is cached: a unit that has just
                # booted may not have mounted its card yet, and freezing that answer for the
                # length of the hold would be exactly the wrong thing to remember.
                info = (
                    self._visit_info
                    if (not self._was_online and self._visit_info is not None)
                    else await puller.probe_unit()
                )
                status.set_unit_online(info.online)
                if info.state is UnitState.UNAUTHORIZED:
                    status.set_state(RunState.UNAUTHORIZED)
                elif not info.online:
                    status.set_state(RunState.OFFLINE)
                    self._was_online = False
                    self._error_retries_started = 0
                else:
                    if not self._was_online:
                        # If a previous window ended with the unit's radios still off --
                        # the engine stopping mid-transfer is the ordinary ending -- put
                        # them right the moment the car is back, before it is asked for
                        # anything. Awaited because the long sleep window must be proven
                        # before recovery spends any of the short resting window. Safe to
                        # repeat while the arrival gate holds below: widening is idempotent,
                        # and recovery does nothing once its durable marker is clear.
                        if not await puller.reconcile_pending_in_awake_window(info.address):
                            # A stale durable transition owns the radios until its exact
                            # baseline is restored. Keep this as the arrival transition so
                            # the next tick retries; do not let a second pull manipulate or
                            # depend on half-restored state.
                            status.set_state(RunState.IDLE)
                            if info.source and not info.card_error:
                                self._visit_info = info
                            await asyncio.sleep(self._interval())
                            continue
                        radios.restore_if_pending(info.address)
                        # Collect what the recording watcher saw while the car was away,
                        # and re-arm it for the drive that is starting. Before the arrival
                        # gate on purpose: a departure window whose pull is held is exactly
                        # the drive the watcher exists to cover. Debounced inside, because
                        # this branch re-runs every tick while the gate holds.
                        health.on_unit_seen(info.address, info.source)
                        # Same arrival moment, same reason: drain the vendor log the
                        # unit wrote while it was away, then start a fresh capture.
                        unit_logs.on_unit_seen(info.address)
                        band.on_unit_present(info.address)
                        # The arrival gate: hold the first pull until the unit has been
                        # running long enough to be arriving rather than leaving. A hold
                        # leaves `_was_online` False so the next tick re-checks, and a real
                        # arrival -- which already has a high uptime -- clears it at once.
                        if await self._arrival_ready(info.address):
                            # A transfer is about to start, so take a fresh view of the unit
                            # rather than the one cached through the hold -- the card may
                            # have been mounted, or replaced, in the minutes since.
                            if self._visit_info is not None:
                                info = await puller.probe_unit()
                                self._visit_info = None
                                status.set_unit_online(info.online)
                                if not info.online:
                                    await asyncio.sleep(self._interval())
                                    continue
                            log.info(
                                "the head unit appeared; starting a pull", address=info.address
                            )
                            self._idle_since = 0.0
                            self._error_retries_started = 0
                            # Not awaited: a pull runs for as long as the window lasts and
                            # the poll has to keep ticking underneath it. `start_run` keeps
                            # the reference so the task cannot be collected and shutdown can
                            # find it.
                            #
                            # `info` is handed over rather than left to be re-derived. This
                            # describe has just reconnected and resolved the card one line
                            # above; doing it again inside the run tore down the link it had
                            # only just proven good, and paid for the round trip twice at
                            # the most expensive moment of the day.
                            puller.start_run(trigger="auto", info=info)
                            self._was_online = True
                        else:
                            # Held for the arrival gate. IDLE like the band hold: not a
                            # failure, and the one state the poll re-checks while the car
                            # is here. `_was_online` stays False so this stays the arrival
                            # transition until the uptime clears.
                            if info.online and info.source and not info.card_error:
                                self._visit_info = info
                            status.set_state(RunState.IDLE)
                    elif drain_again:
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
                        if status.state is RunState.ERROR:
                            log.warning(
                                "the previous automatic pull failed while the head unit "
                                "remained online; retrying",
                                retry=self._error_retries_started,
                                retry_limit=len(ERROR_RETRY_DELAYS_S),
                                last_error=status.last_error,
                            )
                        else:
                            log.info("the head unit is still here; looking for more to copy")
                        puller.start_run(trigger="auto", info=info, continuation=True)
                    # `_was_online` is set the moment a pull actually starts (above), not
                    # merely because the unit is present -- otherwise the arrival gate's
                    # hold would be mistaken for a window already under way, and the next
                    # tick would fall through to the drain-again branch instead of
                    # re-checking the uptime.
                    #
                    # Keep the recorder-health card current while the unit stays present.
                    # Throttled inside, and non-destructive, so it costs one small read now
                    # and then -- what makes the card mean something on a unit parked at
                    # home, where the arrival collect above fires only once.
                    health.on_unit_present(info.address)
                    unit_logs.on_unit_present(info.address)
                    carplay_timing.on_unit_present(info.address)
                    band.on_unit_present(info.address)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("ingest poll failed", error=f"{type(exc).__name__}: {exc}")
                self._was_online = False
                self._error_retries_started = 0
                # Cleared wherever `_was_online` is, because the two together are what
                # decides whether the next tick reuses a describe. Left armed on this path,
                # a transient adb failure -- or the feature being switched off and back on
                # hours later -- would hand a stale address and card path to the radio
                # restore and the health collect.
                self._visit_info = None

            await asyncio.sleep(self._interval())


_poller: IngestPoller | None = None


def get_poller() -> IngestPoller:
    global _poller
    if _poller is None:
        _poller = IngestPoller()
    return _poller
