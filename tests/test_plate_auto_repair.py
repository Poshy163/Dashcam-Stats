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


async def test_v4_zero_plate_footage_is_backed_up_then_queued_for_v5(db_session, monkeypatch):
    from app.pipeline import plate_repair

    rec = historical("zero-plates-v4.ts")
    rec.plate_revision = "plates-v4"
    rec.plate_count = 0
    rec.probe_json = {"plate_repair_requested": "plates-v4:previous"}
    db_session.add(rec)
    await db_session.commit()
    backups = []
    monkeypatch.setattr(plate_repair, "ensure_plate_repair_backup", backups.append)
    assert await queue_plate_repairs(db_session) == 1
    assert backups == ["plates-v5"]
    job = (await db_session.execute(select(ProcessingJob))).scalar_one()
    assert job.stages == ["plates"]
    assert job.recording_id == rec.id


async def test_unavailable_backup_does_not_invalidate_or_enqueue(db_session, monkeypatch):
    import pytest

    from app.pipeline import plate_repair

    rec = historical("backup-failed.ts")
    db_session.add(rec)
    await db_session.commit()

    def fail(revision):
        raise OSError("backup disk full")

    monkeypatch.setattr(plate_repair, "ensure_plate_repair_backup", fail)
    with pytest.raises(OSError, match="backup disk full"):
        await queue_plate_repairs(db_session)
    assert rec.plate_revision == "plates-v3"
    assert not list((await db_session.execute(select(ProcessingJob))).scalars())


async def test_missing_source_coordinates_rebuild_detection_only_where_needed(db_session):
    from app.db.models import TrackedObject

    rec = historical("no-track-samples.ts")
    db_session.add(rec)
    await db_session.flush()
    db_session.add(
        TrackedObject(
            recording_id=rec.id,
            track_key=1,
            class_label="car",
            frame_count=10,
            confidence_max=0.9,
            confidence_avg=0.9,
            first_seen_offset_s=0,
            last_seen_offset_s=2,
            duration_s=2,
        )
    )
    await db_session.commit()
    assert await queue_plate_repairs(db_session) == 1
    job = (await db_session.execute(select(ProcessingJob))).scalar_one()
    assert job.stages == ["detection", "plates"]


async def test_plate_upgrade_backup_is_reused_without_overwriting_original(db_session):
    import sqlite3

    from app.db.backup import ensure_plate_repair_backup

    first = historical("before-upgrade.ts")
    db_session.add(first)
    await db_session.commit()
    path = ensure_plate_repair_backup("plates-v5")
    db_session.add(historical("after-upgrade.ts"))
    await db_session.commit()
    assert ensure_plate_repair_backup("plates-v5") == path
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        names = [r[0] for r in backup.execute("SELECT filename FROM recordings")]
        assert names == ["before-upgrade.ts"]


async def test_short_sparse_track_does_not_force_full_vehicle_inference(db_session):
    from app.db.models import Detection, TrackedObject

    rec = historical("short-pass.ts")
    db_session.add(rec)
    await db_session.flush()
    track = TrackedObject(
        recording_id=rec.id,
        track_key=1,
        class_label="car",
        frame_count=3,
        confidence_max=0.9,
        confidence_avg=0.9,
        first_seen_offset_s=0,
        last_seen_offset_s=0.5,
        duration_s=0.5,
        best_frame_offset_s=0.5,
        best_bbox=[0.2, 0.2, 0.5, 0.5],
    )
    db_session.add(track)
    await db_session.flush()
    db_session.add(
        Detection(
            recording_id=rec.id,
            tracked_object_id=track.id,
            class_label="car",
            t_offset_s=0,
            confidence=0.9,
            x=0.2,
            y=0.2,
            w=0.3,
            h=0.3,
        )
    )
    await db_session.commit()
    assert await queue_plate_repairs(db_session) == 1
    assert (await db_session.execute(select(ProcessingJob))).scalar_one().stages == ["plates"]
