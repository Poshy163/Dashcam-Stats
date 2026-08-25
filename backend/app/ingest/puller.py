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
from datetime import UTC, datetime
from pathlib import Path

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.ingest import adb, band, elevate, origin, radios, transport
from app.ingest.models import (
    DeltaPlan,
    Phase,
    RemoteFile,
    RunResult,
    RunState,
    UnitInfo,
    UnitState,
)
from app.ingest.status import get_status

log = get_logger(__name__)

#: Staged arrivals live here until they are whole. A dot-directory so the scanner's walk
#: skips it -- see `app.scanner.discovery`.
STAGING_DIRNAME = ".ingest_staging"


def _settings() -> object:
    return get_settings_service()


def _get(key: str, default=None):
    try:
        return get_settings_service().get_nowait(f"ingest.{key}")
    except Exception:
        return default


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


async def _reclaim(info: UnitInfo, items: list[RemoteFile]) -> int:
    """Remove recordings the library already holds from the card. Returns how many went.

    Logged rather than silent, at info level, which is the other half of this fix. The
    delete was wrapped in a bare ``suppress(AdbError)`` and said nothing on the way past,
    so "is it deleting?" could not be answered from the Logs page at all -- the same shape
    of mistake as the backup-page warning that sat below the default log level.
    """
    where = {item.name: item.directory or (info.source or "") for item in items}
    removed = 0
    for directory, names in _group_names([item.name for item in items], where).items():
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
) -> transport.TransferResult:
    """Stream *files* off the unit into *staging*, one listener per directory.

    One listener per directory rather than one for the run. The card keeps ordinary
    segments and incident-locked ones in separate directories, and ``tar`` is rooted at
    the directory it is run from -- so the alternative was rooting it at the parent and
    letting members arrive as ``Video/x.ts``, which the receiver refuses outright because
    a member carrying a path is how a tar stream escapes its staging directory. Batching
    keeps that guard untouched and costs one extra connection setup, only on cards that
    actually have locked recordings.

    Its own function because the run calls it more than once: for the plan drawn on
    arrival, and again for each sweep that finds recordings the camera closed while the
    first lot was moving.
    """
    status = get_status()
    # Seeded complete; `_absorb` ANDs each batch onto it.
    transferred = transport.TransferResult(complete=True)
    for directory, batch in _by_directory(files, info.source):
        # Anything still listening is serving a *previous* batch's file list, so clear it
        # before starting ours rather than connecting to the wrong stream.
        await adb.clear_listener(info.address)
        listener = await adb.launch_listener(
            info.address,
            directory,
            [item.name for item in batch],
            port=port,
            timeout_s=timeout_s,
        )
        was_serving = False
        try:
            part = await asyncio.to_thread(
                transport.receive,
                host,
                port,
                staging,
                expected={item.name for item in batch},
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
        if not part.complete:
            # The window shut, or the operator cancelled. Standing up another listener into
            # a link that has already gone would spend what is left of the window on a
            # connection that cannot be answered.
            break
    return transferred


async def _rescue_partials(
    info: UnitInfo,
    *,
    staging: Path,
    footage: Path,
    host: str,
    port: int,
    timeout_s: int,
    unit_now: int,
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
    candidates = [
        item
        for item in orphans
        if not (footage / item.name[len(adb.PARTIAL_PREFIX) :]).exists()
        and not (footage / item.name).exists()
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
    return origin.with_api_key(override) if override else origin.backup_url()


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
    address = str(_get("unit_adb_address", ""))
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


def start_run(*, trigger: str, info: UnitInfo | None = None) -> asyncio.Task[RunResult]:
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
        run_pull(trigger=trigger, info=info), name=f"ingest-pull-{trigger}"
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
    # pending", which reads like a bug in a shutdown path that has to look clean.
    for side in list(_side_tasks):
        side.cancel()
    radios.cancel_pending()
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


async def run_pull(*, trigger: str = "auto", info: UnitInfo | None = None) -> RunResult:
    """Fetch everything the unit has that we do not. Safe to call from anywhere."""
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
    quiet: radios.RadioQuiet | None = None

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
        if not info.source:
            # Present and authorised, but its card is unmounted, reformatted or somewhere
            # unexpected. Saying "not on the network" here sent the operator looking at
            # the wiring for a problem that is inside the unit.
            result = RunResult(
                state=RunState.ERROR,
                error=info.card_error or "the head unit's memory card could not be found",
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

        if not plan.files:
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
        _fire_and_forget(report_event("started", plan=plan))

        # The car's own screen, if the operator asked for it. Also fired rather than
        # awaited: `am start` against a cold browser is not fast, and a courtesy display
        # must not be able to spend the window it is reporting on.
        #
        # Only on the first pull of a visit. Re-firing the same URL reloads the tab
        # (measured: it is how the page is refreshed at all), so sending it again for every
        # re-drain while the car sat there was the "screen keeps refreshing" the driver saw.
        # The page is live on its own, so later passes have nothing to show that it is not
        # already showing. "First of this visit" is read off the clocks rather than kept as
        # state: the last run ended before the unit came online this time, or never did.
        first_of_visit = status.since_finished() > status.online_for()
        if bool(_get("show_on_unit", False)) and first_of_visit:
            display = display_url()
            if display:
                _fire_and_forget(adb.show_url(info.address, display))
            else:
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

        # The unit's Bluetooth and hotspot share the transfer's single-stream radio, and
        # the transfer runs at that radio's measured ceiling — so both go quiet while
        # bytes move, once the unit has been here long enough to be parked rather than
        # passing. Started here, after the idle return, so a window with nothing to copy
        # never touches them; the run's `finally` puts back whatever was taken. The
        # deadline covers one listener timeout per directory the plan can have, plus
        # slack, so the on-unit watchdog cannot fire mid-run.
        if bool(_get("quiet_radios", False)):
            quiet = radios.begin_quiet(
                info.address,
                online_for=status.online_for(),
                watchdog_deadline_s=timeout_s * 2 + 120,
            )

        transferred = await _move(
            info, plan.files, staging=staging, host=host, port=port, timeout_s=timeout_s
        )

        status.set_phase(Phase.VERIFYING)
        expected = {item.name: item.size for item in plan.files}
        committed = await asyncio.to_thread(commit, staging, footage, expected)
        wanted = list(plan.files)

        if bool(_get("delete_after_verify", False)) and committed:
            # Grouped by directory the same way the transfer was: `rm` runs from the
            # directory, so a locked recording deleted against the ordinary Video path
            # would either miss or -- far worse -- match a different file that happened to
            # share its name. `_reclaim` does that grouping and, unlike the bare
            # `suppress(AdbError)` this replaced, says so in the log either way.
            by_name = {item.name: item for item in plan.files}
            await _reclaim(info, [by_name[name] for name in committed if name in by_name])

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
                info, more.files, staging=staging, host=host, port=port, timeout_s=timeout_s
            )
            _absorb(transferred, part)
            status.set_phase(Phase.VERIFYING)
            more_expected = {item.name: item.size for item in more.files}
            more_committed = await asyncio.to_thread(commit, staging, footage, more_expected)
            expected.update(more_expected)
            committed = [*committed, *more_committed]
            wanted.extend(more.files)
            if bool(_get("delete_after_verify", False)) and more_committed:
                by_name = {item.name: item for item in more.files}
                await _reclaim(info, [by_name[name] for name in more_committed if name in by_name])

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
                rescued, rescued_names, rescued_sizes = await _rescue_partials(
                    info,
                    staging=staging,
                    footage=footage,
                    host=host,
                    port=port,
                    timeout_s=timeout_s,
                    unit_now=int(time.time() + skew),
                )
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
        result = RunResult(
            state=state,
            files=len(committed),
            bytes=sum(expected[name] for name in committed),
            seconds=transferred.seconds,
            error=None if state is RunState.OK else transferred.error,
        )
        status.set_backlog(
            max(0, plan.backlog_files - len(committed)),
            max(0, plan.backlog_bytes - result.bytes),
        )
        log.info(
            "pull finished",
            state=state.value,
            files=result.files,
            megabytes=round(result.bytes / 1e6),
            throughput_mbs=result.throughput_mbs,
        )
    except adb.AdbError as exc:
        result = RunResult(state=RunState.ERROR, error=str(exc))
        log.warning("the control channel failed during a pull", error=str(exc))
    except Exception as exc:
        result = RunResult(state=RunState.ERROR, error=f"{type(exc).__name__}: {exc}")
        log.exception("the ingest run failed", error=str(exc))
    finally:
        # Radios first, before any other tidying: this is the one piece of cleanup whose
        # failure follows somebody into the car. Bounded, because the usual reason a
        # restore struggles is that the unit has driven away and every call is a timeout
        # — the marker and the on-unit watchdog carry the cases this cannot reach.
        if quiet is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(quiet.finish(), timeout=30.0)

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
        if result.state not in (RunState.IDLE, RunState.OFFLINE):
            with contextlib.suppress(Exception):
                await _persist(result, trigger)
            with contextlib.suppress(Exception):
                await report_event(
                    "finished" if result.state is RunState.OK else "error", result=result
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
