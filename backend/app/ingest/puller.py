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
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_config
from app.core.logging import get_logger
from app.core.settings_schema import (
    INGEST_SLEEP_WINDOW_ACTIVE_SECONDS,
    INGEST_SLEEP_WINDOW_IDLE_SECONDS,
)
from app.core.settings_service import get_settings_service
from app.ingest import adb, band, elevate, obd_control, origin, radio_coordinator, radios, transport
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
    sync_remote_events,
)
from app.ingest.obd_transfer import (
    get_obd_transfer_status,
    inventory_remote_bundles,
    read_logger_status,
    sync_remote_bundles,
    verified_bundle_matches,
)
from app.ingest.status import get_status

log = get_logger(__name__)

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
_EVENT_SYNC_AWAIT_GRACE_SECONDS = 0.25


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
        if await adb.chrome_is_foreground(address):
            if attempt == 1:
                log.info("the backup page is already showing on the head unit")
            else:
                log.info("opened and verified the backup page on the head unit", attempt=attempt)
            shown = True
            break
        reason = await adb.show_url(address, url)
        if reason:
            log.warning(
                "backup page was not visible on the head unit; will retry while transferring",
                attempt=attempt,
                error=reason,
            )
    if not shown:
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
        if await adb.chrome_is_foreground(address):
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
    if await adb.sleep_countdown(address) >= wanted:
        return True
    if await adb.set_sleep_countdown(address, wanted):
        log.info("widened the head unit's ignition-off window", seconds=wanted)
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
    return await radio_coordinator.reconcile_pending(address=address)


async def reconcile_startup_in_awake_window() -> bool:
    """Recover startup radio state only after protecting its recorded endpoint from sleep."""
    address = await radio_coordinator.pending_recovery_address()
    if address is None:
        # Preserve the coordinator's no-row startup housekeeping without inventing a device
        # endpoint or touching the configured unit when there is no durable recovery owner.
        return await radio_coordinator.reconcile_startup()
    return await reconcile_pending_in_awake_window(address)


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
                    return

    if not await adb.is_parked(address):
        # Still being driven. Narrowing the window now would strand the next ignition-off
        # with the short value before anything had a chance to widen it.
        return

    if await adb.sleep_countdown(address) != idle:
        if await adb.set_sleep_countdown(address, idle):
            log.info("restored the head unit's idle sleep window", seconds=idle)
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
        or bool(get_obd_transfer_status().snapshot()["waiting_on_unit"])
    ):
        return False
    try:
        return await radio_coordinator.pending_recovery_address() is None
    except Exception as exc:
        log.warning(
            "could not prove radio recovery is clear; leaving the sleep window open",
            error=f"{type(exc).__name__}: {exc}",
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
    where = {item.name: item.directory or (info.source or "") for item in items}
    removed = 0
    for directory, names in _group_names([item.name for item in items], where).items():
        if lease is not None:
            lease.raise_if_lease_lost()
        try:
            removed += await adb.delete(info.address, directory, names)
        except adb.AdbError as exc:
            # Still not fatal -- the copies are safe in the library and the card can be
            # reclaimed next window -- but no longer invisible.
            log.warning(
                "could not reclaim space on the card",
                directory=directory,
                files=len(names),
                error=str(exc),
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
    # Seeded complete; `_absorb` ANDs each batch onto it.
    transferred = transport.TransferResult(complete=True)
    for directory, batch in _by_directory(files, info.source):
        chunks = (
            [batch[i : i + chunk_size] for i in range(0, len(batch), chunk_size)]
            if chunk_size > 0
            else [batch]
        )
        for chunk in chunks:
            if lease is not None:
                lease.raise_if_lease_lost()
            # Anything still listening is serving a *previous* batch's file list, so clear it
            # before starting ours rather than connecting to the wrong stream.
            await adb.clear_listener(info.address)
            listener = await adb.launch_listener(
                info.address,
                directory,
                [item.name for item in chunk],
                port=port,
                timeout_s=timeout_s,
            )
            was_serving = False
            status.set_phase(Phase.TRANSFERRING)
            try:
                part = await asyncio.to_thread(
                    transport.receive,
                    host,
                    port,
                    staging,
                    expected={item.name: item.size for item in chunk},
                    on_file_started=status.file_started,
                    on_file_done=status.file_done,
                    on_bytes=status.add_bytes,
                    cancel=status.cancel_event,
                )
            finally:
                # The adb session *is* the listener's lifetime now, so it has to be ended
                # explicitly; leaving it would hold the port against the next batch.
                was_serving = await adb.stop_listener(listener)

            # Which side stopped first. An incomplete transfer whose listener was still serving
            # is the car leaving, which is the expected ending and not a fault; one whose
            # listener had already exited is the unit giving up -- `tar` failing, the remote
            # `timeout` firing -- and that is worth saying out loud, because the two used to
            # produce the same sentence and only one of them is anybody's problem.
            if part.error and not part.complete and not was_serving:
                part.error = f"{part.error} (the head unit stopped serving first)"
            _absorb(transferred, part)

            if on_chunk_completed is not None:
                await on_chunk_completed(chunk)

            if lease is not None:
                lease.raise_if_lease_lost()
            if not part.complete or status.cancel_event.is_set():
                # The window shut, or the operator cancelled. Standing up another listener into
                # a link that has already gone would spend what is left of the window on a
                # connection that cannot be answered.
                break
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
    await _move(info, candidates, staging=staging, host=host, port=port, timeout_s=timeout_s)

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
            log.debug("could not stage a rescued recording", name=item.name, error=str(exc))
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
            await _reclaim(info, [by_target[name] for name in committed if name in by_target])
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
            # Recorded rather than merely skipped. The library has this one already, so it
            # is not fetched -- but it is still occupying the card, and the same size check
            # that justifies not copying it justifies giving that space back.
            plan.already_local.append(item)
            continue

        plan.backlog_files += 1
        plan.backlog_bytes += item.size
        if camera != "both" and not item.name.endswith(f"_{camera}.ts"):
            continue
        plan.files.append(item)

    plan.files.sort(key=lambda item: item.name, reverse=newest_first)
    return plan


def commit(staging: Path, footage: Path, expected: dict[str, int]) -> list[str]:
    """Move only whole files into the footage directory.

    The size check is what makes an interrupted window safe: ``tar`` was streaming into the
    last file when the socket died, so that one is short and is discarded rather than
    published. Everything before it is byte-complete and moves. The scanner only ever sees
    finished files, and it never sees the staging directory at all.

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
        if not path.is_file():
            continue
        wanted = expected.get(path.name)
        try:
            staged = path.stat().st_size
            if wanted is None or staged != wanted:
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
                    reason=(
                        "it was not in this run's plan"
                        if wanted is None
                        else "incomplete; it will be fetched again next window"
                    ),
                )
                path.unlink()
                continue

            target = footage / path.name
            existing = target.stat().st_size if target.is_file() else None
            if existing == wanted:
                # Somebody else got there first -- a previous window, or the same file
                # arriving twice. Nothing to do, and nothing to destroy.
                path.unlink()
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

            # Same filesystem by construction, so this is a rename: atomic, and the
            # scanner can never observe a half-written file under its own name.
            path.replace(target)
            committed.append(path.name)
        except OSError as exc:
            log.warning("could not commit a staged recording", file=path.name, error=str(exc))
    return committed


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
        log.debug("could not take a second look at the card", error=str(exc))
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


async def _await_event_mirror(task: asyncio.Task[EventSyncResult], *, deadline: float) -> None:
    """Collect event mirroring without allowing observability to gate a backup."""
    try:
        # The deadline is captured when the preflight starts, rather than granting a
        # fresh timeout here after card inventory and logger status have completed.
        async with asyncio.timeout_at(deadline):
            await task
    except TimeoutError:
        log.warning(
            "could not mirror OBD app events; backup will continue",
            error="event mirror deadline exceeded",
        )
    except Exception:
        # The consumer handles expected ADB, validation and storage failures itself.
        # This final fence keeps a future unexpected regression fail-soft as well.
        log.warning(
            "could not mirror OBD app events; backup will continue",
            error="unexpected event mirror failure",
        )


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
                status.set_ignition(held=True, reason=reason)
                result = RunResult(state=RunState.IDLE)
                return result
        status.set_ignition(held=False, reason=None)

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
        # collides with its row and no path ever adopts the now-expired lease.
        # Await the longer countdown first.  This path is also used by manual pulls, which
        # bypass the poller's arrival recovery and must not race the resting sleep window.
        if not await reconcile_pending_in_awake_window(info.address):
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
        cleaned = _preflight(preflight, asyncio.to_thread(_clean, staging))
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
        obd_events = _preflight(
            preflight,
            sync_remote_events(info.address, get_config().obd_remote_events_file),
        )
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
        status.set_phase(Phase.PREPARING)
        # Off the event loop: this stats every candidate against the footage share, which
        # is a hard NFS mount in the deployment, and a full card is ~140 files. Doing it
        # inline would freeze /health and the whole UI behind a slow server every time the
        # car appears -- the regression commit da2850a exists to prevent.
        # Carried as an offset rather than an absolute, so the listing's own duration
        # cannot age the reading: the guard is judged at the moment the delta runs.
        unit_now = await clock
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
            camera=str(_get("camera_filter", "both")),
            already_seen=await removed,
            newest_first=str(_get("transfer_order", "oldest_first")) == "newest_first",
            now=time.time() + skew,
        )
        # A second opinion on which files are still open, from two sizes rather than one
        # timestamp. Only when there is something to fetch, so an idle window still costs
        # exactly one listing.
        if plan.files:
            await _drop_still_growing(info, plan, sources)
        status.plan(plan)
        try:
            remote_obd = await obd_inventory
        except Exception as exc:
            remote_obd = []
            obd_inventory_ok = False
            log.warning("could not inventory OBD exports; footage will continue", error=str(exc))
        previous_logger = get_obd_transfer_status().snapshot()["logger"]
        try:
            observed_logger = await obd_logger
        except Exception as exc:
            observed_logger = {
                "state": "status_unavailable",
                "last_error": "logger status read failed",
            }
            log.warning("could not read OBD logger status; footage will continue", error=str(exc))
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

        # Before the idle return, not after it. A card whose whole contents the library
        # already holds produces exactly that idle run, every window, forever -- so leaving
        # the reclaim until after this point is what made "delete from the card" look like
        # it did nothing at all: only files a run happened to copy itself were ever given
        # back, and everything copied before the setting was turned on stayed put.
        if plan.already_local and bool(_get("delete_after_verify", False)):
            # Gated on the same evaluator as every other destructive path here. The delta's
            # own size check has already proved each of these exists locally, so an
            # unmounted share yields an empty list rather than a wrong one -- but the check
            # is cheap on a run that has something to reclaim, and this is the one place
            # that erases footage from the card without having just written it.
            safe, why = await safety
            if safe:
                await _reclaim(info, plan.already_local)
            else:
                log.warning(
                    "not reclaiming card space: the footage directory is not safe", reason=why
                )

        if not plan.files and not remote_obd:
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

        quiet_requested = bool(_get("quiet_radios", False)) and bool(plan.files)
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
        elif quiet_requested and logger_owns_bluetooth and not logger_can_quiesce:
            log.info(
                "leaving the unit's radios on because the OBD logger owns Bluetooth "
                "and does not support ingestion quiescence",
                logger_state=(observed_logger or {}).get("state"),
            )
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
                    lease_loss_callback=status.cancel,
                )
            except radio_coordinator.TransitionBusy as exc:
                # A second process must not inventory/copy/delete the same card while the
                # first owns radio state. IDLE keeps this visit retryable after recovery.
                log.warning("another ingest owns the device radios; postponing this pull")
                result = RunResult(state=RunState.IDLE, error=str(exc))
                return result
            except radio_coordinator.RadioTransitionError as exc:
                log.warning(
                    "could not establish crash-safe radio ownership; leaving radios on",
                    error=str(exc),
                )

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
                        error=str(exc),
                    )
                    restored = await radio_transition.restore(error=exc)
                    radio_transition = None
                    if not restored:
                        # A failed restore can mean the logger is still paused even when
                        # no radio command was attempted.  Do not start a large transfer
                        # while the durable transition is explicitly awaiting recovery.
                        result = RunResult(
                            state=RunState.IDLE,
                            error="OBD logger/radio recovery could not be verified",
                        )
                        return result

        # Small immutable OBD archives go first so the drive survives even if the unit's
        # short post-ignition window closes partway through the much larger footage set.
        # The stage owns its own temp directory, hashes, validation and DB transaction;
        # all failures stay on its queue/status and are deliberately excluded from the
        # footage result below.
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
                log.exception("OBD backup failed without affecting footage", error=str(exc))

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
                    error=str(exc),
                )
                restored = await radio_transition.restore(error=exc)
                radio_transition = None
                if not restored:
                    result = RunResult(
                        state=RunState.IDLE,
                        error="OBD backup recovery could not be verified",
                    )
                    return result

        if not plan.files:
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

        # Fired, not awaited. This is an httpx POST with a ten-second timeout standing
        # between a decided transfer and its first byte, and the one failure it has in
        # practice -- an unreachable webhook host -- is precisely the one that takes the
        # full ten seconds. At 34 MB/s that is 340 MB of footage left on the card so a
        # notification could go out marginally sooner. Nothing downstream reads its result.
        if not continuation:
            _fire_and_forget(report_event("started", plan=plan))

        # Only the first pull of a visit may take the screen over.  The actual launch is
        # deferred until immediately before the transfer starts, after the OBD/radio gates;
        # that way a safety refusal does not show a misleading copying screen.
        first_of_visit = status.since_finished() > status.online_for()
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

        await cleaned
        port = int(_get("data_port", 9000))
        host = info.address.split(":", 1)[0]
        timeout_s = int(_get("listen_timeout_s", 180))
        status.set_phase(Phase.TRANSFERRING)

        # Awaited, and only after logger finalisation plus OBD bundle backup.  The previous
        # background task raced this first bulk read and could still be in its ten-second
        # guard while the transfer was already moving bytes.
        if radio_transition is not None:
            try:
                radio_transition.raise_if_lease_lost()
                await radio_transition.capture_and_quiet()
                radio_transition.raise_if_lease_lost()
            except Exception as exc:
                log.warning(
                    "could not safely quiet radios; restoring before transfer", error=str(exc)
                )
                restored = await radio_transition.restore(error=exc)
                radio_transition = None
                if not restored:
                    result = RunResult(
                        state=RunState.IDLE,
                        error="radio recovery could not be verified before transfer",
                    )
                    return result

        if display:
            display_task = asyncio.create_task(
                _show_backup_page_during_transfer(info.address, display),
                name="ingest-show-backup-page",
            )

        chunk_size = int(_get("chunk_size", 5))
        committed: list[str] = []
        expected: dict[str, int] = {item.name: item.size for item in plan.files}
        wanted = list(plan.files)

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

        transferred = await _move(
            info,
            plan.files,
            staging=staging,
            host=host,
            port=port,
            timeout_s=timeout_s,
            lease=radio_transition,
            chunk_size=chunk_size,
            on_chunk_completed=_commit_and_reclaim_chunk,
        )

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
            if not transferred.complete or status.cancel_event.is_set():
                break
            status.set_phase(Phase.SCANNING)
            more = await asyncio.to_thread(
                delta,
                await adb.inventory_all(info.address, sources),
                footage,
                skip_active_s=int(_get("skip_active_seconds", 15)),
                camera=str(_get("camera_filter", "both")),
                already_seen=await removed,
                newest_first=str(_get("transfer_order", "oldest_first")) == "newest_first",
                now=time.time() + skew,
            )
            if more.files:
                await _drop_still_growing(info, more, sources)
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
            more_expected = {item.name: item.size for item in more.files}
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
            expected.update(more_expected)
            wanted.extend(more.files)

        # The rescue: cut-short recordings stranded outside Video by a power cut. After
        # the sweeps, while the link is still proven good, and only on a run that has not
        # already lost the car -- a rescue is a bonus, never a reason a window fails, so
        # it is fenced in its own try and only what actually landed is counted.
        if (
            bool(_get("rescue_partials", True))
            and transferred.complete
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
                )
                if radio_transition is not None:
                    radio_transition.raise_if_lease_lost()
            except radio_coordinator.RadioTransitionError:
                raise
            except Exception as exc:
                log.warning("could not rescue cut-short recordings", error=str(exc))
            else:
                wanted.extend(rescued)
                committed = [*committed, *rescued_names]
                expected.update(rescued_sizes)

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
        committed_footage_bytes = sum(expected[name] for name in committed)
        result = RunResult(
            state=state,
            files=len(committed)
            + (obd_result.copied + obd_result.duplicates if obd_result is not None else 0),
            bytes=committed_footage_bytes + (obd_result.bytes if obd_result is not None else 0),
            seconds=transferred.seconds + (obd_result.seconds if obd_result is not None else 0),
            error=None if state is RunState.OK else transferred.error,
        )
        status.set_backlog(
            max(0, plan.backlog_files - len(committed)),
            # OBD archives share the run summary but are not footage backlog bytes.
            max(0, plan.backlog_bytes - committed_footage_bytes),
        )
        log.info(
            "pull finished",
            state=state.value,
            files=result.files,
            megabytes=round(result.bytes / 1e6),
            throughput_mbs=result.throughput_mbs,
        )
    except adb.AdbError as exc:
        salvage_plan = locals().get("plan")
        salvage_committed = locals().get("committed", [])
        if salvage_plan is not None and getattr(salvage_plan, "files", None) and staging.is_dir():
            remaining_expected = {
                item.name: item.size
                for item in salvage_plan.files
                if item.name not in set(salvage_committed)
            }
            if remaining_expected:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(commit, staging, footage, remaining_expected)
        result = RunResult(state=RunState.ERROR, error=str(exc))
        log.warning("the control channel failed during a pull", error=str(exc))
    except Exception as exc:
        salvage_plan = locals().get("plan")
        salvage_committed = locals().get("committed", [])
        if salvage_plan is not None and getattr(salvage_plan, "files", None) and staging.is_dir():
            remaining_expected = {
                item.name: item.size
                for item in salvage_plan.files
                if item.name not in set(salvage_committed)
            }
            if remaining_expected:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(commit, staging, footage, remaining_expected)
        result = RunResult(state=RunState.ERROR, error=f"{type(exc).__name__}: {exc}")
        log.exception("the ingest run failed", error=str(exc))
    finally:
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
                restored = await asyncio.wait_for(radio_transition.restore(), timeout=30.0)
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

        # Any preflight nobody got as far as needing -- an idle window, a share that failed
        # its checks. Collected rather than abandoned, so a short run cannot leave a task
        # complaining that its result was never retrieved.
        for task in preflight:
            task.cancel()
        if preflight:
            await asyncio.gather(*preflight, return_exceptions=True)

        if result.seconds <= 0:
            result.seconds = time.monotonic() - started
        status.finish(result)
        # A run that found nothing to do, or found no car, is not an event. Recording and
        # announcing those would put a row in the table and a notification on somebody's
        # phone every single time the engine started -- and, because anything that is not
        # OK reports as an error, it would call "nothing new to copy" a failure. The live
        # state is on /api/ingest/status for anyone who wants to look.
        if result.state not in (RunState.IDLE, RunState.OFFLINE) and not continuation:
            with contextlib.suppress(Exception):
                await _persist(result, trigger)
            with contextlib.suppress(Exception):
                await report_event(
                    "finished" if result.state is RunState.OK else "error", result=result
                )

        # Last of all: narrow only after everything that needed the car has had its turn.
        # Numeric zero is not a drained card until this run actually inventoried it, and a
        # durable active/recovery row means the radios or logger are still owed an exact
        # restore. Both conditions fail closed: an unknown state keeps the long window.
        recovery_clear = await _sleep_window_may_close(result)
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
        log.debug("could not read the removed-recording list", error=str(exc))
        return set()


async def _persist(result: RunResult, trigger: str) -> None:
    """Record the run for history. Live progress deliberately stays in memory."""
    from app.db.models import IngestRun
    from app.db.session import session_scope

    async with session_scope() as session:
        session.add(
            IngestRun(
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                trigger=trigger,
                state=result.state.value,
                files_transferred=result.files,
                bytes_transferred=result.bytes,
                throughput_mbs_avg=result.throughput_mbs,
                error=result.error,
            )
        )


async def report_event(
    event: str, *, plan: DeltaPlan | None = None, result: RunResult | None = None
):
    from app.ingest.reporter import publish

    await publish(event, plan=plan, result=result)
