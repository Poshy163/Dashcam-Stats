"""Durable work queue over the ``processing_jobs`` table.

A separate queue service (Redis, Celery) would buy nothing here: the queue is one row per
recording, and the property that actually matters is that it survives a container restart
mid-job. A database table gives that for free.

The claim is a conditional UPDATE rather than a SELECT followed by an UPDATE, so two
workers racing for the same row cannot both win.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service, local_midnight_utc
from app.db.models import JobKind, JobState, ProcessingJob, Recording, RecordingState

log = get_logger(__name__)

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


def is_paused() -> bool:
    return _paused


def pause() -> None:
    global _paused
    _paused = True
    log.info("processing queue paused")


def resume() -> None:
    global _paused
    _paused = False
    log.info("processing queue resumed")


async def enqueue(
    session: AsyncSession,
    recording_id: int | None,
    *,
    kind: JobKind = JobKind.PROCESS,
    stages: list[str] | None = None,
    priority: int = 100,
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
            await session.execute(
                update(ProcessingJob)
                .where(
                    ProcessingJob.recording_id == recording_id,
                    ProcessingJob.state == JobState.QUEUED,
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

    next_queued = (
        select(ProcessingJob.id)
        .where(
            ProcessingJob.state == JobState.QUEUED,
            (ProcessingJob.not_before.is_(None)) | (ProcessingJob.not_before <= now),
            ~already_running,
        )
        .order_by(ProcessingJob.priority.asc(), ProcessingJob.queued_at.asc())
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
            )
            .returning(ProcessingJob.id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()

    if claimed is None:
        return None

    await session.flush()
    return await session.get(ProcessingJob, claimed)


async def heartbeat(
    session: AsyncSession,
    job_id: int,
    *,
    progress: float | None = None,
    stage: str | None = None,
    speed: float | None = None,
    decoder: str | None = None,
    device: str | None = None,
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
    if speed is not None:
        job.speed_realtime = speed
    if decoder is not None:
        job.decoder = decoder
    if device is not None:
        job.inference_device = device
    await session.flush()


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
    """Requeue every failed job, resetting its attempt counter."""
    result = await session.execute(
        update(ProcessingJob)
        .where(ProcessingJob.state == JobState.FAILED)
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
    rows = (
        await session.execute(
            select(ProcessingJob.state, func.count(ProcessingJob.id)).group_by(ProcessingJob.state)
        )
    ).all()
    counts = {state.value: int(count) for state, count in rows}

    # The user's day, not UTC's — see local_midnight_utc. Adelaide is nine and a half
    # hours ahead, so "Completed today" reset itself at half past nine in the morning.
    midnight = local_midnight_utc()
    completed_today = int(
        (
            await session.execute(
                select(func.count(ProcessingJob.id)).where(
                    ProcessingJob.state == JobState.COMPLETED,
                    ProcessingJob.finished_at >= midnight,
                )
            )
        ).scalar()
        or 0
    )

    return {
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "failed": counts.get("failed", 0),
        "completed": counts.get("completed", 0),
        "cancelled": counts.get("cancelled", 0),
        "completed_today": completed_today,
        "paused": _paused,
    }
