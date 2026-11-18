import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.db.models import Camera, CameraRole, Journey, Recording, RecordingState, TelemetryPoint


async def _token(session) -> bytes:
    value = await session.scalar(text("SELECT revision FROM map_source_state WHERE id = 1"))
    assert isinstance(value, bytes) and len(value) == 16
    return value


async def test_map_revision_tracks_published_inputs_and_rollback(db_session):
    session = db_session
    now = datetime.now(UTC)
    camera = Camera(key="test-map", name="Map test", role=CameraRole.FRONT)
    journey = Journey(started_at=now, ended_at=now)
    session.add_all([camera, journey])
    await session.flush()
    rec = Recording(
        rel_path="token.ts",
        filename="token.ts",
        size_bytes=0,
        camera_id=camera.id,
        journey_id=journey.id,
        state=RecordingState.PROCESSING,
    )
    session.add(rec)
    await session.flush()
    before = await _token(session)
    fix = TelemetryPoint(
        recording_id=rec.id, journey_id=journey.id, t_offset_s=0, has_fix=True, lat=-32, lon=137
    )
    session.add(fix)
    await session.flush()
    assert await _token(session) == before

    rec.state = RecordingState.COMPLETED
    await session.flush()
    published = await _token(session)
    assert published != before

    # Filtering and visibility also change when a reader uses the camera-specific API.
    for obj, field, value in [
        (camera, "role", CameraRole.REAR),
        (camera, "key", "other-map-camera"),
        (rec, "camera_id", None),
        (journey, "motion_json", {"status": "unknown"}),
        (journey, "motion_json", {"status": "moving"}),
        (fix, "lat", -33),
        (fix, "captured_at", now),
        (fix, "speed_kmh", 41),
        (fix, "breaks_segment", True),
        (fix, "journey_id", None),
        (rec, "ignored", True),
    ]:
        prior = await _token(session)
        setattr(obj, field, value)
        await session.flush()
        assert await _token(session) != prior, field

    # A hidden clip's map sources can change throughout reprocessing.
    hidden = await _token(session)
    fix.lon = 150
    rec.filename = "renamed.ts"
    await session.flush()
    assert await _token(session) == hidden

    rec.ignored = False
    rec.processed_at = now
    await session.flush()
    await session.commit()
    restored = await _token(session)
    assert restored != hidden

    # A retired clip can still publish its finalised telemetry, and unrelated writes
    # and map fields assigned their existing values must not evict a valid snapshot.
    rec.state = RecordingState.DELETED
    await session.execute(text("UPDATE telemetry_points SET lat=lat"))
    journey.motion_json = {"status": "moving", "other_detail": 1}
    await session.flush()
    assert await _token(session) == restored

    await session.delete(fix)
    await session.flush()
    assert await _token(session) != restored
    await session.rollback()
    assert await _token(session) == restored

    # Deleting a visible parent must invalidate before its child rows cascade away.
    await session.execute(text("DELETE FROM recordings WHERE rel_path='token.ts'"))
    assert await _token(session) != restored


async def test_compatible_restore_checks_map_triggers_and_starts_new_lineage(
    db_session, app_config, tmp_path
):
    from app.db import backup
    from app.db.session import dispose_engine

    initial_token = await _token(db_session)
    snapshot = backup.create_backup()
    altered = tmp_path / "altered.db"
    altered.write_bytes(snapshot.read_bytes())
    with closing(sqlite3.connect(altered)) as cx, cx:
        cx.execute("DROP TRIGGER map_source_point_insert")
        cx.execute(
            "CREATE TRIGGER map_source_point_insert AFTER INSERT ON telemetry_points BEGIN SELECT 1; END"
        )
    with pytest.raises(ValueError, match="map invalidation triggers"):
        backup.validate_database(altered, compatible=True)

    backup.stage_restore(snapshot.read_bytes())
    await db_session.close()
    await dispose_engine()
    assert backup.apply_pending_restore()
    with closing(sqlite3.connect(app_config.db_path)) as cx:
        new_token = cx.execute("SELECT revision FROM map_source_state WHERE id = 1").fetchone()[0]
    assert len(new_token) == 16 and new_token != initial_token
