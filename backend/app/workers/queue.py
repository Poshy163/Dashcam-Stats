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
from app.core.settings_service import get_settings_service
from app.db.models import JobKind, JobState, ProcessingJob, Recording, RecordingState

log = get_logger(__name__)

#: Retry backoff, indexed by attempt. Transient faults are usually a busy share or a
#: momentarily unavailable mount, which clear in seconds to minutes.
_BACKOFF_S = (30, 300, 1800)

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
    if recording_id is not None and not force:
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
    next_queued = (
        select(ProcessingJob.id)
        .where(
            ProcessingJob.state == JobState.QUEUED,
            (ProcessingJob.not_before.is_(None)) | (ProcessingJob.not_before <= now),
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
) -> None:
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
    session: AsyncSession, job: ProcessingJob, message: str, *, permanent: bool = False
) -> None:
    """Fail a job, scheduling a retry unless it cannot possibly succeed."""
    job.error_message = message[:2000]
    job.finished_at = datetime.now(UTC)
    job.stage_current = None

    retriable = not permanent and job.attempts < job.max_attempts
    if retriable:
        delay = _BACKOFF_S[min(job.attempts - 1, len(_BACKOFF_S) - 1)]
        job.state = JobState.QUEUED
        job.not_before = datetime.now(UTC) + timedelta(seconds=delay)
        job.worker_id = None
        log.info(
            "job will retry",
            job_id=job.id,
            attempt=job.attempts,
            max_attempts=job.max_attempts,
            retry_in_s=delay,
        )
    else:
        job.state = JobState.FAILED
        if permanent:
            # Spending three attempts on a zero-byte file helps nobody.
            log.info("job failed permanently", job_id=job.id, error=message[:200])

    await session.flush()


async def cancel(session: AsyncSession, job_id: int) -> bool:
    job = await session.get(ProcessingJob, job_id)
    if job is None or job.state not in (JobState.QUEUED, JobState.RUNNING):
        return False
    job.state = JobState.CANCELLED
    job.finished_at = datetime.now(UTC)
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
        .execution_options(synchronize_session=False)
    )
    count = int(result.rowcount or 0)
    if count:
        log.warning("reclaimed stale jobs", count=count, timeout_s=timeout)
    await session.flush()
    return count


async def stats(session: AsyncSession) -> dict[str, object]:
    rows = (
        await session.execute(
            select(ProcessingJob.state, func.count(ProcessingJob.id)).group_by(ProcessingJob.state)
        )
    ).all()
    counts = {state.value: int(count) for state, count in rows}

    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
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
