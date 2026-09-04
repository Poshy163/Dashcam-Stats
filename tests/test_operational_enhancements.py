"""Regression coverage for the analysis/operations release."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.backup import create_backup, validate_database
from app.db.models import (
    Camera,
    Journey,
    Recording,
    RecordingState,
    StageState,
    TelemetryPoint,
)
from app.pipeline.orchestrator import invalidate_recordings
from app.pipeline.revisions import CURRENT_REVISIONS, INVALIDATED_REVISION
from app.pipeline.telemetry_quality import quality_rollup, recover_from_paired_camera
from app.retention.planner import _is_event


async def test_reanalysis_invalidates_dependent_views_without_deleting_rows(db_session):
    now = datetime.now(UTC)
    journey = Journey(
        started_at=now, ended_at=now + timedelta(minutes=2), duration_s=120, has_gps=True
    )
    db_session.add(journey)
    await db_session.flush()
    recording = Recording(
        rel_path="revision.ts",
        filename="revision.ts",
        size_bytes=1,
        state=RecordingState.COMPLETED,
        journey_id=journey.id,
        metadata_state=StageState.DONE,
        telemetry_state=StageState.DONE,
        detection_state=StageState.DONE,
        plate_state=StageState.DONE,
        metadata_revision=CURRENT_REVISIONS["metadata"],
        telemetry_revision=CURRENT_REVISIONS["telemetry"],
        detection_revision=CURRENT_REVISIONS["detection"],
        plate_revision=CURRENT_REVISIONS["plates"],
        has_gps=True,
        gps_point_count=1,
    )
    db_session.add(recording)
    await db_session.flush()
    db_session.add(
        TelemetryPoint(
            recording_id=recording.id,
            journey_id=journey.id,
            t_offset_s=0,
            captured_at=now,
            lat=-34.8,
            lon=138.6,
            has_fix=True,
        )
    )
    await db_session.flush()

    selected = await invalidate_recordings(db_session, [recording.id], ["telemetry"])
    await db_session.refresh(recording)
    await db_session.refresh(journey)

    assert selected == ("telemetry", "detection", "plates", "summarise")
    assert recording.telemetry_revision == INVALIDATED_REVISION
    assert recording.detection_revision == INVALIDATED_REVISION
    assert recording.plate_revision == INVALIDATED_REVISION
    assert recording.has_gps is False
    assert journey.has_gps is False
    # Replacement remains non-destructive until its stage successfully writes.
    assert (await db_session.execute(select(TelemetryPoint))).scalars().all()


def test_quality_rollup_separates_real_loss_ocr_and_rejection():
    rows = [
        {"t_offset_s": 0, "has_fix": True, "quality_json": {"gps_status": "valid"}},
        {"t_offset_s": 1, "has_fix": False, "quality_json": {"gps_status": "no_fix"}},
        {"t_offset_s": 2, "has_fix": False, "quality_json": {"gps_status": "missing"}},
        {"t_offset_s": 3, "has_fix": False, "quality_json": {"gps_status": "rejected"}},
    ]
    gaps, longest, _problems, no_fix, ocr_gap, rejected = quality_rollup(rows)
    assert (gaps, longest) == (1, 3.0)
    assert (no_fix, ocr_gap, rejected) == (1, 1, 1)


async def test_paired_camera_recovers_ocr_hole_but_not_explicit_no_fix(db_session):
    cameras = list((await db_session.execute(select(Camera).order_by(Camera.id))).scalars())
    now = datetime.now(UTC)
    front = Recording(
        rel_path="front-pair.ts",
        filename="front-pair.ts",
        size_bytes=1,
        camera_id=cameras[0].id,
        started_at=now,
        ended_at=now + timedelta(seconds=3),
        telemetry_state=StageState.DONE,
    )
    rear = Recording(
        rel_path="rear-pair.ts",
        filename="rear-pair.ts",
        size_bytes=1,
        camera_id=cameras[1].id,
        started_at=now,
        ended_at=now + timedelta(seconds=3),
        telemetry_state=StageState.DONE,
    )
    db_session.add_all([front, rear])
    await db_session.flush()
    db_session.add_all(
        [
            TelemetryPoint(
                recording_id=front.id,
                t_offset_s=0,
                captured_at=now,
                has_fix=False,
                quality_json={"gps_status": "missing", "problems": ["position unreadable"]},
            ),
            TelemetryPoint(
                recording_id=front.id,
                t_offset_s=1,
                captured_at=now + timedelta(seconds=1),
                has_fix=False,
                quality_json={"gps_status": "no_fix", "problems": []},
            ),
            TelemetryPoint(
                recording_id=rear.id,
                t_offset_s=0,
                captured_at=now,
                lat=-34.8,
                lon=138.6,
                has_fix=True,
                quality_json={"gps_status": "valid"},
            ),
            TelemetryPoint(
                recording_id=rear.id,
                t_offset_s=1,
                captured_at=now + timedelta(seconds=1),
                lat=-34.81,
                lon=138.61,
                has_fix=True,
                quality_json={"gps_status": "valid"},
            ),
        ]
    )
    await db_session.flush()

    assert await recover_from_paired_camera(db_session, rear) == 1
    points = list(
        (
            await db_session.execute(
                select(TelemetryPoint)
                .where(TelemetryPoint.recording_id == front.id)
                .order_by(TelemetryPoint.t_offset_s)
            )
        ).scalars()
    )
    assert points[0].has_fix is True
    assert points[0].quality_json["gps_source"] == "paired_camera"
    assert points[1].has_fix is False
    assert points[1].quality_json["gps_status"] == "no_fix"


async def test_a_recovered_point_is_not_evidence_for_another_recovery(db_session):
    """A copy is not a second camera's decode, and copying it again makes it immortal.

    Watched on the live library. A latitude whose sign was lost to OCR was written into one
    camera and recovered into the other. The parser was then fixed and every recording in
    the journey reprocessed -- after which *zero* points read the bad coordinate directly
    and 1,168 still carried it as a recovery, reseeding each other from a value no overlay
    had produced for hours. Reprocessing cannot clear what is copied back in from a
    neighbour, so the copy must not qualify as a donor.
    """
    cameras = list((await db_session.execute(select(Camera).order_by(Camera.id))).scalars())
    now = datetime.now(UTC)
    front = Recording(
        rel_path="front-loop.ts",
        filename="front-loop.ts",
        size_bytes=1,
        camera_id=cameras[0].id,
        started_at=now,
        ended_at=now + timedelta(seconds=3),
        telemetry_state=StageState.DONE,
    )
    rear = Recording(
        rel_path="rear-loop.ts",
        filename="rear-loop.ts",
        size_bytes=1,
        camera_id=cameras[1].id,
        started_at=now,
        ended_at=now + timedelta(seconds=3),
        telemetry_state=StageState.DONE,
    )
    db_session.add_all([front, rear])
    await db_session.flush()
    db_session.add_all(
        [
            # The front camera cannot read its own overlay.
            TelemetryPoint(
                recording_id=front.id,
                t_offset_s=0,
                captured_at=now,
                has_fix=False,
                quality_json={"gps_status": "missing", "problems": ["position unreadable"]},
            ),
            # The rear one has a position, but it is itself a copy from a third recording
            # -- exactly what a reprocessed neighbour looks like mid-repair.
            TelemetryPoint(
                recording_id=rear.id,
                t_offset_s=0,
                captured_at=now,
                lat=34.7971,
                lon=138.7044,
                has_fix=True,
                quality_json={"gps_status": "recovered", "gps_source": "paired_camera"},
            ),
        ]
    )
    await db_session.flush()

    assert await recover_from_paired_camera(db_session, front) == 0

    points = list(
        (
            await db_session.execute(
                select(TelemetryPoint).where(TelemetryPoint.recording_id == front.id)
            )
        ).scalars()
    )
    assert points[0].has_fix is False, "a copy of a copy is not evidence"
    assert points[0].lat is None


async def test_a_donor_from_before_the_source_field_still_counts(db_session):
    """Points predating ``gps_source`` are genuine decodes and must stay usable."""
    cameras = list((await db_session.execute(select(Camera).order_by(Camera.id))).scalars())
    now = datetime.now(UTC)
    front = Recording(
        rel_path="front-legacy.ts",
        filename="front-legacy.ts",
        size_bytes=1,
        camera_id=cameras[0].id,
        started_at=now,
        ended_at=now + timedelta(seconds=3),
        telemetry_state=StageState.DONE,
    )
    rear = Recording(
        rel_path="rear-legacy.ts",
        filename="rear-legacy.ts",
        size_bytes=1,
        camera_id=cameras[1].id,
        started_at=now,
        ended_at=now + timedelta(seconds=3),
        telemetry_state=StageState.DONE,
    )
    db_session.add_all([front, rear])
    await db_session.flush()
    db_session.add_all(
        [
            TelemetryPoint(
                recording_id=front.id,
                t_offset_s=0,
                captured_at=now,
                has_fix=False,
                quality_json={"gps_status": "missing", "problems": ["position unreadable"]},
            ),
            TelemetryPoint(
                recording_id=rear.id,
                t_offset_s=0,
                captured_at=now,
                lat=-34.8,
                lon=138.6,
                has_fix=True,
                quality_json={"gps_status": "valid"},
            ),
        ]
    )
    await db_session.flush()

    assert await recover_from_paired_camera(db_session, front) == 1


def test_protected_and_tagged_recordings_are_retention_events():
    assert _is_event(Recording(rel_path="p.ts", filename="p.ts", protected=True))
    assert _is_event(Recording(rel_path="e.ts", filename="e.ts", event_type="harsh_braking"))
    assert not _is_event(Recording(rel_path="n.ts", filename="n.ts"))


async def test_online_backup_is_a_valid_database(db_session):
    await db_session.flush()
    backup = create_backup()
    assert backup.is_file() and backup.stat().st_size > 0
    validate_database(backup)


async def test_smart_reprocess_queues_only_the_requested_outdated_stage(client, db_session):
    now = datetime.now(UTC)
    current = Recording(
        rel_path="current.ts",
        filename="current.ts",
        size_bytes=1,
        state=RecordingState.COMPLETED,
        processed_at=now,
        metadata_revision=CURRENT_REVISIONS["metadata"],
        telemetry_revision=CURRENT_REVISIONS["telemetry"],
        detection_revision=CURRENT_REVISIONS["detection"],
        plate_revision=CURRENT_REVISIONS["plates"],
    )
    outdated = Recording(
        rel_path="outdated.ts",
        filename="outdated.ts",
        size_bytes=1,
        state=RecordingState.COMPLETED,
        processed_at=now,
        metadata_revision=CURRENT_REVISIONS["metadata"],
        telemetry_revision="telemetry-v1",
        detection_revision=CURRENT_REVISIONS["detection"],
        plate_revision=CURRENT_REVISIONS["plates"],
    )
    db_session.add_all([current, outdated])
    await db_session.commit()

    response = await client.post(
        "/api/reprocess",
        json={"stages": ["telemetry"], "only_outdated": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["queued"] == 1
    await db_session.refresh(current)
    await db_session.refresh(outdated)
    assert current.telemetry_revision == CURRENT_REVISIONS["telemetry"]
    assert outdated.telemetry_revision == INVALIDATED_REVISION
