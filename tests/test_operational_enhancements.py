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


async def test_optional_auth_challenges_everything_except_health(monkeypatch, temp_dirs):
    import httpx
    from httpx import ASGITransport

    from app.config import get_config
    from app.db.session import dispose_engine
    from app.main import create_app

    data, footage = temp_dirs
    monkeypatch.setenv("DASHCAM_DATA_DIR", str(data))
    monkeypatch.setenv("DASHCAM_FOOTAGE_DIR", str(footage))
    monkeypatch.setenv("DASHCAM_AUTH_USERNAME", "viewer")
    monkeypatch.setenv("DASHCAM_AUTH_PASSWORD", "secret")
    get_config.cache_clear()
    secured = create_app()
    transport = ASGITransport(app=secured)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        denied = await http.get("/")
        allowed = await http.get("/", auth=("viewer", "secret"))
        health = await http.get("/health")
    assert denied.status_code == 401
    assert denied.headers["www-authenticate"].startswith("Basic")
    assert allowed.status_code == 200
    assert health.status_code in {200, 503}
    await dispose_engine()
    get_config.cache_clear()
