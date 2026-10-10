"""The explicit journey repair must not reset analysis or touch original footage."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core.settings_service import get_settings_service
from app.db.models import Journey, ProcessingJob, Recording, RecordingState, TelemetryPoint
from app.scanner.discovery import Scanner


async def test_rebuild_recovers_memberships_without_scanning_or_losing_sources(
    client, db_session, app_config, monkeypatch
):
    async def unexpected_scan(*args, **kwargs):
        raise AssertionError("Journey repair must not invoke footage scanning")

    monkeypatch.setattr(Scanner, "scan", unexpected_scan)
    start = datetime(2026, 10, 10, 12, tzinfo=UTC)
    records = []
    for index in range(2):
        filename = f"retained-{index}.ts"
        (app_config.footage_dir / filename).write_bytes(b"original footage")
        recording = Recording(
            rel_path=filename,
            filename=filename,
            size_bytes=16,
            started_at=start + timedelta(seconds=index * 60),
            ended_at=start + timedelta(seconds=(index + 1) * 60),
            duration_s=60,
            state=RecordingState.COMPLETED,
        )
        db_session.add(recording)
        await db_session.flush()
        db_session.add(TelemetryPoint(recording_id=recording.id, t_offset_s=0, speed_kmh=0))
        records.append(recording.id)
    await db_session.commit()

    result = await client.post("/api/journeys/rebuild")
    assert result.status_code == 200, result.text
    assert result.json()["journeys_touched"] == 1
    assert result.json()["total_journeys"] == 1
    assert result.json()["visible_journeys"] == 1
    db_session.expire_all()
    retained = list((await db_session.scalars(select(Recording).order_by(Recording.id))).all())
    assert [recording.id for recording in retained] == records
    assert all(recording.state == RecordingState.COMPLETED for recording in retained)
    assert all(recording.telemetry_revision is None for recording in retained)
    assert len({recording.journey_id for recording in retained}) == 1
    assert retained[0].journey_id is not None
    assert await db_session.scalar(select(func.count(TelemetryPoint.id))) == 2
    assert await db_session.scalar(select(func.count(ProcessingJob.id))) == 0
    for index in range(2):
        assert (app_config.footage_dir / f"retained-{index}.ts").read_bytes() == b"original footage"
    first_id = await db_session.scalar(select(Journey.id))
    repeat = await client.post("/api/journeys/rebuild")
    assert repeat.status_code == 200
    db_session.expire_all()
    assert await db_session.scalar(select(Journey.id)) == first_id


async def test_rebuild_requires_existing_authentication(client):
    configured = await client.put(
        "/api/auth/credential", json={"username": "test", "password": "test-rebuild-password"}
    )
    assert configured.status_code == 204
    await get_settings_service().set_many({"security.require_login": True})
    assert (await client.post("/api/auth/logout")).status_code == 204
    client.cookies.clear()
    response = await client.post("/api/journeys/rebuild")
    assert response.status_code == 401
