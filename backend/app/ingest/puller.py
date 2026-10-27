"""Orchestration: delta, transfer, stage, commit.

The app is the initiator. The unit runs no scheduler, no agent and nothing installed --
it only listens when asked. That is deliberate: the head unit has no battery, so it exists
on the network only while the car is running, and anything that depended on it *starting*
something would have to survive being killed mid-thought every time the engine stops.
Polling for it and pulling puts every moving part on the always-on side.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import get_config
from app.core.logging import get_logger
from app.core.settings_schema import (
    INGEST_SLEEP_WINDOW_ACTIVE_SECONDS,
    INGEST_SLEEP_WINDOW_IDLE_SECONDS,
)
from app.core.settings_service import get_settings_service
from app.ingest import (
    adb,
    band,
    duplicate_cleanup,
    elevate,
    movement,
    obd_control,
    origin,
    parked_filter,
    radio_coordinator,
    radios,
    transport,
)
from app.ingest.models import (
    DeltaPlan,
    Phase,
    RemoteFile,
    RunResult,
    RunState,
    UnitInfo,
    UnitState,
)
from app.ingest.models import (
    ingest_setting as _get,
)
from app.ingest.obd_events import (
    EVENT_SYNC_TIMEOUT_SECONDS,
    EventSyncResult,
    get_logger_event_status,
    sync_remote_events,
)
from app.ingest.obd_storage import redact
from app.ingest.obd_transfer import (
    OBDTransferResult,
    get_obd_transfer_status,
    inventory_remote_bundles,
    read_logger_status,
    sync_remote_bundles,
    verified_bundle_matches,
)
from app.ingest.status import IngestStatus, get_status

log = get_logger(__name__)


@dataclass(slots=True)
class _RunLifecycle:
    started_at: datetime
    released: bool = False
    policy_fingerprint_checks: int = 0
    analysis_released: set[str] = field(default_factory=set)


# Status clears its start and can admit a successor before history writes finish.
# Keep the timestamp and release state with the task that claimed this run.
_run_lifecycle: ContextVar[_RunLifecycle | None] = ContextVar("ingest_run_lifecycle", default=None)

#: Staged arrivals live here until they are whole. A dot-directory so the scanner's walk
#: skips it -- see `app.scanner.discovery`.
STAGING_DIRNAME = ".ingest_staging"

# The first display intent can race a head unit whose browser task is still thawing after a
# drive.  These retries are deliberately few and remain a background courtesy: no transfer byte
# waits for them, and the task is cancelled as soon as the transfer ends.
DISPLAY_RETRY_DELAYS_S = (0.0, 3.0, 10.0)
APP_OWNED_SLEEP_WINDOW_CAPABILITY = "adaptive_sleep_window_v2"
APP_OWNED_SLEEP_STATUS_READ_TIMEOUT_S = 6.0
APP_OWNED_SLEEP_STATUS_MAX_AGE_S = 30.0
QUIET_EVIDENCE_CAPABILITY = "sleep_deadline_evidence_v1"
QUIET_EVIDENCE_WAIT_S = 15.0
QUIET_EVIDENCE_RETRY_S = 2.0
# read_logger_status has a five-second ADB timeout; the parallel runtime read takes
# at most three. Do not start either when its normal bound would exceed the grace.
_QUIET_EVIDENCE_PROBE_BUDGET_S = 5.0
_EVENT_SYNC_AWAIT_GRACE_SECONDS = 0.25

# The first parked pair becomes eligible only after the two-minute ignition margin
# and a closed segment. This opportunity is anchored to the REAL OFF edge, never
# to an ingest trigger. Later paired candidates keep their own existing deadlines.
QUIET_ANALYSIS_FIRST_WINDOW_S = 480.0
QUIET_ANALYSIS_MAX_WAIT_S = 180.0
QUIET_ANALYSIS_RECHECK_S = 10.0
QUIET_ANALYSIS_PROBE_TIMEOUT_S = 20.0
QUIET_ANALYSIS_NATIVE_RESERVE_S = 240.0
QUIET_ANALYSIS_BATCH_CHECK_S = 30.0

# One bounded recovery budget for normal cleanup and later reconciliation. Hotspot
# attestation alone permits three 15-second reads; the old 30-second total could cancel
# before sending its start request, after Bluetooth had already been restored. Leave
# room for Bluetooth/AP verification, watchdog stand-down and the two OBD resume reads.
RADIO_RESTORE_TIMEOUT_S = 120.0


#: How often, while a transfer runs, the page is checked to still be in front. The vendor's
#: CarPlay app (Zlink) raises its own dashboard over ours a short way into a backup --
#: observed live, mid-transfer -- and nothing else puts ours back.
HOLD_FOREGROUND_INTERVAL_S = 8.0

#: The least time between two re-opens of the page, however often the check says it is
#: covered. Re-opening is a VIEW intent, which reloads the tab and flashes Chrome's tab
#: strip, so a check that is wrong about the foreground must not be able to turn that into
#: a reload every tick -- which is exactly what happened when the check read window focus
#: under a CarPlay overlay. The check is fixed; this is the backstop that makes the next
#: wrong answer cost one reload a minute rather than one every eight seconds.
HOLD_REOPEN_MIN_S = 45.0


def _hold_page_foreground() -> bool:
    """Whether to keep the page in front for the life of the transfer. Never raises."""
    try:
        return bool(_get("hold_page_foreground", False))
    except Exception:
        return False


async def _show_backup_page_during_transfer(address: str, url: str) -> None:
    """Open and visibly confirm the car-screen dashboard while a transfer is active, and,
    if asked, keep it there.

    The initial open retries a few times because ``am start`` can succeed while a launcher
    or overlay stays in front. After that the vendor's CarPlay app is free to raise its own
    dashboard over the page, and on the live unit it does exactly that a short way into a
    backup, leaving the car showing Zlink's home screen instead of what is being copied.

    So, while this task lives, it periodically checks the page is still in front and, if
    not, opens it again by URL. By URL deliberately, at the cost of one reload: merely
    resuming Chrome's existing task (its launcher activity) lands on the *tab grid* whenever
    more than one tab is open -- seen on the unit, which had two -- and the grid is not the
    page. A VIEW of the URL shows the tab itself. The reload only happens when something
    actually covered the page, not every tick, so it is the price of recovery rather than
    the "screen refresh" that once fired on every visit. The task is cancelled the moment
    the transfer ends, which is what makes this safe: it can never compete with the driver,
    because a transfer only ever runs on a parked car.
    """
    # Look before opening, every time. A VIEW intent to a page that is already on screen
    # is not a no-op: Chrome reloads the tab and flashes its tab strip. With the engine
    # idling on the driveway the recorder keeps producing footage, the poller re-runs
    # about once a minute, and each run used to fire this unconditionally -- so the page
    # visibly refreshed itself for as long as the car sat there.
    shown = False
    for attempt, delay_s in enumerate(DISPLAY_RETRY_DELAYS_S, start=1):
        if delay_s:
            await asyncio.sleep(delay_s)
        if adb.optional_requests_deferred(address):
            log.info("deferring the backup display after an ADB timeout; prioritising footage")
            return
        if await adb.chrome_is_foreground(address):
            if attempt == 1:
                log.info("the backup page is already showing on the head unit")
            else:
                log.info("opened and verified the backup page on the head unit", attempt=attempt)
            shown = True
            break
        if adb.optional_requests_deferred(address):
            return
        reason = await adb.show_url(address, url)
        if reason:
            log.warning(
                "backup page was not visible on the head unit; will retry while transferring",
                attempt=attempt,
                error=reason,
            )
    if not shown:
        if adb.optional_requests_deferred(address):
            return
        # One last look: the final open may simply not have settled before the loop ended.
        shown = await adb.chrome_is_foreground(address)
        if shown:
            log.info("opened and verified the backup page on the head unit", attempt=attempt)
        else:
            log.warning("could not show the backup page during this transfer")
    if not _hold_page_foreground():
        return

    # Hold it there until the transfer's end cancels this task. Every re-open is logged at
    # info: a demoted repeat is how the reload-every-tick above went unseen.
    last_reopen: float | None = None
    while True:
        await asyncio.sleep(HOLD_FOREGROUND_INTERVAL_S)
        if adb.optional_requests_deferred(address):
            continue
        if await adb.chrome_is_foreground(address):
            continue
        if adb.optional_requests_deferred(address):
            continue
        now = time.monotonic()
        if last_reopen is not None and now - last_reopen < HOLD_REOPEN_MIN_S:
            continue
        last_reopen = now
        reason = await adb.show_url(address, url)
        log.info(
            "put the backup page back over the head unit's own screen",
            error=reason or None,
        )


def _by_directory(files: list[RemoteFile], default: str) -> list[tuple[str, list[RemoteFile]]]:
    """Group a plan into one batch per directory, preserving the plan's order.

    Order is preserved rather than sorted because it is a decision, not an accident:
    ``ingest.transfer_order`` chose it, and a window that only gets through half the plan
    must get through the half that setting asked for. The first directory to appear is
    therefore transferred first.
    """
    batches: dict[str, list[RemoteFile]] = {}
    for item in files:
        batches.setdefault(item.directory or default, []).append(item)
    return list(batches.items())


def _obd_logger_owns_bluetooth(logger_status: object) -> bool:
    """Whether the dashcam logger has reserved the unit's Bluetooth radio.

    State is deliberately not part of this decision. ``parked`` still needs adapter-local
    voltage probes to notice the next engine start, and ``backoff`` needs the radio for its
    bounded reconnect. Treating a new/disabled state with ownership enabled conservatively
    only costs transfer throughput; disabling Bluetooth on an active owner loses telemetry.
    """
    return isinstance(logger_status, dict) and logger_status.get("ownership_enabled") is True


def _obd_logger_status_is_authoritative(logger_status: object) -> bool:
    """Whether radio ownership was positively observed rather than guessed.

    ``None`` is emitted only when the status file was positively absent. Installed
    logger versions publish a real boolean ownership flag. Error sentinels, malformed
    documents and future partial schemas fail closed and keep Bluetooth untouched.
    """
    return logger_status is None or (
        isinstance(logger_status, dict) and isinstance(logger_status.get("ownership_enabled"), bool)
    )


async def _await_quieting_evidence(address: str, logger_status: dict | None) -> dict | None:
    """Allow one logger heartbeat to establish a newly observed OFF edge.

    ACC can reach the server before the logger publishes its boot-bound deadline.
    Refresh unknown/stale power before deciding whether this grace applies: the parked
    gate may have read OFF while the prior runtime snapshot still says ON. Only a newly
    confirmed OFF/unknown case retries; known timers and older loggers do not wait.
    No radio or logger control is attempted.
    """
    capabilities = logger_status.get("capabilities") if isinstance(logger_status, dict) else None
    if not isinstance(capabilities, list) or QUIET_EVIDENCE_CAPABILITY not in capabilities:
        return logger_status
    ownership_observed = _obd_logger_owns_bluetooth(logger_status)
    status = get_status()

    def waiting_for_edge() -> bool:
        snapshot = status.snapshot()
        return (
            snapshot["unit_observation_fresh"] is True
            and snapshot["ignition_state"] == "off"
            and snapshot["sleep_countdown_source"] == "unknown"
            and not status.cancel_event.is_set()
        )

    if status.cancel_event.is_set() or status.snapshot()["sleep_countdown_source"] == "estimated":
        return logger_status
    loop = asyncio.get_running_loop()
    deadline = loop.time() + QUIET_EVIDENCE_WAIT_S
    path = get_config().obd_remote_status_file
    announced_wait = False
    while (
        not status.cancel_event.is_set()
        and deadline - loop.time() >= _QUIET_EVIDENCE_PROBE_BUDGET_S
    ):
        # Refresh both authorities. A newer ownership/quiesce result must accompany
        # the deadline, rather than retaining the snapshot taken before preparation.
        observation, latest_logger = await asyncio.gather(
            adb.runtime_observation(address, logger_status_path=path),
            read_logger_status(address, path),
            return_exceptions=True,
        )
        if isinstance(latest_logger, BaseException) or (
            latest_logger is None and ownership_observed
        ):
            # A transient disappearance cannot undo positively observed ownership.
            # Fail the later authority check rather than skipping logger quiescence.
            logger_status = {"state": "status_unavailable"}
        else:
            logger_status = latest_logger
        ownership_observed = ownership_observed or _obd_logger_owns_bluetooth(latest_logger)
        if status.cancel_event.is_set():
            return logger_status
        if observation is None or isinstance(observation, BaseException):
            status.unit_observation_failed()
            return logger_status
        if status.observe_unit_runtime(observation) or not waiting_for_edge():
            return logger_status
        if not announced_wait:
            log.info(
                "briefly waiting for the logger's ignition-off evidence before radio preparation"
            )
            announced_wait = True
        retry_at = min(deadline, loop.time() + QUIET_EVIDENCE_RETRY_S)
        # Stop requests are a threading.Event; short async sleeps avoid adding a
        # non-cancellable waiter thread solely for this small preparation grace.
        while waiting_for_edge() and loop.time() < retry_at:
            await asyncio.sleep(min(0.1, retry_at - loop.time()))
    return logger_status


_NATIVE_WINDOW_END_MESSAGE = (
    "The native sleep window is ending; remaining footage stays on the card."
)


def _cancel_for_radio_stop(error: object) -> None:
    expected_end = isinstance(error, radio_coordinator.RadioWindowEnding)
    get_status().cancel(
        reason="native_sleep_window_ending" if expected_end else "radio_safety_lease_lost",
        detail=_NATIVE_WINDOW_END_MESSAGE if expected_end else redact(error) if error else None,
    )


def _classify_cancellation(result: RunResult) -> RunResult:
    """Keep safety stops retryable and operator Stop terminal, retaining committed work."""
    status = get_status()
    if status.cancel_event.is_set() and status.cancel_reason is None:
        # Stop can arrive after an exception has already constructed ERROR/PARTIAL,
        # including while restoration is awaited. Its terminal intent outranks that
        # earlier classification; retain all committed counts and failure evidence.
        result.state = RunState.CANCELLED
        result.error = result.error or "the transfer was cancelled"
        return result
    if (
        status.cancel_event.is_set()
        and status.cancel_reason == "native_sleep_window_ending"
        and result.state in {RunState.CANCELLED, RunState.PARTIAL, RunState.ERROR}
    ):
        expected = result.error in {
            None,
            "backup cancelled",
            "the transfer was cancelled",
            _NATIVE_WINDOW_END_MESSAGE,
        }
        result.state = (
            RunState.PARTIAL
            if result.files or result.bytes
            else RunState.IDLE
            if expected
            else RunState.ERROR
        )
        if expected:
            result.error = _NATIVE_WINDOW_END_MESSAGE
        return result
    if (
        status.cancel_event.is_set()
        and status.cancel_reason == "radio_safety_lease_lost"
        and result.state in {RunState.CANCELLED, RunState.PARTIAL}
    ):
        result.state = RunState.PARTIAL if result.files or result.bytes else RunState.ERROR
        generic = {None, "backup cancelled", "the transfer was cancelled"}
        detail = status.cancel_detail or (
            result.error if result.error not in generic else "the radio safety lease was lost"
        )
        prior = result.error
        result.error = f"Backup stopped safely: {redact(detail)}"
        if prior not in generic and prior != detail:
            result.error += f"; {redact(prior)}"
    return result


def _radio_shutdown_hold(reason: object) -> RunResult:
    """Keep requested quieting mandatory while allowing the poller's safe retry."""
    status = get_status()
    if status.cancel_event.is_set():
        if status.cancel_reason in {"radio_safety_lease_lost", "native_sleep_window_ending"}:
            # Defer classification until cleanup is complete: an operator's Stop
            # during restoration still outranks this internal safety cancellation.
            return RunResult(state=RunState.CANCELLED, error=redact(reason))
        return RunResult(state=RunState.CANCELLED, error="the transfer was cancelled")
    text = redact(reason or "").strip() or "radio state could not be verified"
    text = text.removeprefix("Radios left unchanged: ")
    if not text.startswith("Waiting for radio shutdown:"):
        text = f"Waiting for radio shutdown: {text}"
    status.set_radio_quieting_hold(text)
    log.info("holding footage until radio shutdown is verified", reason=text)
    return RunResult(state=RunState.IDLE)


async def widen_sleep_window(address: str) -> bool:
    """Give the unit a long ignition-off window, because the app is here to use it.

    The countdown is the backup window: the radio stays up for the whole of it, so it is
    the only thing that decides how much a park is worth. It is widened on arrival rather
    than left permanently long because the value persists on the unit, and a unit parked
    somewhere the app cannot reach would otherwise hold the car's battery open for the full
    fifteen minutes for no possible benefit. Narrowed again by :func:`close_sleep_window`.
    The return value is proof, not merely command success: managed callers must not begin
    device-side recovery unless the configured value was already present or its write was
    read back successfully.
    """
    # Fixed contract shared with the Android app. Do not read the live settings cache here:
    # an upgraded process can briefly retain an old false toggle or explicit duration, and the
    # persistent vendor property must never leave the app-owned policy or oscillate.
    wanted = INGEST_SLEEP_WINDOW_ACTIVE_SECONDS
    observed = await adb.sleep_countdown(address)
    if observed >= wanted:
        get_status().set_sleep_window(observed)
        return True
    if await adb.set_sleep_countdown(address, wanted):
        log.info("widened the head unit's ignition-off window", seconds=wanted)
        # This configures the next native OFF countdown; it does not restart a timer
        # already latched by SystemUI. Only witnessed OFF evidence anchors that timer.
        get_status().set_sleep_window(wanted, restarted=True)
        return True
    return False


async def reconcile_pending_in_awake_window(address: str) -> bool:
    """Give durable radio recovery the full managed window before attempting it.

    Recovery is the most important work on arrival: until Bluetooth, hotspot and the OBD
    logger are back at their exact baseline, no new backup may start.  The resting countdown
    is deliberately short, though, so awaiting the window change first prevents recovery
    from racing the same sleep boundary it is trying to repair.
    """
    if not await widen_sleep_window(address):
        log.warning(
            "radio recovery deferred because the managed awake window could not be verified",
            address=address,
            seconds=INGEST_SLEEP_WINDOW_ACTIVE_SECONDS,
        )
        return False
    try:
        return await asyncio.wait_for(
            radio_coordinator.reconcile_pending(address=address),
            timeout=RADIO_RESTORE_TIMEOUT_S,
        )
    except TimeoutError:
        # Cancellation of an adopted transition expires its durable row and releases
        # the process fence inside RadioTransition.restore(). Keep that debt for retry.
        log.warning(
            "radio recovery exceeded its restore budget; restoration remains pending",
            timeout_s=RADIO_RESTORE_TIMEOUT_S,
        )
        return False


async def _restore_radio_transition(
    transition: radio_coordinator.RadioTransition, *, error: object | None = None
) -> bool:
    """Bound every run-exit restore while preserving durable debt on interruption."""
    try:
        return await asyncio.wait_for(
            transition.restore(error=error), timeout=RADIO_RESTORE_TIMEOUT_S
        )
    except BaseException as exc:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(transition.require_recovery(exc))
        if isinstance(exc, TimeoutError):
            log.warning(
                "radio cleanup exceeded its restore budget; restoration remains pending",
                timeout_s=RADIO_RESTORE_TIMEOUT_S,
            )
            return False
        raise


async def reconcile_startup_in_awake_window() -> bool:
    """Recover startup radio state only after protecting its recorded endpoint from sleep."""
    address = await radio_coordinator.pending_recovery_address()
    if address is None:
        # Preserve the coordinator's no-row startup housekeeping without inventing a device
        # endpoint or touching the configured unit when there is no durable recovery owner.
        return await radio_coordinator.reconcile_startup()
    return await reconcile_pending_in_awake_window(address)


async def _recover_before_run(address: str, *, first_of_visit: bool) -> bool:
    """Reconcile radio recovery before a run, widening the sleep window only when earned.

    The reconcile happens every run, top-ups included -- a failed restore can otherwise sit
    active while the unit stays online and never be adopted. What must *not* happen every
    run is the widen, and it did: each top-up rewrote the window to 900 s on the way in
    and 300 s on the way out, and every write restarts the unit's countdown. With the
    recorder closing a segment a minute while parked, that was a transfer every ninety
    seconds, each cutting both radios and both times resetting the countdown, so the unit
    sat awake at home for a day. The visit's first run is the bulk upload and earns the
    long window; a run copying the two segments that arrived since does not, and gets the
    idle countdown it found -- which the close at the end then leaves alone, because the
    property already reads idle.

    A durable recovery still gets the window whichever run it lands in. Restoring an
    exact radio baseline is the one thing that must not race the sleep boundary.
    """
    if first_of_visit or await radio_coordinator.pending_recovery_address() is not None:
        return await reconcile_pending_in_awake_window(address)
    return await radio_coordinator.reconcile_pending(address=address)


async def close_sleep_window(address: str, *, drained: bool) -> None:
    """Narrow the window once every device-side obligation is proven complete.

    Android owns the actual transition into sleep.  An earlier implementation called
    ``svc power forcesuspend`` here; the recorder woke the unit whenever it closed another
    segment, creating a wake/suspend loop that could land inside an active recording.  A
    verified five-minute countdown saves the battery without forcing that unsafe edge.
    """
    if not address:
        return
    if not drained:
        return
    # See widen_sleep_window(): this is a protocol value, not a runtime tuning value.
    idle = INGEST_SLEEP_WINDOW_IDLE_SECONDS

    # v2 moves the final transition to the component that observes Wi-Fi, ACC and the
    # ingestion-request file together. Only pay for another bounded ADB read when the
    # status captured earlier in this visit advertised that contract. Capability alone is
    # never permission to skip the server fallback: the newly read status must be current,
    # know ACC, and contain positive property readback evidence from this completion edge.
    cached_logger = get_obd_transfer_status().snapshot().get("logger")
    cached_capabilities = (
        cached_logger.get("capabilities") if isinstance(cached_logger, dict) else None
    )
    if (
        isinstance(cached_capabilities, list)
        and APP_OWNED_SLEEP_WINDOW_CAPABILITY in cached_capabilities
    ):
        try:
            logger_status = await asyncio.wait_for(
                read_logger_status(address, get_config().obd_remote_status_file),
                timeout=APP_OWNED_SLEEP_STATUS_READ_TIMEOUT_S,
            )
        except Exception:
            logger_status = None

        status_is_current = False
        if isinstance(logger_status, dict):
            updated_at = logger_status.get("updated_at_utc")
            if isinstance(updated_at, str):
                with contextlib.suppress(ValueError, OverflowError):
                    updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    if updated.tzinfo is not None:
                        age_s = (datetime.now(UTC) - updated.astimezone(UTC)).total_seconds()
                        status_is_current = -5.0 <= age_s <= APP_OWNED_SLEEP_STATUS_MAX_AGE_S

        fresh_capabilities = (
            logger_status.get("capabilities") if isinstance(logger_status, dict) else None
        )
        fresh_v2 = (
            status_is_current
            and isinstance(logger_status, dict)
            and isinstance(logger_status.get("schema_version"), int)
            and logger_status["schema_version"] >= 6
            and isinstance(fresh_capabilities, list)
            and APP_OWNED_SLEEP_WINDOW_CAPABILITY in fresh_capabilities
        )
        if fresh_v2 and logger_status.get("acc_state_known") is True:
            if logger_status.get("acc_on") is True:
                log.info("left the active sleep window to the OBD app because ACC is on")
                return
            if (
                logger_status.get("acc_on") is False
                and logger_status.get("sleep_window_target_s") == idle
                and logger_status.get("sleep_window_observed_s") == idle
                and logger_status.get("sleep_window_verified") is True
            ):
                # status.json may have been republished with an old verified evidence object.
                # Accept app ownership only when the vendor property agrees *now*; otherwise
                # continue through the parked server fallback, which writes and verifies it.
                if await adb.sleep_countdown(address) == idle:
                    log.info("accepted the OBD app's verified idle sleep window", seconds=idle)
                    get_status().set_sleep_window(idle)
                    return

    if not await adb.is_parked(address):
        # Still being driven. Narrowing the window now would strand the next ignition-off
        # with the short value before anything had a chance to widen it.
        return

    if await adb.sleep_countdown(address) != idle:
        if await adb.set_sleep_countdown(address, idle):
            log.info("restored the head unit's idle sleep window", seconds=idle)
            get_status().set_sleep_window(idle, restarted=True)
        else:
            log.warning(
                "could not verify the head unit's idle sleep window",
                address=address,
                seconds=idle,
            )


async def _sleep_window_may_close(result: RunResult) -> bool:
    """Prove that no device-side work or recovery depends on the awake window."""
    status_snapshot = get_status().snapshot()
    if (
        result.state not in (RunState.OK, RunState.IDLE)
        or not status_snapshot["backlog_known"]
        or bool(status_snapshot["backlog_files"])
        or bool(status_snapshot.get("policy_deferred_files", 0))
        or bool(get_obd_transfer_status().snapshot()["waiting_on_unit"])
    ):
        return False
    try:
        return await radio_coordinator.pending_recovery_address() is None
    except Exception as exc:
        log.warning(
            "could not prove radio recovery is clear; leaving the sleep window open",
            error=redact(f"{type(exc).__name__}: {exc}"),
        )
        return False


async def _reclaim(
    info: UnitInfo,
    items: list[RemoteFile],
    *,
    lease: radio_coordinator.RadioTransition | None = None,
) -> int:
    """Remove recordings the library already holds from the card. Returns how many went.

    Logged rather than silent, at info level, which is the other half of this fix. The
    delete was wrapped in a bare ``suppress(AdbError)`` and said nothing on the way past,
    so "is it deleting?" could not be answered from the Logs page at all -- the same shape
    of mistake as the backup-page warning that sat below the default log level.
    """
    grouped: dict[str, list[RemoteFile]] = {}
    for item in items:
        directory = item.directory or (info.source or "")
        if directory:
            grouped.setdefault(directory, []).append(item)
    removed = 0
    for directory, directory_items in grouped.items():
        if lease is not None:
            lease.raise_if_lease_lost()
        try:
            removed += await adb.delete_if_unchanged(info.address, directory, directory_items)
        except adb.AdbError as exc:
            # Still not fatal -- the copies are safe in the library and the card can be
            # reclaimed next window -- but no longer invisible.
            log.warning(
                "could not reclaim space on the card",
                directory=directory,
                files=len(directory_items),
                error=redact(exc),
            )
        if lease is not None:
            lease.raise_if_lease_lost()
    if removed:
        log.info(
            "reclaimed card space for recordings already in the library",
            files=removed,
            megabytes=round(sum(i.size for i in items) / 1e6),
        )
    return removed


async def _cleanup_existing_duplicates(
    info: UnitInfo,
    footage: Path,
    candidates: list[RemoteFile],
    *,
    clock_known: bool,
    storage_safe: bool,
    admitted: bool,
    result: RunResult,
    status: IngestStatus,
) -> None:
    """Optional cleanup after transfer and radio restoration, under this run's ownership."""
    if (
        not candidates
        or not admitted
        or not bool(_get("delete_after_verify", False))
        or not clock_known
        or not storage_safe
        or result.state not in (RunState.OK, RunState.IDLE)
        or status.cancel_event.is_set()
    ):
        return
    remaining = status.sleep_countdown_remaining_s()
    if remaining is None or remaining <= 15:
        return
    try:
        # No hashing/deletion may compete with restoration or a different radio owner.
        if await asyncio.wait_for(radio_coordinator.pending_recovery_address(), timeout=3):
            return
        if status.cancel_event.is_set():
            return
        status.set_phase(Phase.VERIFYING)
        cleaned = await duplicate_cleanup.cleanup_verified_duplicates(
            info,
            footage,
            candidates,
            native_remaining=status.sleep_countdown_remaining_s,
            cancelled=status.cancel_event.is_set,
        )
        log.info(
            "checked existing server copies before camera duplicate cleanup",
            attempted_files=cleaned.attempted_files,
            verified_files=cleaned.verified_files,
            deleted_files=cleaned.deleted_files,
            deleted_bytes=cleaned.deleted_bytes,
            retained_preservations=cleaned.retained_preservations,
            skipped_files=cleaned.skipped_files,
            stop_reason=cleaned.stop_reason,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # A cleanup fault retains camera files; it cannot fail an already durable backup.
        log.warning("camera duplicate cleanup deferred", error_category=type(exc).__name__)


def _group_names(names: list[str], where: dict[str, str]) -> dict[str, list[str]]:
    """Bucket committed filenames by the directory they came from.

    A name with no known directory is dropped rather than guessed at. This feeds ``rm`` on
    the unit, and the one thing that must never happen there is deleting a file the run
    cannot account for.
    """
    grouped: dict[str, list[str]] = {}
    for name in names:
        directory = where.get(name)
        if directory:
            grouped.setdefault(directory, []).append(name)
    return grouped


def _absorb(total: transport.TransferResult, part: transport.TransferResult) -> None:
    """Fold one batch's outcome into the run's.

    ``complete`` is an AND, so the caller seeds it ``True`` -- a fresh ``TransferResult``
    is ``False`` and ANDing onto that would report every run as incomplete. The first error
    is kept rather than the last, because it is the one that explains why the batches after
    it never happened.
    """
    total.files.extend(part.files)
    total.bytes_received += part.bytes_received
    total.seconds += part.seconds
    total.complete = total.complete and part.complete
    if total.error is None:
        total.error = part.error


#: How many finished chunks may be waiting to be committed before the transfer waits.
#:
#: Backpressure, not a buffer to be filled. Each queued chunk is `chunk_size` recordings
#: sitting in staging on the footage filesystem, so an unbounded queue would trade card
#: space for local disk on a link fast enough to outrun the commit -- and the whole point of
#: committing progressively is that an interrupted window leaves finished files where the
#: scanner can see them, not stranded in staging.
COMMIT_QUEUE_DEPTH = 2


class _CommitPipeline:
    """Commit-and-reclaim running *behind* the transfer instead of inside it.

    The bulk socket and the ADB control channel are independent -- :mod:`app.ingest.radios`
    relies on exactly that, since the control channel is idle while the tar stream moves
    bytes. Yet the commit and the card delete used to run between chunks with the stream
    stopped, so the one thing guaranteed not to be doing work during them was the link.

    The cost of that is small on average and occasionally large. A commit is a ``stat`` and
    a rename; a reclaim is one ADB round trip, measured at ~185 ms against the live unit, on
    a chunk that takes some twelve seconds to move. But an ADB call that wedges takes its
    full timeout, and this application has recorded twenty-second ones -- twenty seconds of
    a twenty-minute window spent holding a healthy link idle while a delete decides whether
    it is going to answer.

    So chunks are handed to a worker and the stream carries straight on. Nothing about the
    safety ordering changes: a card delete still happens only after the commit for those
    exact files returned them, and both still run one chunk at a time in arrival order.
    What changes is who waits.

    Failures are kept, not raised into the transfer. The first one stops further work --
    nothing should still be erasing a card after a commit failed -- but the queue keeps
    draining so :meth:`close` can never deadlock, and the error surfaces there.
    """

    def __init__(
        self,
        handler: Callable[[list[RemoteFile]], Awaitable[None]],
        *,
        depth: int = COMMIT_QUEUE_DEPTH,
    ) -> None:
        self._handler = handler
        self._queue: asyncio.Queue[list[RemoteFile]] = asyncio.Queue(maxsize=max(1, depth))
        self._worker: asyncio.Task[None] | None = None
        #: Reported once, to whichever of `submit` or `close` reaches it first.
        self._error: BaseException | None = None
        #: Latched, and never cleared. Reporting the failure must not un-stop the worker:
        #: the reason it stopped is that a commit failed, and that stays true after
        #: somebody has been told about it.
        self._failed = False

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._drain(), name="ingest-commit-pipeline")

    async def submit(self, chunk: list[RemoteFile]) -> None:
        """Hand a finished chunk over, waiting only if the queue is already full."""
        if self._worker is None:
            # No pipeline: do it inline, which is the old behaviour exactly.
            await self._handler(chunk)
            return
        self._raise_if_failed()
        await self._queue.put(chunk)

    async def _drain(self) -> None:
        while True:
            chunk = await self._queue.get()
            try:
                if not self._failed:
                    await self._handler(chunk)
            except asyncio.CancelledError:
                self._queue.task_done()
                raise
            except BaseException as exc:
                # Kept rather than raised here: a worker that dies takes the queue's
                # drain with it, and `close` is where the run can act on this.
                self._failed = True
                if self._error is None:
                    self._error = exc
                self._queue.task_done()
            else:
                self._queue.task_done()

    def _raise_if_failed(self) -> None:
        error = self._error
        if error is not None:
            self._error = None
            raise error

    async def close(self, *, discard: bool = False) -> None:
        """Finish what is queued, then stop. ``discard`` abandons it instead.

        The transfer's own bookkeeping reads the committed list immediately after the last
        chunk, so this has to be awaited before then -- a run may not decide what is still
        outstanding while a worker is halfway through telling it.
        """
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        if discard:
            # Drop only work that has not started. The current handler may be
            # inside an uninterruptible commit thread; let it settle before
            # salvage or restoration releases this run's ownership.
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._queue.task_done()

        async def settle() -> None:
            await self._queue.join()
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker

        settling = asyncio.create_task(settle(), name="ingest-commit-settle")
        cancelled = False
        while not settling.done():
            try:
                await asyncio.shield(settling)
            except asyncio.CancelledError:
                cancelled = True
        settling.result()
        if cancelled:
            raise asyncio.CancelledError
        if not discard:
            self._raise_if_failed()


async def _can_resume_stream(address: str) -> bool:
    """Distinguish an absent unit from a stalled shell without resetting either radio."""
    status = get_status()
    remaining = status.sleep_countdown_remaining_s()
    if status.cancel_event.is_set() or (remaining is not None and remaining < 60):
        return False
    reachable = await adb.is_listening(address)
    responsive = await adb.probe_shell(address) if reachable else False
    ignition = await adb.ignition_state(address, timeout=3.0) if responsive else "unknown"
    remaining = status.sleep_countdown_remaining_s()
    allowed = (
        responsive
        and ignition == "off"
        and not status.cancel_event.is_set()
        and (remaining is None or remaining >= 60)
    )
    log.info(
        "checked the head unit after an interrupted footage stream",
        adb_port_reachable=reachable,
        basic_shell_responsive=responsive,
        ignition=ignition,
        retry_allowed=allowed,
    )
    return allowed


async def _move(
    info: UnitInfo,
    files: list[RemoteFile],
    *,
    staging: Path,
    host: str,
    port: int,
    timeout_s: int,
    lease: radio_coordinator.RadioTransition | None = None,
    chunk_size: int = 5,
    on_chunk_completed: Callable[[list[RemoteFile]], Awaitable[None]] | None = None,
    yield_after_chunk: Callable[[], Awaitable[bool]] | None = None,
) -> transport.TransferResult:
    """Stream *files* off the unit into *staging*, in bounded chunks per directory.

    One listener per directory chunk rather than one for the entire run. The card keeps
    ordinary segments and incident-locked ones in separate directories, and ``tar`` is
    rooted at the directory it is run from. Chunking streams files in small batches (e.g. 5
    at a time) so completed recordings are committed and reclaimed from the card
    progressively. If a transfer is interrupted, all recordings completed before the drop
    are preserved on disk and freed on the card rather than lost in staging and redone.

    Its own function because the run calls it more than once: for the plan drawn on
    arrival, and again for each sweep that finds recordings the camera closed while the
    first lot was moving.
    """
    status = get_status()
    if files and bool(_get("quiet_radios", False)) and lease is None:
        raise radio_coordinator.RadioTransitionError(
            "requested radio shutdown has not been verified for footage transfer"
        )
    # Seeded complete; `_absorb` ANDs each batch onto it.
    transferred = transport.TransferResult(complete=True)
    # One recovery per pass, never one per chunk. A bad link must not consume the
    # whole sleep window retrying every recording. Later runs still use the normal delta.
    recovery_used = False
    for directory, batch in _by_directory(files, info.source):
        chunks = (
            [batch[i : i + chunk_size] for i in range(0, len(batch), chunk_size)]
            if chunk_size > 0
            else [batch]
        )
        for chunk in chunks:
            pending = chunk
            batch_result = transport.TransferResult()
            chunk_retried = False
            while pending:
                if lease is not None:
                    lease.raise_if_lease_lost()
                if status.cancel_event.is_set():
                    batch_result.complete = False
                    batch_result.error = "the transfer was cancelled"
                    break
                # Only restart this tar listener. Disconnect/root would also tear down
                # other ADB work and must never be part of in-flight recovery.
                await adb.clear_listener(info.address)
                if lease is not None:
                    await lease.verify_footage_quiet()
                if status.cancel_event.is_set():
                    batch_result.complete = False
                    batch_result.error = "the transfer was cancelled"
                    break
                listener = await adb.launch_listener(
                    info.address,
                    directory,
                    [item.name for item in pending],
                    port=port,
                    timeout_s=timeout_s,
                )
                status.set_phase(Phase.TRANSFERRING)
                try:
                    part = await asyncio.to_thread(
                        transport.receive,
                        host,
                        port,
                        staging,
                        expected={item.name: item.size for item in pending},
                        on_file_started=status.file_started,
                        on_file_done=status.file_done,
                        on_bytes=status.add_bytes,
                        cancel=status.cancel_event,
                    )
                finally:
                    was_serving = await adb.stop_listener(listener)
                batch_result.files.extend(part.files)
                batch_result.bytes_received += part.bytes_received
                batch_result.seconds += part.seconds
                batch_result.complete = part.complete
                batch_result.error = part.error
                if part.error and not part.complete and not was_serving:
                    batch_result.error = f"{part.error} (the head unit stopped serving first)"
                pending = [item for item in pending if item.name not in set(part.files)]
                if lease is not None:
                    lease.raise_if_lease_lost()
                if part.complete or not pending or not part.retryable or recovery_used:
                    break
                if not await _can_resume_stream(info.address):
                    break
                recovery_used = True
                chunk_retried = True
                completed_bytes = sum(item.size for item in chunk if item.name in part.files)
                status.account_for_retry(part.bytes_received - completed_bytes)
                log.warning(
                    "retrying unfinished footage once after a connection interruption",
                    files_remaining=len(pending),
                    files_preserved=len(batch_result.files),
                    error=part.error,
                    listener_exited=not was_serving,
                )
            if chunk_retried and batch_result.complete:
                log.info("resumed interrupted footage successfully", files=len(batch_result.files))
            _absorb(transferred, batch_result)

            if on_chunk_completed is not None:
                await on_chunk_completed(chunk)

            if lease is not None:
                lease.raise_if_lease_lost()
            if not batch_result.complete or status.cancel_event.is_set():
                # The window shut, or the operator cancelled. Standing up another listener into
                # a link that has already gone would spend what is left of the window on a
                # connection that cannot be answered.
                break
            if yield_after_chunk is not None:
                yield_batch = await yield_after_chunk()
                if lease is not None:
                    lease.raise_if_lease_lost()
                if yield_batch:
                    # An intentional batch boundary, not cancellation or a failed copy.
                    # The caller retains the unrequested remainder as ordinary backlog.
                    return transferred
        if not transferred.complete or status.cancel_event.is_set():
            break
    return transferred


# NOTE: `already_seen` is the same set `delta` takes, and for the same reason -- see the
# note on its parameter there.
async def _rescue_partials(
    info: UnitInfo,
    *,
    staging: Path,
    footage: Path,
    host: str,
    port: int,
    timeout_s: int,
    unit_now: int,
    already_seen: set[str] | None = None,
    lease: radio_coordinator.RadioTransition | None = None,
) -> tuple[list[RemoteFile], list[str], dict[str, int]]:
    """Recover cut-short recordings the camera never finished. Best-effort by contract.

    When the engine stops mid-segment, the camera's finalise races the unit's three-second
    shutdown countdown; the segment that loses stays stranded in the DCIM root as
    ``pre_<start>_camera_N.ts`` — never moved into ``Video``, so the ordinary listing never
    sees it, and it holds the most valuable footage there is: the last minute before the
    car shut off. Verified against the live card, the stranded files are plain playable
    MPEG-TS up to the cut.

    Each orphan is pulled over the same transport as everything else, renamed **in
    staging** to the name the camera would have given it — the scanner must never see a
    ``pre_`` name in the footage directory — and committed like any other recording. A
    target that already exists in the library means this orphan was rescued on an earlier
    window and is skipped, which is what makes the rescue idempotent when
    delete-after-verify is off.

    Returns ``(rescued, committed_names, expected_sizes)`` containing only what actually
    landed, so the caller's OK/PARTIAL arithmetic never counts a rescue that did not
    happen — a failed rescue costs nothing and is simply tried again next window.
    """
    if not info.source:
        return [], [], {}
    orphans = await adb.list_orphan_partials(info.address, info.source, unit_now=unit_now)
    # Local absence is not the only reason to skip one.
    #
    # `already_seen` is the set of recordings the library has *deliberately* removed, and
    # `delta` consults it for exactly the reason its own docstring gives: without it the two
    # subsystems fight -- the file is absent from disk, so it is asked for again, retention
    # deletes it again, and every driveway window is spent re-fetching the oldest footage
    # while today's never gets a turn. A rescued partial is the same class of file and was
    # not being given the same guard.
    seen = already_seen or set()
    candidates = [
        item
        for item in orphans
        if not (footage / item.name[len(adb.PARTIAL_PREFIX) :]).exists()
        and not (footage / item.name).exists()
        and item.name[len(adb.PARTIAL_PREFIX) :] not in seen
        and item.name not in seen
    ]
    if not candidates:
        return [], [], {}
    log.info(
        "the card holds cut-short recordings the camera never finished; rescuing them",
        files=len(candidates),
        megabytes=round(sum(item.size for item in candidates) / 1e6),
    )
    get_status().extend_plan(DeltaPlan(files=candidates))
    await _move(
        info, candidates, staging=staging, host=host, port=port, timeout_s=timeout_s, lease=lease
    )

    expected: dict[str, int] = {}
    by_target: dict[str, RemoteFile] = {}
    for item in candidates:
        staged = staging / item.name
        if not staged.is_file():
            continue
        target = item.name[len(adb.PARTIAL_PREFIX) :]
        try:
            staged.rename(staging / target)
        except OSError as exc:
            log.debug("could not stage a rescued recording", name=item.name, error=redact(exc))
            continue
        expected[target] = item.size
        by_target[target] = item

    committed = await asyncio.to_thread(commit, staging, footage, expected)
    if committed:
        log.info(
            "rescued cut-short recordings into the library",
            files=len(committed),
            names=committed[:5],
        )
        if bool(_get("delete_after_verify", False)):
            await _reclaim(
                info, [by_target[name] for name in committed if name in by_target], lease=lease
            )
    rescued = [by_target[name] for name in committed if name in by_target]
    return rescued, committed, {name: expected[name] for name in committed if name in expected}


def display_url() -> str:
    """Where the head unit's browser gets sent, or "" if there is nowhere to send it.

    Shared with the Backup page's Test button rather than duplicated there, so that what
    the test proves is what a real transfer would actually do -- a test that resolves its
    own address separately is a test of the test.
    """
    override = str(_get("unit_display_url", "") or "").strip()
    # Both paths reach the same in-dash browser, so both are tagged as the kiosk view.
    return origin.with_api_key(origin.as_kiosk(override)) if override else origin.backup_url()


def delta(
    remote: list[RemoteFile],
    footage: Path,
    *,
    skip_active_s: int,
    camera: str,
    already_seen: set[str] | None = None,
    newest_first: bool = False,
    now: float | None = None,
) -> DeltaPlan:
    """Decide what to fetch.

    Size-based, never checksummed. Comparing sizes costs one ``stat`` per local file and
    makes a truncated arrival self-healing: a file cut short when the car pulled away has
    the wrong size, so the next window fetches it again without anyone tracking that it was
    partial. Checksumming would read both copies in full to learn the same thing.

    ``already_seen`` names recordings the library has processed and since removed -- by
    retention, or by the damaged-footage policy. Without it the two subsystems fight: the
    file is absent from disk, so the delta asks for it again, retention deletes it again,
    and every driveway window is spent re-fetching the oldest footage while today's never
    gets a turn.

    ``newest_first`` exists because the ordering is a policy question, not an implementation
    detail, and the default answer is only right while the backlog fits in a window. Oldest
    first keeps the library contiguous and is what you want when the card is nearly caught
    up. Once the backlog is permanently larger than one window it starves: every window is
    spent on the oldest files and today's drive is never reached at all. The camera's names
    sort chronologically, so the choice is one sort key.

    ``now`` is **the unit's clock**, not this machine's, and passing the wrong one is not a
    rounding error. Every ``mtime`` here was stamped by the head unit, which has no battery
    and a hand-set clock; comparing those timestamps against the container's NTP-synced
    time measures the drift between two machines rather than the age of a file. Fifteen
    seconds of drift -- ordinary on a device like this -- makes the active-segment guard
    below never fire at all, and the segment the camera is writing right now is exactly the
    file that must not be copied. Defaults to this machine's clock only so the function
    stays callable without a unit.
    """
    now = time.time() if now is None else now
    plan = DeltaPlan()
    seen = already_seen or set()

    for item in remote:
        if item.name in seen:
            continue
        # Both lenses write continuously while the car runs. The newest segment of each is
        # open in the recorder right now, and copying it would produce a truncated file
        # that looks complete.
        if now - item.mtime < skip_active_s:
            plan.active_skipped += 1
            continue

        local = footage / item.name
        try:
            same = local.is_file() and local.stat().st_size == item.size
        except OSError:
            same = False
        if same:
            # This is an unverified candidate, never permission to unlink. The optional
            # cleanup pass compares full hashes and preserves a durable independent copy.
            if camera == "both" or item.name.endswith(f"_{camera}.ts"):
                plan.duplicate_candidates.append(item)
            continue

        plan.backlog_files += 1
        plan.backlog_bytes += item.size
        if camera != "both" and not item.name.endswith(f"_{camera}.ts"):
            continue
        plan.files.append(item)

    plan.files.sort(key=lambda item: item.name, reverse=newest_first)
    return plan


async def _filter_parked_inventory(
    info: UnitInfo, remote: list[RemoteFile], logger: dict | None, *, refresh_logger: bool = False
) -> parked_filter.FilterResult:
    enabled = bool(_get("skip_parked_footage", False))
    if not enabled and not (
        isinstance(logger, dict) and parked_filter.CAPABILITY in (logger.get("capabilities") or [])
    ):
        return parked_filter.FilterResult(remote)
    if refresh_logger:
        try:
            logger = await read_logger_status(info.address, get_config().obd_remote_status_file)
        except Exception:
            logger = None
    native = get_status().snapshot()
    lifecycle = _run_lifecycle.get()
    used = lifecycle.policy_fingerprint_checks if lifecycle is not None else 0
    filtered = await parked_filter.filter_inventory(
        info.address,
        get_config().obd_remote_status_file,
        remote,
        info.source or "",
        logger,
        enabled=enabled,
        native_remaining_s=native["sleep_countdown_remaining_s"],
        fingerprint_budget=max(0, parked_filter.MAX_FINGERPRINT_CHECKS - used),
    )
    if lifecycle is not None:
        lifecycle.policy_fingerprint_checks += filtered.fingerprint_checks
        released = [
            item for item, _ in filtered.deferred if item.name in lifecycle.analysis_released
        ]
        if released:
            # A completed/bounded opportunity is never renewed inside a bulk batch.
            # This only restores ordinary upload; it grants no exclusion authority.
            filtered.files.extend(released)
            filtered.deferred = [
                (item, until)
                for item, until in filtered.deferred
                if item.name not in lifecycle.analysis_released
            ]
    return filtered


def _logger_can_analyze(logger: dict | None) -> bool:
    """The published counterpart of the companion's parked/unquieted decoder gate."""
    if not isinstance(logger, dict):
        return False
    try:
        age = (
            datetime.now(UTC) - movement._timestamp(logger.get("updated_at_utc"))
        ).total_seconds()
    except (movement.ManifestError, TypeError, ValueError):
        return False
    return bool(
        -30 <= age <= 30
        and parked_filter.CAPABILITY in (logger.get("capabilities") or [])
        and logger.get("movement_state") == "ready"
        and logger.get("transfer_policy_enabled") is True
        and logger.get("transfer_policy_state") in {"ready", "scanning", "waiting"}
        and logger.get("acc_state_known") is True
        and logger.get("acc_on") is False
        and logger.get("engine_running") is False
        and logger.get("head_unit_state") == "awake"
        and logger.get("current_drive_id") is None
        and logger.get("ingestion_sleep_hold_known") is True
        and logger.get("ingestion_sleep_hold") is False
    )


def _analysis_native_budget(snapshot: dict) -> tuple[datetime, float] | None:
    """Only a fresh, real native OFF timer can fund an optional analysis handoff."""
    remaining = snapshot.get("sleep_countdown_remaining_s")
    if (
        snapshot.get("unit_observation_fresh") is not True
        or snapshot.get("ignition_state") != "off"
        or snapshot.get("sleep_countdown_source") != "estimated"
        or type(remaining) not in {int, float}
        or not QUIET_ANALYSIS_NATIVE_RESERVE_S < remaining <= 3600
    ):
        return None
    try:
        off_at = movement._timestamp(snapshot.get("ignition_off_at"))
    except (movement.ManifestError, TypeError, ValueError):
        return None
    if off_at > datetime.now(UTC):
        return None
    return off_at, float(remaining)


async def _quiet_analysis_handoff(
    info: UnitInfo,
    sources: list[str],
    remote: list[RemoteFile],
    logger: dict | None,
    filtered: parked_filter.FilterResult,
    *,
    footage: Path,
    seen: set[str],
) -> tuple[list[RemoteFile], dict | None, parked_filter.FilterResult, bool]:
    """Yield BEFORE any ingestion lease so the parked camera can finish full proofs.

    The first opportunity is fixed to the real OFF edge, including across triggers
    and server restarts. Later paired pending clips use their unchanged end+6min
    deadline and a shorter monotonic cap. No call here quiesces, extends sleep or
    treats a pending/failed analysis as permission to omit footage.
    """
    status = get_status()
    if (
        not bool(_get("skip_parked_footage", False))
        or not bool(_get("quiet_radios", False))
        or not _logger_can_analyze(logger)
        or not filtered.analysis_ready
    ):
        return remote, logger, filtered, False
    budget = _analysis_native_budget(status.snapshot())
    if budget is None:
        return remote, logger, filtered, False
    off_at, native_remaining = budget
    now = datetime.now(UTC)
    first_until = off_at + timedelta(seconds=QUIET_ANALYSIS_FIRST_WINDOW_S)
    # A staggered pair may appear after four minutes; keep the whole first
    # opportunity available, always ending at the same original OFF+480 ceiling.
    anticipate = (now - off_at).total_seconds() < QUIET_ANALYSIS_FIRST_WINDOW_S
    pending_until = min((until for _, until in filtered.deferred), default=None)
    if pending_until is None and not anticipate:
        return remote, logger, filtered, False
    until = (
        first_until
        if anticipate
        else min(pending_until, now + timedelta(seconds=QUIET_ANALYSIS_MAX_WAIT_S))
    )
    if pending_until is not None:
        until = min(until, pending_until)
    until = min(until, now + timedelta(seconds=native_remaining - QUIET_ANALYSIS_NATIVE_RESERVE_S))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (until - now).total_seconds()
    waited = False
    saw_pending = bool(filtered.deferred)
    try:
        while deadline - loop.time() > 0 and not status.cancel_event.is_set():
            pending = await asyncio.to_thread(
                _with_deferred_backlog, DeltaPlan(), filtered, footage, seen, time.time()
            )
            status.set_policy_deferred(pending)
            terminal = await asyncio.to_thread(
                delta,
                [
                    item
                    for item in filtered.analysis_terminal_files
                    if (start := filtered.analysis_terminal_starts.get(item.name)) is not None
                    and start >= off_at + timedelta(seconds=120)
                ],
                footage,
                skip_active_s=0,
                camera="both",
                already_seen=seen,
            )
            if not filtered.deferred and terminal.files:
                break  # A local/unrelated terminal clip cannot starve a valid pending pair.
            # If everything is already local there is no bulk phase to starve the
            # decoder. Let the ordinary idle path release the run immediately.
            ordinary = await asyncio.to_thread(
                delta, filtered.files, footage, skip_active_s=0, camera="both", already_seen=seen
            )
            if not ordinary.files and not filtered.deferred:
                break
            status.set_phase(Phase.ANALYSING)
            status.set_analysis_wait(
                until.isoformat(), "pending_analysis" if saw_pending else "awaiting_parked_clip"
            )
            waited = True
            wake = min(deadline, loop.time() + QUIET_ANALYSIS_RECHECK_S)
            while loop.time() < wake and not status.cancel_event.is_set():
                await asyncio.sleep(min(0.25, wake - loop.time()))
            if status.cancel_event.is_set() or deadline - loop.time() <= 0:
                break
            try:
                async with asyncio.timeout(
                    min(QUIET_ANALYSIS_PROBE_TIMEOUT_S, deadline - loop.time())
                ):
                    observation, latest_logger, latest_remote = await asyncio.gather(
                        adb.runtime_observation(
                            info.address, logger_status_path=get_config().obd_remote_status_file
                        ),
                        read_logger_status(info.address, get_config().obd_remote_status_file),
                        adb.inventory_all(info.address, sources),
                    )
                    if observation is None:
                        status.unit_observation_failed()
                        break
                    if status.observe_unit_runtime(observation):
                        break
                    remote, logger = latest_remote, latest_logger
                    budget = _analysis_native_budget(status.snapshot())
                    if budget is None or not _logger_can_analyze(logger):
                        break
                    latest_off, native_remaining = budget
                    if abs((latest_off - off_at).total_seconds()) > 2:
                        break  # A new/revised OFF edge is not a renewed wait budget.
                    deadline = min(
                        deadline, loop.time() + native_remaining - QUIET_ANALYSIS_NATIVE_RESERVE_S
                    )
                    until = min(
                        until, datetime.now(UTC) + timedelta(seconds=max(0, deadline - loop.time()))
                    )
                    filtered = await _filter_parked_inventory(info, remote, logger)
                    if not filtered.analysis_ready:
                        break
                    if filtered.deferred:
                        saw_pending = True
                        next_until = min(until for _, until in filtered.deferred)
                        until = min(until, next_until)
                        deadline = min(
                            deadline, loop.time() + (until - datetime.now(UTC)).total_seconds()
                        )
                    elif saw_pending:
                        break  # Quiet proofs or ordinary upload resolved this opportunity.
            except asyncio.CancelledError:
                raise
            except Exception:
                # Optional analysis cannot veto normal footage backup.
                break
    finally:
        lifecycle = _run_lifecycle.get()
        if waited and lifecycle is not None:
            lifecycle.analysis_released.update(item.name for item, _ in filtered.deferred)
        status.set_analysis_wait(None, None)
        if waited:
            status.set_phase(Phase.SCANNING)
    return remote, logger, filtered, waited


def _with_deferred_backlog(
    plan: DeltaPlan,
    filtered: parked_filter.FilterResult,
    footage: Path,
    seen: set[str],
    now: float,
    camera: str = "both",
) -> DeltaPlan:
    pending = delta(
        [item for item, _ in filtered.deferred],
        footage,
        skip_active_s=0,
        camera="both",
        already_seen=seen,
        now=now,
    )
    names = {item.name for item in pending.files}
    filtered.deferred = [(item, until) for item, until in filtered.deferred if item.name in names]
    selected = [
        (item, until)
        for item, until in filtered.deferred
        if camera == "both" or item.name.endswith(f"_{camera}.ts")
    ]
    deadlines = [until for _, until in selected]
    plan.policy_deferred_files = len(selected)
    plan.policy_deferred_bytes = sum(item.size for item, _ in selected)
    plan.policy_deferred_until_utc = min(deadlines).isoformat() if deadlines else None
    plan.backlog_files += pending.backlog_files
    plan.backlog_bytes += pending.backlog_bytes
    return plan


@dataclass(slots=True)
class SpaceFit:
    """How much of a plan the footage volume can actually take right now."""

    #: Files that fit, in the plan's own order.
    fits: list[RemoteFile]
    #: Files pushed back to the backlog for lack of room, in the plan's own order.
    deferred: list[RemoteFile]
    free_bytes: int
    reserve_bytes: int

    @property
    def deferred_bytes(self) -> int:
        return sum(item.size for item in self.deferred)

    @property
    def nothing_fits(self) -> bool:
        return bool(self.deferred) and not self.fits


def fit_to_free_space(files: list[RemoteFile], free_bytes: int, reserve_bytes: int) -> SpaceFit:
    """Cut a plan down to what fits above the configured reserve.

    Walks the plan in order and stops at the first file that would push the volume below
    the reserve, instead of skipping it to try a smaller one later: the order *is* the
    policy (oldest first keeps the library contiguous; newest first gets today's drive),
    and cherry-picking by size would quietly undo it. A reserve of zero means the check
    is off, and a plan that fits is returned untouched.
    """
    budget = free_bytes - reserve_bytes
    fits: list[RemoteFile] = []
    deferred: list[RemoteFile] = []
    if reserve_bytes <= 0:
        return SpaceFit(list(files), [], free_bytes, reserve_bytes)
    for index, item in enumerate(files):
        if item.size > budget:
            deferred = list(files[index:])
            break
        budget -= item.size
        fits.append(item)
    return SpaceFit(fits, deferred, free_bytes, reserve_bytes)


def _free_bytes(path: Path) -> int | None:
    """Free space on the volume holding ``path``; None if it cannot be measured."""
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return None


def _space_hold_reason(fit: SpaceFit, *, footage: Path) -> str:
    free_gb = fit.free_bytes / 1e9
    reserve_gb = fit.reserve_bytes / 1e9
    held = len(fit.deferred)
    if fit.nothing_fits:
        return (
            f"{footage} has {free_gb:.1f} GB free and the backup keeps {reserve_gb:.1f} GB "
            f"in reserve, so none of the {held} waiting recording{'s' if held != 1 else ''} "
            f"fit. Free some space, lower the reserve in Settings > Backup / Ingest, or "
            f"let retention catch up; the footage stays on the card until then."
        )
    return (
        f"{footage} has {free_gb:.1f} GB free with {reserve_gb:.1f} GB kept in reserve; "
        f"{held} recording{'s' if held != 1 else ''} ({fit.deferred_bytes / 1e9:.1f} GB) "
        f"stay on the card until space is freed."
    )


async def _fit_plan_to_space(
    files: list[RemoteFile], footage: Path, status: IngestStatus
) -> SpaceFit | None:
    """Measure the footage volume and trim ``files`` in place to what fits.

    Returns None when the check is off or the volume cannot be measured -- in both cases
    the plan is left alone, and the status reflects whatever was learnt about free space.
    The measurement is taken fresh each call: a sweep partway through a multi-GB run sees
    a very different disk from the one the opening plan was drawn against.
    """
    reserve_bytes = int(max(0.0, float(_get("min_free_gb", 5.0))) * 1e9)
    free = await asyncio.to_thread(_free_bytes, footage)
    if free is None:
        log.warning("could not measure free space on the footage volume", path=str(footage))
        return None
    fit = fit_to_free_space(files, free, reserve_bytes)
    if not fit.deferred:
        status.set_destination_space(free)
        return fit
    reason = _space_hold_reason(fit, footage=footage)
    status.set_destination_space(free, held=True, reason=reason)
    log.warning(
        "footage volume is low on space; holding part of the plan on the card",
        free_gb=round(free / 1e9, 1),
        reserve_gb=round(reserve_bytes / 1e9, 1),
        deferred_files=len(fit.deferred),
        deferred_gb=round(fit.deferred_bytes / 1e9, 1),
    )
    files[:] = fit.fits
    return fit


def commit(staging: Path, footage: Path, expected: dict[str, int]) -> list[str]:
    """Move only whole files into the footage directory.

    The size check is what makes an interrupted window safe: ``tar`` was streaming into the
    last file when the socket died, so that one is short and is discarded rather than
    published. Everything before it is byte-complete and moves. Only files in ``expected``
    are touched, since a later chunk may already be arriving in the same staging directory.
    The scanner only ever sees finished files, and never sees the staging directory at all.

    Two things this deliberately does *not* do.

    It does not create the footage directory. If the share is not mounted, the mount point
    inside the container is an ordinary empty directory on the writable layer, and creating
    it would quietly write a whole card into the container filesystem, report success, and
    -- with delete-after-verify on -- then erase the originals from the card. Retention
    already treats an absent or empty footage directory as a fault rather than as
    permission to act, and this takes the same line.

    It does not overwrite a recording that is already the right size. The delta only asks
    for files that are missing or the wrong size locally, so replacing a short local copy
    with the complete one is the intent; replacing a *complete* one is somebody else's
    footage going away.
    """
    committed: list[str] = []
    if not staging.is_dir():
        return committed
    if not footage.is_dir():
        log.error(
            "refusing to commit: the footage directory is not there, so the share is "
            "probably not mounted",
            footage=str(footage),
        )
        return committed

    for path in sorted(staging.iterdir()):
        wanted = expected.get(path.name)
        if wanted is None:
            # The next chunk may already be arriving while this chunk is committed.
            # Only this inventory belongs to us: deleting another chunk's staging file
            # can discard a complete transfer or unlink a file the receiver still writes.
            # Stale/unrequested files are cleaned before a run starts, never mid-stream.
            continue
        if not path.is_file():
            continue
        try:
            staged = path.stat().st_size
            if staged != wanted:
                # Never silently. This check is what keeps a half-arrived file out of the
                # library, and it is also the exact place a recording that never lands
                # disappears without trace: the window ends, the operator sees a hole in
                # the footage, and nothing anywhere says why. For the last file of an
                # interrupted window this is the expected ending; for any other file it
                # means something upstream is wrong, and saying so is the only way to
                # tell the two apart.
                log.warning(
                    "discarding a staged recording that is not the size it was listed at",
                    file=path.name,
                    listed_bytes=wanted,
                    staged_bytes=staged,
                    reason="incomplete; it will be fetched again next window",
                )
                path.unlink()
                continue

            target = footage / path.name
            existing = target.stat().st_size if target.is_file() else None
            if existing == wanted:
                # Somebody else got there first after delta planned this transfer. Equal
                # size is not equal identity: a recorder-reused name or concurrent writer
                # may have published unrelated bytes. This is a rare collision path, so a
                # full streaming comparison is cheaper than risking the card source.
                if not _files_equal(path, target):
                    log.warning(
                        "not committing a same-size recording over different local content",
                        file=path.name,
                        bytes=wanted,
                    )
                    # Preserve it below a directory that commit() and run-start _clean()
                    # both ignore. The source is retained too because this name is absent
                    # from ``committed``.
                    conflict_dir = staging / ".conflicts"
                    conflict_dir.mkdir(exist_ok=True)
                    conflict = conflict_dir / path.name
                    if conflict.exists():
                        conflict = conflict_dir / f"{path.stem}-{time.time_ns()}{path.suffix}"
                    path.replace(conflict)
                    continue
                _sync_file_and_parent(target)
                path.unlink()
                committed.append(path.name)
                continue
            if existing is not None and existing > wanted:
                log.warning(
                    "not replacing a larger recording already in the library",
                    file=path.name,
                    existing_bytes=existing,
                    incoming_bytes=wanted,
                )
                path.unlink()
                continue

            # Flush the bytes before publication. A successful rename alone only makes the
            # name atomic; it does not make dirty pages durable, yet the caller may delete
            # the card copy as soon as this function reports the name committed.
            _sync_file(path)

            # Same filesystem by construction, so this is an atomic rename and the scanner
            # can never observe a half-written file under its own name.
            path.replace(target)
            # Persist the directory entry where the platform supports directory handles.
            # Windows cannot open a directory this way; flushed file contents plus its
            # replace semantics remain the strongest portable boundary available there.
            _sync_directory(footage)
            committed.append(path.name)
        except OSError as exc:
            log.warning("could not commit a staged recording", file=path.name, error=redact(exc))
    return committed


def _sync_file(path: Path) -> None:
    """Flush one local copy, propagating storage failures to the commit boundary."""
    with path.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _files_equal(left: Path, right: Path, *, chunk_size: int = 1 << 20) -> bool:
    """Compare a finalization collision without loading recordings into memory."""
    with left.open("rb") as left_handle, right.open("rb") as right_handle:
        while True:
            left_chunk = left_handle.read(chunk_size)
            right_chunk = right_handle.read(chunk_size)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _sync_directory(path: Path) -> None:
    """Persist a directory entry where supported; never hide real storage errors."""
    if os.name == "nt":
        # Python/Win32 cannot obtain a fsync-compatible directory descriptor. File data is
        # still flushed before ReplaceFile/MoveFileEx semantics publish the destination.
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_file_and_parent(path: Path) -> None:
    _sync_file(path)
    _sync_directory(path.parent)


def _clean(staging: Path) -> None:
    if not staging.is_dir():
        return
    for path in staging.iterdir():
        with contextlib.suppress(OSError):
            if path.is_file():
                path.unlink()


async def _drop_still_growing(info: UnitInfo, plan: DeltaPlan, sources: list[str]) -> None:
    """Take a second look at the card and drop anything whose size has moved since the first.

    The mtime guard in :func:`delta` is inference -- "this was written recently, so it is
    probably still open" -- and it rests on two things that are not guaranteed. The
    timestamp is stamped by the unit's own hand-set clock, and the card is vfat behind
    FUSE, where mtime has two-second granularity and is under no obligation to advance on
    every write. Either one failing silently defeats it.

    Two sizes taken a moment apart are evidence rather than inference. A file that grew
    between them is open in the recorder right now whatever its mtime claims, and copying
    it is the one failure this whole design is arranged around: a recording that arrives
    looking complete and is not. It costs one listing of a card that was just listed, and
    it buys back the transfer time that file would have spent -- because a copy taken
    mid-write fails its size check at commit and is thrown away regardless, so the window
    was spent either way.

    A file that has *vanished* between the two listings is dropped for a different reason
    and gets its own line. That is the recorder recycling the card out from under the run,
    which on a card as full as this one is how footage is actually, permanently lost.
    """
    try:
        fresh = {item.name: item.size for item in await adb.inventory_all(info.address, sources)}
    except adb.AdbError as exc:
        # Best effort. The commit's size check is still behind this, so the worst case of
        # not knowing is the transfer this was meant to save.
        log.debug("could not take a second look at the card", error=redact(exc))
        return

    keep: list[RemoteFile] = []
    growing: list[str] = []
    gone: list[str] = []
    for item in plan.files:
        current = fresh.get(item.name)
        if current is None:
            gone.append(item.name)
        elif current != item.size:
            growing.append(item.name)
        else:
            keep.append(item)

    if not growing and not gone:
        return

    plan.files[:] = keep
    # Counted as skipped rather than lost: they are still on the card and still in the
    # backlog, and the next window gets them once the camera has closed them.
    plan.active_skipped += len(growing)
    if growing:
        log.info(
            "leaving recordings the camera is still writing",
            files=len(growing),
            names=growing[:3],
        )
    if gone:
        log.warning(
            "recordings vanished from the card while this run was planning; the recorder "
            "is recycling the card to make room, and anything it reaches first is lost "
            "for good -- turn on 'Delete from the card after copying' so the space comes "
            "back from footage that is already safely in the library",
            files=len(gone),
            names=gone[:3],
        )


async def probe_unit() -> UnitInfo:
    """One cheap control round trip, used by the presence poll."""
    address = adb.normalised_address(str(_get("unit_adb_address", "")))
    return await adb.describe(address, str(_get("source_path_override", "") or ""))


#: The pull currently in flight, if any.
#:
#: Held rather than fired and forgotten for two reasons. A bare ``create_task`` reference
#: is only kept by the loop while the task is running, so the task can be garbage collected
#: mid-flight; and shutdown has to be able to find the transfer to stop it.
_current: asyncio.Task[RunResult] | None = None

#: Side effects that must never be able to delay or fail a transfer.
#:
#: Held for the same reason ``_current`` is: the event loop keeps only a weak reference to a
#: running task, so one that is not stored somewhere can be collected mid-flight.
_side_tasks: set[asyncio.Task] = set()

# Event mirroring is read-only on the unit and idempotent in the database. Its
# cancellation may still be draining an aiosqlite operation after its deadline.
# Keep exactly one such task alive, independently of a footage run's cleanup.
_event_mirror_task: asyncio.Task[EventSyncResult] | None = None


def _fire_and_forget(coro) -> None:
    task = asyncio.create_task(coro)
    _side_tasks.add(task)
    task.add_done_callback(_side_tasks.discard)


def _preflight(tracked: list[asyncio.Task], coro) -> asyncio.Task:
    """Start work that does not depend on the head unit, and remember it for cleanup.

    Tracked rather than fired, because unlike the reporting side effects these have results
    the run needs, and a window that ends before it needs them -- nothing new to copy, a
    share that failed its checks -- must still collect them rather than leave a task to warn
    about a result nobody retrieved.
    """
    task = asyncio.create_task(coro)
    tracked.append(task)
    return task


def _collect_event_mirror(task: asyncio.Task[EventSyncResult]) -> None:
    global _event_mirror_task
    if _event_mirror_task is task:
        _event_mirror_task = None
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


def _start_event_mirror(address: str, path: str) -> asyncio.Task[EventSyncResult] | None:
    global _event_mirror_task
    if _event_mirror_task is not None and not _event_mirror_task.done():
        get_logger_event_status().skipped()
        if _event_mirror_task.cancelling():
            get_logger_event_status().cleanup_pending()
        log.debug("skipping app event mirror while the previous mirror is still cleaning up")
        return None
    task = asyncio.create_task(sync_remote_events(address, path), name="ingest-event-mirror")
    _event_mirror_task = task
    task.add_done_callback(_collect_event_mirror)
    return task


def _cancel_event_mirror(task: asyncio.Task[EventSyncResult] | None) -> None:
    # The mirror has its own timeout. Do not inject a second CancelledError into
    # database rollback/connection termination already started by that timeout.
    if task is not None and not task.done():
        if task is _event_mirror_task:
            get_logger_event_status().cleanup_pending()
        if not task.cancelling():
            task.cancel()


async def _await_event_mirror(
    task: asyncio.Task[EventSyncResult] | None, *, deadline: float
) -> None:
    """Collect event mirroring without allowing observability to gate a backup."""
    if task is None:
        return
    try:
        # Unlike timeout()/wait_for(), wait() does not wait for the child's
        # cancellation cleanup. SQLite may still be finishing an operation there.
        done, _ = await asyncio.wait(
            (task,), timeout=max(0.0, deadline - asyncio.get_running_loop().time())
        )
    except asyncio.CancelledError:
        _cancel_event_mirror(task)
        raise
    if not done:
        _cancel_event_mirror(task)
        log.warning(
            "could not mirror OBD app events; backup will continue",
            error="event mirror deadline exceeded",
        )
        return
    try:
        task.result()
    except asyncio.CancelledError:
        log.warning(
            "could not mirror OBD app events; backup will continue", error="mirror cancelled"
        )
    except Exception:
        # The consumer handles expected ADB, validation and storage failures itself.
        # This final fence keeps a future unexpected regression fail-soft as well.
        log.warning(
            "could not mirror OBD app events; backup will continue",
            error="unexpected event mirror failure",
        )


async def _join_staging_cleanup(task: asyncio.Task[None]) -> None:
    """Keep run ownership until the uninterruptible staging deletion thread exits."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    # Retrieve any filesystem failure even when cancellation is being propagated.
    try:
        task.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


def start_run(
    *, trigger: str, info: UnitInfo | None = None, continuation: bool = False
) -> asyncio.Task[RunResult]:
    """Begin a pull in the background and keep hold of it.

    ``info`` is the caller's *own* fresh look at the unit, handed over rather than thrown
    away. The poller has just described the unit -- reconnected, asked its state, resolved
    the card -- one line before it gets here, and re-asking all of that inside the run meant
    disconnecting the transport that had only just been established and paying for the whole
    round trip twice, at the one moment in the day when it is most expensive.
    """
    global _current
    if _current is not None and not _current.done():
        return _current
    _current = asyncio.create_task(
        run_pull(trigger=trigger, info=info, continuation=continuation),
        name=f"ingest-pull-{trigger}",
    )
    return _current


async def shutdown() -> None:
    """Drain transfer and observability ownership before the database is disposed."""
    try:
        await _shutdown_pull()
    finally:
        task = _event_mirror_task
        if task is not None:
            _cancel_event_mirror(task)
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    cancelled |= current is not None and bool(current.cancelling())
                except Exception:
                    break
            _collect_event_mirror(task)
            if cancelled:
                raise asyncio.CancelledError


async def _shutdown_pull() -> None:
    """Stop an in-flight transfer before the application goes away.

    The receive loop runs on a worker thread, and ``asyncio.to_thread`` cannot be
    cancelled -- so cancelling the task alone would leave that thread writing into the
    staging directory and calling back into status while the database is being disposed.
    Setting the flag is what actually stops it; the reader checks it once per megabyte.
    """
    # Fired side effects are best-effort by definition, but they still have to be tidied
    # away. A task left pending when the loop closes prints "Task was destroyed but it is
    # pending", which reads like a bug in a shutdown path that has to look clean -- and
    # cancelling alone does not tidy anything, because the exception is only scheduled.
    # The gather is what makes this a shutdown rather than a request for one.
    sides = list(_side_tasks)
    for side in sides:
        side.cancel()
    if sides:
        await asyncio.gather(*sides, return_exceptions=True)
    await radios.cancel_pending()
    task = _current
    if task is None or task.done():
        return
    get_status().cancel()
    try:
        # Shielded, so the timeout below cancels the wait rather than the run: the run has
        # a `finally` that commits whatever arrived and records the outcome, and that is
        # worth the few seconds it takes.
        await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
    except Exception:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    log.info("stopped an in-flight transfer for shutdown")


async def stop_for_maintenance() -> bool:
    """Drain the active run and reconcile radios without stopping the arrival poller."""
    await _shutdown_pull()
    return await radio_coordinator.reconcile_pending()


async def run_pull(
    *, trigger: str = "auto", info: UnitInfo | None = None, continuation: bool = False
) -> RunResult:
    """Fetch everything the unit has that we do not. Safe to call from anywhere.

    ``continuation`` marks a re-drain of a visit that has already been announced. A
    window is drained by however many runs it takes -- one per batch of segments the
    camera closes while the car sits there -- and each of those used to fire its own
    pair of webhooks. One park produced twelve, because the recorder keeps writing a
    segment a minute after the ignition goes off and every one of them is a fresh run.
    The visit is the event worth announcing, not the runs inside it.
    """
    status = get_status()
    if not bool(_get("enabled", False)):
        status.set_state(RunState.DISABLED)
        return RunResult(state=RunState.DISABLED)

    if not status.try_begin():
        log.info("a pull is already running; ignoring the trigger", trigger=trigger)
        return RunResult(state=RunState.RUNNING, error="already running")

    lifecycle = _RunLifecycle(started_at=datetime.now(UTC))
    lifecycle_token = _run_lifecycle.set(lifecycle)
    started = time.monotonic()
    result = RunResult(state=RunState.ERROR, error="backup cleanup did not complete")
    staging_cleanup_tasks: list[asyncio.Task[None]] = []
    try:
        result = await _run_pull_started(
            trigger=trigger,
            info=info,
            continuation=continuation,
            staging_cleanup_tasks=staging_cleanup_tasks,
        )
        return result
    except asyncio.CancelledError:
        result = RunResult(state=RunState.CANCELLED, error="backup cancelled")
        raise
    except Exception as exc:
        result = RunResult(state=RunState.ERROR, error=redact(f"{type(exc).__name__}: {exc}"))
        log.error("the ingest run could not finish", error=redact(exc))
        return result
    finally:
        try:
            # A cancellation in the main cleanup must not release ownership while
            # this thread can still delete a subsequent run's staged arrivals.
            for task in staging_cleanup_tasks:
                with contextlib.suppress(Exception):
                    await _join_staging_cleanup(task)
        finally:
            try:
                # Includes settings/path initialization before the inner try, and
                # cancellation or failure during its awaited cleanup. Normal runs
                # already publish their richer result before history/sleep handling.
                if not lifecycle.released and status.running:
                    result.seconds = time.monotonic() - started
                    _classify_cancellation(result)
                    result.error = redact(result.error) if result.error else None
                    status.finish(result)
                    lifecycle.released = True
                    # Initialization and failed cleanup are material attempts too.
                    # The inner path records normal results after finishing status;
                    # only this fallback owns history when it never got that far.
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await _record_run_completion(result, trigger, continuation=continuation)
            finally:
                _run_lifecycle.reset(lifecycle_token)


async def _run_pull_started(
    *,
    trigger: str,
    info: UnitInfo | None,
    continuation: bool,
    staging_cleanup_tasks: list[asyncio.Task[None]],
) -> RunResult:
    status = get_status()
    started = time.monotonic()
    result = RunResult(state=RunState.ERROR)
    footage = Path(str(await get_settings_service().footage_dir()))
    staging = footage / STAGING_DIRNAME
    preflight: list[asyncio.Task] = []
    display_task: asyncio.Task[None] | None = None
    radio_transition: radio_coordinator.RadioTransition | None = None
    obd_result = None
    obd_ack: obd_control.LoggerAck | None = None
    obd_inventory_ok = True
    obd_transfer_error: Exception | None = None
    obd_events: asyncio.Task[EventSyncResult] | None = None
    committed: list[str] = []
    expected: dict[str, int] = {}
    initial_backlog_sizes: dict[str, int] = {}
    interrupted_error: str | None = None
    duplicate_candidates: list[RemoteFile] = []
    duplicate_clock_known = False
    duplicate_safety: asyncio.Task | None = None
    duplicate_cleanup_admitted = False

    def cancel_for_radio_lease_loss() -> None:
        detail = getattr(radio_transition, "lease_loss_error", None)
        _cancel_for_radio_stop(detail)

    try:
        # Only if nobody has already looked. The presence poll describes the unit
        # immediately before starting a run, and probing again from in here re-ran a
        # `disconnect`/`connect` against a link that had just been proven good.
        if info is None:
            info = await probe_unit()
        status.set_unit_online(info.online)
        if info.state is UnitState.UNAUTHORIZED:
            result = RunResult(
                state=RunState.UNAUTHORIZED,
                error="the head unit has not authorised this ADB key",
            )
            return result
        if not info.online:
            result = RunResult(state=RunState.OFFLINE, error="the head unit is not on the network")
            return result
        # A backup starts on one condition only: the ignition is off. A backup turns the
        # unit's Bluetooth off and drops its hotspot to keep the radio clear -- and wireless
        # CarPlay runs over exactly that Bluetooth and that hotspot. Observed in the field:
        # eight quiet/restore cycles in fifteen minutes while the operator sat in the car,
        # each one dropping CarPlay, and a run that started as they arrived tearing the link
        # down mid-handshake so the phone never reconnected on its own. So unless the unit
        # says, clearly, that the ignition is off, nothing here touches it at all -- not the
        # listing, not the band gate, not the radios. The unit stays awake for a window
        # after ignition-off, and that window is the backup's.
        #
        # Strict on purpose, at the operator's request: an unreadable ignition line holds
        # too, rather than being waved through. That is the pessimistic bias `is_parked`
        # has always had, but with the reason on the Backup page instead of silence, so a
        # unit that never backs up says why. Held as IDLE, like the band gate: the poller
        # re-checks every thirty seconds, and a hold is a postponement, not a fault.
        if bool(_get("only_when_parked", True)):
            ignition = await adb.ignition_state(info.address)
            if ignition != "off":
                if ignition == "on":
                    reason = (
                        "The ignition is on. Backing up now would disconnect CarPlay, so "
                        "the copy waits until the car is switched off."
                    )
                else:
                    reason = (
                        "Could not read whether the ignition is off, and a backup only "
                        "starts when it is. It will be re-checked shortly."
                    )
                status.set_ignition(held=True, reason=reason, state=ignition)
                result = RunResult(state=RunState.IDLE)
                return result
        status.set_ignition(held=False, reason=None, state="off")

        if not info.source:
            # Present and authorised, but its card is unmounted, reformatted or somewhere
            # unexpected. Saying "not on the network" here sent the operator looking at
            # the wiring for a problem that is inside the unit.
            result = RunResult(
                state=RunState.ERROR,
                error=info.card_error or "the head unit's memory card could not be found",
            )
            return result

        # Reconcile before every run, including a same-visit re-drain. A failed restore
        # can otherwise remain active while the unit stays online: each later run merely
        # collides with its row and no path ever adopts the now-expired lease. Whether this
        # run is the visit's first decides whether it may also widen the sleep window --
        # see _recover_before_run -- and is decided here, once, so the screen logic below
        # agrees with it.
        first_of_visit = status.since_finished() > status.online_for()
        if not await _recover_before_run(info.address, first_of_visit=first_of_visit):
            result = RunResult(
                state=RunState.IDLE,
                error="an earlier ingest radio transition still requires recovery",
            )
            return result

        # Started before the unit is asked anything, and awaited only where each result is
        # actually needed. Not one of the three depends on the card: two are database reads
        # and the third walks the staging directory on the (NFS) share. They ran in series
        # between the card listing and the first byte for no reason beyond the order they
        # happened to be written in, and on a share that is a hard mount over the network
        # that series is worth hundreds of milliseconds -- which is footage, at 34 MB/s.
        removed = _preflight(preflight, _deliberately_removed())
        safety = _preflight(preflight, _footage_is_safe_to_write(footage))
        duplicate_safety = safety
        cleaned = asyncio.create_task(
            asyncio.to_thread(_clean, staging), name="ingest-staging-clean"
        )
        staging_cleanup_tasks.append(cleaned)
        # Asked here so it overlaps the card listing rather than adding a round trip to
        # the critical path. What it answers -- how far the unit's clock is from this
        # one -- is what makes the active-segment guard mean anything at all.
        clock = _preflight(preflight, adb.unit_clock(info.address))
        # OBD exports are a second, failure-isolated inventory.  It overlaps the footage
        # listing and is awaited before the idle return so a visit with no new video can
        # still deliver a completed drive.  Any exception is fenced below; telemetry must
        # never turn a successful footage backup into a failed one.
        obd_inventory = _preflight(
            preflight,
            inventory_remote_bundles(info.address, get_config().obd_remote_ready_dir),
        )
        # Read the public logger status on every visit, even when no completed bundle is
        # waiting. Radio ownership matters while a drive is in progress, which is exactly
        # when there is normally nothing in ready/ yet. Started beside the inventories so
        # its bounded ADB read does not extend the transfer's critical path.
        obd_logger = _preflight(
            preflight,
            read_logger_status(info.address, get_config().obd_remote_status_file),
        )
        # The Android app owns a transition-only event ring. Mirror its bounded public
        # projection on every visit, including an otherwise idle one, so boot/reconnect
        # evidence does not depend on footage or a completed drive being present.
        obd_events = _start_event_mirror(info.address, get_config().obd_remote_events_file)
        obd_events_deadline = (
            asyncio.get_running_loop().time()
            + EVENT_SYNC_TIMEOUT_SECONDS
            + _EVENT_SYNC_AWAIT_GRACE_SECONDS
        )

        status.set_phase(Phase.SCANNING)
        sources = [info.source]
        if bool(_get("include_locked", True)):
            # Resolved per run rather than cached on the unit info: the directory only
            # exists once something has been locked, so a card that had none yesterday can
            # have one today. Costs one `[ -d ]` on the control channel.
            locked = await adb.resolve_locked(info.address, info.source)
            if locked:
                sources.append(locked)
        remote = await adb.inventory_all(info.address, sources)
        # New companions publish bounded advisory movement evidence. Read it before
        # drawing the footage plan, but never let it change that plan. Older units
        # make no extra ADB request. Logger-read failures are handled normally below.
        try:
            movement_logger = await obd_logger
        except Exception:
            movement_logger = None
        if isinstance(movement_logger, dict) and movement.CAPABILITY in (
            movement_logger.get("capabilities") or []
        ):
            try:
                await movement.inspect_before_plan(
                    info.address, get_config().obd_remote_status_file, remote, info.source
                )
            except Exception:
                # A diagnostic regression must still fail open for footage backup.
                log.warning("movement manifest observation failed; footage will continue")
        filtered_inventory = await _filter_parked_inventory(info, remote, movement_logger)
        (
            remote,
            movement_logger,
            filtered_inventory,
            analysis_waited,
        ) = await _quiet_analysis_handoff(
            info,
            sources,
            remote,
            movement_logger,
            filtered_inventory,
            footage=footage,
            seen=await removed,
        )
        if status.cancel_event.is_set():
            result = RunResult(state=RunState.CANCELLED)
            return result
        if analysis_waited:
            # Inventory and clock samples obtained before a bounded handoff have aged.
            # Revalidate the transfer policy and read a new clock offset before delta.
            filtered_inventory = await _filter_parked_inventory(
                info, remote, movement_logger, refresh_logger=True
            )
        remote = filtered_inventory.files
        status.set_phase(Phase.PREPARING)
        # Off the event loop: this stats every candidate against the footage share, which
        # is a hard NFS mount in the deployment, and a full card is ~140 files. Doing it
        # inline would freeze /health and the whole UI behind a slow server every time the
        # car appears -- the regression commit da2850a exists to prevent.
        # Carried as an offset rather than an absolute, so the listing's own duration
        # cannot age the reading: the guard is judged at the moment the delta runs.
        unit_now = await adb.unit_clock(info.address) if analysis_waited else await clock
        duplicate_clock_known = unit_now is not None
        skew = (unit_now - time.time()) if unit_now is not None else 0.0
        if abs(skew) >= 5.0:
            log.info(
                "the head unit's clock differs from this one; using the unit's own time "
                "to judge which recordings are still being written",
                seconds=round(skew),
            )
        plan = await asyncio.to_thread(
            delta,
            remote,
            footage,
            skip_active_s=int(_get("skip_active_seconds", 15)),
            camera="both",
            already_seen=await removed,
            newest_first=str(_get("transfer_order", "oldest_first")) == "newest_first",
            now=time.time() + skew,
        )
        plan = await asyncio.to_thread(
            _with_deferred_backlog,
            plan,
            filtered_inventory,
            footage,
            await removed,
            time.time() + skew,
            camera=str(_get("camera_filter", "both")),
        )
        # Growth filtering removes candidates but leaves their backlog intact.
        # Remember them first so a later sweep cannot count the same file twice.
        initial_backlog_sizes = {
            item.name: item.size
            for item in [*plan.files, *(item for item, _ in filtered_inventory.deferred)]
        }
        camera = str(_get("camera_filter", "both"))
        if camera != "both":
            plan.files = [item for item in plan.files if item.name.endswith(f"_{camera}.ts")]
        duplicate_candidates = [
            item
            for item in plan.duplicate_candidates
            if camera == "both" or item.name.endswith(f"_{camera}.ts")
        ]
        # A second opinion on which files are still open, from two sizes rather than one
        # timestamp. Only when there is something to fetch, so an idle window still costs
        # exactly one listing.
        if plan.files:
            await _drop_still_growing(info, plan, sources)
        status.plan(plan)
        try:
            remote_obd = (
                await inventory_remote_bundles(info.address, get_config().obd_remote_ready_dir)
                if analysis_waited
                else await obd_inventory
            )
        except Exception as exc:
            remote_obd = []
            obd_inventory_ok = False
            log.warning("could not inventory OBD exports; footage will continue", error=redact(exc))
        previous_logger = get_obd_transfer_status().snapshot()["logger"]
        try:
            observed_logger = (
                await read_logger_status(info.address, get_config().obd_remote_status_file)
                if analysis_waited
                else await obd_logger
            )
        except Exception as exc:
            observed_logger = {
                "state": "status_unavailable",
                "last_error": "logger status read failed",
            }
            log.warning(
                "could not read OBD logger status; footage will continue", error=redact(exc)
            )
        if observed_logger is not None:
            get_obd_transfer_status().set_logger(observed_logger)
        elif _obd_logger_owns_bluetooth(previous_logger):
            # A transient/malformed read must not turn a prior positive ownership signal
            # into permission to kill the logger. The public snapshot's checked-at time
            # remains unchanged, making the staleness visible while preserving safety.
            observed_logger = previous_logger
        else:
            get_obd_transfer_status().set_logger(None)
        await _await_event_mirror(obd_events, deadline=obd_events_deadline)

        if status.cancel_event.is_set():
            result = RunResult(state=RunState.CANCELLED)
            return result

        if not plan.files and not remote_obd:
            if (
                duplicate_candidates
                and duplicate_clock_known
                and bool(_get("delete_after_verify", False))
            ):
                # Cleanup-only runs honor the band policy without nudging Wi-Fi,
                # restarting adbd, quiescing the logger or creating a radio lease.
                duplicate_cleanup_admitted = await band.gate(info.address, allow_adjustments=False)
            result = RunResult(state=RunState.IDLE)
            return result

        # The band gate sits here, after the plan proves there is footage waiting and
        # before anything irreversible: no webhook has fired, no screen has been taken
        # over, no radio touched. A hold comes back as IDLE on purpose — IDLE is the one
        # state the poller re-asks every thirty seconds while the car is still on the
        # driveway, it is not recorded as a failed run, and it does not page anybody.
        # Deliberately with no `error`: a hold is a postponement, not a fault, and one
        # routed through the error field would have the Backup page reporting a problem
        # from the moment the car left until the next time somebody drives. The reason
        # for the page to show lives in the status instead.
        if not await band.gate(info.address):
            result = RunResult(state=RunState.IDLE)
            return result

        # The one safe moment to restart the unit's ADB as root, if the operator asked
        # for it and the unit allows it. `adb root` restarts adbd, and the listener that
        # serves the bulk socket lives inside an adb session -- so this has to happen
        # before any listener exists and can never move later. It is also why the
        # presence poll must not do this: that reconnects every couple of seconds.
        if not await elevate.ensure_root(info.address) and elevate.channel_lost():
            # Refusing root is the ordinary case and costs nothing -- the run carries on
            # and the hotspot stays up. Losing the *channel* is different: the daemon was
            # restarted and has not come back, so every call after this would be issued at
            # a unit that cannot hear it. Carrying on would fire a "transfer started"
            # webhook for a transfer that can never move a byte, spend ten seconds inside
            # a refused connect, and then blame the head unit for stopping first.
            #
            # IDLE, not ERROR, and the difference is the whole point: ERROR is terminal
            # for the poller -- `_should_drain_again` refuses to re-run it -- so a daemon
            # that comes back a second after the budget expired would still cost the
            # entire window. IDLE is re-drained as soon as the unit answers, and the
            # cooldown inside `elevate` stops that retry restarting the daemon again, so
            # the second run simply transfers without root.
            result = RunResult(state=RunState.IDLE)
            return result

        if status.cancel_event.is_set():
            result = RunResult(state=RunState.CANCELLED)
            return result

        quiet_requested = bool(_get("quiet_radios", False)) and bool(plan.files)
        if quiet_requested:
            observed_logger = await _await_quieting_evidence(info.address, observed_logger)
            if status.cancel_event.is_set():
                result = RunResult(state=RunState.CANCELLED)
                return result
        if quiet_requested and not radios.new_quieting_allowed():
            result = _radio_shutdown_hold(status.radio_quieting_hold_reason)
            return result
        logger_owns_bluetooth = _obd_logger_owns_bluetooth(observed_logger)
        logger_can_quiesce = obd_control.supports_quiesce(observed_logger)
        logger_status_authoritative = _obd_logger_status_is_authoritative(observed_logger)

        # A logger that exposes the v1 quiesce capability can prove that its active
        # command/sample/drive/export are durable before Bluetooth is touched.  An older
        # logger retains the established ownership-yield behaviour: no control file is
        # invented for it and both radios stay up.
        if quiet_requested and not logger_status_authoritative:
            log.warning(
                "leaving the unit's radios on because OBD logger ownership could not be read",
                logger_state=(observed_logger or {}).get("state"),
            )
            result = _radio_shutdown_hold("OBD logger ownership could not be read")
            return result
        elif quiet_requested and logger_owns_bluetooth and not logger_can_quiesce:
            log.info(
                "leaving the unit's radios on because the OBD logger owns Bluetooth "
                "and does not support ingestion quiescence",
                logger_state=(observed_logger or {}).get("state"),
            )
            result = _radio_shutdown_hold("the OBD logger cannot safely pause Bluetooth")
            return result
        elif quiet_requested:
            delay = max(0.0, radios.QUIET_AFTER_ONLINE_S - status.online_for())
            if delay:
                await asyncio.sleep(delay)
            try:
                radio_transition = await radio_coordinator.begin(
                    trigger=trigger,
                    address=info.address,
                    logger_status=observed_logger,
                    logger_status_path=get_config().obd_remote_status_file,
                    watchdog_deadline_s=int(_get("listen_timeout_s", 180)) * 2 + 120,
                    allow_zlink_rearm=bool(_get("zlink_hotspot_rearm", False)),
                    lease_loss_callback=cancel_for_radio_lease_loss,
                )
            except radio_coordinator.TransitionBusy as exc:
                # A second process must not inventory/copy/delete the same card while the
                # first owns radio state. IDLE keeps this visit retryable after recovery.
                log.warning("another ingest owns the device radios; postponing this pull")
                result = _radio_shutdown_hold(exc)
                return result
            except radio_coordinator.RadioTransitionError as exc:
                log.warning(
                    "could not establish crash-safe radio ownership; leaving radios on",
                    error=redact(exc),
                )
                result = _radio_shutdown_hold(exc)
                return result

            if radio_transition is not None and logger_owns_bluetooth:
                try:
                    obd_ack = await radio_transition.prepare_logger()
                    # Finalisation may have atomically published a new bundle after the
                    # first inventory. This authoritative second listing is the one that
                    # must be copied before Bluetooth disappears.
                    remote_obd = await inventory_remote_bundles(
                        info.address, get_config().obd_remote_ready_dir
                    )
                    obd_inventory_ok = True
                    if obd_ack.bundle_filename is not None and obd_ack.bundle_filename not in {
                        item.name for item in remote_obd
                    }:
                        raise radio_coordinator.RadioTransitionError(
                            "logger acknowledgement bundle is not visible in ready storage"
                        )
                except Exception as exc:
                    log.warning(
                        "OBD logger could not be quiesced; leaving radios on",
                        error=redact(exc),
                    )
                    restored = await _restore_radio_transition(radio_transition, error=exc)
                    radio_transition = None
                    result = _radio_shutdown_hold(
                        f"logger preparation failed: {exc}"
                        if restored
                        else "OBD logger/radio recovery could not be verified"
                    )
                    return result

        # Small immutable OBD archives go first so the drive survives even if the unit's
        # short post-ignition window closes partway through the much larger footage set.
        # The stage owns its own temp directory, hashes, validation and DB transaction;
        # all failures stay on its queue/status and are deliberately excluded from the
        # footage result below.
        if status.cancel_event.is_set():
            result = RunResult(state=RunState.CANCELLED)
            return result
        if remote_obd:
            status.set_phase(Phase.TRANSFERRING)
            try:
                if radio_transition is not None:
                    radio_transition.raise_if_lease_lost()
                obd_result = await sync_remote_bundles(
                    info, ingest_status=status, remote=remote_obd
                )
                if radio_transition is not None:
                    radio_transition.raise_if_lease_lost()
            except Exception as exc:
                obd_transfer_error = exc
                log.error("OBD backup failed without affecting footage", error=redact(exc))

        if radio_transition is not None:
            try:
                if not obd_inventory_ok:
                    raise radio_coordinator.RadioTransitionError(
                        "OBD export inventory could not be verified"
                    )
                if obd_transfer_error is not None:
                    raise radio_coordinator.RadioTransitionError(
                        "OBD export transfer did not complete"
                    ) from obd_transfer_error
                if obd_result is not None and obd_result.failed:
                    raise radio_coordinator.RadioTransitionError(
                        "one or more OBD exports did not complete durable backup"
                    )
                if obd_result is not None and not obd_result.complete:
                    raise radio_coordinator.RadioTransitionError(
                        "one or more pending OBD exports are still missing from durable backup"
                    )
                if (
                    obd_ack is not None
                    and obd_ack.bundle_filename is not None
                    and not await verified_bundle_matches(
                        obd_ack.bundle_filename,
                        obd_ack.bundle_sha256 or "",
                    )
                ):
                    raise radio_coordinator.RadioTransitionError(
                        "logger acknowledgement bundle is not durably verified on the server"
                    )
                radio_transition.raise_if_lease_lost()
                await radio_transition.mark_obd_transfer_complete()
            except Exception as exc:
                log.warning(
                    "OBD backup did not complete before radio shutdown; leaving radios on",
                    error=redact(exc),
                )
                restored = await _restore_radio_transition(radio_transition, error=exc)
                radio_transition = None
                result = _radio_shutdown_hold(
                    f"OBD backup must complete before shutdown: {exc}"
                    if restored
                    else "OBD backup recovery could not be verified"
                )
                return result

        if not plan.files:
            duplicate_cleanup_admitted = True
            if obd_result is not None and (obd_result.copied or obd_result.duplicates):
                result = RunResult(
                    state=RunState.OK,
                    files=obd_result.copied + obd_result.duplicates,
                    bytes=obd_result.bytes,
                    seconds=obd_result.seconds,
                )
            else:
                # IDLE keeps the visit eligible for another drain.  The OBD status carries
                # the validation/copy error; the footage pipeline does not inherit it.
                result = RunResult(state=RunState.IDLE)
            return result

        log.info(
            "pulling from the head unit",
            trigger=trigger,
            files=len(plan.files),
            megabytes=round(plan.bytes / 1e6),
            backlog_files=plan.backlog_files,
        )
        # Awaited here, after the delta and before a single byte moves, because this is the
        # last moment at which refusing costs nothing. Past this point the card may be
        # deleted from. Computing it earlier changes nothing about that: it was always a
        # point-in-time snapshot, and it still gates the transfer rather than merely
        # preceding it.
        safe, why = await safety
        if not safe:
            result = RunResult(state=RunState.ERROR, error=why)
            log.error("refusing to transfer into an unsafe footage directory", reason=why)
            return result

        # Same moment, same reasoning: the volume is measured once the plan is known and
        # before anything is fetched. Deferring part of the plan is a hold, not a fault --
        # the run copies what fits and the rest stays on the card as backlog. Deferring
        # *all* of it is reported as an error, because then nothing will move until
        # somebody acts, and an idle result would hide that behind "up to date".
        if plan.files:
            fit = await _fit_plan_to_space(plan.files, footage, status)
            if fit is not None and fit.nothing_fits:
                why = _space_hold_reason(fit, footage=footage)
                result = RunResult(state=RunState.ERROR, error=why)
                log.error("refusing to transfer: the footage volume is full", reason=why)
                return result
            if fit is not None and fit.deferred:
                status.plan(plan)

        # Only the first pull of a visit may take the screen over.  The actual launch is
        # deferred until immediately before the transfer starts, after the OBD/radio gates;
        # that way a safety refusal does not show a misleading copying screen.
        # `first_of_visit` was decided before the recovery gate above.
        display = ""
        if bool(_get("show_on_unit", False)) and first_of_visit:
            display = display_url()
            if not display:
                # Reachable only before this app has ever been opened in a browser, since
                # the learned address is now kept across restarts. A warning rather than
                # the debug line this used to be: while it was below the default log level
                # the feature could fail on every single run and leave no trace at all,
                # which is exactly what it did.
                log.warning(
                    "not showing the backup page: this app's own address is not known yet. "
                    "Open the dashboard once, or set the address in Settings > Backup / Ingest."
                )

        await asyncio.shield(cleaned)
        if status.cancel_event.is_set():
            result = RunResult(state=RunState.CANCELLED)
            return result
        port = int(_get("data_port", 9000))
        host = info.address.split(":", 1)[0]
        timeout_s = int(_get("listen_timeout_s", 180))
        status.set_phase(Phase.PREPARING)

        # Awaited, and only after logger finalisation plus OBD bundle backup.  The previous
        # background task raced this first bulk read and could still be in its ten-second
        # guard while the transfer was already moving bytes.
        if radio_transition is not None:
            try:
                radio_transition.raise_if_lease_lost()
                await radio_transition.capture_and_quiet()
                radio_transition.raise_if_lease_lost()
            except Exception as exc:
                # The controller can witness the native deadline before the heartbeat
                # translates it. Preserve only that explicit typed proof; an identical
                # message from an unproven/dead watchdog remains a safety failure.
                if isinstance(exc, radios.WatchdogWindowEnding):
                    exc = radio_coordinator.RadioWindowEnding(exc.recovery_state)
                window_ending = isinstance(exc, radio_coordinator.RadioWindowEnding)
                if window_ending:
                    _cancel_for_radio_stop(exc)
                log.warning(
                    "could not safely quiet radios; restoring before transfer", error=redact(exc)
                )
                reason = (
                    _NATIVE_WINDOW_END_MESSAGE
                    if window_ending
                    else status.radio_quieting_hold_reason or str(exc)
                )
                restored = await _restore_radio_transition(radio_transition, error=exc)
                radio_transition = None
                result = _radio_shutdown_hold(
                    reason if restored else "radio recovery could not be verified before transfer"
                )
                return result

        if quiet_requested and radio_transition is None:
            result = _radio_shutdown_hold("radio shutdown has not been verified")
            return result
        if status.cancel_event.is_set():
            result = RunResult(state=RunState.CANCELLED)
            return result
        duplicate_cleanup_admitted = True
        status.set_phase(Phase.TRANSFERRING)
        if not continuation:
            # A radio hold is not a started copy. Notifications remain asynchronous
            # and are emitted only after every footage admission check succeeds.
            _fire_and_forget(report_event("started", plan=plan))

        if display:
            display_task = asyncio.create_task(
                _show_backup_page_during_transfer(info.address, display),
                name="ingest-show-backup-page",
            )

        chunk_size = int(_get("chunk_size", 5))
        expected = {item.name: item.size for item in plan.files}
        wanted = list(plan.files)
        analysis_batch_yielded = False
        analysis_yield_plan = DeltaPlan()
        analysis_new_backlog: dict[str, int] = {}
        last_analysis_check = asyncio.get_running_loop().time()

        def _note_analysis_backlog(
            candidate_plan: DeltaPlan, candidate_inventory: parked_filter.FilterResult
        ) -> None:
            # A pair can close after the arrival inventory. Preserve those new
            # files in the total backlog as well as the separate pending count.
            for item in [
                *candidate_plan.files,
                *(item for item, _ in candidate_inventory.deferred),
            ]:
                if item.name not in initial_backlog_sizes and item.name not in expected:
                    analysis_new_backlog[item.name] = item.size

        async def _yield_for_new_analysis() -> bool:
            nonlocal analysis_batch_yielded, analysis_yield_plan, last_analysis_check
            if not bool(_get("skip_parked_footage", False)) or not quiet_requested:
                return False
            now = asyncio.get_running_loop().time()
            if now - last_analysis_check < QUIET_ANALYSIS_BATCH_CHECK_S:
                return False
            last_analysis_check = now
            if _analysis_native_budget(status.snapshot()) is None:
                return False
            try:
                async with asyncio.timeout(QUIET_ANALYSIS_PROBE_TIMEOUT_S):
                    latest = await _filter_parked_inventory(
                        info,
                        await adb.inventory_all(info.address, sources),
                        movement_logger,
                        refresh_logger=True,
                    )
                    if not latest.analysis_ready:
                        return False
                    remaining_plan = await asyncio.to_thread(
                        delta,
                        latest.files,
                        footage,
                        skip_active_s=int(_get("skip_active_seconds", 15)),
                        camera="both",
                        already_seen=await removed,
                        now=time.time() + skew,
                    )
                    analysis_yield_plan = await asyncio.to_thread(
                        _with_deferred_backlog,
                        remaining_plan,
                        latest,
                        footage,
                        await removed,
                        time.time() + skew,
                        camera=str(_get("camera_filter", "both")),
                    )
                    if not analysis_yield_plan.policy_deferred_files:
                        return False
                    _note_analysis_backlog(analysis_yield_plan, latest)
                    status.set_policy_deferred(analysis_yield_plan)
                    analysis_batch_yielded = True
                    log.info(
                        "ending the footage batch to release a bounded on-camera analysis opportunity"
                    )
                    return True
            except asyncio.CancelledError:
                raise
            except Exception:
                return False  # Optional observation never interrupts an ordinary backup.

        async def _commit_and_reclaim_chunk(chunk: list[RemoteFile]) -> None:
            chunk_expected = {item.name: item.size for item in chunk}
            status.set_phase(Phase.VERIFYING)
            chunk_committed = await asyncio.to_thread(commit, staging, footage, chunk_expected)
            if chunk_committed:
                committed.extend(chunk_committed)
                if bool(_get("delete_after_verify", False)):
                    if radio_transition is not None:
                        radio_transition.raise_if_lease_lost()
                    by_name = {item.name: item for item in chunk}
                    to_reclaim = [by_name[name] for name in chunk_committed if name in by_name]
                    if to_reclaim:
                        await _reclaim(info, to_reclaim, lease=radio_transition)

        # Started here rather than around the whole run: the sweeps below draw their own
        # plans and commit inline, and a pipeline outliving the transfer it belongs to
        # would have chunks in flight while the run decided it was finished.
        pipeline = _CommitPipeline(
            _commit_and_reclaim_chunk,
            depth=int(_get("commit_queue_depth", COMMIT_QUEUE_DEPTH)),
        )
        pipeline.start()
        try:
            transferred = await _move(
                info,
                plan.files,
                staging=staging,
                host=host,
                port=port,
                timeout_s=timeout_s,
                lease=radio_transition,
                chunk_size=chunk_size,
                on_chunk_completed=pipeline.submit,
                yield_after_chunk=_yield_for_new_analysis,
            )
        except BaseException:
            # The window shut or the lease went. Whatever is queued has not been committed,
            # and the second pass below is what picks those files up -- so drop it rather
            # than spend a dead link finding out.
            await pipeline.close(discard=True)
            raise
        # Awaited before `committed` is read: the run may not work out what is still
        # outstanding while the worker is halfway through telling it.
        await pipeline.close()

        status.set_phase(Phase.VERIFYING)
        remaining_expected = {
            item.name: item.size for item in plan.files if item.name not in set(committed)
        }
        if remaining_expected and staging.is_dir():
            more_committed = await asyncio.to_thread(commit, staging, footage, remaining_expected)
            if more_committed:
                committed.extend(more_committed)
                if bool(_get("delete_after_verify", False)):
                    if radio_transition is not None:
                        radio_transition.raise_if_lease_lost()
                    by_name = {item.name: item for item in plan.files}
                    to_reclaim = [by_name[name] for name in more_committed if name in by_name]
                    if to_reclaim:
                        await _reclaim(info, to_reclaim, lease=radio_transition)

        # The sweeps: re-check the card before calling the run done. The plan was drawn
        # when the car arrived, and the recording the camera was writing at that moment --
        # the last minute of the drive, the clip of actually parking -- was skipped as
        # still being written. By the time a multi-GB transfer finishes it has long been
        # closed, and without this it waited for the next re-drain, or the next drive. So
        # the card is listed again and anything newly closed is copied in the same run,
        # while the radios are still quiet and the link is still proven good. Bounded, and
        # only while the previous pass completed: a link that has already dropped gets no
        # second listing to fail on. Stops the first time nothing new has appeared.
        sweeps = int(_get("sweep_passes", 2))
        for _ in range(max(0, sweeps)):
            if analysis_batch_yielded or not transferred.complete or status.cancel_event.is_set():
                break
            status.set_phase(Phase.SCANNING)
            sweep_inventory = await _filter_parked_inventory(
                info,
                await adb.inventory_all(info.address, sources),
                movement_logger,
                refresh_logger=True,
            )
            filtered_inventory = sweep_inventory
            more = await asyncio.to_thread(
                delta,
                sweep_inventory.files,
                footage,
                skip_active_s=int(_get("skip_active_seconds", 15)),
                camera="both",
                already_seen=await removed,
                newest_first=str(_get("transfer_order", "oldest_first")) == "newest_first",
                now=time.time() + skew,
            )
            more = await asyncio.to_thread(
                _with_deferred_backlog,
                more,
                sweep_inventory,
                footage,
                await removed,
                time.time() + skew,
                camera=str(_get("camera_filter", "both")),
            )
            status.set_policy_deferred(more)
            if more.policy_deferred_files and quiet_requested and sweep_inventory.analysis_ready:
                analysis_batch_yielded = True
                analysis_yield_plan = more
                _note_analysis_backlog(more, sweep_inventory)
                break  # Restore the logger before starting another sweep of ordinary footage.
            if camera != "both":
                more.files = [item for item in more.files if item.name.endswith(f"_{camera}.ts")]
            if more.files:
                await _drop_still_growing(info, more, sources)
            if more.files:
                fit = await _fit_plan_to_space(more.files, footage, status)
                if fit is not None and fit.nothing_fits:
                    break
            if not more.files:
                break
            log.info(
                "re-checking the card found recordings closed during the transfer; "
                "copying them in the same run",
                files=len(more.files),
                megabytes=round(more.bytes / 1e6),
            )
            status.extend_plan(more)
            status.set_phase(Phase.TRANSFERRING)
            more_expected = {item.name: item.size for item in more.files}
            # Keep this inventory before receiving: a quiet-state failure between
            # chunks must still salvage complete files from the current sweep.
            expected.update(more_expected)
            wanted.extend(more.files)
            part = await _move(
                info,
                more.files,
                staging=staging,
                host=host,
                port=port,
                timeout_s=timeout_s,
                lease=radio_transition,
                chunk_size=chunk_size,
                on_chunk_completed=_commit_and_reclaim_chunk,
            )
            _absorb(transferred, part)
            status.set_phase(Phase.VERIFYING)
            remaining_more = {
                name: size for name, size in more_expected.items() if name not in set(committed)
            }
            if remaining_more and staging.is_dir():
                more_committed = await asyncio.to_thread(commit, staging, footage, remaining_more)
                if more_committed:
                    committed.extend(more_committed)
                    if bool(_get("delete_after_verify", False)):
                        if radio_transition is not None:
                            radio_transition.raise_if_lease_lost()
                        by_name = {item.name: item for item in more.files}
                        to_reclaim = [by_name[name] for name in more_committed if name in by_name]
                        if to_reclaim:
                            await _reclaim(info, to_reclaim, lease=radio_transition)

        # The rescue: cut-short recordings stranded outside Video by a power cut. After
        # the sweeps, while the link is still proven good, and only on a run that has not
        # already lost the car -- a rescue is a bonus, never a reason a window fails, so
        # it is fenced in its own try and only what actually landed is counted.
        if (
            bool(_get("rescue_partials", True))
            and transferred.complete
            and not analysis_batch_yielded
            and not status.cancel_event.is_set()
        ):
            try:
                if radio_transition is not None:
                    radio_transition.raise_if_lease_lost()
                rescued, rescued_names, rescued_sizes = await _rescue_partials(
                    info,
                    staging=staging,
                    footage=footage,
                    host=host,
                    port=port,
                    timeout_s=timeout_s,
                    unit_now=int(time.time() + skew),
                    already_seen=await removed,
                    lease=radio_transition,
                )
                if radio_transition is not None:
                    radio_transition.raise_if_lease_lost()
            except radio_coordinator.RadioTransitionError:
                raise
            except Exception as exc:
                log.warning("could not rescue cut-short recordings", error=redact(exc))
            else:
                wanted.extend(rescued)
                committed = [*committed, *rescued_names]
                expected.update(rescued_sizes)

        if analysis_batch_yielded:
            plan.backlog_files += len(analysis_new_backlog)
            plan.backlog_bytes += sum(analysis_new_backlog.values())
            delivered = set(transferred.files) | set(committed)
            wanted = [item for item in wanted if item.name in delivered]
            status.plan(
                DeltaPlan(
                    files=wanted,
                    backlog_files=plan.backlog_files,
                    backlog_bytes=plan.backlog_bytes,
                    active_skipped=plan.active_skipped,
                    policy_deferred_files=analysis_yield_plan.policy_deferred_files,
                    policy_deferred_bytes=analysis_yield_plan.policy_deferred_bytes,
                    policy_deferred_until_utc=analysis_yield_plan.policy_deferred_until_utc,
                )
            )

        state = (
            RunState.OK
            if transferred.complete and len(committed) == len(wanted)
            else (
                RunState.CANCELLED
                if status.cancel_event.is_set()
                else RunState.PARTIAL
                if committed
                else RunState.ERROR
            )
        )
        if state is RunState.OK and radio_transition is None:
            # Preserve already durable footage if task cancellation lands in the
            # optional final inventory/telemetry stage instead of its cooperative flag.
            result = RunResult(
                state=state,
                files=len(committed)
                + (obd_result.copied + obd_result.duplicates if obd_result else 0),
                bytes=sum(expected[name] for name in committed)
                + (obd_result.bytes if obd_result else 0),
                seconds=transferred.seconds + (obd_result.seconds if obd_result else 0),
            )
            # Without a logger checkpoint, ignition-off finalisation can publish its
            # immutable bundle just after the arrival inventory. Take one last look
            # under this run's ownership; do not wait for a second driveway visit.
            # A quieted logger was already checkpointed and must get its radios back
            # promptly, so that path needs no extra inventory or transfer here.
            late_inventory = None
            late_result = None
            try:
                late_inventory = await inventory_remote_bundles(
                    info.address, get_config().obd_remote_ready_dir
                )
                get_obd_transfer_status().set_inventory(len(late_inventory))
                observed_logger = await read_logger_status(
                    info.address, get_config().obd_remote_status_file
                )
                if observed_logger is not None:
                    get_obd_transfer_status().set_logger(observed_logger)
                # Earlier retained/rejected bundles belong to the next retry, not a
                # second attempt (and double-counted result) in this same run.
                earlier_names = {item.name for item in remote_obd}
                late_bundles = [item for item in late_inventory if item.name not in earlier_names]
                if late_bundles and not status.cancel_event.is_set():
                    late_result = await sync_remote_bundles(
                        info, ingest_status=status, remote=late_bundles
                    )
                    if obd_result is None:
                        obd_result = late_result
                    else:
                        for field in (
                            "discovered",
                            "copied",
                            "duplicates",
                            "failed",
                            "missing",
                            "removed_from_unit",
                            "bytes",
                            "seconds",
                        ):
                            setattr(
                                obd_result,
                                field,
                                getattr(obd_result, field) + getattr(late_result, field),
                            )
                        obd_result.complete = obd_result.complete and late_result.complete
                        obd_result.error = obd_result.error or late_result.error
            except Exception as exc:
                obd_inventory_ok = False
                obd_transfer_error = exc
                get_obd_transfer_status().finish(
                    OBDTransferResult(
                        failed=1,
                        complete=False,
                        error="Could not finish checking newly completed OBD bundles",
                    )
                )
                log.warning("late OBD catch-up failed without affecting footage", error=redact(exc))
            finally:
                if late_inventory is not None:
                    # sync_remote_bundles sees only new names. Preserve older retained
                    # exports in the full pending count so sleep cannot close over them.
                    get_obd_transfer_status().set_inventory(
                        max(
                            0,
                            len(late_inventory)
                            - (late_result.removed_from_unit if late_result else 0),
                        )
                    )
            if status.cancel_event.is_set():
                state = RunState.CANCELLED
        committed_footage_bytes = sum(expected[name] for name in committed)
        result = RunResult(
            state=state,
            files=len(committed)
            + (obd_result.copied + obd_result.duplicates if obd_result is not None else 0),
            bytes=committed_footage_bytes + (obd_result.bytes if obd_result is not None else 0),
            seconds=transferred.seconds + (obd_result.seconds if obd_result is not None else 0),
            error=None if state is RunState.OK else transferred.error,
        )
        skipped_now = (
            {item.name for item in filtered_inventory.skipped} & set(initial_backlog_sizes)
        ) - set(committed)
        extra_names = set(expected) - set(initial_backlog_sizes)
        known_backlog_files = plan.backlog_files + len(extra_names)
        known_backlog_bytes = plan.backlog_bytes + sum(
            size - initial_backlog_sizes.get(name, 0) for name, size in expected.items()
        )
        status.set_backlog(
            max(
                status.policy_deferred_files,
                known_backlog_files - len(committed) - len(skipped_now),
            ),
            max(
                status.policy_deferred_bytes,
                known_backlog_bytes
                - committed_footage_bytes
                - sum(initial_backlog_sizes[name] for name in skipped_now),
            ),
        )
        log.info(
            "pull finished",
            state=state.value,
            files=result.files,
            megabytes=round(result.bytes / 1e6),
            throughput_mbs=result.throughput_mbs,
        )
    except asyncio.CancelledError:
        result.state = RunState.CANCELLED
        result.error = "backup cancelled"
        raise
    except radio_coordinator.RadioWindowEnding:
        interrupted_error = _NATIVE_WINDOW_END_MESSAGE
        result = RunResult(state=RunState.ERROR, error=interrupted_error)
        status.cancel(reason="native_sleep_window_ending", detail=interrupted_error)
        log.info("ending the pull before native sleep; preserving remaining footage")
    except adb.AdbError as exc:
        interrupted_error = redact(exc)
        result = RunResult(state=RunState.ERROR, error=interrupted_error)
        log.warning("the control channel failed during a pull", error=redact(exc))
    except Exception as exc:
        interrupted_error = redact(f"{type(exc).__name__}: {exc}")
        result = RunResult(state=RunState.ERROR, error=interrupted_error)
        log.error("the ingest run failed", error=redact(exc))
    finally:
        # This task alone is optional observability: its database cleanup may
        # outlive the run, with the single retained slot preventing accumulation.
        _cancel_event_mirror(obd_events)
        # The dashboard is relevant only while this run owns the transfer.  Do not let a
        # delayed retry take the screen over after the car has gone or a later run has begun.
        if display_task is not None:
            display_task.cancel()
            await asyncio.gather(display_task, return_exceptions=True)

        # Radios first, before any other tidying: this is the one piece of cleanup whose
        # failure follows somebody into the car. Bounded, because the usual reason a
        # restore struggles is that the unit has driven away and every call is a timeout
        # — the marker and the on-unit watchdog carry the cases this cannot reach.
        if radio_transition is not None:
            restored = False
            try:
                restored = await _restore_radio_transition(radio_transition)
            except (asyncio.CancelledError, Exception) as exc:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(radio_transition.require_recovery(exc))
            if not restored and result.state is RunState.OK:
                result = RunResult(
                    state=RunState.PARTIAL,
                    files=result.files,
                    bytes=result.bytes,
                    seconds=result.seconds,
                    error="radio restoration remains pending and will be retried",
                )
            elif not restored and status.cancel_reason == "native_sleep_window_ending":
                detail = "radio restoration remains pending and will be retried"
                if interrupted_error is not None:
                    interrupted_error = f"{interrupted_error}; {detail}"
                else:
                    result.error = f"{result.error or _NATIVE_WINDOW_END_MESSAGE}; {detail}"
                    result.state = (
                        RunState.PARTIAL if result.files or result.bytes else RunState.ERROR
                    )

        if interrupted_error is not None:
            # Restore the driver's radios first. Then preserve and count every
            # complete local arrival, including a sweep interrupted by quiet-state
            # loss. Recovery must not erase progress already committed/reclaimed.
            remaining_expected = {
                name: size for name, size in expected.items() if name not in set(committed)
            }
            if remaining_expected and staging.is_dir():
                with contextlib.suppress(Exception):
                    committed.extend(
                        await asyncio.to_thread(commit, staging, footage, remaining_expected)
                    )
            committed = list(dict.fromkeys(committed))
            saved_bytes = sum(expected[name] for name in committed)
            saved_files = len(committed)
            if obd_result is not None:
                saved_files += obd_result.copied + obd_result.duplicates
            result = RunResult(
                state=RunState.PARTIAL if saved_files else RunState.ERROR,
                files=saved_files,
                bytes=saved_bytes + (obd_result.bytes if obd_result is not None else 0),
                seconds=time.monotonic() - started,
                error=interrupted_error,
            )
            if expected:
                initial_plan = locals().get("plan")
                extra_names = set(expected) - set(initial_backlog_sizes)
                backlog_files = (initial_plan.backlog_files if initial_plan else 0) + len(
                    extra_names
                )
                backlog_bytes = (initial_plan.backlog_bytes if initial_plan else 0) + sum(
                    size - initial_backlog_sizes.get(name, 0) for name, size in expected.items()
                )
                status.set_backlog(
                    max(status.policy_deferred_files, backlog_files - len(committed)),
                    max(status.policy_deferred_bytes, backlog_bytes - saved_bytes),
                )

        # Restoring the driver's radios/logger outranks a courtesy screen change. An
        # unresponsive launcher previously spent up to 25 seconds ahead of restoration.
        if display_task is not None and bool(_get("hand_screen_back", True)):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(adb.hand_screen_back(info.address), timeout=25.0)

        # Any preflight nobody got as far as needing -- an idle window, a share that failed
        # its checks. Collected rather than abandoned, so a short run cannot leave a task
        # complaining that its result was never retrieved.
        duplicate_storage_safe = False
        if (
            duplicate_safety is not None
            and duplicate_safety.done()
            and not duplicate_safety.cancelled()
        ):
            with contextlib.suppress(Exception):
                duplicate_storage_safe = bool(duplicate_safety.result()[0])
        for task in preflight:
            task.cancel()
        if preflight:
            await asyncio.gather(*preflight, return_exceptions=True)
        for task in staging_cleanup_tasks:
            await _join_staging_cleanup(task)

        if info is not None:
            await _cleanup_existing_duplicates(
                info,
                footage,
                duplicate_candidates,
                clock_known=duplicate_clock_known,
                storage_safe=duplicate_storage_safe,
                admitted=duplicate_cleanup_admitted,
                result=result,
                status=status,
            )

        if result.seconds <= 0:
            result.seconds = time.monotonic() - started
        if committed:
            # Discover only after the batch (including partial/salvaged arrivals), never
            # once per chunk. The scheduler serializes and coalesces this with its normal
            # scans, honors scanner.enabled/auto_process, and supplies settling retries.
            from app.workers import scheduler

            try:
                scheduler.request_ingest_scan()
            except Exception as exc:
                log.error("could not notify scanner about committed footage", error=redact(exc))
        lifecycle = _run_lifecycle.get()
        _classify_cancellation(result)
        result.error = redact(result.error) if result.error else None
        status.finish(result)
        if lifecycle is not None:
            lifecycle.released = True
        # A run that found nothing to do, or found no car, is not an event. Recording and
        # announcing those would put a row in the table and a notification on somebody's
        # phone every single time the engine started -- and, because anything that is not
        # OK reports as an error, it would call "nothing new to copy" a failure. The live
        # state is on /api/ingest/status for anyone who wants to look.
        await _record_run_completion(result, trigger, continuation=continuation)

        # Last of all: narrow only after everything that needed the car has had its turn.
        # Numeric zero is not a drained card until this run actually inventoried it, and a
        # durable active/recovery row means the radios or logger are still owed an exact
        # restore. Both conditions fail closed: an unknown state keeps the long window.
        recovery_clear = (
            obd_inventory_ok
            and obd_transfer_error is None
            and (obd_result is None or (obd_result.complete and not obd_result.failed))
            and await _sleep_window_may_close(result)
        )
        with contextlib.suppress(Exception):
            await close_sleep_window(
                info.address if info else "",
                drained=recovery_clear,
                # A quarantined/interrupted OBD export is still data on the unit even
                # when the footage delta is empty.  Leave the visit open for a re-drain.
                # (Folded into the bool here rather than changing close_sleep_window's
                # public contract.)
            )

    return result


#: Safety checks a transfer must pass before it may write into the footage directory.
#:
#: Retention and the damaged-footage policy both gate on ``evaluate_safety`` before they
#: touch this directory, and ingest is now the third writer, so it uses the same evaluator
#: rather than inventing its own idea of "looks mounted". The checks that matter here are
#: the ones that catch an absent share -- an unmounted network mount is indistinguishable
#: from an empty directory, which is exactly how a whole card ends up written underneath a
#: mount point and then deleted from the camera.
#:
#: ``minimum_files`` and ``consistent_with_index`` are deliberately not required: they
#: protect *deletion* by refusing to act on a suspiciously empty library, and an empty
#: library is the normal state of a fresh install that has not ingested anything yet.
_REQUIRED_SAFETY_CHECKS = ("directory_exists", "not_data_directory", "not_system_root")


async def _footage_is_safe_to_write(footage: Path) -> tuple[bool, str | None]:
    """Whether the footage directory is really there and really writable."""
    from app.core.paths import is_writable
    from app.db.session import session_scope
    from app.retention.safety import evaluate_safety

    async with session_scope() as session:
        report = await evaluate_safety(session, footage)

    failed = [
        check
        for check in report.checks
        if check.name in _REQUIRED_SAFETY_CHECKS and not check.passed
    ]
    if failed:
        return False, failed[0].reason or f"{footage} failed the {failed[0].name} check"

    # Asked directly rather than read off the report. ``evaluate_safety`` returns early
    # when a *deletion* guard fails -- an almost-empty library trips `minimum_files` --
    # and `report.writable` is computed after those returns, so it is False for a fresh
    # install that is merely empty. Emptiness is the normal state of a library that has
    # not ingested anything yet, and it is not a reason to refuse to write.
    if not await asyncio.to_thread(is_writable, footage):
        return False, f"{footage} is mounted read-only; drop the ':ro' to copy footage into it"

    # A directory that is supposed to be a mount and is not one is the signature of an
    # absent share, and writing a card into a bare mount point is how the originals end up
    # shadowed by the eventual remount and then deleted from the camera.
    #
    # Gated on the operator's own declaration. ``storage.require_mountpoint`` already means
    # "footage lives on a mount", and plenty of people instead keep it on a local disk --
    # where an empty directory is simply a library nothing has filled yet, which is exactly
    # what this feature exists to do.
    requires_mount = True
    try:
        requires_mount = bool(get_settings_service().get_nowait("storage.require_mountpoint"))
    except Exception:
        pass

    mount = next((c for c in report.checks if c.name == "is_mount_point"), None)
    if requires_mount and mount is not None and not mount.passed:
        return False, (
            mount.reason or f"{footage} is not a mount point; the share is probably not mounted"
        )
    return True, None


async def _deliberately_removed() -> set[str]:
    """Filenames the library has processed and then removed on purpose.

    Retention deleting old footage and this feature fetching it back would otherwise be a
    loop, and one that starves new recordings: the card holds the oldest files too, and
    every window is finite.
    """
    from sqlalchemy import or_, select

    from app.db.models import Recording
    from app.db.session import session_scope

    try:
        async with session_scope() as session:
            rows = await session.execute(
                select(Recording.filename).where(
                    or_(Recording.deleted_at.is_not(None), Recording.file_missing.is_(True))
                )
            )
            return {name for name in rows.scalars() if name}
    except Exception as exc:
        # A failure here must not stop a transfer; the worst case is re-fetching a file.
        log.debug("could not read the removed-recording list", error=redact(exc))
        return set()


async def _persist(result: RunResult, trigger: str) -> None:
    """Record the run for history. Live progress deliberately stays in memory."""
    from app.db.models import IngestRun
    from app.db.session import session_scope

    finished_at = datetime.now(UTC)
    lifecycle = _run_lifecycle.get()
    async with session_scope() as session:
        session.add(
            IngestRun(
                started_at=lifecycle.started_at if lifecycle is not None else finished_at,
                finished_at=finished_at,
                trigger=trigger,
                state=result.state.value,
                files_transferred=result.files,
                bytes_transferred=result.bytes,
                throughput_mbs_avg=result.throughput_mbs,
                error=redact(result.error) if result.error else None,
            )
        )


async def _record_run_completion(result: RunResult, trigger: str, *, continuation: bool) -> None:
    """Persist every material attempt while announcing only once per visit."""
    if result.state in (RunState.IDLE, RunState.OFFLINE):
        return
    try:
        await _persist(result, trigger)
    except Exception as exc:
        # History is diagnostic evidence. Losing it must not fail a completed transfer,
        # but swallowing the write error made a real 72-file run indistinguishable from
        # one that never happened.
        log.error(
            "could not persist completed ingest run history",
            trigger=trigger,
            state=result.state.value,
            error=redact(f"{type(exc).__name__}: {exc}"),
        )
    # History is one row per completed attempt, including a successful re-drain.
    # Notification is one per visit. Coupling these conditions made history stale whenever
    # an initial band/recovery hold returned IDLE and the continuation did the transfer.
    if not continuation:
        with contextlib.suppress(Exception):
            await report_event(
                "finished" if result.state is RunState.OK else "error", result=result
            )


async def report_event(
    event: str, *, plan: DeltaPlan | None = None, result: RunResult | None = None
):
    from app.ingest.reporter import publish

    if result is not None and result.error:
        result.error = redact(result.error)
    await publish(event, plan=plan, result=result)
