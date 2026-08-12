"""Durable work queue over the ``processing_jobs`` table.

A separate queue service (Redis, Celery) would buy nothing here: the queue is one row per
recording, and the property that actually matters is that it survives a container restart
mid-job. A database table gives that for free.

The claim is a conditional UPDATE rather than a SELECT followed by an UPDATE, so two
workers racing for the same row cannot both win.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.core.logging import get_logger
from app.core.settings_service import get_settings_service, local_midnight_utc
from app.db.models import (
    BULK_PRIORITY,
    NEW_FOOTAGE_PRIORITY,
    JobKind,
    JobState,
    ProcessingJob,
    Recording,
    RecordingState,
    StageState,
)

log = get_logger(__name__)

# The priority tiers are imported rather than defined here: the scanner sets the same
# values and cannot import this module without a cycle, so they are declared beside the
# column they describe. See the note in `app.db.models`.

#: What a job says when a queue reset retired it. It is not a failure and never was one:
#: the work it described has been superseded by the run that replaced it.
RESET_REASON = "cleared by a full reprocess reset"

#: Retry backoff, indexed by attempt. Transient faults are usually a busy share or a
#: momentarily unavailable mount, which clear in seconds to minutes.
_BACKOFF_S = (30, 300, 1800)

#: Backoff for a retry that does not spend an attempt -- see :func:`fail`. Short, because
#: the thing being waited out is another writer finishing, not a mount coming back.
_CONTENTION_BACKOFF_S = 20

#: Ceiling on those free retries. Without one, a database that is permanently locked --
#: an external process holding the file, a full disk -- would requeue the same job every
#: twenty seconds until the container is restarted, with nothing ever appearing as failed.
MAX_CONTENTION_RETRIES = 6

_paused = False

#: When the queue was last wiped and rebuilt. Every count the Queue page reports is scoped
#: to it, which is what makes a reset read as a fresh run rather than as the old one with
#: new rows appended: yesterday's failures, cancellations and completions belong to the run
#: that was replaced, and a run that has just started has none of any of them.
_reset_at: datetime | None = None


def _pause_marker() -> Path:
    """A local-volume marker makes an operator's pause survive container replacement."""
    return get_config().data_dir / ".queue-paused"


def _reset_marker() -> Path:
    """The reset epoch, kept beside the pause flag and for the same reason.

    A restart must not resurrect the counters of a run the user explicitly discarded.
    """
    return get_config().data_dir / ".queue-reset"


def restore_reset_epoch() -> datetime | None:
    """Restore the durable reset epoch before anything reports a queue count."""
    global _reset_at
    _reset_at = None
    try:
        raw = _reset_marker().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("could not restore the queue reset epoch", error=str(exc))
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("the queue reset marker does not hold a timestamp", value=raw[:64])
        return None
    _reset_at = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    log.info("processing queue counts are scoped to the last reset", since=_reset_at.isoformat())
    return _reset_at


def reset_epoch() -> datetime | None:
    """When the current queue run began, or None if the queue has never been reset."""
    return _reset_at


def record_reset(moment: datetime) -> None:
    """Open a new queue run. Jobs queued before *moment* no longer count towards it."""
    global _reset_at
    _reset_at = moment
    try:
        marker = _reset_marker()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(moment.isoformat(), encoding="utf-8")
    except OSError as exc:
        log.warning("could not persist the queue reset epoch", error=str(exc))


def _current_run():
    """Restrict a job query to the run in progress.

    Expressed against ``queued_at`` rather than ``finished_at`` on purpose: the jobs the
    reset itself cancelled finish *at* the epoch, so filtering on when they ended would
    count every one of them as a cancellation belonging to the new run.
    """
    epoch = _reset_at
    return true() if epoch is None else ProcessingJob.queued_at >= epoch


def restore_pause_state() -> bool:
    """Restore the durable pause flag before workers are allowed to claim jobs."""
    global _paused
    try:
        _paused = _pause_marker().exists()
    except OSError as exc:
        _paused = False
        log.warning("could not restore processing queue pause state", error=str(exc))
    if _paused:
        log.info("processing queue remains paused after restart")
    return _paused


def is_paused() -> bool:
    return _paused


def pause() -> None:
    global _paused
    _paused = True
    try:
        marker = _pause_marker()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
    except OSError as exc:
        log.warning("could not persist processing queue pause", error=str(exc))
    log.info("processing queue paused")


def resume() -> None:
    global _paused
    _paused = False
    try:
        _pause_marker().unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not clear processing queue pause", error=str(exc))
    log.info("processing queue resumed")


async def enqueue(
    session: AsyncSession,
    recording_id: int | None,
    *,
    kind: JobKind = JobKind.PROCESS,
    stages: list[str] | None = None,
    priority: int = NEW_FOOTAGE_PRIORITY,
    force: bool = False,
) -> ProcessingJob | None:
    """Queue work, refusing to stack duplicates for the same recording.

    ``force`` is for explicit user-requested reprocessing, where an existing queued job
    should be superseded rather than silently swallowing the request.
    """
    if recording_id is not None:
        if not force:
            existing = (
                (
                    await session.execute(
                        select(ProcessingJob).where(
                            ProcessingJob.recording_id == recording_id,
                            ProcessingJob.state.in_([JobState.QUEUED, JobState.RUNNING]),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None:
                return existing
        else:
            # Supersede, which is what "force" has always meant, rather than stack. Two
            # presses of "reprocess everything" used to leave two queued jobs per
            # recording and the whole library decoded twice; a third press, three times.
            # A job already RUNNING is left alone -- cancelling it would throw away work
            # in progress, and `claim_next` will not let the replacement start until it
            # has finished.
            # FAILED as well as QUEUED.
            #
            # Superseding only the queued job left every previous failure sitting in the
            # table for good, and `stats` counts by state -- so one recording showed up as
            # both "failed" and "queued" at once, and the failure count on the queue page
            # never came down no matter how many times the library was reprocessed. Those
            # rows describe an attempt that has just been explicitly replaced; they are
            # history, and history is what `result` and the logs are for.
            await session.execute(
                update(ProcessingJob)
                .where(
                    ProcessingJob.recording_id == recording_id,
                    ProcessingJob.state.in_([JobState.QUEUED, JobState.FAILED]),
                )
                .values(
                    state=JobState.CANCELLED,
                    finished_at=datetime.now(UTC),
                    worker_id=None,
                    error_message="superseded by a newer request",
                )
                .execution_options(synchronize_session=False)
            )

    settings = get_settings_service()
    job = ProcessingJob(
        recording_id=recording_id,
        kind=kind,
        stages=stages,
        state=JobState.QUEUED,
        priority=priority,
        max_attempts=int(settings.get_nowait("processing.retry_max_attempts")) + 1,
    )
    session.add(job)

    if recording_id is not None:
        recording = await session.get(Recording, recording_id)
        if recording is not None and recording.state not in (
            RecordingState.PROCESSING,
            RecordingState.QUEUED,
        ):
            recording.state = RecordingState.QUEUED

    await session.flush()
    return job


async def claim_next(session: AsyncSession, worker_id: str) -> ProcessingJob | None:
    """Atomically take the next runnable job, or None."""
    if _paused:
        return None

    now = datetime.now(UTC)

    # One statement, deliberately. Reading the candidate id and then updating it in a
    # separate statement takes a read snapshot first and needs to upgrade to a write lock
    # afterwards; if any other connection wrote in between, SQLite fails that upgrade
    # *immediately* with SQLITE_BUSY_SNAPSHOT, which busy_timeout does not wait out. Under
    # two workers plus the scheduler that quietly abandoned jobs. Selecting the row inside
    # the UPDATE keeps the whole claim atomic, so there is no snapshot to invalidate.
    #
    # The `state == QUEUED` predicate still does the mutual exclusion: whichever worker
    # lands first flips the row, and the other matches zero rows and asks again.
    # A second job for a recording that is already running is not runnable yet, whichever
    # worker gets to it. `enqueue(force=True)` creates exactly that -- a bulk reprocess
    # queues every recording, including the one a worker is halfway through -- and without
    # this predicate the other worker claimed it and two runs decoded, deleted and
    # rewrote the same recording's rows at once. Both then wrote a result, the second over
    # the first, and whichever delete landed between the other's delete and its inserts
    # took those inserts with it.
    #
    # Expressed as a correlated NOT EXISTS inside the same statement rather than as a
    # filter applied afterwards, for the same reason the claim itself is one statement:
    # anything read separately is a snapshot that can be stale by the time the UPDATE runs.
    busy = ProcessingJob.__table__.alias("busy")
    already_running = (
        select(busy.c.id)
        .where(
            busy.c.state == JobState.RUNNING,
            busy.c.recording_id.is_not(None),
            busy.c.recording_id == ProcessingJob.recording_id,
        )
        .exists()
    )
    visible_recordings = select(Recording.id).where(Recording.ignored.is_(False))

    # Oldest footage first, by the moment the camera recorded it.
    #
    # Not by `queued_at`, which is what this used to do and which says nothing useful: a
    # bulk reprocess stamps nine hundred jobs within the same second, so they came out in
    # whatever order the rows were inserted. Working through a library chronologically is
    # what a person watching it expects, it makes progress legible -- the date reached is
    # the progress -- and it means an interrupted run leaves a contiguous processed span
    # rather than holes scattered through the archive.
    #
    # Recordings whose start time could not be established sort last rather than first:
    # an unparsed filename is not evidence of age, and putting unknowns at the front would
    # let a handful of oddities delay everything real behind them.
    next_queued = (
        select(ProcessingJob.id)
        .outerjoin(Recording, Recording.id == ProcessingJob.recording_id)
        .where(
            ProcessingJob.state == JobState.QUEUED,
            (ProcessingJob.not_before.is_(None)) | (ProcessingJob.not_before <= now),
            ~already_running,
            or_(
                ProcessingJob.recording_id.is_(None),
                ProcessingJob.recording_id.in_(visible_recordings),
            ),
        )
        .order_by(
            ProcessingJob.priority.asc(),
            Recording.started_at.is_(None),
            Recording.started_at.asc(),
            ProcessingJob.queued_at.asc(),
        )
        .limit(1)
        .scalar_subquery()
    )

    claimed = (
        await session.execute(
            update(ProcessingJob)
            .where(
                ProcessingJob.id == next_queued,
                ProcessingJob.state == JobState.QUEUED,
            )
            .values(
                state=JobState.RUNNING,
                worker_id=worker_id,
                started_at=now,
                heartbeat_at=now,
                attempts=ProcessingJob.attempts + 1,
                progress=0.0,
                finished_at=None,
                error_message=None,
            )
            .returning(ProcessingJob.id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()

    if claimed is None:
        return None

    await session.flush()
    job = await session.get(ProcessingJob, claimed)
    if job is not None and job.recording_id is not None:
        # A retry is active work, not a current failure. Keeping the previous attempt's
        # text on both rows made the Queue and recording pages claim ffprobe was failing
        # while the replacement attempt was already progressing successfully.
        await session.execute(
            update(Recording)
            .where(Recording.id == job.recording_id)
            .values(error_message=None)
            .execution_options(synchronize_session=False)
        )
    return job


async def heartbeat(
    session: AsyncSession,
    job_id: int,
    *,
    progress: float | None = None,
    stage: str | None = None,
    speed: float | None = None,
    decoder: str | None = None,
    device: str | None = None,
    resource: str | None = None,
) -> JobState | None:
    """Publish progress, and report the job's state back to the caller.

    The state is returned because this is the pool's only regular tick, so it is also how
    a cancellation request reaches a run that is already in flight.
    """
    values: dict[str, object] = {"heartbeat_at": datetime.now(UTC)}
    if progress is not None:
        values["progress"] = max(0.0, min(1.0, progress))
    if stage is not None:
        values["stage_current"] = stage
    if speed is not None:
        values["speed_realtime"] = speed
    if decoder is not None:
        values["decoder"] = decoder
    if device is not None:
        values["inference_device"] = device
    if resource is not None:
        values["resource_state"] = resource

    await session.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == job_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return (
        await session.execute(select(ProcessingJob.state).where(ProcessingJob.id == job_id))
    ).scalar_one_or_none()


async def complete(
    session: AsyncSession,
    job: ProcessingJob,
    result: dict | None = None,
    *,
    speed: float | None = None,
    decoder: str | None = None,
    device: str | None = None,
) -> None:
    """Finish a job, recording how it ran as well as that it ran.

    The three diagnostics are written here rather than left to the heartbeat, which is the
    only thing that used to write them and is cancelled the moment the job ends. Realtime
    speed is worse still: it is not even known until the last stage returns, by which point
    the next heartbeat is a race against the ``finally`` that kills it. On a real library
    that race was lost every time — ``speed_realtime`` was empty on all 200 jobs sampled
    and ``decoder`` on 195 — so the Queue page had columns for throughput, decoder and
    inference device that were blank for every recording ever processed.
    """
    job.state = JobState.COMPLETED
    job.finished_at = datetime.now(UTC)
    job.progress = 1.0
    job.stage_current = None
    job.result = result
    job.error_message = None
    job.resource_state = None
    if speed is not None:
        job.speed_realtime = speed
    if decoder is not None:
        job.decoder = decoder
    if device is not None:
        job.inference_device = device
    await session.flush()
    await _follow_up_after_thumbnail(session, job)


async def _follow_up_after_thumbnail(session: AsyncSession, job: ProcessingJob) -> None:
    """Hand a recording on to its analysis job once the thumbnail pass has released it.

    The thumbnail-first pass is a *phase*, not a second queue. A recording holds exactly one
    job at a time and this is the transition between them, which is the whole reason the
    analysis job is created here rather than alongside the thumbnail job during the rebuild:
    queueing both up front would put every recording in the queue twice, double the waiting
    count, and give the claim two rows per recording to keep apart.

    It runs on every terminal outcome, not only success. A recording whose thumbnail could
    not be produced still has to be analysed -- and the analysis pass probes the file
    properly, so it is also what reaches a real verdict about footage this pass could only
    say "no usable frame" about.

    Creating the successor is part of the same transaction as finishing the thumbnail job,
    so there is no instant in which the recording has no job. ``enqueue`` refuses to stack,
    so a retried outcome write cannot produce a second one either.
    """
    if job.kind is not JobKind.THUMBNAIL or job.recording_id is None:
        return
    recording = await session.get(Recording, job.recording_id)
    if recording is None or recording.ignored:
        return
    await enqueue(
        session,
        job.recording_id,
        # A recording that has never been analysed is not being *re*-processed, whatever
        # queued it. The kind is what the Queue page and the scanner's repair both read.
        kind=JobKind.REPROCESS if recording.processed_at is not None else JobKind.PROCESS,
        # The thumbnail job carries the stage selection the run was started with; it has no
        # use for it itself, and this is where it was being kept for.
        stages=list(job.stages) if job.stages else None,
        priority=BULK_PRIORITY,
    )


async def fail(
    session: AsyncSession,
    job: ProcessingJob,
    message: str,
    *,
    permanent: bool = False,
    transient: bool = False,
) -> None:
    """Fail a job, scheduling a retry unless it cannot possibly succeed.

    ``transient`` marks a failure that was contention rather than a fault in the work --
    in practice SQLite's write lock held by another writer. Those retries are *free*: the
    attempt counter is wound back, so a run of collisions cannot add up to a permanently
    failed recording. Two of the recordings in this library were marked failed after four
    attempts having never once been given a reason that had anything to do with the
    footage; each attempt died on the first ``DELETE`` of a stage while the scheduler's
    journey rebuild held the lock.

    A separate ceiling stops that becoming an unbounded loop -- see
    ``MAX_CONTENTION_RETRIES``.
    """
    job.error_message = message[:2000]
    job.finished_at = datetime.now(UTC)
    job.stage_current = None
    job.worker_id = None

    if permanent:
        job.state = JobState.FAILED
        # Spending three attempts on a zero-byte file helps nobody.
        log.info("job failed permanently", job_id=job.id, error=message[:200])
        await session.flush()
        await _follow_up_after_thumbnail(session, job)
        return

    if transient:
        # Refund the attempt. `attempts` is incremented by the claim, so decrementing it
        # here leaves the counter exactly where it was before this run -- and `result`
        # carries how many refunds have been granted, so the ceiling is durable across
        # restarts rather than living in the worker's memory.
        granted = int((job.result or {}).get("contention_retries", 0)) + 1
        if granted <= MAX_CONTENTION_RETRIES:
            job.attempts = max(0, job.attempts - 1)
            job.state = JobState.QUEUED
            job.not_before = datetime.now(UTC) + timedelta(seconds=_CONTENTION_BACKOFF_S)
            job.result = {**(job.result or {}), "contention_retries": granted}
            log.warning(
                "the database was busy; requeuing without spending an attempt",
                job_id=job.id,
                contention_retries=granted,
                of=MAX_CONTENTION_RETRIES,
                retry_in_s=_CONTENTION_BACKOFF_S,
            )
            await session.flush()
            return
        log.error(
            "the database has stayed busy across every free retry; failing normally",
            job_id=job.id,
            contention_retries=granted,
        )

    if job.attempts < job.max_attempts:
        delay = _BACKOFF_S[min(max(0, job.attempts - 1), len(_BACKOFF_S) - 1)]
        job.state = JobState.QUEUED
        job.not_before = datetime.now(UTC) + timedelta(seconds=delay)
        # A job waiting for another go is not a job that is failing. Keeping the last
        # attempt's text in `error_message` put a decoder abort from yesterday next to a
        # `queued` badge on every row of the queue page -- reading as a live fault on work
        # that has not been tried yet, and staying there for as long as the backlog took.
        # `claim_next` clears the same field on the *recording* for exactly this reason.
        # The text is kept where history belongs, and the attempt counter already says
        # that something went wrong before.
        job.result = {**(job.result or {}), "last_error": message[:2000]}
        job.error_message = None
        log.info(
            "job will retry",
            job_id=job.id,
            attempt=job.attempts,
            max_attempts=job.max_attempts,
            retry_in_s=delay,
        )
    else:
        job.state = JobState.FAILED

    await session.flush()
    if job.state is JobState.FAILED:
        await _follow_up_after_thumbnail(session, job)


async def clear(session: AsyncSession, *, reason: str = RESET_REASON) -> dict[str, int]:
    """Empty the queue: nothing waiting, nothing running, nothing failed.

    Retired rather than deleted, and the distinction is not squeamishness. ``log_entries``
    references ``processing_jobs`` with ``ON DELETE CASCADE`` and foreign keys are enforced,
    so a ``DELETE`` here would take every log line those jobs ever wrote with it -- which is
    the record of what the previous run actually did, and the one thing a reset has no
    business destroying. Cancelling empties the queue just as completely: a cancelled job is
    not claimable, not retryable, and counts towards nothing.

    ``FAILED`` goes with them for the same reason ``enqueue(force=True)`` retires it -- those
    rows describe attempts that have just been explicitly replaced. Leaving them behind is
    what made the failure count on the Queue page immovable no matter how many times the
    library was reprocessed.

    A ``RUNNING`` job is cancelled here too, which is what stops its worker writing an
    outcome: the pool re-reads the row before recording one and declines when it finds the
    job cancelled. Stopping the run *itself* is the caller's job -- see
    ``WorkerPool.abort_active`` -- and must happen before any replacement work is queued.

    Returns the number of jobs cleared, keyed by the state they were in.
    """
    retired = (JobState.QUEUED, JobState.RUNNING, JobState.FAILED)
    counts = {
        state.value: int(count)
        for state, count in (
            await session.execute(
                select(ProcessingJob.state, func.count(ProcessingJob.id))
                .where(ProcessingJob.state.in_(retired))
                .group_by(ProcessingJob.state)
            )
        ).all()
    }
    await session.execute(
        update(ProcessingJob)
        .where(ProcessingJob.state.in_(retired))
        .values(
            state=JobState.CANCELLED,
            finished_at=datetime.now(UTC),
            worker_id=None,
            progress=0.0,
            stage_current=None,
            resource_state=None,
            not_before=None,
            heartbeat_at=None,
            error_message=reason,
        )
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    if counts:
        log.info("cleared the processing queue", **counts)
    return counts


async def cancel(session: AsyncSession, job_id: int) -> bool:
    """Cancel a job, whether it is queued or already running.

    For a running job this only records the request; the worker pool picks it up on its
    next heartbeat and stops the run. The worker will then decline to write a result over
    the top of it, which it previously did — so a cancelled job came back a few minutes
    later as COMPLETED, or as QUEUED with a retry pending.
    """
    job = await session.get(ProcessingJob, job_id)
    if job is None or job.state not in (JobState.QUEUED, JobState.RUNNING):
        return False
    job.state = JobState.CANCELLED
    job.finished_at = datetime.now(UTC)
    job.worker_id = None
    await session.flush()
    return True


async def retry_failed(session: AsyncSession) -> int:
    """Requeue visible failed jobs, never resurrecting blacklisted footage."""
    visible_recordings = select(Recording.id).where(Recording.ignored.is_(False))
    result = await session.execute(
        update(ProcessingJob)
        .where(
            ProcessingJob.state == JobState.FAILED,
            or_(
                ProcessingJob.recording_id.is_(None),
                ProcessingJob.recording_id.in_(visible_recordings),
            ),
        )
        .values(
            state=JobState.QUEUED,
            attempts=0,
            not_before=None,
            error_message=None,
            worker_id=None,
            finished_at=None,
            # Including the free-retry budget the queue grants for database contention:
            # "retry" from the UI means start again, not resume with whatever allowances
            # the previous run had already spent.
            result=None,
        )
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    return int(result.rowcount or 0)


async def reconcile_legacy_contention_failures(session: AsyncSession) -> tuple[int, int]:
    """Retire or requeue lock failures created before contention retries existed.

    Old versions charged ``database is locked`` against the ordinary attempt budget. Those
    rows remain FAILED forever even after a later bulk re-analysis has superseded them, so
    the Queue page continues reporting an active fault that was fixed days ago.

    Only rows with no retry metadata are legacy. A current job that exhausts its bounded
    contention allowance carries ``contention_retries`` in ``result`` and must stay failed;
    otherwise every container restart would silently defeat that ceiling.

    Returns ``(requeued, superseded)``.
    """
    candidates = (
        (
            await session.execute(
                select(ProcessingJob)
                .where(
                    ProcessingJob.state == JobState.FAILED,
                    ProcessingJob.result.is_(None),
                    ProcessingJob.error_message.ilike("%database is locked%"),
                )
                .order_by(ProcessingJob.id.desc())
            )
        )
        .scalars()
        .all()
    )
    requeued = superseded = 0
    for job in candidates:
        recording = (
            await session.get(Recording, job.recording_id) if job.recording_id is not None else None
        )
        newer = None
        if job.recording_id is not None:
            newer = (
                await session.execute(
                    select(ProcessingJob.id)
                    .where(
                        ProcessingJob.recording_id == job.recording_id,
                        ProcessingJob.id > job.id,
                        ProcessingJob.state != JobState.CANCELLED,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()

        if newer is not None or (recording is not None and recording.ignored):
            job.state = JobState.CANCELLED
            job.worker_id = None
            job.stage_current = None
            job.error_message = (
                "superseded by a newer processing job after a historical database lock"
                if newer is not None
                else "recording was blacklisted after a historical database lock"
            )
            if (
                recording is not None
                and recording.error_message
                and "database is locked" in recording.error_message.lower()
                and recording.state in (RecordingState.QUEUED, RecordingState.PROCESSING)
            ):
                recording.error_message = None
            superseded += 1
            continue

        job.state = JobState.QUEUED
        job.attempts = 0
        job.progress = 0.0
        job.stage_current = None
        job.not_before = None
        job.error_message = None
        job.worker_id = None
        job.started_at = None
        job.finished_at = None
        if recording is not None:
            recording.state = RecordingState.QUEUED
            recording.error_message = None
        requeued += 1

    if candidates:
        log.info(
            "reconciled historical database lock failures",
            found=len(candidates),
            requeued=requeued,
            superseded=superseded,
        )
    await session.flush()
    return requeued, superseded


async def reconcile_misclassified_probe_crashes(session: AsyncSession) -> int:
    """Restore recordings hidden when an aborted ffprobe was called source damage.

    Releases before the probe classifier was tightened marked every ``FFmpegError`` as
    permanent. The live Intel/FFmpeg build can exit with SIGABRT and no output, so one
    driver race blacklisted the recording and made its failed job invisible. Retrying all
    rows with this exact historical message is safe: a genuinely bad file will be probed
    again and receive a conclusive permanent verdict, while a valid file continues.
    """
    recordings = list(
        (
            await session.execute(
                select(Recording).where(
                    Recording.state == RecordingState.INVALID,
                    Recording.file_missing.is_(False),
                    Recording.error_message.ilike("ffprobe produced no output for %"),
                )
            )
        ).scalars()
    )
    if not recordings:
        return 0

    for recording in recordings:
        probe = dict(recording.probe_json) if isinstance(recording.probe_json, dict) else {}
        failure = probe.get("probe_failure")
        if isinstance(failure, dict) and failure.get("permanent") is True:
            # A newer release reached a positive source verdict. Its short display error
            # can match the legacy text, but the stored evidence says not to resurrect it.
            continue
        policy = probe.get("damaged_policy")
        if isinstance(policy, dict):
            probe.pop("damaged_policy", None)
        # This flag was added by the automatic hide, not by a successful probe. Preserve
        # genuine warning evidence should an old row happen to carry any.
        if not probe.get("source_unusable") and not probe.get("warnings"):
            probe.pop("source_damaged", None)
        recording.probe_json = probe
        recording.ignored = False
        recording.state = RecordingState.QUEUED
        recording.metadata_state = StageState.PENDING
        recording.error_message = None

        failed_job = (
            (
                await session.execute(
                    select(ProcessingJob)
                    .where(
                        ProcessingJob.recording_id == recording.id,
                        ProcessingJob.state == JobState.FAILED,
                        ProcessingJob.error_message.ilike("ffprobe produced no output for %"),
                    )
                    .order_by(ProcessingJob.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if failed_job is not None:
            failed_job.state = JobState.QUEUED
            failed_job.attempts = 0
            failed_job.progress = 0.0
            failed_job.stage_current = None
            failed_job.not_before = None
            failed_job.error_message = None
            failed_job.worker_id = None
            failed_job.started_at = None
            failed_job.finished_at = None
            failed_job.result = None
        else:
            await enqueue(
                session,
                recording.id,
                kind=JobKind.REPROCESS if recording.processed_at is not None else JobKind.PROCESS,
                stages=None,
                priority=BULK_PRIORITY
                if recording.processed_at is not None
                else NEW_FOOTAGE_PRIORITY,
            )

    restored = sum(
        recording.state is RecordingState.QUEUED and recording.error_message is None
        for recording in recordings
    )
    if restored:
        log.warning(
            "restored recordings misclassified after ffprobe aborts",
            recordings=restored,
        )
    await session.flush()
    return restored


#: Marks a recording whose hide has already been reconsidered, so this runs once.
_MEDIA_REVIEW_KEY = "media_failure_reviewed"


async def reconcile_media_failure_hides(session: AsyncSession) -> int:
    """Give back recordings hidden because the media stack was failing, not the file.

    ``stage_inspect`` classified a recording unusable whenever no offset produced a
    thumbnail, and the damaged-footage policy turned that into ``ignored``. During the
    Intel driver crash loop every decode was being killed by an aborting FFmpeg, so valid
    footage collected that verdict -- and an ignored recording is filtered out by
    ``claim_next``, ``retry_failed`` and every bulk requeue, which means nothing would ever
    have looked at it again. One sixty-second recording on the live library was in exactly
    this state.

    Reviewing them is safe and self-correcting: a genuinely unusable file simply earns the
    same verdict on the next run, this time from a machine that is working. The marker
    stops that becoming a loop -- each row is reconsidered once, not after every restart.
    """
    rows = list(
        (
            await session.execute(
                select(Recording).where(
                    Recording.ignored.is_(True),
                    Recording.file_missing.is_(False),
                    func.json_extract(Recording.probe_json, "$.source_unusable") == 1,
                    func.json_extract(Recording.probe_json, f"$.{_MEDIA_REVIEW_KEY}").is_(None),
                )
            )
        ).scalars()
    )
    if not rows:
        return 0

    for recording in rows:
        probe = dict(recording.probe_json) if isinstance(recording.probe_json, dict) else {}
        probe.pop("damaged_policy", None)
        probe.pop("source_damaged", None)
        probe.pop("source_unusable", None)
        # Kept so a second restart does not undo a hide that a healthy run re-applied.
        probe[_MEDIA_REVIEW_KEY] = True
        recording.probe_json = probe
        recording.ignored = False
        recording.error_message = None
        recording.state = RecordingState.QUEUED
        recording.metadata_state = StageState.PENDING
        # The thumbnail is the thing that could not be produced, so let it be attempted
        # again rather than inheriting "there isn't one" as a settled fact.
        recording.thumbnail_path = None
        await enqueue(
            session,
            recording.id,
            kind=JobKind.REPROCESS if recording.processed_at is not None else JobKind.PROCESS,
            stages=None,
            priority=BULK_PRIORITY if recording.processed_at is not None else NEW_FOOTAGE_PRIORITY,
        )

    log.warning(
        "restored recordings hidden while the media stack was failing",
        recordings=len(rows),
    )
    await session.flush()
    return len(rows)


async def reclaim_stale(session: AsyncSession) -> int:
    """Return jobs whose worker stopped reporting back to the queue.

    Without this a container killed mid-job would leave that recording RUNNING forever
    and it would never be picked up again.
    """
    timeout = await get_settings_service().job_heartbeat_timeout_s()
    cutoff = datetime.now(UTC) - timedelta(seconds=timeout)

    result = await session.execute(
        update(ProcessingJob)
        .where(
            ProcessingJob.state == JobState.RUNNING,
            (ProcessingJob.heartbeat_at.is_(None)) | (ProcessingJob.heartbeat_at < cutoff),
        )
        .values(state=JobState.QUEUED, worker_id=None, progress=0.0, stage_current=None)
        .returning(ProcessingJob.recording_id)
        .execution_options(synchronize_session=False)
    )
    recording_ids = [rid for rid in result.scalars() if rid is not None]
    count = len(recording_ids)
    if count:
        # The recording has to come back with the job. Reclaiming only the job left the
        # recording stamped PROCESSING, and nothing anywhere puts that right: the queue
        # would run the job again, but if the job were also lost -- cancelled, pruned, or
        # never re-created -- the recording sat in PROCESSING forever, invisible to
        # `queue_unprocessed`, which looks for DISCOVERED and METADATA_EXTRACTED.
        await session.execute(
            update(Recording)
            .where(
                Recording.id.in_(recording_ids),
                Recording.state == RecordingState.PROCESSING,
            )
            .values(state=RecordingState.QUEUED)
            .execution_options(synchronize_session=False)
        )
        log.warning("reclaimed stale jobs", count=count, timeout_s=timeout)
    await session.flush()
    return count


async def reclaim_interrupted(session: AsyncSession) -> int:
    """Immediately return every RUNNING job owned by the previous app process.

    This is called only while the worker pool is starting or after all of its tasks have
    stopped, so no live worker can own these rows. Waiting for the heartbeat timeout here
    left freshly claimed jobs looking active for five minutes after a native GPU crash;
    the replacement process then claimed more work and the Queue page showed four running
    jobs for two workers.
    """
    result = await session.execute(
        update(ProcessingJob)
        .where(ProcessingJob.state == JobState.RUNNING)
        .values(
            state=JobState.QUEUED,
            worker_id=None,
            attempts=func.max(0, ProcessingJob.attempts - 1),
            progress=0.0,
            stage_current=None,
            resource_state=None,
            not_before=None,
            started_at=None,
            heartbeat_at=None,
            speed_realtime=None,
            decoder=None,
            inference_device=None,
        )
        .returning(ProcessingJob.recording_id)
        .execution_options(synchronize_session=False)
    )
    recording_ids = [rid for rid in result.scalars() if rid is not None]
    if recording_ids:
        await session.execute(
            update(Recording)
            .where(
                Recording.id.in_(recording_ids),
                Recording.state == RecordingState.PROCESSING,
            )
            .values(state=RecordingState.QUEUED)
            .execution_options(synchronize_session=False)
        )
        log.warning("reclaimed jobs interrupted by application restart", count=len(recording_ids))
    await session.flush()
    return len(recording_ids)


async def release_stranded_recordings(session: AsyncSession) -> int:
    """Un-stick recordings left PROCESSING with no job to finish them.

    A process killed between claiming a job and writing its outcome leaves the pair out of
    step, and the two halves are healed by different things: the job by ``reclaim_stale``,
    the recording by nothing at all. So a hard restart during a run stranded that recording
    permanently -- ``queue_unprocessed`` does not look at PROCESSING, the queue had no job
    for it, and the only visible symptom was a recording that showed as processing with no
    worker and never changed again.

    Demoted rather than requeued directly: the state it goes back to is the one the
    scanner would have left it in, so the ordinary queueing path picks it up and there is
    no second place that decides what work a recording needs.
    """
    active = select(ProcessingJob.recording_id).where(
        ProcessingJob.state.in_([JobState.QUEUED, JobState.RUNNING]),
        ProcessingJob.recording_id.isnot(None),
    )
    result = await session.execute(
        update(Recording)
        .where(
            Recording.state.in_([RecordingState.PROCESSING, RecordingState.QUEUED]),
            Recording.id.notin_(active),
        )
        .values(state=RecordingState.DISCOVERED)
        .execution_options(synchronize_session=False)
    )
    count = int(result.rowcount or 0)
    if count:
        log.warning("released recordings that were left mid-processing", count=count)
    await session.flush()
    return count


async def stats(session: AsyncSession) -> dict[str, object]:
    """Everything the Queue page counts, scoped to the run in progress.

    The scope is what makes "reprocess all footage" produce a page that reads as a fresh
    run. Without it the reset cleared the queue and the tiles above it went on reporting the
    previous run's failures and completions -- true of the table, and the opposite of what
    the user had just asked for. Jobs from before the last reset are still in the table and
    still in the list below; they are simply no longer part of this run's arithmetic.
    """
    visible = or_(ProcessingJob.recording_id.is_(None), Recording.ignored.is_(False))
    rows = (
        await session.execute(
            select(ProcessingJob.state, func.count(ProcessingJob.id))
            .outerjoin(Recording, Recording.id == ProcessingJob.recording_id)
            .where(visible, _current_run())
            .group_by(ProcessingJob.state)
        )
    ).all()
    counts = {state.value: int(count) for state, count in rows}

    # What is left of the thumbnail-first pass. Reported separately because it is a phase
    # of the run rather than a queue of its own, and because "1,400 waiting" means something
    # quite different when the next hour of it is thumbnails.
    thumbnails = int(
        (
            await session.execute(
                select(func.count(ProcessingJob.id))
                .outerjoin(Recording, Recording.id == ProcessingJob.recording_id)
                .where(
                    visible,
                    _current_run(),
                    ProcessingJob.kind == JobKind.THUMBNAIL,
                    ProcessingJob.state.in_([JobState.QUEUED, JobState.RUNNING]),
                )
            )
        ).scalar()
        or 0
    )

    # The user's day, not UTC's — see local_midnight_utc. Adelaide is nine and a half
    # hours ahead, so "Completed today" reset itself at half past nine in the morning.
    midnight = local_midnight_utc()
    completed_today = int(
        (
            await session.execute(
                select(func.count(ProcessingJob.id)).where(
                    ProcessingJob.state == JobState.COMPLETED,
                    ProcessingJob.finished_at >= midnight,
                    _current_run(),
                    or_(
                        ProcessingJob.recording_id.is_(None),
                        ProcessingJob.recording_id.in_(
                            select(Recording.id).where(Recording.ignored.is_(False))
                        ),
                    ),
                )
            )
        ).scalar()
        or 0
    )

    recent = (
        await session.execute(
            select(ProcessingJob.started_at, ProcessingJob.finished_at)
            .where(
                ProcessingJob.state == JobState.COMPLETED,
                ProcessingJob.started_at.is_not(None),
                ProcessingJob.finished_at.is_not(None),
                ProcessingJob.finished_at >= datetime.now(UTC) - timedelta(hours=24),
                ProcessingJob.recording_id.is_not(None),
                # Thumbnails are seconds and analyses are minutes, so averaging the two
                # together would produce a throughput figure describing neither and an
                # estimate for the analysis backlog built out of thumbnail timings.
                ProcessingJob.kind != JobKind.THUMBNAIL,
            )
            .order_by(ProcessingJob.finished_at.desc())
            .limit(100)
        )
    ).all()
    durations = [
        (finished - started).total_seconds()
        for started, finished in recent
        if started is not None and finished is not None and finished > started
    ]
    queued = counts.get("queued", 0)
    running = counts.get("running", 0)
    throughput = None
    eta_minutes = None
    if durations:
        average_s = sum(durations) / len(durations)
        workers = int(get_settings_service().get_nowait("processing.max_workers"))
        throughput = round(workers * 3600.0 / average_s, 1)
        if throughput > 0:
            # The analysis backlog, which is what the average above measures. Counting the
            # thumbnail pass in it would report hours for work that takes minutes.
            eta_minutes = round((max(0, queued - thumbnails) / throughput) * 60.0, 1)

    return {
        "queued": queued,
        "running": running,
        "failed": counts.get("failed", 0),
        "completed": counts.get("completed", 0),
        "cancelled": counts.get("cancelled", 0),
        "completed_today": completed_today,
        "thumbnails_pending": thumbnails,
        # Which pass the run is in, so the page can say so rather than leaving the user to
        # infer it from a queue full of jobs that are not what they look like.
        "phase": "thumbnails" if thumbnails else ("analysis" if queued or running else "idle"),
        "paused": _paused,
        "throughput_per_hour": throughput,
        "estimated_minutes_remaining": eta_minutes,
    }
