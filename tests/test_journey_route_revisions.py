from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import create_engine, event, text

from app.db.models import Camera, CameraRole, Journey, Recording, RecordingState, TelemetryPoint
from app.db.session import alembic_config


def test_interrupted_group_migration_can_be_retried(app_config):
    engine = create_engine(app_config.sync_sqlalchemy_url)
    config = alembic_config()

    def migrate(action, target):
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            action(config, target)

    def check_stamp(expected):
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == expected

    def interrupt_at(statement):
        def stop(connection, cursor, sql, parameters, context, executemany):
            if sql.startswith(statement):
                raise RuntimeError("simulated interruption")

        return stop

    try:
        migrate(command.upgrade, "0030")
        for action, target, before, statement in (
            (command.upgrade, "0031", "0030", "ALTER TABLE map_source_state ADD"),
            (command.downgrade, "0030", "0031", "ALTER TABLE journeys DROP"),
        ):
            stop = interrupt_at(statement)
            event.listen(engine, "before_cursor_execute", stop)
            with pytest.raises(RuntimeError, match="simulated interruption"):
                migrate(action, target)
            event.remove(engine, "before_cursor_execute", stop)
            check_stamp(before)
            migrate(action, target)
            check_stamp(target)
    finally:
        engine.dispose()


async def _manifest(session):
    await session.flush()
    rows = await session.execute(
        text("""
        SELECT NULL AS journey_id, unassigned_route_revision FROM map_source_state WHERE id=1
        UNION ALL SELECT id, route_revision FROM journeys ORDER BY journey_id
    """)
    )
    result = dict(rows.all())
    assert all(isinstance(revision, bytes) and len(revision) == 16 for revision in result.values())
    return result


async def test_only_changed_route_groups_advance(db_session):
    session = db_session
    now = datetime.now(UTC)
    camera = Camera(key="route-revision-camera", name="Test front", role=CameraRole.FRONT)
    session.add_all(
        [
            camera,
            Journey(id=0, started_at=now, ended_at=now),
            Journey(id=-1, started_at=now, ended_at=now),
        ]
    )
    await session.flush()
    clip = Recording(
        filename="route.ts",
        rel_path="route.ts",
        state=RecordingState.PROCESSING,
        camera_id=camera.id,
        journey_id=0,
    )
    session.add(clip)
    await session.flush()

    async def changed(prior, *ids):
        current = await _manifest(session)
        assert {i for i in prior if current[i] != prior[i]} == set(ids)
        return current

    versions = await _manifest(session)
    fix = TelemetryPoint(
        recording_id=clip.id, journey_id=-1, t_offset_s=0, lat=-35, lon=138, has_fix=True
    )
    session.add(fix)
    await changed(versions)  # Hidden ingest is covered once at publication.
    clip.state = RecordingState.COMPLETED
    versions = await changed(versions, -1)  # Follow the point, not recording.journey_id.
    fix.speed_kmh = 68
    await changed(versions)
    fix.breaks_segment = True
    versions = await changed(versions, -1)
    fix.journey_id = None
    versions = await changed(versions, -1, None)
    fix.has_fix, fix.journey_id = False, 0
    versions = await changed(versions, None)
    fix.has_fix, fix.journey_id = True, None
    versions = await changed(versions, None)
    session.add(
        TelemetryPoint(
            recording_id=clip.id, journey_id=0, t_offset_s=1, lat=-35, lon=138, has_fix=True
        )
    )
    versions = await changed(versions, 0)
    camera.role = CameraRole.REAR
    versions = await changed(versions, 0, None)
    fix.lat = -34
    camera.key = "renamed-rear"
    await changed(versions)
    clip.camera_id = None
    versions = await changed(versions, 0, None)
    clip.telemetry_revision = "invalidated"
    versions = await changed(versions, 0, None)
    clip.telemetry_revision = "telemetry-v6"
    versions = await changed(versions, 0, None)
    await session.commit()
    await session.execute(text("UPDATE telemetry_points SET gps_quality='rejected'"))
    await changed(versions, 0, None)
    await session.rollback()
    await changed(versions)
    await session.execute(text("DELETE FROM recordings WHERE rel_path='route.ts'"))
    await changed(versions, 0, None)


async def test_journey_motion_deletion_and_id_reuse(db_session):
    session = db_session
    now = datetime.now(UTC)
    journey = Journey(started_at=now, ended_at=now)
    clip = Recording(filename="fix.ts", rel_path="fix.ts", state=RecordingState.COMPLETED)
    session.add_all([journey, clip])
    await session.flush()
    jid = journey.id
    session.add(
        TelemetryPoint(
            recording_id=clip.id, journey_id=jid, t_offset_s=0, lat=-35, lon=138, has_fix=True
        )
    )
    initial = await _manifest(session)
    journey.motion_json = {"status": "unknown"}
    blocked = await _manifest(session)
    assert blocked[jid] != initial[jid] and blocked[None] == initial[None]
    journey.motion_json = {"status": "unconfirmed"}
    assert await _manifest(session) == blocked
    await session.execute(text("DELETE FROM journeys WHERE id=:id"), {"id": jid})
    gone = await _manifest(session)
    assert jid not in gone and gone[None] != blocked[None]
    session.expunge(journey)
    session.add(Journey(id=jid, started_at=now, ended_at=now))
    recreated = await _manifest(session)
    assert recreated[jid] not in (initial[jid], blocked[jid])
    assert recreated[None] == gone[None]
