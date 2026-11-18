from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import (
    BULK_PRIORITY,
    JobKind,
    JobState,
    ProcessingJob,
    Recording,
    RecordingState,
    StageState,
    TelemetryPoint,
)
from app.pipeline import telemetry_repair as repair
from app.pipeline.telemetry_quality import mark_stale_paired_copies
from app.workers import queue


def recording(name, state=RecordingState.COMPLETED, **kw):
    return Recording(
        filename=name, rel_path=name, state=state, processed_at=datetime.now(UTC), **kw
    )


async def test_bounded_review_keeps_history_visible_and_revisits_backlog(db_session, monkeypatch):
    records = [recording(f"review-{i}.ts") for i in range(7)]
    records[3].file_missing = True
    records[4].ignored = True
    records[5].state = RecordingState.INVALID
    records[6].state = RecordingState.FAILED
    records[6].metadata_state = StageState.DONE
    db_session.add_all(records)
    await db_session.flush()
    active = ProcessingJob(recording_id=records[2].id, state=JobState.QUEUED, stages=["plates"])
    db_session.add(active)
    await db_session.flush()
    evaluated = []

    async def classify(session, ids):
        evaluated.extend(ids)
        return set(ids) - {records[0].id}

    monkeypatch.setattr(repair, "suspect_telemetry_recording_ids", classify)
    assert await repair.review_telemetry(db_session, batch_size=2, queue_limit=1) == 2
    await db_session.commit()
    await db_session.refresh(records[1])
    assert records[1].state == RecordingState.COMPLETED
    assert records[1].telemetry_review_state == repair.SOURCE_REQUESTED
    job = (
        await db_session.scalars(
            select(ProcessingJob).where(ProcessingJob.recording_id == records[1].id)
        )
    ).one()
    assert (job.kind, job.priority, job.stages) == (JobKind.REPROCESS, BULK_PRIORITY, ["telemetry"])
    assert await repair.review_telemetry(db_session, queue_limit=1) == 1
    await db_session.refresh(records[6])
    assert records[6].telemetry_review_state == repair.SOURCE_PENDING
    assert active.state == JobState.QUEUED
    assert await repair.review_telemetry(db_session, queue_limit=1) == 0
    # An exhausted request is retained for the existing failed-job UI. Repeated
    # scheduler runs enqueue other suspects without retrying this one forever.
    job.state = JobState.FAILED
    active.state = JobState.COMPLETED
    await db_session.commit()
    assert await repair.review_telemetry(db_session, queue_limit=1) == 1
    assert (
        len(
            (
                await db_session.scalars(
                    select(ProcessingJob).where(ProcessingJob.recording_id == records[1].id)
                )
            ).all()
        )
        == 1
    )
    assert evaluated == [records[i].id for i in (0, 1, 6, 2)]
    assert all(records[i].telemetry_review_state is None for i in (3, 4, 5))


async def test_source_success_defers_only_stale_proven_camera_copies(db_session, monkeypatch):
    now = datetime.now(UTC).replace(microsecond=0)
    source, stale, current, independent = [recording(f"pair-{i}.ts") for i in range(4)]
    db_session.add_all([source, stale, current, independent])
    await db_session.flush()
    for rec, lon, provenance in [
        (source, 130, {"gps_source": "direct"}),
        (stale, 131, {"gps_source": "paired_camera", "paired_recording_id": source.id}),
        (current, 130, {"gps_source": "paired_camera", "paired_recording_id": source.id}),
        (independent, 131, {"gps_source": "direct"}),
    ]:
        db_session.add(
            TelemetryPoint(
                recording_id=rec.id,
                captured_at=now,
                t_offset_s=1.0,
                has_fix=True,
                lat=-35,
                lon=lon,
                gps_quality="valid" if rec in (source, independent) else "interpolated",
                quality_json=provenance,
            )
        )
        rec.telemetry_review_state = repair.CHECKED
    source.telemetry_review_state = repair.SOURCE_REQUESTED
    await db_session.flush()
    await mark_stale_paired_copies(
        db_session, source, now - timedelta(seconds=2), now + timedelta(seconds=2)
    )
    assert stale.telemetry_review_state == repair.COPY_PENDING
    assert current.telemetry_review_state == independent.telemetry_review_state == repair.CHECKED

    async def still_suspect(session, ids):
        return {source.id} if source.id in ids else set()

    monkeypatch.setattr(repair, "suspect_telemetry_recording_ids", still_suspect)
    await repair.finish_telemetry_review(db_session, source, repair.SOURCE_REQUESTED)
    assert source.telemetry_review_state == repair.STILL_SUSPECT
    await repair.review_telemetry(db_session)
    await db_session.refresh(stale)
    assert stale.telemetry_review_state == repair.COPY_REQUESTED
    stale.gps_point_count = 1
    await repair.finish_telemetry_review(db_session, stale, repair.COPY_REQUESTED)
    assert stale.telemetry_review_state == repair.REPROCESSED
    independent.telemetry_review_state = repair.SOURCE_REQUESTED
    independent.gps_point_count = 0
    await repair.finish_telemetry_review(db_session, independent, repair.SOURCE_REQUESTED)
    assert independent.telemetry_review_state == repair.NO_FIX
    assert await repair.review_telemetry(db_session) == 0


async def test_paused_queue_does_not_consume_review_checkpoint(db_session, monkeypatch):
    rec = recording("paused.ts")
    db_session.add(rec)
    await db_session.flush()
    monkeypatch.setattr(queue, "is_paused", lambda: True)
    assert await repair.review_telemetry(db_session) == 0
    assert rec.telemetry_review_state is None


async def test_superseding_manual_work_and_retries_keep_the_review_durable(db_session):
    rec = recording("superseded.ts", telemetry_review_state=repair.SOURCE_PENDING)
    broken_metadata = recording(
        "unprobed.ts", state=RecordingState.FAILED, metadata_state=StageState.FAILED
    )
    db_session.add_all([rec, broken_metadata])
    await db_session.flush()
    assert await repair.review_telemetry(db_session) == 0
    manual = await queue.enqueue(
        db_session, rec.id, kind=JobKind.REPROCESS, stages=["plates"], force=True
    )
    await db_session.refresh(rec)
    assert rec.telemetry_review_state == repair.SOURCE_PENDING
    assert manual.result is None
    manual.state = JobState.COMPLETED
    rec.state = RecordingState.COMPLETED
    await db_session.flush()
    await repair.review_telemetry(db_session)
    replacement = await queue.enqueue(
        db_session, rec.id, kind=JobKind.REPROCESS, stages=["telemetry"], force=True
    )
    assert replacement.result == {"telemetry_review": repair.SOURCE_REQUESTED}
    replacement.state = JobState.RUNNING
    rec.telemetry_review_state = repair.NO_FIX
    await db_session.flush()
    assert await repair.review_request(db_session, rec) == repair.SOURCE_REQUESTED
    replacement.state = JobState.COMPLETED
    await db_session.flush()
    assert await repair.review_request(db_session, rec) is None
    await repair.finish_telemetry_review(db_session, rec, None)
    assert rec.telemetry_review_state is None
    assert broken_metadata.telemetry_review_state is None


async def test_successful_redecode_without_fixes_clears_old_position(db_session, monkeypatch):
    from unittest.mock import AsyncMock

    from app.osd.engine import TelemetryResult, TelemetrySample
    from app.pipeline import stages

    record = recording("no-fix.ts", has_gps=True, gps_point_count=1, start_lat=-35, start_lon=130)
    db_session.add(record)
    await db_session.flush()
    sample = TelemetrySample(
        t_offset_s=0,
        captured_at=None,
        lat=None,
        lon=None,
        has_fix=False,
        speed_kmh=None,
        heading_deg=None,
        ocr_confidence=1.0,
        raw_text="",
    )
    result = TelemetryResult(samples=[sample], frames_read=1)
    monkeypatch.setattr(stages, "_get_templates", AsyncMock(return_value=True))
    monkeypatch.setattr(stages.TelemetryExtractor, "extract", AsyncMock(return_value=result))
    await stages.stage_telemetry(db_session, record)
    assert (record.has_gps, record.gps_point_count, record.start_lat, record.start_lon) == (
        False,
        0,
        None,
        None,
    )
