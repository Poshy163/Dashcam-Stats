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
from app.ingest import adb, transport
from app.ingest.models import DeltaPlan, RemoteFile, RunResult, RunState, UnitInfo, UnitState
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


def delta(
    remote: list[RemoteFile],
    footage: Path,
    *,
    skip_active_s: int,
    camera: str,
    already_seen: set[str] | None = None,
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
    """
    now = time.time()
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
            continue

        plan.backlog_files += 1
        plan.backlog_bytes += item.size
        if camera != "both" and not item.name.endswith(f"_{camera}.ts"):
            continue
        plan.files.append(item)

    plan.files.sort(key=lambda item: item.name)
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
            if wanted is None or path.stat().st_size != wanted:
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


def start_run(*, trigger: str) -> asyncio.Task[RunResult]:
    """Begin a pull in the background and keep hold of it."""
    global _current
    if _current is not None and not _current.done():
        return _current
    _current = asyncio.create_task(run_pull(trigger=trigger), name=f"ingest-pull-{trigger}")
    return _current


async def shutdown() -> None:
    """Stop an in-flight transfer before the application goes away.

    The receive loop runs on a worker thread, and ``asyncio.to_thread`` cannot be
    cancelled -- so cancelling the task alone would leave that thread writing into the
    staging directory and calling back into status while the database is being disposed.
    Setting the flag is what actually stops it; the reader checks it once per megabyte.
    """
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


async def run_pull(*, trigger: str = "auto") -> RunResult:
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

    try:
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

        remote = await adb.inventory(info.address, info.source)
        # Off the event loop: this stats every candidate against the footage share, which
        # is a hard NFS mount in the deployment, and a full card is ~140 files. Doing it
        # inline would freeze /health and the whole UI behind a slow server every time the
        # car appears -- the regression commit da2850a exists to prevent.
        plan = await asyncio.to_thread(
            delta,
            remote,
            footage,
            skip_active_s=int(_get("skip_active_seconds", 15)),
            camera=str(_get("camera_filter", "both")),
            already_seen=await _deliberately_removed(),
        )
        status.plan(plan)
        if not plan.files:
            result = RunResult(state=RunState.IDLE)
            return result

        log.info(
            "pulling from the head unit",
            trigger=trigger,
            files=len(plan.files),
            megabytes=round(plan.bytes / 1e6),
            backlog_files=plan.backlog_files,
        )
        # Checked here, after the delta and before a single byte moves, because this is the
        # last moment at which refusing costs nothing. Past this point the card may be
        # deleted from.
        safe, why = await _footage_is_safe_to_write(footage)
        if not safe:
            result = RunResult(state=RunState.ERROR, error=why)
            log.error("refusing to transfer into an unsafe footage directory", reason=why)
            return result

        await report_event("started", plan=plan)

        await asyncio.to_thread(_clean, staging)
        port = int(_get("data_port", 9000))
        await adb.launch_listener(
            info.address,
            info.source,
            [item.name for item in plan.files],
            port=port,
            timeout_s=int(_get("listen_timeout_s", 180)),
        )

        host = info.address.split(":", 1)[0]
        transferred = await asyncio.to_thread(
            transport.receive,
            host,
            port,
            staging,
            expected={item.name for item in plan.files},
            on_file_started=status.file_started,
            on_file_done=status.file_done,
            on_bytes=status.add_bytes,
            cancel=status.cancel_event,
        )

        expected = {item.name: item.size for item in plan.files}
        committed = await asyncio.to_thread(commit, staging, footage, expected)

        if bool(_get("delete_after_verify", False)) and committed:
            with contextlib.suppress(adb.AdbError):
                await adb.delete(info.address, info.source, committed)

        state = (
            RunState.OK
            if transferred.complete and len(committed) == len(plan.files)
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
        if result.seconds <= 0:
            result.seconds = time.monotonic() - started
        status.finish(result)
        # A run that found nothing to do, or found no car, is not history. Recording those
        # would put a row in the table every time the engine started, and bury the
        # transfers anyone actually wants to look at.
        if result.state not in (RunState.IDLE, RunState.OFFLINE) or result.error:
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
