from datetime import UTC, datetime

from sqlalchemy import select

from app.core.settings_service import get_settings_service
from app.db.models import (
    BULK_PRIORITY,
    JobState,
    Plate,
    PlateObservation,
    ProcessingJob,
    Recording,
    RecordingState,
    StageState,
)
from app.pipeline.plate_repair import queue_plate_repairs, validation_key
from app.pipeline.revisions import CURRENT_REVISIONS


def historical(name, **kwargs):
    return Recording(
        rel_path=name,
        filename=name,
        size_bytes=100,
        state=RecordingState.COMPLETED,
        processed_at=datetime.now(UTC),
        detection_state=StageState.DONE,
        plate_state=StageState.DONE,
        plate_revision="plates-v3",
        **kwargs,
    )


async def test_update_repair_is_bounded_idempotent_and_preserves_existing_work(db_session):
    records = [historical(f"history-{i}.ts") for i in range(5)]
    records[2].file_missing = True
    records[3].processed_at = None
    records[4].plate_revision = CURRENT_REVISIONS["plates"]
    records[4].probe_json = {"plate_validation_key": validation_key()}
    db_session.add_all(records)
    await db_session.flush()
    active = ProcessingJob(
        recording_id=records[1].id, state=JobState.RUNNING, stages=["detection", "plates"]
    )
    db_session.add(active)
    await db_session.flush()
    assert await queue_plate_repairs(db_session, limit=1) == 1
    assert await queue_plate_repairs(db_session, limit=1) == 0
    jobs = list(
        (await db_session.execute(select(ProcessingJob).order_by(ProcessingJob.id))).scalars()
    )
    assert len(jobs) == 2
    assert active.state == JobState.RUNNING
    assert jobs[-1].recording_id == records[0].id
    assert jobs[-1].stages == ["plates"]
    assert jobs[-1].priority == BULK_PRIORITY
    # Exhausted retries stay failed across repeated update/startup checks.
    jobs[-1].state = JobState.FAILED
    await db_session.flush()
    assert await queue_plate_repairs(db_session) == 0
    # A changed regional profile legitimately requests a new validation attempt.
    await db_session.commit()
    await get_settings_service().set("plates.region", "AU-SA")
    assert await queue_plate_repairs(db_session) == 2
    assert active.state == JobState.RUNNING


async def test_current_but_inconsistent_observation_is_revalidated(db_session):
    recording = historical("inconsistent.ts")
    recording.plate_revision = CURRENT_REVISIONS["plates"]
    recording.probe_json = {"plate_validation_key": validation_key()}
    plate = Plate(normalised_text="ABC123", display_text="ABC123")
    db_session.add_all([recording, plate])
    await db_session.flush()
    db_session.add(
        PlateObservation(
            recording_id=recording.id,
            plate_id=plate.id,
            t_offset_s=1,
            raw_text="ABC123",
            normalised_text="ABC123",
            ocr_confidence=0.99,
            detection_confidence=0.99,
            bbox={"mirrored": True},
        )
    )
    await db_session.flush()
    assert await queue_plate_repairs(db_session) == 1
    assert await queue_plate_repairs(db_session) == 0


async def test_automatic_repair_can_be_disabled(db_session):
    db_session.add(historical("disabled.ts"))
    await db_session.commit()
    await get_settings_service().set("plates.auto_revalidate", False)
    assert await queue_plate_repairs(db_session) == 0
