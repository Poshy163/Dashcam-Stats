"""Review stored OCR evidence once and queue confirmed repairs behind new footage."""

from __future__ import annotations

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.db.models import (
    BULK_PRIORITY,
    JobKind,
    JobState,
    ProcessingJob,
    Recording,
    RecordingState,
    StageState,
)
from app.pipeline.telemetry_anomalies import suspect_telemetry_recording_ids

log = get_logger(__name__)

# NULL means the current stored telemetry has not been checked. A subsequent rule
# version can reset these checkpoints explicitly in its migration.
CHECKED = "checked"
REPROCESSED = "reprocessed"
STILL_SUSPECT = "still_suspect"
NO_FIX = "no_fix"
SOURCE_PENDING = "source_pending"
COPY_PENDING = "copy_pending"
SOURCE_REQUESTED = "source_requested"
COPY_REQUESTED = "copy_requested"
_REQUESTED = (SOURCE_REQUESTED, COPY_REQUESTED)
REVIEW_BATCH_SIZE = 128


async def review_telemetry(
    session: AsyncSession, *, batch_size: int = REVIEW_BATCH_SIZE, queue_limit: int = 8
) -> int:
    """Return the number inspected, so the scheduler can continue a full batch promptly."""
    from app.workers import queue

    settings = get_settings_service()
    if (
        queue.is_paused()
        or not settings.get_nowait("telemetry.enabled")
        or not await settings.auto_process()
    ):
        return 0

    active = (
        select(ProcessingJob.id)
        .where(
            ProcessingJob.recording_id == Recording.id,
            ProcessingJob.state.in_([JobState.QUEUED, JobState.RUNNING]),
        )
        .exists()
    )
    eligible = (
        Recording.ignored.is_(False),
        Recording.file_missing.is_(False),
        Recording.state.in_([RecordingState.COMPLETED, RecordingState.FAILED]),
        or_(Recording.state != RecordingState.FAILED, Recording.metadata_state == StageState.DONE),
        ~active,
    )
    unchecked = Recording.telemetry_review_state.is_(None)
    ids = list(
        (
            await session.scalars(
                select(Recording.id)
                .where(
                    *eligible,
                    unchecked,
                    or_(Recording.processed_at.is_not(None), Recording.telemetry_point_count > 0),
                )
                .order_by(Recording.id)
                .limit(batch_size)
            )
        ).all()
    )
    if ids:
        suspects = await suspect_telemetry_recording_ids(session, ids)
        for subset, state in ((suspects, SOURCE_PENDING), (set(ids) - suspects, CHECKED)):
            if subset:
                await session.execute(
                    update(Recording)
                    .where(
                        Recording.id.in_(subset),
                        unchecked,
                        *eligible,
                    )
                    .values(telemetry_review_state=state)
                    .execution_options(synchronize_session=False)
                )

    inflight = await session.scalar(
        select(func.count())
        .select_from(Recording)
        .where(
            Recording.telemetry_review_state.in_(_REQUESTED),
            active,
        )
    )
    slots = max(0, queue_limit - (inflight or 0))
    if slots:
        pending = (SOURCE_PENDING, COPY_PENDING)
        candidates = (
            await session.execute(
                select(Recording.id, Recording.telemetry_review_state)
                .where(
                    *eligible,
                    Recording.telemetry_review_state.in_(pending),
                )
                .order_by(Recording.id)
                .limit(slots)
            )
        ).all()
        for rid, status in candidates:
            # Claim the checkpoint and enqueue in one transaction. Concurrent manual
            # or scanner jobs retain ownership, then a later tick can try again.
            requested = SOURCE_REQUESTED if status == SOURCE_PENDING else COPY_REQUESTED
            acquired = await session.scalar(
                update(Recording)
                .where(
                    Recording.id == rid,
                    Recording.telemetry_review_state == status,
                    *eligible,
                )
                .values(telemetry_review_state=requested)
                .returning(Recording.id)
            )
            if acquired is None:
                continue
            job = await queue.enqueue(
                session,
                rid,
                kind=JobKind.REPROCESS,
                stages=["telemetry"],
                priority=BULK_PRIORITY,
                preserve_recording_state=True,
            )
            if job is not None:
                job.result = {"telemetry_review": requested}
            log.info("requested historical OCR repair", recording_id=rid, reason=status)
    return len(ids)


async def review_request(session: AsyncSession, recording: Recording) -> str | None:
    state = recording.telemetry_review_state
    if state in _REQUESTED:
        return state
    if state in (REPROCESSED, STILL_SUSPECT, NO_FIX):
        # A retry of the same durable job can repeat telemetry after a later stage
        # failed. Its result still belongs to this review, even across a restart.
        return await session.scalar(
            select(ProcessingJob.result["telemetry_review"].as_string())
            .where(
                ProcessingJob.recording_id == recording.id,
                ProcessingJob.state == JobState.RUNNING,
            )
            .limit(1)
        )
    return None


async def supersede_review(
    session: AsyncSession, recording_id: int, stages: list[str] | None
) -> str | None:
    """Hand a waiting repair to a replacement telemetry job, or defer it behind other work."""
    from app.pipeline.orchestrator import expand_stages

    role = ProcessingJob.result["telemetry_review"].as_string()
    waiting = await session.scalar(
        select(role)
        .where(
            ProcessingJob.recording_id == recording_id,
            ProcessingJob.state == JobState.QUEUED,
            role.in_(_REQUESTED),
        )
        .limit(1)
    )
    if waiting is None or "telemetry" in expand_stages(stages):
        return waiting
    await session.execute(
        update(Recording)
        .where(Recording.id == recording_id, Recording.telemetry_review_state == waiting)
        .values(
            telemetry_review_state=SOURCE_PENDING if waiting == SOURCE_REQUESTED else COPY_PENDING
        )
    )
    return None


async def finish_telemetry_review(
    session: AsyncSession, recording: Recording, request: str | None
) -> None:
    state = request
    if state in _REQUESTED:
        # A repeated OCR error remains an identifiable result for review. Retrying
        # the same footage with the same templates would consume the queue forever.
        suspect = await suspect_telemetry_recording_ids(session, [recording.id])
        if suspect:
            recording.telemetry_review_state = STILL_SUSPECT
        elif not recording.gps_point_count:
            recording.telemetry_review_state = NO_FIX
        else:
            recording.telemetry_review_state = REPROCESSED
        log.info(
            "finished historical OCR repair",
            recording_id=recording.id,
            reason=state,
            outcome=recording.telemetry_review_state,
            fixes=recording.gps_point_count,
        )
    elif recording.telemetry_review_state in (CHECKED, REPROCESSED, STILL_SUSPECT, NO_FIX):
        # An ordinary reprocess replaced the evidence that the last review examined.
        recording.telemetry_review_state = None
