"""Footage discovery.

The scan is designed around three constraints from the brief: a restart must not
reprocess everything, a scan must not hash hundreds of gigabytes, and a file the dashcam
is still writing must not be analysed halfway through.

The cost hierarchy is deliberate. For an unchanged file the scan does exactly one
``stat()`` and nothing else — no hashing, no ffprobe, no database write. Only a changed
size or mtime escalates to a partial fingerprint, and only a changed fingerprint escalates
to reprocessing.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.db.models import (
    JobKind,
    JobState,
    ProcessingJob,
    Recording,
    RecordingState,
    ScanRun,
    StageState,
)
from app.db.session import session_scope
from app.scanner.fingerprint import fingerprint_file
from app.scanner.naming import parse_recording_name, resolve_camera

log = get_logger(__name__)

#: Rows are flushed in batches so a scan over a very large share holds a bounded amount
#: of state regardless of how many files it finds.
_BATCH_SIZE = 200


@dataclass(slots=True)
class _Entry:
    path: Path
    rel_path: str
    size: int
    mtime_ns: int


@dataclass(slots=True)
class ScanSummary:
    scan_run_id: int | None = None
    seen: int = 0
    new: int = 0
    changed: int = 0
    unsettled: int = 0
    missing: int = 0
    errors: int = 0
    duration_s: float = 0.0
    queued: int = 0
    error_message: str | None = None
    warnings: list[str] = field(default_factory=list)


#: How far the file count may fall below the index before a scan refuses to conclude that
#: the difference is deletion. Mirrors the retention guard in :mod:`app.retention.safety`
#: on purpose: the two are answering the same question — does this directory look like the
#: one we indexed? — and they should not be able to disagree.
_DISK_VS_INDEX_MIN_RATIO = 0.5


class Scanner:
    """Walks the footage root and keeps the ``recordings`` index in step with it."""

    def __init__(self, footage_dir: Path | None = None) -> None:
        self._footage_dir_override = footage_dir
        #: Directories the walk could not read during the current scan.
        self._walk_errors = 0

    async def _reconciliation_blocked(self, summary: ScanSummary) -> str | None:
        """Why this scan must not conclude anything is missing, or ``None`` if it may.

        Marking a file missing is a destructive conclusion drawn from an absence, and an
        absence is exactly what a broken mount looks like. The footage directory is bind
        mounted, so if the host source is empty or unmounted when the container starts,
        the path still exists, the walk yields nothing, no exception is raised — and every
        indexed recording gets stamped ``file_missing`` with a ``deleted_at`` timestamp
        while the run is recorded as a clean success.

        The damage does not stop at the index. ``evaluate_safety`` counts only rows that
        are *not* flagged missing, and its consistency guard passes when the index is
        empty. So zeroing the index disarms the one retention check designed to notice a
        wrong or partial mount, using the very event it exists to detect.

        Retention already refuses to act on a directory it cannot vouch for. The scanner
        needs the same posture from the other side: reconcile deletions only from a view
        of the share that is worth trusting.
        """
        if summary.errors:
            return (
                f"{summary.errors} error(s) during the walk; the view of the share is "
                "incomplete, so nothing is being marked missing"
            )

        indexed = await self._indexed_count()
        if indexed == 0:
            return None

        if summary.seen == 0:
            return (
                f"the footage directory looks empty while {indexed} recordings are "
                "indexed; treating that as an unavailable share rather than as deletions"
            )
        if summary.seen < indexed * _DISK_VS_INDEX_MIN_RATIO:
            return (
                f"only {summary.seen} files found against {indexed} indexed; a drop this "
                "large is a mount problem far more often than it is real deletion"
            )
        return None

    @staticmethod
    async def _indexed_count() -> int:
        async with session_scope() as session:
            return int(
                (
                    await session.execute(
                        select(func.count(Recording.id)).where(Recording.file_missing.is_(False))
                    )
                ).scalar()
                or 0
            )

    async def _footage_dir(self) -> Path:
        if self._footage_dir_override is not None:
            return self._footage_dir_override
        return await get_settings_service().footage_dir()

    # -- filesystem walk ---------------------------------------------------------------

    def _walk(
        self, root: Path, extensions: frozenset[str], follow_symlinks: bool
    ) -> Iterator[_Entry]:
        """Depth-first walk yielding one entry at a time.

        ``os.scandir`` rather than ``glob``/``rglob``: scandir exposes the stat data the
        directory listing already carries, so an unchanged file costs no extra syscall,
        and streaming keeps memory flat over hundreds of thousands of files.
        """
        stack: list[Path] = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=follow_symlinks):
                                stack.append(Path(entry.path))
                                continue
                            if not entry.is_file(follow_symlinks=follow_symlinks):
                                continue
                            if os.path.splitext(entry.name)[1].lower() not in extensions:
                                continue
                            stat = entry.stat(follow_symlinks=follow_symlinks)
                            path = Path(entry.path)
                            yield _Entry(
                                path=path,
                                rel_path=path.relative_to(root).as_posix(),
                                size=stat.st_size,
                                mtime_ns=stat.st_mtime_ns,
                            )
                        except OSError:
                            # A single unreadable entry must not abort the whole scan.
                            continue
            except OSError as exc:
                # Counted, not just logged. A directory that becomes unreadable partway
                # through leaves a partial view of the share, and the caller has to know
                # that before it decides anything is missing.
                self._walk_errors += 1
                log.warning("could not read directory", path=str(current), error=str(exc))

    # -- scan --------------------------------------------------------------------------

    async def scan(self, trigger: str = "scheduled") -> ScanSummary:
        started = time.monotonic()
        summary = ScanSummary()
        self._walk_errors = 0
        settings = get_settings_service()

        root = await self._footage_dir()
        extensions = await settings.media_extensions()
        follow_symlinks = bool(settings.get_nowait("scanner.follow_symlinks"))
        settle_s = await settings.settle_seconds()
        sample_bytes = int(settings.get_nowait("scanner.fingerprint_bytes"))
        # Filenames carry local wall-clock time with no zone, exactly like the on-screen
        # overlay. Both have to be interpreted through the same configured timezone or
        # they land hours apart in the database and tear journeys in half.
        tz = await settings.timezone()

        async with session_scope() as session:
            run = ScanRun(trigger=trigger)
            session.add(run)
            await session.flush()
            summary.scan_run_id = run.id
            run_id = run.id

        if not root.exists() or not root.is_dir():
            summary.error_message = f"footage directory {root} is not available"
            log.error("scan aborted", reason=summary.error_message)
            await self._finish_run(run_id, summary, started)
            return summary

        now_ns = time.time_ns()
        settle_ns = int(settle_s * 1_000_000_000)
        seen_paths: set[str] = set()
        batch: list[_Entry] = []

        try:
            for entry in self._walk(root, extensions, follow_symlinks):
                summary.seen += 1
                seen_paths.add(entry.rel_path)
                batch.append(entry)
                if len(batch) >= _BATCH_SIZE:
                    await self._process_batch(batch, summary, now_ns, settle_ns, sample_bytes, tz)
                    batch.clear()
            if batch:
                await self._process_batch(batch, summary, now_ns, settle_ns, sample_bytes, tz)
        except Exception as exc:
            summary.errors += 1
            summary.error_message = f"{type(exc).__name__}: {exc}"
            log.exception("scan failed", error=str(exc))

        summary.errors += self._walk_errors
        skip_reason = await self._reconciliation_blocked(summary)
        if skip_reason is None:
            summary.missing = await self._mark_missing(seen_paths, summary)
        else:
            summary.error_message = summary.error_message or skip_reason
            log.warning("skipped marking files missing", reason=skip_reason, seen=summary.seen)

        await self._finish_run(run_id, summary, started)

        # Exactly one line per scan. One per file would bury everything else in the log.
        log.info(
            "footage scan complete",
            trigger=trigger,
            seen=summary.seen,
            new=summary.new,
            changed=summary.changed,
            unsettled=summary.unsettled,
            missing=summary.missing,
            errors=summary.errors,
            duration_s=round(summary.duration_s, 2),
        )
        return summary

    async def _process_batch(
        self,
        batch: list[_Entry],
        summary: ScanSummary,
        now_ns: int,
        settle_ns: int,
        sample_bytes: int,
        tz: tzinfo,
    ) -> None:
        async with session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(Recording).where(Recording.rel_path.in_([e.rel_path for e in batch]))
                    )
                )
                .scalars()
                .all()
            )
            existing = {row.rel_path: row for row in rows}

            for entry in batch:
                try:
                    await self._reconcile(
                        session,
                        entry,
                        existing.get(entry.rel_path),
                        summary,
                        now_ns,
                        settle_ns,
                        sample_bytes,
                        tz,
                    )
                except Exception as exc:
                    summary.errors += 1
                    log.warning("could not index file", file=entry.rel_path, error=str(exc))

    async def _reconcile(
        self,
        session: AsyncSession,
        entry: _Entry,
        row: Recording | None,
        summary: ScanSummary,
        now_ns: int,
        settle_ns: int,
        sample_bytes: int,
        tz: tzinfo,
    ) -> None:
        now = datetime.now(UTC)
        # A file whose mtime is inside the settle window may still be growing. The dashcam
        # writes to this share directly, so processing one now would analyse a partial
        # segment and cache the wrong duration and telemetry.
        unsettled = settle_ns > 0 and (now_ns - entry.mtime_ns) < settle_ns

        if row is None:
            parsed = parse_recording_name(entry.path.name)
            camera = await resolve_camera(session, parsed.camera_key) if parsed else None
            recording = Recording(
                rel_path=entry.rel_path,
                filename=entry.path.name,
                camera_id=camera.id if camera else None,
                size_bytes=entry.size,
                mtime_ns=entry.mtime_ns,
                started_at=parsed.started_at.replace(tzinfo=tz).astimezone(UTC) if parsed else None,
                state=RecordingState.DISCOVERED,
                first_seen_at=now,
                last_scanned_at=now,
            )
            if not unsettled:
                recording.fingerprint = await fingerprint_file(entry.path, sample_bytes)
            session.add(recording)
            summary.new += 1
            if unsettled:
                summary.unsettled += 1
            return

        row.last_scanned_at = now
        if row.file_missing:
            # Reappeared — most often a share that was briefly unmounted.
            row.file_missing = False
            row.deleted_at = None

        if unsettled:
            summary.unsettled += 1
            # Record the new size so the next scan can tell whether it is still growing.
            row.size_bytes = entry.size
            row.mtime_ns = entry.mtime_ns
            return

        if row.size_bytes == entry.size and row.mtime_ns == entry.mtime_ns:
            # The overwhelmingly common path: nothing to do, and nothing was read.
            return

        # Cheap metadata changed, so escalate to content. A touched file whose bytes are
        # identical must not trigger a re-analysis of the whole recording.
        fingerprint = await fingerprint_file(entry.path, sample_bytes)
        row.size_bytes = entry.size
        row.mtime_ns = entry.mtime_ns

        if row.fingerprint == fingerprint:
            return

        row.fingerprint = fingerprint
        row.state = RecordingState.DISCOVERED
        row.metadata_state = StageState.PENDING
        row.telemetry_state = StageState.PENDING
        row.detection_state = StageState.PENDING
        row.plate_state = StageState.PENDING
        row.error_message = None
        row.error_count = 0
        summary.changed += 1

    async def _mark_missing(self, seen: set[str], summary: ScanSummary) -> int:
        """Flag indexed files that are no longer on disk.

        Rows are kept, never deleted: the detections, plates and telemetry derived from a
        recording remain useful history after the video itself is gone.
        """
        marked = 0
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(Recording.id, Recording.rel_path).where(
                        Recording.file_missing.is_(False)
                    )
                )
            ).all()
            missing_ids = [rid for rid, rel in rows if rel not in seen]
            if missing_ids:
                await session.execute(
                    update(Recording)
                    .where(Recording.id.in_(missing_ids))
                    .values(file_missing=True, deleted_at=datetime.now(UTC))
                )
                marked = len(missing_ids)
        return marked

    async def _finish_run(self, run_id: int, summary: ScanSummary, started: float) -> None:
        summary.duration_s = time.monotonic() - started
        async with session_scope() as session:
            run = await session.get(ScanRun, run_id)
            if run is None:
                return
            run.finished_at = datetime.now(UTC)
            run.files_seen = summary.seen
            run.files_new = summary.new
            run.files_changed = summary.changed
            run.files_missing = summary.missing
            run.files_unsettled = summary.unsettled
            run.errors = summary.errors
            run.duration_s = summary.duration_s
            run.error_message = summary.error_message


async def queue_unprocessed(session: AsyncSession, limit: int | None = None) -> int:
    """Create processing jobs for recordings that need work.

    Skips anything that already has a queued or running job, so calling this repeatedly —
    which the scheduler does — never stacks duplicates.
    """
    active = select(ProcessingJob.recording_id).where(
        ProcessingJob.state.in_([JobState.QUEUED, JobState.RUNNING]),
        ProcessingJob.recording_id.isnot(None),
    )

    stmt = (
        select(Recording)
        .where(
            Recording.ignored.is_(False),
            Recording.file_missing.is_(False),
            Recording.state.in_(
                [
                    RecordingState.DISCOVERED,
                    RecordingState.METADATA_EXTRACTED,
                ]
            ),
            Recording.id.notin_(active),
        )
        .order_by(Recording.started_at.desc().nullslast())
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    recordings = (await session.execute(stmt)).scalars().all()
    for recording in recordings:
        session.add(
            ProcessingJob(
                recording_id=recording.id,
                kind=JobKind.PROCESS,
                stages=None,  # None means "whatever this recording still needs"
                state=JobState.QUEUED,
            )
        )
        recording.state = RecordingState.QUEUED
    return len(recordings)


async def pending_count(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(
                select(func.count(Recording.id)).where(
                    Recording.ignored.is_(False),
                    Recording.file_missing.is_(False),
                    Recording.state.in_(
                        [
                            RecordingState.DISCOVERED,
                            RecordingState.METADATA_EXTRACTED,
                            RecordingState.QUEUED,
                        ]
                    ),
                )
            )
        ).scalar()
        or 0
    )
